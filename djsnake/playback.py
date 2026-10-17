"""Voice connection, downloads into the queue and the playback chain."""

import asyncio
import os
import time

import discord

from . import media, state
from .config import CROSSFADE_SECONDS, FADE_OUT_SECONDS, FRAMES_PER_SECOND
from .media import crossfade_eligible, playback_options
from .mixer import CrossfadeMixer
from .player import MusicPlayer, discard_tracks, push_history
from .sessions import open_session, refresh_panel, set_action, short, touch


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
        await ctx.send("❌ You must be in a voice channel!")
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
        # A fresh join (re)posts the session link; a later !play does not.
        await open_session(ctx, player, announce=not already_here)
        return True
    else:
        await ctx.send("❌ Unable to connect to voice channel.")
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


async def download_and_queue_song(
    ctx, player, url, skip_voice_check=False, from_web=False, quiet=False
):
    """Download one track and queue it. Returns the track, or None.
    `quiet` (playlist items) posts no message, not even on failure."""
    epoch = player.epoch
    try:
        if cancelled(player, epoch):
            return None

        if not skip_voice_check:
            if not player.voice_client or not player.voice_client.is_connected():
                if not await join_voice_channel(ctx):
                    await ctx.send("❌ Unable to join voice channel!")
                    return None

        loop = asyncio.get_running_loop()
        if not quiet:
            player.single_downloads += 1
            refresh_panel(player)
        try:
            track = await loop.run_in_executor(None, media.download_media, url)
        finally:
            if not quiet:
                player.single_downloads = max(0, player.single_downloads - 1)

        if cancelled(player, epoch):
            discard_tracks([track])
            refresh_panel(player)
            return None

        player.add_to_queue(track)
        if quiet:
            refresh_panel(player)
        else:
            set_action(player, f"➕ Added: {short(track['title'])}")

        if not player.is_playing:
            await play_next(ctx)
        return track

    except Exception as e:
        if quiet:
            print(f"Playlist item failed: {e}")
            return None
        refresh_panel(player)
        await ctx.send(f"❌ Download error: {str(e)}")
        return None


async def queue_playlist(ctx, player, entries, from_web=False):
    """Download playlist entries one by one into the queue while earlier
    ones play. `loading` keeps play_next from leaving voice meanwhile.
    Progress lives in the status panel, not in channel messages."""
    epoch = player.epoch
    stopped = False
    loaded = 0
    total = len(entries)

    def progress(text, final=False):
        player.load_line, player.load_final = text, final
        refresh_panel(player)

    progress(f"📥 Loading 0/{total}…")
    player.loading_jobs += 1
    try:
        for entry in entries:
            if cancelled(player, epoch):
                stopped = True
                break

            # Build video URL
            video_id = entry.get("id") or entry.get("url") or entry.get("webpage_url")
            if not video_id:
                continue

            if isinstance(video_id, str) and video_id.startswith("http"):
                video_url = video_id
            else:
                video_url = f"https://www.youtube.com/watch?v={video_id}"

            track = await download_and_queue_song(
                ctx, player, video_url, skip_voice_check=True, quiet=True
            )
            if track is not None and not cancelled(player, epoch):
                loaded += 1
                progress(f"📥 Loading {loaded}/{total}…")
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)

    if stopped or cancelled(player, epoch):
        progress(f"⏹️ Loading stopped ({loaded}/{total})", final=True)
        return
    text = f"✅ {loaded}/{total} tracks loaded"
    if loaded < total:
        text += f" ({total - loaded} unavailable)"
    progress(text, final=True)
    # The last track may have finished while the queue was empty during
    # loading: start the queue again, or leave if there is nothing left.
    if not player.is_playing:
        await play_next(ctx)


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
        await ctx.send(f"❌ Playback error: {str(e)}")


async def _advance(ctx, player):
    """Retire the current track according to pending_action and start the
    next one. Must run under player.lock."""
    action, player.pending_action = player.pending_action, None
    if action is None:
        player.last_action = None  # The track ended by itself
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
    else:
        if finished:
            push_history(player, finished)
        next_track = player.next_in_queue()

    while next_track is not None and not os.path.exists(next_track["filepath"]):
        await ctx.send(f"❌ File not found for {next_track['title']}")
        next_track = player.next_in_queue()
        offset, keep_paused = 0.0, False  # The seek was for the lost track

    if next_track is None:
        player.is_playing = False
        # A playlist may still be downloading its next tracks. Stay connected
        # and idle instead of disconnecting; the next queued track will
        # trigger play_next() again once it finishes downloading.
        if player.loading:
            return
        if player.voice_client and player.voice_client.is_connected():
            await player.voice_client.disconnect()
        # The web session survives: it expires after SESSION_IDLE_HOURS.
        refresh_panel(player)
        return

    # Check voice connection
    if not player.voice_client or not player.voice_client.is_connected():
        if not await join_voice_channel(ctx):
            player.queue.insert(0, next_track)
            player.is_playing = False
            refresh_panel(player)
            await ctx.send("❌ Unable to join voice channel!")
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
                _start_crossfade(player, source_mixer, tail_gen), loop
            )

    player.now_playing = next_track
    player.is_playing = True
    offset, audio_source, frames, fade_frames = open_track_source(
        player, next_track, offset
    )
    mixer = CrossfadeMixer(audio_source, gen, frames, fade_frames, on_tail)
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
    if keep_paused:
        player.voice_client.pause()
        player.paused_at = now
    player.last_active = state.clock()
    refresh_panel(player)


async def _start_crossfade(player, mixer, gen):
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
                or not player.queue
            ):
                return
            if player.paused_at is not None:
                mixer.rearm()  # Try again once resumed
                return
            current, upcoming = player.now_playing, player.queue[0]
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
            player.queue.remove(upcoming)
            push_history(player, current)
            player.now_playing = upcoming
            player.started_at = time.monotonic() - offset
            player.paused_at = None
            player.paused_total = 0.0
            player.last_action = None
            player.last_active = state.clock()
            refresh_panel(player)
    except Exception as e:
        print(f"Crossfade failed: {e}")
