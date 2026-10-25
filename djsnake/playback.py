"""Voice connection, downloads into the queue and the playback chain."""

import asyncio
import functools
import os
import time

import discord

from . import lyrics, media, spotify, state, stats
from . import config
from .config import CROSSFADE_SECONDS, FADE_OUT_SECONDS, FRAMES_PER_SECOND
from .media import crossfade_eligible, playback_options
from .mixer import CrossfadeMixer
from .player import MusicPlayer, discard_tracks, guild_of, push_history, retire_play
from .i18n import ctx_lang, t
from .sessions import (
    open_session,
    player_lang,
    refresh_panel,
    set_action,
    short,
    touch,
)
from .stay import remember_channel, set_stay, stay_channel, stay_on
from .volume import percent_to_gain, volume_for


# Simplified voice connection function to avoid error 4006
async def safe_voice_connect(voice_channel, max_attempts=3):
    """Secure voice connection that avoids error 4006"""
    guild = voice_channel.guild

    # Check if we already have a working connection to the same channel
    if guild.voice_client and guild.voice_client.is_connected():
        if guild.voice_client.channel == voice_channel:
            print(f"✅ Already connected to {voice_channel.name}")
            return guild.voice_client
        else:
            print(
                f"🔄 Switching from {guild.voice_client.channel.name} to {voice_channel.name}"
            )
            await guild.voice_client.disconnect(force=True)
            await asyncio.sleep(2)
    elif guild.voice_client:
        print(f"🔄 Cleaning up disconnected voice client")
        await guild.voice_client.disconnect(force=True)
        await asyncio.sleep(2)

    for attempt in range(max_attempts):
        try:
            print(
                f"🔌 Attempt {attempt + 1}/{max_attempts} to connect to {voice_channel.name}"
            )

            # Wait between attempts to avoid error 4006 and WebSocket issues
            if attempt > 0:
                wait_time = 5 + (attempt * 2)  # Longer waits for WebSocket stability
                print(f"⏳ Waiting {wait_time} seconds...")
                await asyncio.sleep(wait_time)

            # Connection with optimal parameters for Docker/WebSocket
            voice_client = await voice_channel.connect(
                timeout=90.0,  # Longer timeout for Docker
                reconnect=False,  # No auto reconnect to avoid loops
                self_deaf=True,  # Mute ourselves
                self_mute=False,  # But can speak (play music)
            )

            # Check that the connection works
            if voice_client and voice_client.is_connected():
                print(f"✅ Successfully connected to {voice_channel.name}")
                return voice_client
            else:
                print("⚠️ Connection established but not confirmed")
                if voice_client:
                    await voice_client.disconnect()

        except discord.errors.ConnectionClosed as e:
            if e.code == 4006:
                print(f"❌ Error 4006 detected (attempt {attempt + 1})")
                # For 4006, wait longer and clean completely
                await asyncio.sleep(5)
            else:
                print(f"❌ Connection error {e.code}: {e.reason}")

        except discord.ClientException as e:
            if "already connected" in str(e).lower():
                # If already connected, find and return the connection
                if guild.voice_client and guild.voice_client.channel == voice_channel:
                    print("✅ Existing connection found and reused")
                    return guild.voice_client
            print(f"❌ Client error: {e}")

        except Exception as e:
            error_msg = str(e)
            if "522" in error_msg or "Invalid response status" in error_msg:
                print(f"🌐 WebSocket error (attempt {attempt + 1}): {e}")
                print("💡 This is often caused by network connectivity issues")
            else:
                print(f"❌ Unexpected error: {e}")

    print("❌ All connection attempts failed")
    return None


async def join_voice_channel(ctx):
    """Simplified voice connection function"""
    author = getattr(ctx, "author", None)
    if not author or not author.voice:
        await ctx.send(t("play.not_in_voice", ctx_lang(ctx)), ephemeral=True)
        return False

    voice_channel = author.voice.channel

    # Initialize player if necessary
    if ctx.guild.id not in state.music_players:
        state.music_players[ctx.guild.id] = MusicPlayer()

    player = state.music_players[ctx.guild.id]
    # Expire a stale session now: once in voice it would count as active.
    touch(player)

    current = ctx.guild.voice_client
    already_here = bool(
        current and current.is_connected() and current.channel == voice_channel
    )
    voice_client = await safe_voice_connect(voice_channel)

    if voice_client:
        player.voice_client = voice_client
        remember_channel(ctx.guild.id, voice_channel.id)  # 24/7: rejoin it later
        if not already_here:
            stats.record_session(ctx.guild.id)
            stats.record_listeners(ctx.guild.id, stats.count_listeners(voice_channel))
        # A fresh join (re)posts the session link; a later !play does not.
        await open_session(ctx, player, announce=not already_here)
        return True
    else:
        await ctx.send(t("play.connect_failed", ctx_lang(ctx)), ephemeral=True)
        return False


def open_track_source(player, track, offset, fade_in=None):
    """Start FFmpeg for `track` from `offset`. Returns (offset, source, frames,
    fade_frames): the clamped offset, the source, its length in frames (None
    if unknown) and the length of its fade-out (0: cannot crossfade)."""
    eligible = crossfade_eligible(track)
    fade_out = CROSSFADE_SECONDS if player.crossfade and eligible else FADE_OUT_SECONDS
    offset, kwargs = playback_options(track, offset, fade_in, fade_out)
    source = discord.FFmpegPCMAudio(track["filepath"], **kwargs)
    end = track.get("end")
    if end is None:
        return offset, source, None, 0
    remaining = max(0.0, end - offset)
    frames = round(remaining * FRAMES_PER_SECOND)
    fade_frames = 0
    if eligible:
        # Toggled on mid-track, the fade already running is 4 s: overlap that.
        fade_frames = round(min(fade_out, remaining) * FRAMES_PER_SECOND)
    return offset, source, frames, fade_frames


def cancelled(player, epoch):
    return player.should_stop or player.epoch != epoch


def spotify_fields(spotify_track):
    """What a Spotify link tells about the YouTube track found for it: the
    artist (stats, lyrics), and the title and duration the lyrics lookup
    prefers to the video's."""
    artists = spotify_track.get("artists") or []
    fields = {"artist": artists[0]} if artists and artists[0] else {}
    if spotify_track.get("title"):
        fields["lyrics_title"] = spotify_track["title"]
    if spotify_track.get("duration"):
        fields["lyrics_duration"] = spotify_track["duration"]
    return fields


def pending_label(entry):
    """(title, source) listing a playlist entry in pending_downloads."""
    if "spotify" in entry:
        # A metadata track (Spotify, Deezer or Apple Music), see catalog.py.
        track = entry["spotify"]
        artists = ", ".join(a for a in track.get("artists") or [] if a)
        title = track.get("title") or None
        if artists and title:
            title = f"{artists} – {title}"
        return title, track.get("source") or "spotify"
    url = entry.get("url") or entry.get("webpage_url")
    return entry.get("title") or None, media.source_of(url)


async def download_and_queue_song(
    ctx,
    player,
    url,
    skip_voice_check=False,
    from_web=False,
    quiet=False,
    extra=None,
    pending_key=None,
):
    """Download one track and queue it. Returns the track, or None.
    `quiet` (playlist items) posts no message, not even on failure. `extra`
    adds fields to the track (the Spotify artist). `pending_key` is the
    caller's entry in player.pending_downloads; without one the link is
    listed here, title unknown. Either way the entry goes when this returns."""
    epoch = player.epoch
    if pending_key is None and not cancelled(player, epoch):
        pending_key = player.add_pending(None, media.source_of(url))
    try:
        return await _download_and_queue(
            ctx, player, url, epoch, skip_voice_check, quiet, extra, pending_key
        )
    finally:
        if pending_key is not None:
            player.drop_pending(pending_key)


def _title_reporter(player, key):
    """yt-dlp progress hook (download thread) naming the pending entry."""
    loop = asyncio.get_running_loop()
    named = False

    def hook(title):
        nonlocal named
        if not named and title:
            named = True
            try:
                loop.call_soon_threadsafe(player.name_pending, key, title)
            except RuntimeError:
                pass  # Loop closed: nobody is watching any more

    return hook


async def _download_and_queue(
    ctx, player, url, epoch, skip_voice_check, quiet, extra, pending_key
):
    try:
        if cancelled(player, epoch):
            return None

        if not skip_voice_check:
            if not player.voice_client or not player.voice_client.is_connected():
                if not await join_voice_channel(ctx):
                    await ctx.send(t("cmd.cannot_join", ctx_lang(ctx)))
                    return None

        loop = asyncio.get_running_loop()
        if not quiet:
            player.single_downloads += 1
            refresh_panel(player)
        try:
            download = media.download_media
            if any(
                item["key"] == pending_key and item["title"] is None
                for item in player.pending_downloads
            ):
                download = functools.partial(
                    download, on_title=_title_reporter(player, pending_key)
                )
            track = await loop.run_in_executor(None, download, url)
        finally:
            if not quiet:
                player.single_downloads = max(0, player.single_downloads - 1)

        if cancelled(player, epoch):
            discard_tracks([track])
            refresh_panel(player)
            return None

        # For the stats: who asked (None from the web page, which has no author).
        author = getattr(ctx, "author", None)
        track["requester"] = getattr(author, "display_name", None)
        # Who may always skip it, even without the DJ role (permissions.py).
        track["requester_id"] = getattr(author, "id", None)
        if extra:
            track.update(extra)
        player.add_to_queue(track)
        player.drop_pending(pending_key)  # Never listed twice on the page
        lyrics.prefetch(track)  # Background, ready before the track plays
        if quiet:
            refresh_panel(player)
        else:
            set_action(
                player,
                t("action.added", player_lang(player), title=short(track["title"])),
            )

        if not player.is_playing:
            await play_next(ctx)
        return track

    except Exception as e:
        if quiet:
            print(f"Playlist item failed: {e}")
            return None
        refresh_panel(player)
        await ctx.send(t("play.download_error", ctx_lang(ctx), error=e))
        return None


async def queue_playlist(ctx, player, entries, from_web=False):
    """Download playlist entries one by one into the queue while earlier
    ones play. `loading` keeps play_next from leaving voice meanwhile.
    Progress lives in the status panel, not in channel messages."""
    epoch = player.epoch
    stopped = False
    loaded = 0
    not_found = 0  # Spotify tracks with no YouTube match
    total = len(entries)
    loop = asyncio.get_running_loop()

    def progress(key, final=False, **params):
        lang = player_lang(player)
        player.load_line, player.load_final = t(key, lang, **params), final
        refresh_panel(player)

    progress("load.progress", loaded=0, total=total)
    # Every entry is listed now and leaves as it is queued, fails or is
    # skipped; the finally drops what a stop or an error left behind.
    keys = [
        (
            player.add_pending(*pending_label(entry))
            if not cancelled(player, epoch)
            else None
        )
        for entry in entries
    ]
    player.loading_jobs += 1
    try:
        for entry, key in zip(entries, keys):
            if cancelled(player, epoch):
                stopped = True
                break

            extra = None
            if "spotify" in entry:
                # A Spotify track: search YouTube for it first.
                extra = spotify_fields(entry["spotify"])
                video_url = await loop.run_in_executor(
                    None, spotify.find_youtube_match, entry["spotify"]
                )
                if video_url is None:
                    player.drop_pending(key)
                    if not cancelled(player, epoch):
                        not_found += 1
                    continue
            else:
                # Build video URL
                video_url = media.entry_url(entry)
                if not video_url:
                    player.drop_pending(key)
                    continue

            track = await download_and_queue_song(
                ctx,
                player,
                video_url,
                skip_voice_check=True,
                quiet=True,
                extra=extra,
                pending_key=key,
            )
            if track is not None and not cancelled(player, epoch):
                loaded += 1
                progress("load.progress", loaded=loaded, total=total)
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)
        for key in keys:
            if key is not None:
                player.drop_pending(key)

    if stopped or cancelled(player, epoch):
        progress("load.stopped", final=True, loaded=loaded, total=total)
        return
    lang = player_lang(player)
    text = t("load.done", lang, loaded=loaded, count=total)
    missing = []
    if not_found:
        missing.append(t("load.not_found", lang, count=not_found))
    unavailable = total - loaded - not_found
    if unavailable > 0:
        missing.append(t("load.unavailable", lang, count=unavailable))
    if missing:
        text += f" ({', '.join(missing)})"
    player.load_line, player.load_final = text, True
    refresh_panel(player)
    # The last track may have finished while the queue was empty during
    # loading: start the queue again, or leave if there is nothing left.
    if not player.is_playing:
        await play_next(ctx)


async def queue_spotify(ctx, player, kind, tracks, from_web=False):
    """Queue the tracks read from a Spotify link: each one is searched on
    YouTube, and the best match is downloaded like any other track. A list
    loads in the background like a YouTube playlist."""
    if kind != "track":
        await queue_playlist(
            ctx, player, [{"spotify": track} for track in tracks], from_web=from_web
        )
        return
    epoch = player.epoch
    track = tracks[0]
    if cancelled(player, epoch):
        return
    key = player.add_pending(*pending_label({"spotify": track}))
    try:
        player.single_downloads += 1
        refresh_panel(player)
        try:
            loop = asyncio.get_running_loop()
            video_url = await loop.run_in_executor(
                None, spotify.find_youtube_match, track
            )
        finally:
            player.single_downloads = max(0, player.single_downloads - 1)
            refresh_panel(player)
        if cancelled(player, epoch):
            return
        if video_url is None:
            player.drop_pending(key)
            await ctx.send(
                t("play.no_match", ctx_lang(ctx), track=spotify.describe(track)),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await download_and_queue_song(
            ctx,
            player,
            video_url,
            skip_voice_check=from_web,
            from_web=from_web,
            extra=spotify_fields(track),
            pending_key=key,
        )
    finally:
        player.drop_pending(key)


async def queue_search(ctx, player, query, from_web=False):
    """Queue the best YouTube match for a plain text search (`!play daft punk
    one more time`), scored like a Spotify track. Says so when none fits."""
    epoch = player.epoch
    if cancelled(player, epoch):
        return
    query = " ".join(query.split())
    key = player.add_pending(query[:100], "youtube")
    try:
        player.single_downloads += 1
        refresh_panel(player)
        try:
            loop = asyncio.get_running_loop()
            video_url = await loop.run_in_executor(None, spotify.find_text_match, query)
        finally:
            player.single_downloads = max(0, player.single_downloads - 1)
            refresh_panel(player)
        if cancelled(player, epoch):
            return
        if video_url is None:
            player.drop_pending(key)
            await ctx.send(
                t("play.no_match", ctx_lang(ctx), track=query[:100]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await download_and_queue_song(
            ctx,
            player,
            video_url,
            skip_voice_check=from_web,
            from_web=from_web,
            pending_key=key,
        )
    finally:
        player.drop_pending(key)


async def play_next(ctx, gen=None, origin=None):
    """Advance playback. Called by the `after` callback (with the generation
    of the track that ended) or to start an idle player (gen=None)."""
    player = state.music_players.get(ctx.guild.id)
    if not player or (origin is not None and origin is not player):
        return  # Player replaced by !reset
    try:
        async with player.lock:
            if player.should_stop:
                return
            if gen is not None and gen != player.play_gen:
                return  # Stale callback
            if gen is None and player.is_playing:
                return  # Already playing; its callback will continue
            await _advance(ctx, player)
    except Exception as e:
        print(f"Error in play_next: {e}")
        player.is_playing = False
        await ctx.send(t("play.playback_error", ctx_lang(ctx), error=e))


async def _advance(ctx, player):
    """Retire the current track according to pending_action and start the
    next one. Must run under player.lock."""
    action, player.pending_action = player.pending_action, None
    if action is None:
        player.last_action = None  # The track ended by itself
    if action in ("restart", "seek"):
        player.play_accum = player.played_seconds()  # Same track, same play
    else:
        retire_play(player, ctx.guild.id, action)
    finished = player.now_playing
    player.now_playing = None
    player.started_at = None
    player.paused_at = None
    player.paused_total = 0.0

    offset = 0.0
    keep_paused = False
    if action in ("restart", "seek") and finished:
        next_track = finished
        if action == "seek":
            offset, keep_paused = player.seek_position, player.seek_paused
    elif action == "previous" and finished and player.history:
        player.queue.insert(0, finished)
        next_track = player.history.pop()
        # Loop queue put that track back at the end of the queue when it
        # ended: it plays now, so drop that copy (no duplicate entries).
        player.queue = [q for q in player.queue if q is not next_track]
    elif action is None and player.loop == "track" and finished:
        # Loop track: replay it as a new play (stats count each pass)
        next_track = finished
    else:
        if finished:
            push_history(player, finished)
            if player.loop == "queue" and action in (None, "next"):
                player.queue.append(finished)  # Back to the end of the queue
        next_track = player.next_in_queue()
    if next_track is None and autoplay_wanted(player):
        next_track = _take_autoplay(ctx, player, finished)

    while next_track is not None and not os.path.exists(next_track["filepath"]):
        await ctx.send(
            t("play.file_not_found", ctx_lang(ctx), title=next_track["title"])
        )
        next_track = player.next_in_queue()
        offset, keep_paused = 0.0, False  # The seek was for the lost track

    if next_track is None:
        player.is_playing = False
        # A playlist may still be downloading its next tracks. Stay connected
        # and idle instead of disconnecting; the next queued track will
        # trigger play_next() again once it finishes downloading.
        if player.loading:
            return
        if player.in_voice():
            # Linger in voice for a while: a track added meanwhile plays at
            # once, without leaving and rejoining.
            schedule_idle_disconnect(ctx, player)
        # The web session survives: it expires after SESSION_IDLE_HOURS.
        refresh_panel(player)
        return

    # Check voice connection
    if not player.voice_client or not player.voice_client.is_connected():
        if not await join_voice_channel(ctx):
            player.queue.insert(0, next_track)
            player.is_playing = False
            refresh_panel(player)
            await ctx.send(t("cmd.cannot_join", ctx_lang(ctx)))
            return

    player.play_gen += 1
    gen = player.play_gen
    loop = asyncio.get_running_loop()

    def after_playing(error):
        # Runs on the audio thread: only hand over to the event loop. A
        # crossfade chain ends on a later track than the one started here.
        if error:
            print(f"Playback error: {error}")
        try:
            asyncio.run_coroutine_threadsafe(play_next(ctx, mixer.gen, player), loop)
        except Exception as e:
            print(f"Error scheduling next song: {e}")

    def on_tail(source_mixer, tail_gen):
        # Audio thread, once per track, as its fade-out starts.
        if player.crossfade:
            asyncio.run_coroutine_threadsafe(
                _start_crossfade(player, source_mixer, tail_gen, ctx), loop
            )

    player.cancel_idle()  # Something plays again: no more leaving
    player.note_played(next_track)
    player.now_playing = next_track
    player.is_playing = True
    offset, audio_source, frames, fade_frames = open_track_source(
        player, next_track, offset
    )
    gain = percent_to_gain(volume_for(ctx.guild.id))
    mixer = CrossfadeMixer(audio_source, gen, frames, fade_frames, on_tail, gain)
    player.mixer = mixer
    try:
        player.voice_client.play(mixer, after=after_playing)
    except Exception:
        player.now_playing = None
        player.is_playing = False
        player.mixer = None
        mixer.cleanup()  # Its FFmpeg process is already running
        discard_tracks([next_track])
        raise
    now = time.monotonic()
    player.started_at = now - offset
    # A loop replay is a new play; only a restart or seek continues one
    player.mark_started(
        offset, same_track=next_track is finished and action in ("restart", "seek")
    )
    if keep_paused:
        player.voice_client.pause()
        player.paused_at = now
    player.last_active = state.clock()
    prefetch_autoplay(ctx, player)
    refresh_panel(player)


def autoplay_wanted(player):
    """Autoplay may pick the next track: on, no loop mode, not stopping, and
    someone is listening (an empty channel leaves after the grace instead)."""
    return (
        player.autoplay
        and not player.loop_active()
        and not player.should_stop
        and not channel_empty(player)
    )


def _youtube_id(track):
    """The track's YouTube video id, or None (SoundCloud and Bandcamp tracks
    have ids of their own, which YouTube's mixes know nothing about)."""
    if not track or media.source_of(track.get("url")) != "youtube":
        return None
    video_id = track.get("video_id")
    return video_id if media._VIDEO_ID_RE.match(video_id or "") else None


def _autoplay_seed(player, finished=None):
    track = finished or player.now_playing
    if track:
        return _youtube_id(track)  # None after a SoundCloud/Bandcamp track
    return player.recent_ids[-1] if player.recent_ids else None


def _autoplay_exclude(player):
    """Video ids autoplay must not pick: recently played, playing, queued."""
    ids = set(player.recent_ids)
    for track in [player.now_playing, *player.queue, *player.history]:
        if track and track.get("video_id"):
            ids.add(track["video_id"])
    return ids


async def _find_autoplay(player, seed, epoch):
    """Pick a track related to video `seed` and download it. Returns the
    track (marked `autoplay`) or None. Never blocks the event loop."""
    loop = asyncio.get_running_loop()
    try:
        candidates = await loop.run_in_executor(None, media.related_tracks, seed)
    except Exception as e:
        print(f"Autoplay: could not read related tracks: {e}")
        return None
    picks = media.choose_related(candidates, _autoplay_exclude(player))
    for entry in picks[: config.AUTOPLAY_TRIES]:
        if cancelled(player, epoch):
            return None
        url = f"https://www.youtube.com/watch?v={entry['id']}"
        try:
            track = await loop.run_in_executor(None, media.download_media, url)
        except Exception as e:
            print(f"Autoplay: download failed: {e}")
            continue
        duration = track.get("duration")
        if cancelled(player, epoch) or (
            duration and duration > config.AUTOPLAY_MAX_SECONDS
        ):
            discard_tracks([track])  # Unknown length turned out too long
            continue
        track["autoplay"] = True
        track["requester"] = "Autoplay"
        track.setdefault("video_id", entry["id"])
        return track
    return None


async def _prefetch_autoplay(ctx, player, seed, epoch):
    """Find and download the autoplay pick, outside the player's lock (the
    search and downloads can take minutes). If the queue ran out meanwhile
    and the player sits idle, start it with the pick."""
    track = await _find_autoplay(player, seed, epoch)
    if track is None:
        return
    if cancelled(player, epoch) or not autoplay_wanted(player):
        discard_tracks([track])
        return
    player.drop_autoplay_next()
    player.autoplay_next = track
    if (
        state.music_players.get(guild_of(player)) is player
        and not player.is_playing
        and player.now_playing is None
        and player.in_voice()
    ):
        await play_next(ctx)  # gen=None: only starts an idle player


def prefetch_autoplay(ctx, player, finished=None):
    """While the last queued track plays, prepare the autoplay pick so the
    switch has no gap (or, once the queue ran out, so the idle player starts
    again). A no-op unless autoplay is on and the queue is empty."""
    task = player.autoplay_task
    if (
        not autoplay_wanted(player)
        or player.queue
        or player.autoplay_next
        or (task is not None and not task.done())
        or not player.in_voice()
    ):
        return
    seed = _autoplay_seed(player, finished)
    if not seed:
        return
    task = asyncio.get_running_loop().create_task(
        _prefetch_autoplay(ctx, player, seed, player.epoch)
    )
    player.autoplay_task = task
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)


def _take_autoplay(ctx, player, finished):
    """The prepared autoplay pick, or None: the caller then lingers idle.
    Runs under player.lock, so it never waits for the network: without a
    ready pick it starts preparing one, which restarts the idle player."""
    track, player.autoplay_next = player.autoplay_next, None
    if track is not None:
        return track
    prefetch_autoplay(ctx, player, finished)
    return None


def channel_empty(player):
    """True when no human is left in the bot's voice channel. Unknown
    members (no member list) count as not empty."""
    channel = getattr(player.voice_client, "channel", None)
    members = getattr(channel, "members", None)
    if members is None:
        return False
    return stats.count_listeners(channel) == 0


def schedule_idle_disconnect(ctx, player, delay=None):
    """(Re)start the player's single idle timer: leave voice after `delay`
    seconds (IDLE_DISCONNECT_SECONDS, or the empty channel grace) unless
    something plays meanwhile. Any previous timer is cancelled first. In 24/7
    mode there is no timer: the bot never leaves by itself (returns None)."""
    if stay_on(ctx.guild.id):
        player.cancel_idle()
        return None
    if delay is None:
        delay = config.IDLE_DISCONNECT_SECONDS
        if channel_empty(player):
            delay = min(delay, config.EMPTY_CHANNEL_GRACE_SECONDS)
    player.cancel_idle()
    task = asyncio.get_running_loop().create_task(
        _idle_disconnect(ctx, player, player.play_gen, delay)
    )
    player.idle_task, player.idle_delay = task, delay
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)
    return task


def idle_channel_emptied(ctx, player):
    """A human left: if the bot lingers alone, leave after the grace."""
    if not player.lingering() or not channel_empty(player):
        return
    grace = min(config.IDLE_DISCONNECT_SECONDS, config.EMPTY_CHANNEL_GRACE_SECONDS)
    if player.idle_delay is not None and player.idle_delay <= grace:
        return  # Already on the short timer
    schedule_idle_disconnect(ctx, player, grace)
    refresh_panel(player)


async def _idle_disconnect(ctx, player, gen, delay):
    await asyncio.sleep(delay)
    async with player.lock:
        if player.idle_task is not asyncio.current_task():
            return  # Replaced by a newer timer
        player.idle_task = player.idle_delay = None
        if (
            state.music_players.get(ctx.guild.id) is not player
            or player.should_stop
            or gen != player.play_gen
            or player.is_playing
            or player.queue
            or not player.in_voice()
            or stay_on(ctx.guild.id)  # 24/7 was turned on meanwhile
        ):
            return  # Stale: replaced, stopped, playing again or already out
        if player.loading or player.pending_downloads or player.single_downloads:
            # A download is still on its way: wait another round for it.
            schedule_idle_disconnect(ctx, player)
            return
        # Leave exactly as the queue end used to.
        await player.voice_client.disconnect()
    refresh_panel(player)


# --- 24/7 mode ---------------------------------------------------------------


async def rejoin_stay(guild):
    """Join the guild's remembered 24/7 channel (nothing starts playing).
    False when there is none, it is gone, the bot may not connect, or the
    connection fails. Uses safe_voice_connect() with two attempts."""
    channel_id = stay_channel(guild.id)
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None or not callable(getattr(channel, "connect", None)):
        return False  # Channel deleted (or not a voice channel)
    if not channel.permissions_for(guild.me).connect:
        return False
    voice_client = await safe_voice_connect(channel, max_attempts=2)
    if not voice_client:
        return False
    player = state.music_players.get(guild.id)
    if player is None:
        player = state.music_players[guild.id] = MusicPlayer()
    player.voice_client = voice_client
    refresh_panel(player)
    return True


def give_up_stay(guild_id):
    """24/7 cannot be kept: turn it off and say so on the panel."""
    try:
        set_stay(guild_id, False)
    except OSError as e:
        print(f"⚠️  Could not save the 24/7 setting: {e}")
    player = state.music_players.get(guild_id)
    if player is not None:
        set_action(player, t("action.stay_lost", player_lang(player)))
    print(f"24/7 mode turned off for guild {guild_id}")


async def restore_stay(guilds):
    """After a restart: rejoin the remembered channel of every guild with 24/7
    on, one at a time. A guild that is gone, a deleted channel or a missing
    permission is skipped; nothing is played."""
    for guild in guilds:
        if not stay_on(guild.id) or not stay_channel(guild.id):
            continue
        if guild.voice_client and guild.voice_client.is_connected():
            continue
        try:
            if await rejoin_stay(guild):
                print(f"🌙 24/7: rejoined the voice channel of {guild.name}")
            else:
                print(f"🌙 24/7: could not rejoin the voice channel of {guild.name}")
        except Exception as e:
            print(f"🌙 24/7: rejoin failed for {guild.name}: {e}")


async def recover_stay(guild):
    """The bot was dropped from the voice channel of a 24/7 guild (Discord
    ended the voice session, or someone disconnected or moved it away): rejoin
    once. Dropped again within STAY_RETRY_WINDOW, or when the channel is gone
    or off limits, 24/7 turns itself off, so there is never a reconnect loop."""
    now = state.clock()
    last = state.stay_retries.get(guild.id)
    state.stay_retries[guild.id] = now
    if last is not None and now - last < config.STAY_RETRY_WINDOW:
        give_up_stay(guild.id)
        return
    try:
        ok = await rejoin_stay(guild)
    except Exception as e:
        print(f"🌙 24/7: rejoin failed: {e}")
        ok = False
    if not ok:
        give_up_stay(guild.id)


async def _start_crossfade(player, mixer, gen, ctx=None):
    """Overlap the next queued track with the end of the current one.

    Called when the current track's fade-out starts. The next track becomes
    now_playing right away (history and queue change as for a normal advance)
    while the old one drains underneath; nothing calls play() or stop(), so
    the `after` callback fires once, at the end of the chain. Any explicit
    control stops the whole mixer and so cancels the overlap.
    """
    try:
        async with player.lock:
            if (
                not player.crossfade
                or player.should_stop
                or gen != player.play_gen
                or player.mixer is not mixer
                or player.pending_action is not None
                or not player.is_playing
                or not player.in_voice()
            ):
                return
            current = player.now_playing
            # Loop track (or a one-track loop queue) overlaps the track with itself
            replay = player.loop == "track" or (
                player.loop == "queue" and not player.queue
            )
            if replay:
                upcoming = current
            elif player.queue:
                upcoming = player.queue[0]
            else:
                return
            if player.paused_at is not None:
                mixer.rearm()  # Try again once resumed
                return
            if not (crossfade_eligible(current) and crossfade_eligible(upcoming)):
                return
            if not os.path.exists(upcoming["filepath"]):
                return  # The normal advance reports it
            overlap = mixer.remaining_seconds()
            if overlap is None or overlap < 1.0:
                return  # Too late: let it end normally
            offset, source, frames, fade_frames = open_track_source(
                player, upcoming, 0.0, fade_in=overlap
            )
            # FFmpeg needs a moment to start; read its first frame here so
            # the audio thread never waits for it.
            loop = asyncio.get_running_loop()
            try:
                first = await loop.run_in_executor(None, source.read)
            except Exception:
                source.cleanup()
                raise
            if not first or not mixer.attach(
                source, first, player.play_gen + 1, frames, fade_frames
            ):
                source.cleanup()
                return
            player.play_gen += 1
            retire_play(player, guild_of(player), None)
            if not replay:
                player.queue.remove(upcoming)
                push_history(player, current)
                if player.loop == "queue":
                    player.queue.append(current)
            player.note_played(upcoming)
            player.now_playing = upcoming
            player.started_at = time.monotonic() - offset
            player.paused_at = None
            player.paused_total = 0.0
            player.mark_started(offset, same_track=False)
            player.last_action = None
            player.last_active = state.clock()
            refresh_panel(player)
            if ctx is not None:
                prefetch_autoplay(ctx, player)  # As after a normal advance
    except Exception as e:
        print(f"Crossfade failed: {e}")
