"""Controls shared by Discord commands and the web page."""

import asyncio
import time
import types

from . import media, spotify, state
from .config import MAX_PLAYLIST_SIZE, MAX_SEARCH_LENGTH, PREVIOUS_RESTART_SECONDS
from .media import looks_like_url, parse_youtube_url
from .playback import (
    cancelled,
    download_and_queue_song,
    play_next,
    queue_playlist,
    queue_search,
    queue_spotify,
)
from .player import discard_tracks
from .sessions import refresh_panel


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
        if self.channel is None:
            return _NullMessage()
        try:
            return await self.channel.send(content, **kwargs)
        except Exception as e:
            print(f"Could not send: {e}")
            return _NullMessage()


class ControlError(Exception):
    """A control that cannot be applied. `code` picks the message."""

    # code -> (HTTP status, message), shown on Discord and on the web page
    MESSAGES = {
        "not_in_voice": (409, "I'm not in a voice channel."),
        "nothing_playing": (409, "No music is currently playing."),
        "track_not_found": (404, "That track is not in the queue."),
        "bad_index": (400, "Invalid position."),
        "bad_url": (
            400,
            "Only YouTube (video or playlist) and Spotify (track, album or "
            "playlist) links are accepted.",
        ),
        "spotify_short_link": (
            400,
            "Short spotify.link links are not supported: open it and copy the "
            "open.spotify.com link.",
        ),
        "spotify_not_found": (
            404,
            "That Spotify playlist, album or track was not found (private or "
            "deleted?).",
        ),
        "spotify_empty": (400, "That Spotify link has no tracks."),
        "spotify_unreadable": (
            502,
            "Could not read that Spotify link right now (Spotify may have "
            "changed its page).",
        ),
    }

    def __init__(self, code):
        super().__init__(code)
        self.code = code
        self.status, self.message = self.MESSAGES[code]


def _require_playing(player):
    if not player.in_voice():
        raise ControlError("not_in_voice")
    if not player.is_playing or not player.now_playing:
        raise ControlError("nothing_playing")


async def _stop_with(player, action):
    """Stop the current track; play_next then applies `action`."""
    async with player.lock:
        _require_playing(player)
        if player.pending_action is not None:
            return  # A transition is already under way
        player.pending_action = action
        player.voice_client.stop()


async def control_next(player):
    await _stop_with(player, "next")


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


def seek_label(player, applied):
    """Panel line for a seek; the time is relative to the track's automix
    start, as the web page shows it."""
    start = (player.now_playing or {}).get("start") or 0.0
    rel = int(max(0.0, applied - start))
    return f"⏩ Seeked to {rel // 60}:{rel % 60:02d}"


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


async def control_add(player, guild_id, url):
    """Validate a link and download it in the background. A Spotify link's
    track list is read first, so its errors reach the caller."""
    try:
        spotify_ref = spotify.check_url(url)
    except spotify.SpotifyError as e:
        raise ControlError(e.code)
    if spotify_ref is not None:
        if not player.in_voice():
            raise ControlError("not_in_voice")
        epoch = player.epoch
        tracks = await read_spotify(*spotify_ref)
        if player.epoch != epoch:
            return  # Stopped or reset while the link was being read
        _background(_add_spotify_from_web(player, guild_id, spotify_ref[0], tracks))
        return
    parsed = parse_youtube_url(url)
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


async def read_spotify(kind, spotify_id):
    """Track list of a Spotify link (up to MAX_PLAYLIST_SIZE). Raises
    ControlError with the reason it cannot be read."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None, spotify.get_tracks, kind, spotify_id, MAX_PLAYLIST_SIZE
        )
    except spotify.SpotifyError as e:
        print(f"Spotify {kind} {spotify_id}: {e}")
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
                await ctx.send(
                    "❌ Could not read the playlist added from the web page."
                )
            else:
                entries = entries[:MAX_PLAYLIST_SIZE]
                await queue_playlist(ctx, player, entries, from_web=True)
    except Exception as e:
        print(f"Web add failed: {e}")
    finally:
        player.loading_jobs = max(0, player.loading_jobs - 1)
    if not player.is_playing and not cancelled(player, epoch):
        await play_next(ctx)
