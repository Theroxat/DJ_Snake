"""Controls shared by Discord commands and the web page."""

import asyncio
import random
import time
import types

import discord

from . import catalog, media, playlists, spotify, state
from .config import (
    LOOP_MODES,
    MAX_PLAYLIST_SIZE,
    MAX_SAVED_PLAYLISTS,
    MAX_SAVED_TRACKS,
    MAX_SEARCH_LENGTH,
    PREVIOUS_RESTART_SECONDS,
    SAVED_PLAYLIST_NAME_MAX,
)
from .media import looks_like_url, parse_media_url, parse_youtube_url
from .playback import (
    cancelled,
    download_and_queue_song,
    play_next,
    prefetch_autoplay,
    queue_playlist,
    queue_search,
    queue_spotify,
)
from .player import discard_tracks, guild_of
from .i18n import ctx_lang, t
from .sessions import player_lang, refresh_panel
from . import voteskip
from .volume import percent_to_gain, save_volumes, valid_volume, volume_for


class _NullMessage:
    async def edit(self, **kwargs):
        pass

    async def delete(self):
        pass


class WebContext:
    """Stands in for a command context when the web page triggers work.
    It has no author, so it can never make the bot join voice."""

    author = None

    def __init__(self, guild_id, player):
        self.guild = state.bot.get_guild(guild_id) or types.SimpleNamespace(
            id=guild_id, name=""
        )
        self.channel = player.text_channel

    async def send(self, content=None, **kwargs):
        kwargs.pop("ephemeral", None)  # Channel messages cannot be private
        if self.channel is None:
            return _NullMessage()
        try:
            return await self.channel.send(content, **kwargs)
        except Exception as e:
            print(f"Could not send: {e}")
            return _NullMessage()


class ChannelContext:
    """A command context reduced to what outlives the command: guild, channel
    and author, replying with plain channel messages. A slash command's own
    replies stop working 15 minutes after the interaction, but downloads, the
    playback chain and the idle timer keep a context for much longer."""

    def __init__(self, ctx):
        self.guild, self.channel = ctx.guild, ctx.channel
        self.author, self.command = ctx.author, getattr(ctx, "command", None)
        self.message = self.interaction = None

    async def send(self, content=None, **kwargs):
        kwargs.pop("ephemeral", None)
        return await self.channel.send(content, **kwargs)


class ControlError(Exception):
    """A control that cannot be applied. `code` picks the HTTP status and the
    message (`control.<code>` in the locales); the code never changes with
    the language."""

    # code -> HTTP status
    STATUS = {
        "not_in_voice": 409,
        "nothing_playing": 409,
        "track_not_found": 404,
        "bad_index": 400,
        "queue_empty": 409,
        "bad_loop": 400,
        "nothing_to_shuffle": 409,
        "bad_volume": 400,
        "vote_not_in_channel": 403,
        "vote_deafened": 403,
        "vote_already": 409,
        "bad_threshold": 400,
        "threshold_not_saved": 500,
        "volume_not_saved": 500,
        "bad_url": 400,
        "short_link": 400,
        "link_not_found": 404,
        "link_empty": 400,
        "link_unreadable": 502,
        "spotify_short_link": 400,
        "spotify_not_found": 404,
        "spotify_empty": 400,
        "spotify_unreadable": 502,
        "bad_playlist_name": 400,
        "playlist_not_found": 404,
        "playlist_exists": 409,
        "playlist_forbidden": 403,
        "playlist_limit": 409,
        "playlist_full": 409,
        "playlist_empty": 400,
        "playlist_unavailable": 500,
    }

    def __init__(self, code):
        super().__init__(code)
        self.code = code
        self.status = self.STATUS[code]

    def text(self, lang=None):
        """The message in `lang` (default: BOT_LANGUAGE)."""
        return t(
            f"control.{self.code}",
            lang,
            name_max=SAVED_PLAYLIST_NAME_MAX,
            playlists_max=MAX_SAVED_PLAYLISTS,
            tracks_max=MAX_SAVED_TRACKS,
        )

    @property
    def message(self):
        return self.text()


def _require_playing(player):
    if not player.in_voice():
        raise ControlError("not_in_voice")
    if not player.is_playing or not player.now_playing:
        raise ControlError("nothing_playing")


async def _stop_with(player, action, gen=None):
    """Stop the current track; play_next then applies `action`. With `gen`,
    only while that track (`play_gen`) still plays. Returns True when this
    call started the transition, False when one was already under way or the
    track changed."""
    async with player.lock:
        _require_playing(player)
        if gen is not None and player.play_gen != gen:
            return False  # Another track started since the caller looked
        if player.pending_action is not None:
            return False  # A transition is already under way
        player.pending_action = action
        player.voice_client.stop()
        return True


async def control_next(player, gen=None):
    return await _stop_with(player, "next", gen)


async def _show_vote(player, text, channel, message, final):
    """Edit the track's one vote message, or post it in `channel` (best
    effort). A final text is not kept for the next vote."""
    gen = player.play_gen
    if message is not None:
        try:
            await message.edit(content=text)
            return
        except discord.HTTPException:
            pass
    if channel is None:
        return
    try:
        sent = await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        return
    if not final and player.play_gen == gen and player.now_playing is not None:
        player.vote_message = sent


async def voteskip_update(player, guild_id, channel=None):
    """Recompute the votes of the current track (call after a vote or a voice
    change), skip when enough listeners voted, and show the count in the
    track's one vote message. Returns (count, needed, skipped)."""
    gen = player.play_gen  # The track these votes are for
    count, needed = voteskip.status(player, guild_id)
    lang = player_lang(player)
    message = player.vote_message
    skipped = False
    if count > 0 and count >= needed:
        try:
            skipped = await control_next(player, gen=gen)
        except ControlError:
            return count, needed, False
        if not skipped:  # The track already changed or is changing
            return count, needed, False
    key = "vote.skipped" if skipped else "vote.progress"
    text = t(key, lang, count=count, needed=needed)
    await _show_vote(player, text, channel or player.text_channel, message, skipped)
    refresh_panel(player)
    return count, needed, skipped


async def control_voteskip(player, guild_id, member, channel=None):
    """`member` votes to skip the current track. Returns (count, needed,
    skipped). One vote per member; only a listener in the bot's channel."""
    _require_playing(player)
    voice = getattr(member, "voice", None)
    if getattr(voice, "channel", None) != player.voice_client.channel:
        raise ControlError("vote_not_in_channel")
    if not voteskip.is_listener(member):
        raise ControlError("vote_deafened")
    if member.id in player.votes:
        raise ControlError("vote_already")
    player.votes.add(member.id)
    return await voteskip_update(player, guild_id, channel)


def control_voteskip_threshold(guild_id, percent):
    """Set the guild's threshold (1-100); applies even when saving fails."""
    if not voteskip.valid_percent(percent):
        raise ControlError("bad_threshold")
    try:
        voteskip.set_threshold(guild_id, percent)
    except OSError as e:
        print(f"Could not save the vote-skip threshold: {e}")
        raise ControlError("threshold_not_saved")
    player = state.music_players.get(guild_id)
    if player is not None:
        refresh_panel(player)  # Also notifies the page


async def control_previous(player):
    """Restart the current track, or go back to the last played one.
    Returns "restart" or "previous"."""
    elapsed = player.elapsed() or 0.0
    if player.now_playing:  # Played time counts from the automix start
        elapsed -= player.now_playing.get("start") or 0.0
    if elapsed > PREVIOUS_RESTART_SECONDS or not player.history:
        action = "restart"
    else:
        action = "previous"
    await _stop_with(player, action)
    return action


async def control_seek(player, position):
    """Restart the current track at `position` seconds (clamped to the
    track), keeping it paused if it was. Returns the applied position."""
    async with player.lock:
        _require_playing(player)
        position = max(0.0, float(position))
        track = player.now_playing
        start, end = track.get("start") or 0.0, track.get("end")
        if end is not None:  # Automix window, as playback_options clamps it
            position = max(start, min(position, end - 1))
        elif track.get("duration"):
            position = max(0.0, min(position, track["duration"] - 1))
        if player.pending_action == "seek":
            player.seek_position = position  # Not applied yet: retarget it
            return position
        if player.pending_action is not None:
            return position  # Another transition is already under way
        player.seek_position = position
        player.seek_paused = player.paused_at is not None
        player.pending_action = "seek"
        player.voice_client.stop()
        return position


async def control_jump(player, guild_id, track_id):
    """Play a queued track now; the current one goes to history like a skip
    and the tracks queued before it keep their order. Returns the track."""
    async with player.lock:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        track = player.queue.pop(_find_queued(player, track_id))
        player.queue.insert(0, track)
        idle = not player.is_playing or not player.now_playing
        if not idle:
            # A pending action is only applied by play_next, after this
            # stop(): overriding it is safe and makes the jump win.
            already_stopping = player.pending_action is not None
            player.pending_action = "next"
            if not already_stopping:
                player.voice_client.stop()
    if idle:
        await play_next(WebContext(guild_id, player))
    return track


async def control_pause(player):
    """Returns False if it was already paused."""
    async with player.lock:
        _require_playing(player)
        if player.paused_at is not None:
            return False
        player.voice_client.pause()
        player.paused_at = time.monotonic()
        return True


async def control_resume(player):
    """Returns False if it was not paused."""
    async with player.lock:
        _require_playing(player)
        if player.paused_at is None:
            return False
        player.voice_client.resume()
        player.paused_total += time.monotonic() - player.paused_at
        player.paused_at = None
        return True


def control_crossfade(player, enabled):
    """Turn the session's crossfade on or off. It applies from the next
    transition; an overlap already running finishes."""
    player.crossfade = bool(enabled)
    player.last_active = state.clock()
    refresh_panel(player)


def control_loop(player, mode=None):
    """Set the session's loop mode ("off", "track" or "queue"); None cycles
    to the next one. Returns the new mode."""
    if mode is None:
        mode = LOOP_MODES[(LOOP_MODES.index(player.loop) + 1) % len(LOOP_MODES)]
    if mode not in LOOP_MODES:
        raise ControlError("bad_loop")
    player.loop = mode
    player.last_active = state.clock()
    refresh_panel(player)
    return mode


async def control_shuffle(player):
    """Shuffle the upcoming queue once (the current track stays)."""
    async with player.lock:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        if len(player.queue) < 2:
            raise ControlError("nothing_to_shuffle")
        random.shuffle(player.queue)
    player.last_active = state.clock()


def control_volume(player, guild_id, percent):
    """Set the server's volume (0-150 %, 100 = the normalised level). It
    applies to the playing track at once, glides over a few frames and is
    saved for later. Raises `volume_not_saved` (the change still applies
    until restart) if DATA_DIR is not writable."""
    if not valid_volume(percent):
        raise ControlError("bad_volume")
    state.guild_volumes[guild_id] = percent
    player.last_active = state.clock()
    if player.mixer is not None:
        player.mixer.set_volume(percent_to_gain(percent))
    refresh_panel(player)
    try:
        save_volumes()
    except OSError as e:
        print(f"Could not save volumes: {e}")
        raise ControlError("volume_not_saved")
    return volume_for(guild_id)


def control_autoplay(player, enabled):
    """Turn the session's autoplay on or off. On, a related track is
    prepared at once if the queue is empty; off, the prepared one is dropped."""
    player.autoplay = bool(enabled)
    player.last_active = state.clock()
    if not player.autoplay:
        player.drop_autoplay_next()
    elif player.is_playing:
        guild_id = guild_of(player)
        if guild_id is not None:
            prefetch_autoplay(WebContext(guild_id, player), player)
    refresh_panel(player)


def seek_label(player, applied):
    """Panel line for a seek; the time is relative to the track's automix
    start, as the web page shows it."""
    start = (player.now_playing or {}).get("start") or 0.0
    rel = int(max(0.0, applied - start))
    return t("action.seeked", player_lang(player), time=f"{rel // 60}:{rel % 60:02d}")


def _find_queued(player, track_id):
    for i, track in enumerate(player.queue):
        if track["id"] == track_id:
            return i
    raise ControlError("track_not_found")


async def control_remove(player, track_id):
    """Returns the removed track."""
    async with player.lock:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        track = player.queue.pop(_find_queued(player, track_id))
    discard_tracks([track])
    return track


async def control_move(player, track_id, index):
    """Move a queued track to a 0-based queue index. Returns the track."""
    async with player.lock:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        i = _find_queued(player, track_id)
        if not 0 <= index < len(player.queue):
            raise ControlError("bad_index")
        track = player.queue.pop(i)
        player.queue.insert(index, track)
    return track


async def control_clear(player):
    """Empty the upcoming queue; the current track keeps playing. Returns the
    removed tracks."""
    async with player.lock:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        if not player.queue:
            raise ControlError("queue_empty")
        removed = list(player.queue)
        player.queue.clear()
    discard_tracks(removed)
    return removed


async def control_add(player, guild_id, url):
    """Validate a link and download it in the background. A Spotify link's
    track list is read first, so its errors reach the caller."""
    try:
        link = catalog.check_link(url)
    except spotify.SpotifyError as e:
        raise ControlError(e.code)
    if link is not None:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        epoch = player.epoch
        tracks = await read_spotify(*link)
        if player.epoch != epoch:
            return  # Stopped or reset while the link was being read
        _background(_add_spotify_from_web(player, guild_id, link[0], tracks))
        return
    parsed = parse_media_url(url)
    if parsed is None:
        text = " ".join(url.split()) if isinstance(url, str) else ""
        if not text or len(text) > MAX_SEARCH_LENGTH or looks_like_url(text):
            raise ControlError("bad_url")
        if not player.in_voice():
            raise ControlError("not_in_voice")
        _background(_add_search_from_web(player, guild_id, text))
        return
    if not player.in_voice():
        raise ControlError("not_in_voice")
    kind, clean_url = parsed
    _background(_add_from_web(player, guild_id, kind, clean_url))


async def read_spotify(kind, ref, provider="spotify"):
    """Track list of a Spotify, Deezer or Apple Music link (up to
    MAX_PLAYLIST_SIZE). Raises ControlError with the reason it cannot be read."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, catalog.get_tracks, provider, kind, ref, MAX_PLAYLIST_SIZE
        )
    except spotify.SpotifyError as e:
        print(f"{provider} {kind} {ref}: {e}")
        raise ControlError(e.code)


def _background(coro):
    task = asyncio.get_running_loop().create_task(coro)
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)


async def _add_spotify_from_web(player, guild_id, kind, tracks):
    ctx = WebContext(guild_id, player)
    epoch = player.epoch
    player.loading_jobs += 1  # Keeps the bot in voice, as in _add_from_web
    try:
        await queue_spotify(ctx, player, kind, tracks, from_web=True)
    except Exception as e:
        print(f"Web Spotify add failed: {e}")
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)
    if not player.is_playing and not cancelled(player, epoch):
        await play_next(ctx)


async def _add_search_from_web(player, guild_id, query):
    ctx = WebContext(guild_id, player)
    epoch = player.epoch
    player.loading_jobs += 1  # Keeps the bot in voice, as in _add_from_web
    try:
        await queue_search(ctx, player, query, from_web=True)
    except Exception as e:
        print(f"Web search add failed: {e}")
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)
    if not player.is_playing and not cancelled(player, epoch):
        await play_next(ctx)


async def _add_from_web(player, guild_id, kind, url):
    ctx = WebContext(guild_id, player)
    epoch = player.epoch
    # Counts as loading, so the queue running out meanwhile keeps the bot in
    # voice (the web page cannot make it join again).
    player.loading_jobs += 1
    try:
        if kind == "single":
            await download_and_queue_song(
                ctx, player, url, skip_voice_check=True, from_web=True
            )
        else:
            loop = asyncio.get_running_loop()
            key = player.add_pending(None)  # The link, until its list is read
            try:
                entries = await loop.run_in_executor(None, media.get_playlist_info, url)
            finally:
                player.drop_pending(key)
            if cancelled(player, epoch):
                pass
            elif not entries:
                await ctx.send(t("play.web_playlist_failed", ctx_lang(ctx)))
            else:
                entries = entries[:MAX_PLAYLIST_SIZE]
                await queue_playlist(ctx, player, entries, from_web=True)
    except Exception as e:
        print(f"Web add failed: {e}")
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)
    if not player.is_playing and not cancelled(player, epoch):
        await play_next(ctx)


# --- Saved playlists (storage in playlists.py) ------------------------------


async def _playlist_call(fn, *args, **kwargs):
    try:
        return await playlists.run(fn, *args, **kwargs)
    except playlists.PlaylistError as e:
        raise ControlError(e.code)


def _current_and_queue(player):
    tracks = [player.now_playing] if player.now_playing else []
    return tracks + list(player.queue)


async def control_playlist_save(player, guild_id, name, author_id=None, is_admin=False):
    """Save the current track and the queue as a playlist. Returns
    {name, count}."""
    return await _playlist_call(
        playlists.save,
        guild_id,
        name,
        _current_and_queue(player),
        creator_id=author_id,
        is_admin=is_admin,
    )


async def control_playlist_add(player, guild_id, name):
    """Add the current track to a saved playlist. Returns {name, count}."""
    if not player.now_playing:
        raise ControlError("nothing_playing")
    return await _playlist_call(playlists.add_track, guild_id, name, player.now_playing)


async def control_playlist_get(guild_id, name):
    """A saved playlist: {name, creator_id, tracks}."""
    return await _playlist_call(playlists.get, guild_id, name)


async def control_playlist_list(guild_id):
    return await _playlist_call(playlists.list_all, guild_id)


async def control_playlist_delete(guild_id, name, author_id=None, is_admin=False):
    """Returns the deleted playlist's name."""
    return await _playlist_call(
        playlists.delete, guild_id, name, user_id=author_id, is_admin=is_admin
    )


def playlist_entries(playlist):
    """A saved playlist as the entries queue_playlist() takes. Links are
    checked again: only YouTube videos are downloaded."""
    entries = []
    for track in playlist["tracks"]:
        parsed = parse_youtube_url(track["url"])
        if parsed is not None and parsed[0] == "single":
            entries.append({"url": parsed[1], "title": track["title"]})
    return entries


async def control_playlist_load(player, guild_id, name):
    """Queue a saved playlist from the web page (the bot must be in voice);
    its tracks download in the background. Returns the playlist."""
    if not player.in_voice():
        raise ControlError("not_in_voice")
    playlist = await control_playlist_get(guild_id, name)
    entries = playlist_entries(playlist)
    if not entries:
        raise ControlError("playlist_empty")
    _background(_load_playlist_from_web(player, guild_id, entries))
    return playlist


async def _load_playlist_from_web(player, guild_id, entries):
    ctx = WebContext(guild_id, player)
    epoch = player.epoch
    player.loading_jobs += 1  # Keeps the bot in voice, as in _add_from_web
    try:
        await queue_playlist(ctx, player, entries, from_web=True)
    except Exception as e:
        print(f"Web playlist load failed: {e}")
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)
    if not player.is_playing and not cancelled(player, epoch):
        await play_next(ctx)
