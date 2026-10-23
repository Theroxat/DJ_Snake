"""Web server for the synced clip page (aiohttp, on the bot's loop)."""

import asyncio
import ipaddress
import json
import math
import os
import re
import time

from aiohttp import web

from . import state, stats
from .config import (
    LOGO_TYPES,
    MAX_CONTROL_BODY,
    MAX_LOGO_BYTES,
    MAX_STREAMS_PER_CLIENT,
    MAX_STREAMS_PER_SESSION,
    MAX_VIEWERS_PER_SESSION,
    PUBLIC_BASE_URL,
    SSE_CHECK_SECONDS,
    SSE_HEARTBEAT_SECONDS,
    SSE_MIN_INTERVAL,
    STATS_CACHE_SECONDS,
    THEME_NAME_MAX,
    THEME_PRESETS,
    THEME_SKINS,
    VIEWER_WINDOW_SECONDS,
    WEB_INDEX,
    WEB_PORT,
)
from .controls import (
    ControlError,
    control_add,
    control_crossfade,
    control_jump,
    control_move,
    control_next,
    control_pause,
    control_previous,
    control_remove,
    control_resume,
    control_seek,
    seek_label,
)
from .player import notify_state
from .sessions import refresh_panel, session_player, set_action, short, touch
from .themes import (
    check_edit_token,
    clean_theme_name,
    logo_path,
    remove_logo,
    save_themes,
    set_logo,
    sniff_image,
    theme_state,
    valid_color,
)

# --- Web server: synced clip page -------------------------------------------
#
# Contract relied on by web/index.html:
#   GET /                      landing text
#   GET /{sid}                 the page (it handles unknown/ended sessions)
#   GET /api/{sid}/state       session state JSON, 404 {"active": false} if none;
#                              an X-Viewer header (the tab's random id) counts
#                              the tab in `viewers`; `downloading` lists the
#                              first MAX_DOWNLOADING tracks asked for but not
#                              queued yet, `downloading_total` counts them all
#   GET /api/{sid}/events      ?viewer=<tab id>, the same state pushed as
#                              Server-Sent Events (see Live updates below)
#   GET /api/{sid}/stats       ?range=week|month|all, the guild's listening stats
#   POST /api/{sid}/control    {"action", "id"?, "index"?, "url"?, "position"?,
#                              "enabled"?} -> {"ok": ...}; actions: next,
#                              previous, pause, resume, remove, move, add,
#                              seek, jump, crossfade ({"enabled": bool})
#   GET /media/{sid}/{id}      the mp4 of a playing, queued or history track
#   POST /api/{sid}/theme      {skin, base, bg, text, accent, name}, with the
#                              X-Edit-Token header of a `!theme edit` link
#   POST|DELETE /api/{sid}/theme/logo   raw PNG/JPEG/WebP/GIF body (same token)
#   GET /theme-logo/{sid}      the guild's logo (?v= busts caches)
# Paths are never built from the request: tracks are looked up by id, themes
# and logos by the session's guild id.


def _track_summary(track):
    return {
        "id": track["id"],
        "title": track["title"],
        "duration": track.get("duration"),
        "thumbnail": track.get("thumbnail"),
        "url": track.get("url"),
    }


async def web_landing(request):
    return web.Response(text="DJ Snake")


async def web_page(request):
    if not os.path.isfile(WEB_INDEX):
        return web.Response(status=404, text="Page not found")
    # The URL may carry a `!theme edit` token: never leak it as a referrer.
    headers = {"Cache-Control": "no-cache", "Referrer-Policy": "same-origin"}
    return web.FileResponse(WEB_INDEX, headers=headers)


# Pending downloads listed in the state (the rest is only counted).
MAX_DOWNLOADING = 50

_VIEWER_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")


def count_viewer(sid, viewer):
    """Note that tab `viewer` (None: not given) polls `sid`; returns how
    many tabs polled within VIEWER_WINDOW_SECONDS or have a live stream open. The map keeps poll order
    (a tab moves to the end each time), so pruning stops at the first fresh
    entry, and it holds at most MAX_VIEWERS_PER_SESSION ids."""
    now = state.clock()
    seen = state.viewers.setdefault(sid, {})
    if viewer is not None and _VIEWER_RE.match(viewer):
        seen.pop(viewer, None)
        seen[viewer] = now
    cutoff = now - VIEWER_WINDOW_SECONDS
    for key in list(seen):
        if seen[key] >= cutoff:
            break
        del seen[key]
    while len(seen) > MAX_VIEWERS_PER_SESSION:
        del seen[next(iter(seen))]
    # Open live streams count too (a tab that streams and polled lately
    # counts once; a stream without an id counts on its own).
    streaming = state.streams.get(sid, ())
    ids = {s.viewer for s in streaming if s.viewer is not None} - seen.keys()
    anonymous = sum(1 for s in streaming if s.viewer is None)
    return min(MAX_VIEWERS_PER_SESSION, len(seen) + len(ids) + anonymous)


def state_body(sid, player, viewers):
    """The session state the page renders (GET /api/{sid}/state and each
    live update event)."""
    guild = state.bot.get_guild(state.sessions[sid])
    in_voice = player.in_voice()
    channel = None
    now_playing = None
    queue = []
    downloading = []
    if in_voice:
        if player.voice_client.channel:
            channel = player.voice_client.channel.name
        if player.now_playing and player.started_at is not None:
            track = player.now_playing
            now_playing = _track_summary(track)
            now_playing["video_url"] = f"/media/{sid}/{track['id']}"
            now_playing["elapsed"] = player.elapsed()  # File time
            # Automix window (file time): the page's progress bar spans it.
            now_playing["start"] = track.get("start") or 0.0
            now_playing["end"] = track.get("end") or track.get("duration")
        queue = [_track_summary(t) for t in player.queue]
        downloading = list(player.pending_downloads)

    return {
        "active": True,
        "in_voice": in_voice,
        "guild": guild.name if guild else "",
        "channel": channel,
        "loading": player.loading,
        "paused": now_playing is not None and player.paused_at is not None,
        "can_previous": now_playing is not None and player.pending_action is None,
        "crossfade": player.crossfade,
        # True while the previous track still drains under this one.
        "crossfading": now_playing is not None
        and player.mixer is not None
        and player.mixer.outgoing is not None,
        "server_time": time.time(),
        "viewers": viewers,
        "now_playing": now_playing,
        "queue": queue,
        # Asked for, not queued yet (title None: link not read yet).
        "downloading": [dict(item) for item in downloading[:MAX_DOWNLOADING]],
        "downloading_total": len(downloading),
        "theme": theme_state(sid, state.sessions[sid]),
    }


async def web_state(request):
    sid = request.match_info["sid"]
    headers = {"Cache-Control": "no-store"}
    player = session_player(sid)
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    viewers = count_viewer(sid, request.headers.get("X-Viewer"))
    return web.json_response(state_body(sid, player, viewers), headers=headers)


# --- Live updates (Server-Sent Events) ---------------------------------------
#
# GET /api/{sid}/events?viewer=<tab id> streams the same JSON as /state, as
# `data:` events: one on connect, then one after each notify_state(player)
# (coalesced to one per SSE_MIN_INTERVAL), plus one whenever a state diff run
# every SSE_CHECK_SECONDS finds a change nobody notified (crossfade drain,
# viewers that poll). A `: ping` comment goes out after SSE_HEARTBEAT_SECONDS
# of silence. When the session ends the stream sends {"active": false} and
# closes. An open stream counts as a viewer.

# {"closing": bool}: set at shutdown, every stream then ends.
_STREAMS = web.AppKey("live_streams", dict)


class LiveStream:
    def __init__(self, sid, viewer, client):
        self.sid = sid
        self.viewer = viewer  # Tab id, or None (counted anyway)
        self.client = client  # _client_key() of the connection
        self.wake = asyncio.Event()
        self.player = None  # Whose watchers hold `wake`

    def watch(self, player):
        if self.player is player:
            return
        self.unwatch()
        player.watchers.add(self.wake)
        self.player = player

    def unwatch(self):
        if self.player is not None:
            self.player.watchers.discard(self.wake)
            self.player = None


def _client_key(request):
    """The client's address for the per-client stream cap. Behind a local
    reverse proxy or tunnel (loopback or private peer), the last
    X-Forwarded-For hop, which that proxy appended itself."""
    remote = request.remote or ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    try:
        peer = ipaddress.ip_address(remote)
    except ValueError:
        return remote
    if forwarded and (peer.is_loopback or peer.is_private):
        return forwarded.split(",")[-1].strip()[:64] or remote
    return remote


def _wake_streams(sid):
    """Wake every stream of `sid` (the viewer count changed)."""
    for stream in list(state.streams.get(sid, ())):
        stream.wake.set()


def _signature(body):
    """What a state diff compares: everything but the clock fields."""
    body = dict(body, server_time=None)
    if body.get("now_playing"):
        body["now_playing"] = dict(body["now_playing"], elapsed=None)
    return json.dumps(body, sort_keys=True, default=str)


def _sse(body):
    return b"data: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"


async def web_events(request):
    sid = request.match_info["sid"]
    headers = {"Cache-Control": "no-store"}
    player = session_player(sid)
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    if request.app[_STREAMS]["closing"]:
        return _control_error(503, "Shutting down.")
    viewer = request.query.get("viewer")
    if viewer is not None and not _VIEWER_RE.match(viewer):
        viewer = None
    client = _client_key(request)
    same_client = sum(
        1 for s in state.streams.values() for x in s if x.client == client
    )
    if (
        len(state.streams.get(sid, ())) >= MAX_STREAMS_PER_SESSION
        or same_client >= MAX_STREAMS_PER_CLIENT
    ):
        # The page falls back to polling and retries later.
        return _control_error(429, "Too many live connections.")

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            # nginx: never buffer the stream (see the reverse proxy docs).
            "X-Accel-Buffering": "no",
        }
    )
    stream = LiveStream(sid, viewer, client)
    open_streams = state.streams.setdefault(sid, set())
    open_streams.add(stream)
    stream.watch(player)
    try:
        await response.prepare(request)
        await response.write(b"retry: 5000\n\n")
        _wake_streams(sid)  # Everyone's viewer count, this stream's first state
        loop = asyncio.get_running_loop()
        last_sig = None
        last_sent = float("-inf")  # loop.time() of the last state
        last_write = loop.time()  # ... of the last state or ping
        last_check = loop.time()
        while not request.app[_STREAMS]["closing"]:
            now = loop.time()
            timeout = min(
                last_write + SSE_HEARTBEAT_SECONDS, last_check + SSE_CHECK_SECONDS
            )
            try:
                await asyncio.wait_for(stream.wake.wait(), max(0.0, timeout - now))
                woken = True
            except asyncio.TimeoutError:
                woken = False
            if request.app[_STREAMS]["closing"]:
                break
            transport = request.transport
            if transport is None or transport.is_closing():
                break  # The tab went away
            if woken:
                # Coalesce a burst: later changes land in this same state.
                wait = last_sent + SSE_MIN_INTERVAL - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                stream.wake.clear()
            player = session_player(sid)
            if player is None:
                await response.write(_sse({"active": False}))
                break
            stream.watch(player)  # !reset may have replaced the player
            last_check = loop.time()
            body = state_body(sid, player, count_viewer(sid, None))
            sig = _signature(body)
            if woken or sig != last_sig:
                await response.write(_sse(body))
                last_sig = sig
                last_sent = last_write = loop.time()
            elif loop.time() - last_write >= SSE_HEARTBEAT_SECONDS:
                await response.write(b": ping\n\n")
                last_write = loop.time()
    except (ConnectionResetError, ConnectionError):
        pass  # Closed by the client mid-write
    finally:
        stream.unwatch()
        open_streams.discard(stream)
        if not open_streams and state.streams.get(sid) is open_streams:
            del state.streams[sid]
        _wake_streams(sid)  # The others' viewer count
    return response


async def _close_streams(app):
    """On shutdown: end every stream now instead of waiting for a timeout."""
    app[_STREAMS]["closing"] = True
    for streams in list(state.streams.values()):
        for stream in list(streams):
            stream.wake.set()


async def web_media(request):
    sid = request.match_info["sid"]
    track_id = request.match_info["track_id"]
    player = session_player(sid)
    if player is None:
        raise web.HTTPNotFound()
    tracks = ([player.now_playing] if player.now_playing else []) + list(player.queue)
    tracks += list(player.history)
    for track in tracks:
        if track.get("id") == track_id:
            filepath = track["filepath"]
            if os.path.isfile(filepath):
                # Never stored by a proxy or tunnel in front (private), and
                # revalidated by the browser (no-cache): a clip URL must
                # always serve the file of that track.
                headers = {"Cache-Control": "private, no-cache"}
                return web.FileResponse(filepath, headers=headers)
            break
    raise web.HTTPNotFound()


# (guild id, range) -> (time.monotonic() expiry, summary): bounds the work a
# page (or anyone with the link) can cause.
_stats_cache = {}


async def web_stats(request):
    sid = request.match_info["sid"]
    headers = {"Cache-Control": "no-store"}
    if session_player(sid) is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    range_name = request.query.get("range", "week")
    if range_name not in stats.RANGES:
        return _control_error(400, "Unknown period.")
    guild_id = state.sessions[sid]
    key = (guild_id, range_name)
    now = time.monotonic()
    cached = _stats_cache.get(key)
    if cached is not None and cached[0] > now:
        return web.json_response(cached[1], headers=headers)
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, stats.summary, guild_id, range_name)
    except Exception as e:
        print(f"Stats read failed: {e}")
        return _control_error(500, "Stats are unavailable right now.")
    for stale in [k for k, v in _stats_cache.items() if v[0] <= now]:
        del _stats_cache[stale]
    _stats_cache[key] = (now + STATS_CACHE_SECONDS, result)
    return web.json_response(result, headers=headers)


def _control_error(status, message):
    return web.json_response({"ok": False, "error": message}, status=status)


async def _read_body(request, limit):
    """The request body, or None if it is larger than `limit` bytes."""
    if (request.content_length or 0) > limit:
        return None
    # StreamReader.read() may return a partial chunk: read until EOF.
    body = b""
    while len(body) <= limit:
        chunk = await request.content.read(limit + 1 - len(body))
        if not chunk:
            break
        body += chunk
    return None if len(body) > limit else body


async def _read_json(request):
    """(dict, None) or (None, error response) for a small JSON body."""
    body = await _read_body(request, MAX_CONTROL_BODY)
    if body is None:
        return None, _control_error(400, "Request too large.")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None, _control_error(400, "Invalid request.")
    if not isinstance(data, dict):
        return None, _control_error(400, "Invalid request.")
    return data, None


async def web_control(request):
    sid = request.match_info["sid"]
    player = session_player(sid)
    if player is None:
        return _control_error(404, "Unknown or expired session.")
    guild_id = state.sessions[sid]

    data, error = await _read_json(request)
    if error is not None:
        return error
    action = data.get("action")
    if action not in (
        "next",
        "previous",
        "pause",
        "resume",
        "remove",
        "move",
        "add",
        "seek",
        "jump",
        "crossfade",
    ):
        return _control_error(400, "Unknown action.")

    track_id = data.get("id")
    index = data.get("index")
    if action in ("remove", "move", "jump") and not (
        isinstance(track_id, str) and 0 < len(track_id) <= 64
    ):
        return _control_error(400, "Invalid track.")
    if action == "move" and (type(index) is not int):
        return _control_error(400, "Invalid position.")
    position = data.get("position")
    if action == "seek" and not (
        isinstance(position, (int, float))
        and not isinstance(position, bool)
        and math.isfinite(position)
        and position >= 0
    ):
        return _control_error(400, "Invalid position.")
    if action == "add" and not isinstance(data.get("url"), str):
        return _control_error(400, "Missing link.")
    if action == "crossfade" and not isinstance(data.get("enabled"), bool):
        return _control_error(400, "Invalid request.")

    touch(player)
    if player.sid != sid:  # Expired just now
        return _control_error(404, "Unknown or expired session.")

    try:
        if action == "next":
            await control_next(player)
            set_action(player, "⏭️ Skipped via the web page")
        elif action == "previous":
            if await control_previous(player) == "restart":
                set_action(player, "⏮️ Restarted via the web page")
            else:
                set_action(player, "⏮️ Previous track via the web page")
        elif action == "pause":
            await control_pause(player)
            refresh_panel(player)
        elif action == "resume":
            await control_resume(player)
            refresh_panel(player)
        elif action == "remove":
            track = await control_remove(player, track_id)
            set_action(player, f"🗑️ Removed: {short(track['title'])}")
        elif action == "move":
            await control_move(player, track_id, index)
            set_action(player, "🔀 Queue reordered")
        elif action == "seek":
            applied = await control_seek(player, position)
            set_action(player, seek_label(player, applied))
        elif action == "crossfade":
            control_crossfade(player, data["enabled"])
        elif action == "jump":
            track = await control_jump(player, guild_id, track_id)
            set_action(player, f"▶️ Jumped to: {short(track['title'])}")
        elif action == "add":
            await control_add(player, guild_id, data["url"])
            return web.json_response({"ok": True}, status=202)
    except ControlError as e:
        return _control_error(e.status, e.message)
    return web.json_response({"ok": True})


def _theme_auth(request):
    """(player, guild id, None) for a valid edit token on a live session,
    else (None, None, error response)."""
    sid = request.match_info["sid"]
    player = session_player(sid)
    if player is None:
        return None, None, _control_error(404, "Unknown or expired session.")
    guild_id = state.sessions[sid]
    if not check_edit_token(request.headers.get("X-Edit-Token"), guild_id):
        return (
            None,
            None,
            _control_error(
                403,
                "Invalid or expired edit link. " "Run !theme edit in Discord again.",
            ),
        )
    return player, guild_id, None


def _theme_saved(request, player, guild_id):
    touch(player)
    set_action(player, "🎨 Clip page theme updated via the web page")
    theme = theme_state(request.match_info["sid"], guild_id)
    return web.json_response({"ok": True, "theme": theme})


_SAVE_FAILED = "Could not save on the server."


async def web_theme(request):
    player, guild_id, error = _theme_auth(request)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    if data.get("skin") not in THEME_SKINS:
        return _control_error(400, "Unknown theme.")
    if data.get("base") not in THEME_PRESETS:
        return _control_error(400, "Unknown base style.")
    if not all(valid_color(data.get(k)) for k in ("bg", "text", "accent")):
        return _control_error(400, "Invalid color (use #rrggbb).")
    name = clean_theme_name(data.get("name", ""))
    if name is None:
        return _control_error(400, f"Invalid name ({THEME_NAME_MAX} characters max).")
    previous = state.guild_themes.get(guild_id)
    state.guild_themes[guild_id] = {
        **(previous or {}),
        "skin": data["skin"],
        "base": data["base"],
        "bg": data["bg"].lower(),
        "text": data["text"].lower(),
        "accent": data["accent"].lower(),
        "name": name,
    }
    try:
        save_themes()
    except OSError as e:
        # Keep memory and disk in step: the page would otherwise show a theme
        # that is lost at the next restart.
        if previous is None:
            state.guild_themes.pop(guild_id, None)
        else:
            state.guild_themes[guild_id] = previous
        print(f"Could not save theme: {e}")
        return _control_error(500, _SAVE_FAILED)
    return _theme_saved(request, player, guild_id)


async def web_theme_logo(request):
    player, guild_id, error = _theme_auth(request)
    if error is not None:
        return error
    if request.method == "DELETE":
        try:
            remove_logo(guild_id)
        except OSError as e:
            print(f"Could not remove logo: {e}")
            return _control_error(500, _SAVE_FAILED)
        return _theme_saved(request, player, guild_id)
    body = await _read_body(request, MAX_LOGO_BYTES)
    if body is None:
        return _control_error(400, "Image too large (512 KB max).")
    ext = sniff_image(body)
    if ext is None:
        return _control_error(400, "Unsupported format (PNG, JPEG, WebP or GIF).")
    try:
        set_logo(guild_id, body, ext)
    except OSError as e:
        print(f"Could not save logo: {e}")
        return _control_error(500, _SAVE_FAILED)
    return _theme_saved(request, player, guild_id)


async def web_theme_logo_file(request):
    sid = request.match_info["sid"]
    if session_player(sid) is None:
        raise web.HTTPNotFound()
    guild_id = state.sessions[sid]
    logo = state.guild_themes.get(guild_id, {}).get("logo")
    if not logo:
        raise web.HTTPNotFound()
    try:
        with open(logo_path(guild_id, logo["ext"]), "rb") as f:
            data = f.read(MAX_LOGO_BYTES + 1)
    except OSError:
        raise web.HTTPNotFound()
    return web.Response(
        body=data,
        content_type=LOGO_TYPES[logo["ext"]],
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "Cache-Control": "public, max-age=86400",
        },
    )


def _notifies(handler):
    """Wrap a route that changes the session: whatever it did (even a failed
    action), the page's live streams get a fresh state. New control actions
    need nothing more."""

    async def wrapped(request):
        try:
            return await handler(request)
        finally:
            sid = request.match_info["sid"]
            player = state.music_players.get(state.sessions.get(sid))
            if player is not None and player.sid == sid:
                notify_state(player)

    return wrapped


def build_web_app():
    app = web.Application()
    app[_STREAMS] = {"closing": False}
    app.on_shutdown.append(_close_streams)
    app.router.add_get("/", web_landing)
    app.router.add_get("/api/{sid}/state", web_state)
    app.router.add_get("/api/{sid}/events", web_events)
    app.router.add_post("/api/{sid}/control", _notifies(web_control))
    app.router.add_get("/api/{sid}/stats", web_stats)
    app.router.add_post("/api/{sid}/theme", _notifies(web_theme))
    app.router.add_post("/api/{sid}/theme/logo", _notifies(web_theme_logo))
    app.router.add_delete("/api/{sid}/theme/logo", _notifies(web_theme_logo))
    app.router.add_get("/media/{sid}/{track_id}", web_media)
    app.router.add_get("/theme-logo/{sid}", web_theme_logo_file)
    app.router.add_get("/{sid}", web_page)
    return app


web_runner = None


async def start_web_server():
    """Start the web server in the bot's event loop (once)."""
    global web_runner
    if web_runner is not None:
        return
    # Open streams end at once on shutdown (_close_streams): no long wait.
    runner = web.AppRunner(build_web_app(), access_log=None, shutdown_timeout=5)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    except OSError as e:
        # A busy port must not keep the music bot from starting.
        print(f"❌ Web server could not listen on port {WEB_PORT}: {e}")
        await runner.cleanup()
        return
    web_runner = runner
    print(f"Web server listening on port {WEB_PORT} ({PUBLIC_BASE_URL})")


async def stop_web_server():
    """Close the live streams and the server (bot shutdown)."""
    global web_runner
    runner, web_runner = web_runner, None
    if runner is not None:
        await runner.cleanup()
