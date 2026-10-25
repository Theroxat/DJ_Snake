"""Web server for the synced clip page (aiohttp, on the bot's loop)."""

import asyncio
import ipaddress
import json
import math
import os
import re
import time

from aiohttp import web

from . import lyrics, state, stats
from .config import (
    COMMAND_PREFIX,
    APP_VERSION,
    LOGO_TYPES,
    LOOP_MODES,
    MAX_CONTROL_BODY,
    MAX_LOGO_BYTES,
    MAX_STREAMS_PER_CLIENT,
    MAX_STREAMS_PER_SESSION,
    MAX_STREAMS_TOTAL,
    MAX_VIEWERS_PER_SESSION,
    METRICS_ENABLED,
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
    control_autoplay,
    control_crossfade,
    control_jump,
    control_loop,
    control_move,
    control_next,
    control_pause,
    control_playlist_list,
    control_playlist_load,
    control_playlist_save,
    control_previous,
    control_remove,
    control_resume,
    control_seek,
    control_shuffle,
    control_volume,
    seek_label,
)
from .i18n import lang_for, t
from .player import notify_state
from .sessions import (
    refresh_panel,
    resolve_link,
    session_player,
    set_action,
    short,
    touch,
)
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
from .volume import valid_volume, volume_for
from .voteskip import web_status

# --- Web server: synced clip page -------------------------------------------
#
# Contract relied on by web/index.html:
#   GET /                      landing text
#   {sid} below is a session id (the control link) or the session's view-only
#   id; both serve the same routes, but a view-only id gets 403 on control and
#   theme routes, and `can_control: false` in the state. URLs in the state are
#   built from the id the request used.
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
#                              seek, jump, crossfade and autoplay ({"enabled": bool}),
#                              loop ({"mode": off|track|queue}, none cycles),
#                              shuffle,
#                              volume ({"value": int 0-150, percent})
#                              plus save_playlist ({"name"}: the current track
#                              and the queue) and load_playlist ({"name"}, 202)
#   GET /api/{sid}/playlists   the guild's saved playlists, {"playlists":
#                              [{name, tracks, duration}]}
#   GET /media/{sid}/{id}      the mp4 of a playing, queued or history track
#   GET /api/{sid}/lyrics/{id} {synced: [[ms, line]...] | null, plain} of such a
#                              track (LRCLIB), 202 {"pending": true} while the
#                              lookup runs, 404 when none (or lyrics are off)
#   POST /api/{sid}/theme      {skin, base, bg, text, accent, name}, with the
#                              X-Edit-Token header of a `!theme edit` link
#   POST|DELETE /api/{sid}/theme/logo   raw PNG/JPEG/WebP/GIF body (same token)
#   GET /theme-logo/{sid}      the guild's logo (?v= busts caches)
#   GET /healthz               200 {"status": "ok", ...} when the bot is connected
#                              to Discord, else 503 {"status": "unavailable",
#                              ...}; no ids, names or secrets
#   GET /metrics               Prometheus text; 404 unless METRICS is on
# Paths are never built from the request: tracks are looked up by id, themes
# and logos by the session's guild id.


def _discord_problem():
    """Why the bot is not connected to Discord (None when it is). Gateway
    resumes are routine and briefly close the websocket; the Docker health
    check retries, so one blip does not mark the container unhealthy."""
    bot = state.bot
    if bot.is_closed():
        return "closed"
    if not bot.is_ready():
        return "not ready"
    if not math.isfinite(bot.latency):  # No websocket, or no heartbeat answer
        return "websocket closed"
    return None


def _uptime_s():
    return int(time.monotonic() - state.started_at)


async def web_healthz(request):
    headers = {"Cache-Control": "no-store"}
    problem = _discord_problem()
    if problem is not None:
        body = {"status": "unavailable", "discord": problem, "uptime_s": _uptime_s()}
        return web.json_response(body, status=503, headers=headers)
    body = {
        "status": "ok",
        "discord": "connected",
        "latency_ms": round(state.bot.latency * 1000, 1),
        "guilds": len(state.bot.guilds),
        "players": sum(1 for p in state.music_players.values() if p.in_voice()),
        "uptime_s": _uptime_s(),
        "version": APP_VERSION,
    }
    return web.json_response(body, headers=headers)


def _metric(lines, name, kind, help_text, value):
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")
    lines.append(f"{name} {value}")


async def web_metrics(request):
    if not METRICS_ENABLED:
        raise web.HTTPNotFound()
    players = list(state.music_players.values())
    cutoff = state.clock() - VIEWER_WINDOW_SECONDS
    viewers = sum(
        1 for seen in list(state.viewers.values()) for t in seen.values() if t >= cutoff
    )
    latency = state.bot.latency
    lines = []
    _metric(
        lines,
        "djsnake_active_players",
        "gauge",
        "Guilds where the bot is in a voice channel.",
        sum(1 for p in players if p.in_voice()),
    )
    _metric(
        lines,
        "djsnake_queued_tracks",
        "gauge",
        "Tracks waiting in the queues.",
        sum(len(p.queue) for p in players),
    )
    _metric(
        lines,
        "djsnake_downloads_in_progress",
        "gauge",
        "Tracks asked for but not queued yet.",
        sum(max(len(p.pending_downloads), p.single_downloads) for p in players),
    )
    _metric(
        lines,
        "djsnake_web_viewers",
        "gauge",
        "Clip page tabs that polled recently.",
        viewers,
    )
    _metric(
        lines,
        "djsnake_discord_latency_seconds",
        "gauge",
        "Discord gateway heartbeat latency (NaN without a connection).",
        f"{latency:.6f}" if math.isfinite(latency) else "NaN",
    )
    _metric(
        lines,
        "djsnake_tracks_played_total",
        "counter",
        "Tracks played since the bot started.",
        state.counters["tracks_played"],
    )
    return web.Response(
        text="\n".join(lines) + "\n",
        content_type="text/plain",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


def _track_summary(track):
    return {
        "id": track["id"],
        "title": track["title"],
        "duration": track.get("duration"),
        "thumbnail": track.get("thumbnail"),
        "url": track.get("url"),
        "autoplay": bool(track.get("autoplay")),
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


def _session(request):
    """(link id, sid, player, can_control) for the id in the URL: a session
    id (control link) or a view-only id; player None if neither is live."""
    link = request.match_info["sid"]
    sid, can_control = resolve_link(link)
    player = session_player(sid) if sid is not None else None
    return link, sid, player, can_control


def state_body(link, sid, player, viewers, can_control):
    """The session state the page renders (GET /api/{sid}/state and each
    live update event). `link` is the id the page used (the control or the
    view-only id): every URL in it is built from that id, so a view-only
    page never sees the control id."""
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
            # Built from the id the page used: a view-only page never sees
            # the control id.
            now_playing["video_url"] = f"/media/{link}/{track['id']}"
            # No video stream: the page shows the artwork instead.
            now_playing["audio_only"] = bool(track.get("audio_only"))
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
        "loop": player.loop,
        "volume": volume_for(state.sessions[sid]),
        "autoplay": player.autoplay,
        # Skip votes {count, needed} while any are active, else null.
        "votes": web_status(player, state.sessions[sid]),
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
        "theme": theme_state(link, state.sessions[sid]),
        "lyrics": lyrics.ENABLED,
        # False on a view-only link: the page hides its controls.
        "can_control": can_control,
        # The guild's language (`!language`): the page's default.
        "lang": lang_for(state.sessions[sid]),
    }


async def web_state(request):
    link, sid, player, can_control = _session(request)
    headers = {"Cache-Control": "no-store"}
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    viewers = count_viewer(sid, request.headers.get("X-Viewer"))
    body = state_body(link, sid, player, viewers, can_control)
    return web.json_response(body, headers=headers)


# --- Live updates (Server-Sent Events) ---------------------------------------
#
# GET /api/{sid}/events?viewer=<tab id> streams the same JSON as /state, as
# `data:` events: one on connect, then one after each notify_state(player)
# (coalesced to one per SSE_MIN_INTERVAL), plus one whenever a state diff run
# every SSE_CHECK_SECONDS finds a change nobody notified (crossfade drain,
# viewers that poll). A `ping` event goes out after SSE_HEARTBEAT_SECONDS
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
    link, sid, player, can_control = _session(request)
    headers = {"Cache-Control": "no-store"}
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    if request.app[_STREAMS]["closing"]:
        return _fail(request, 503, "web.shutting_down")
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
        or sum(len(s) for s in state.streams.values()) >= MAX_STREAMS_TOTAL
    ):
        # The page falls back to polling and retries later.
        return _fail(request, 429, "web.too_many_streams")

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            # no-transform: a compressing proxy must not buffer the stream.
            "Cache-Control": "no-cache, no-transform",
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
            # Resolve the link again: a rotated control id stops working, a
            # view-only id follows its session to the new control id.
            now_sid, can_control = resolve_link(link)
            player = session_player(now_sid) if now_sid is not None else None
            if player is None:
                await response.write(_sse({"active": False}))
                break
            if now_sid != sid:
                open_streams.discard(stream)
                if not open_streams and state.streams.get(sid) is open_streams:
                    del state.streams[sid]
                sid = stream.sid = now_sid
                open_streams = state.streams.setdefault(sid, set())
                open_streams.add(stream)
            stream.watch(player)  # !reset may have replaced the player
            last_check = loop.time()
            body = state_body(link, sid, player, count_viewer(sid, None), can_control)
            sig = _signature(body)
            if woken or sig != last_sig:
                await response.write(_sse(body))
                last_sig = sig
                last_sent = last_write = loop.time()
            elif loop.time() - last_write >= SSE_HEARTBEAT_SECONDS:
                # A real event (comments never reach the page's
                # EventSource): the page's liveness check counts on it.
                await response.write(b"event: ping\ndata: {}\n\n")
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


def _find_track(player, track_id):
    """The playing, a queued or a history track with this id, or None."""
    tracks = ([player.now_playing] if player.now_playing else []) + list(player.queue)
    tracks += list(player.history)
    for track in tracks:
        if track.get("id") == track_id:
            return track
    return None


async def web_media(request):
    track_id = request.match_info["track_id"]
    _, _, player, _ = _session(request)
    if player is None:
        raise web.HTTPNotFound()
    track = _find_track(player, track_id)
    if track is not None and os.path.isfile(track["filepath"]):
        # Never stored by a proxy or tunnel in front (private), and
        # revalidated by the browser (no-cache): a clip URL must always
        # serve the file of that track.
        headers = {"Cache-Control": "private, no-cache"}
        return web.FileResponse(track["filepath"], headers=headers)
    raise web.HTTPNotFound()


async def web_lyrics(request):
    _, _, player, _ = _session(request)
    headers = {"Cache-Control": "no-store"}
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    track = _find_track(player, request.match_info["track_id"])
    if track is None:
        return _fail(request, 404, "web.unknown_track")
    result = await lyrics.get(track)
    if result == lyrics.PENDING:
        return web.json_response({"pending": True}, status=202, headers=headers)
    if result == lyrics.UNAVAILABLE:
        # Not "none": the page asks again later instead of caching it.
        return web.json_response(
            {"ok": False, "error": t("web.lyrics_unavailable", request_lang(request))},
            status=503,
            headers=headers,
        )
    if not result:
        return web.json_response(
            {"ok": False, "error": t("web.no_lyrics", request_lang(request))},
            status=404,
            headers=headers,
        )
    return web.json_response(
        {"synced": result["synced"], "plain": result["plain"]}, headers=headers
    )


# (guild id, range) -> (time.monotonic() expiry, summary): bounds the work a
# page (or anyone with the link) can cause.
_stats_cache = {}


async def web_stats(request):
    _, sid, player, _ = _session(request)
    headers = {"Cache-Control": "no-store"}
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    range_name = request.query.get("range", "week")
    if range_name not in stats.RANGES:
        return _fail(request, 400, "web.unknown_period")
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
        return _fail(request, 500, "web.stats_unavailable")
    for stale in [k for k, v in _stats_cache.items() if v[0] <= now]:
        del _stats_cache[stale]
    _stats_cache[key] = (now + STATS_CACHE_SECONDS, result)
    return web.json_response(result, headers=headers)


async def web_playlists(request):
    _, sid, player, _ = _session(request)
    headers = {"Cache-Control": "no-store"}
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    try:
        found = await control_playlist_list(state.sessions[sid])
    except ControlError as e:
        return _control_error(e.status, e.text(request_lang(request)))
    return web.json_response({"playlists": found}, headers=headers)


def _control_error(status, message):
    return web.json_response({"ok": False, "error": message}, status=status)


def request_lang(request):
    """The language of the session's guild (BOT_LANGUAGE for an unknown
    session): human error messages follow it."""
    sid, _ = resolve_link(request.match_info.get("sid"))
    return lang_for(state.sessions.get(sid) if sid is not None else None)


def _fail(request, status, key, **params):
    """An error response with the `key` message in the session's language."""
    return _control_error(status, t(key, request_lang(request), **params))


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
        return None, _fail(request, 400, "web.too_large")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None, _fail(request, 400, "web.bad_request")
    if not isinstance(data, dict):
        return None, _fail(request, 400, "web.bad_request")
    return data, None


# Every action of POST /api/{id}/control. They all change the session for
# everyone, so they all need the control link: a new action is added here
# (and dispatched below) and the permission gate in web_control() covers it
# with no other change. Something a view-only page may do belongs in a GET
# route, never in this list.
CONTROL_ACTIONS = frozenset(
    {
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
        "loop",
        "shuffle",
        "volume",
        "autoplay",
        "save_playlist",
        "load_playlist",
    }
)


async def web_control(request):
    _, sid, player, can_control = _session(request)
    if player is None:
        return _fail(request, 404, "web.unknown_session")
    # The one permission gate of the web API, before the body is even read:
    # a view-only id can run none of CONTROL_ACTIONS (fail closed).
    if not can_control:
        return _fail(request, 403, "web.view_only")
    guild_id = state.sessions[sid]

    data, error = await _read_json(request)
    if error is not None:
        return error
    action = data.get("action")
    if action not in CONTROL_ACTIONS:
        return _fail(request, 400, "web.unknown_action")

    track_id = data.get("id")
    index = data.get("index")
    if action in ("remove", "move", "jump") and not (
        isinstance(track_id, str) and 0 < len(track_id) <= 64
    ):
        return _fail(request, 400, "web.bad_track")
    if action == "move" and (type(index) is not int):
        return _fail(request, 400, "web.bad_position")
    position = data.get("position")
    if action == "seek" and not (
        isinstance(position, (int, float))
        and not isinstance(position, bool)
        and math.isfinite(position)
        and position >= 0
    ):
        return _fail(request, 400, "web.bad_position")
    if action == "add" and not isinstance(data.get("url"), str):
        return _fail(request, 400, "web.missing_link")
    if action in ("crossfade", "autoplay") and not isinstance(
        data.get("enabled"), bool
    ):
        return _fail(request, 400, "web.bad_request")
    name = data.get("name")
    if action in ("save_playlist", "load_playlist") and not (
        isinstance(name, str) and 0 < len(name) <= 200
    ):
        return _fail(request, 400, "web.bad_playlist_name")

    mode = data.get("mode")
    if action == "loop" and mode is not None and mode not in LOOP_MODES:
        return _fail(request, 400, "control.bad_loop")
    if action == "volume" and not valid_volume(data.get("value")):
        return _fail(request, 400, "control.bad_volume")

    touch(player)
    if player.sid != sid:  # Expired just now
        return _fail(request, 404, "web.unknown_session")
    lang = lang_for(guild_id)

    try:
        if action == "next":
            await control_next(player)
            set_action(player, t("action.web_skipped", lang))
        elif action == "previous":
            if await control_previous(player) == "restart":
                set_action(player, t("action.web_restarted", lang))
            else:
                set_action(player, t("action.web_previous", lang))
        elif action == "pause":
            await control_pause(player)
            refresh_panel(player)
        elif action == "resume":
            await control_resume(player)
            refresh_panel(player)
        elif action == "remove":
            track = await control_remove(player, track_id)
            title = short(track["title"])
            set_action(player, t("action.web_removed", lang, title=title))
        elif action == "move":
            await control_move(player, track_id, index)
            set_action(player, t("action.web_reordered", lang))
        elif action == "seek":
            applied = await control_seek(player, position)
            set_action(player, seek_label(player, applied))
        elif action == "crossfade":
            control_crossfade(player, data["enabled"])
        elif action == "loop":
            mode = control_loop(player, mode)
            label = t(f"loop.mode.{mode}", lang)
            set_action(player, t("action.web_loop", lang, mode=label))
        elif action == "shuffle":
            await control_shuffle(player)
            set_action(player, t("action.web_shuffled", lang))
        elif action == "volume":
            _announce_volume(player, guild_id, data["value"])
        elif action == "autoplay":
            control_autoplay(player, data["enabled"])
        elif action == "jump":
            track = await control_jump(player, guild_id, track_id)
            title = short(track["title"])
            set_action(player, t("action.web_jumped", lang, title=title))
        elif action == "save_playlist":
            saved = await control_playlist_save(player, guild_id, name)
            set_action(player, t("action.web_playlist_saved", lang, name=saved["name"]))
        elif action == "load_playlist":
            loaded = await control_playlist_load(player, guild_id, name)
            set_action(
                player, t("action.web_playlist_loading", lang, name=loaded["name"])
            )
            return web.json_response({"ok": True}, status=202)
        elif action == "add":
            await control_add(player, guild_id, data["url"])
            return web.json_response({"ok": True}, status=202)
    except ControlError as e:
        return _control_error(e.status, e.text(lang))
    return web.json_response({"ok": True})


def _announce_volume(player, guild_id, percent):
    """Apply a web volume change. The panel line is the usual coalesced edit
    (one per PANEL_MIN_INTERVAL), so dragging the slider does not spam."""
    control_volume(player, guild_id, percent)  # Raises before the line below
    set_action(player, t("action.web_volume", lang_for(guild_id), volume=percent))


def _theme_auth(request):
    """(player, guild id, None) for a valid edit token on a live session,
    else (None, None, error response)."""
    _, sid, player, can_control = _session(request)
    if player is None:
        return None, None, _fail(request, 404, "web.unknown_session")
    if not can_control:  # The editor link is always a control link
        return None, None, _fail(request, 403, "web.view_only")
    guild_id = state.sessions[sid]
    if not check_edit_token(request.headers.get("X-Edit-Token"), guild_id):
        return (
            None,
            None,
            _fail(request, 403, "web.bad_edit_token"),
        )
    return player, guild_id, None


def _theme_saved(request, player, guild_id):
    touch(player)
    lang = lang_for(guild_id)
    set_action(player, t("action.web_theme", lang))
    theme = theme_state(request.match_info["sid"], guild_id)
    return web.json_response({"ok": True, "theme": theme})


async def web_theme(request):
    player, guild_id, error = _theme_auth(request)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    if data.get("skin") not in THEME_SKINS:
        return _fail(request, 400, "web.unknown_theme")
    if data.get("base") not in THEME_PRESETS:
        return _fail(request, 400, "web.unknown_base")
    if not all(valid_color(data.get(k)) for k in ("bg", "text", "accent")):
        return _fail(request, 400, "web.bad_color")
    name = clean_theme_name(data.get("name", ""))
    if name is None:
        return _fail(request, 400, "web.bad_name", max=THEME_NAME_MAX)
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
        return _fail(request, 500, "web.save_failed")
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
            return _fail(request, 500, "web.save_failed")
        return _theme_saved(request, player, guild_id)
    body = await _read_body(request, MAX_LOGO_BYTES)
    if body is None:
        return _fail(request, 400, "web.logo_too_large")
    ext = sniff_image(body)
    if ext is None:
        return _fail(request, 400, "web.logo_format")
    try:
        set_logo(guild_id, body, ext)
    except OSError as e:
        print(f"Could not save logo: {e}")
        return _fail(request, 500, "web.save_failed")
    return _theme_saved(request, player, guild_id)


async def web_theme_logo_file(request):
    _, sid, player, _ = _session(request)
    if player is None:
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
    app.router.add_get("/healthz", web_healthz)
    app.router.add_get("/metrics", web_metrics)
    app.router.add_get("/api/{sid}/state", web_state)
    app.router.add_get("/api/{sid}/events", web_events)
    app.router.add_post("/api/{sid}/control", _notifies(web_control))
    app.router.add_get("/api/{sid}/stats", web_stats)
    app.router.add_get("/api/{sid}/playlists", web_playlists)
    app.router.add_get("/api/{sid}/lyrics/{track_id}", web_lyrics)
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
