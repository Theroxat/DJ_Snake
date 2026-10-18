"""Web server for the synced clip page (aiohttp, on the bot's loop)."""

import asyncio
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
    MAX_VIEWERS_PER_SESSION,
    PUBLIC_BASE_URL,
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
    many tabs polled within VIEWER_WINDOW_SECONDS. The map keeps poll order
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
    return len(seen)


async def web_state(request):
    sid = request.match_info["sid"]
    headers = {"Cache-Control": "no-store"}
    player = session_player(sid)
    if player is None:
        return web.json_response({"active": False}, status=404, headers=headers)
    viewers = count_viewer(sid, request.headers.get("X-Viewer"))

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

    return web.json_response(
        {
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
        },
        headers=headers,
    )


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
                return web.FileResponse(filepath)
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
        return _control_error(400, "Période inconnue.")
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
        return _control_error(500, "Statistiques indisponibles pour le moment.")
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
        return None, _control_error(400, "Requête trop volumineuse.")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None, _control_error(400, "Requête invalide.")
    if not isinstance(data, dict):
        return None, _control_error(400, "Requête invalide.")
    return data, None


async def web_control(request):
    sid = request.match_info["sid"]
    player = session_player(sid)
    if player is None:
        return _control_error(404, "Session inconnue ou expirée.")
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
        return _control_error(400, "Action inconnue.")

    track_id = data.get("id")
    index = data.get("index")
    if action in ("remove", "move", "jump") and not (
        isinstance(track_id, str) and 0 < len(track_id) <= 64
    ):
        return _control_error(400, "Morceau invalide.")
    if action == "move" and (type(index) is not int):
        return _control_error(400, "Position invalide.")
    position = data.get("position")
    if action == "seek" and not (
        isinstance(position, (int, float))
        and not isinstance(position, bool)
        and math.isfinite(position)
        and position >= 0
    ):
        return _control_error(400, "Position invalide.")
    if action == "add" and not isinstance(data.get("url"), str):
        return _control_error(400, "Lien manquant.")
    if action == "crossfade" and not isinstance(data.get("enabled"), bool):
        return _control_error(400, "Requête invalide.")

    touch(player)
    if player.sid != sid:  # Expired just now
        return _control_error(404, "Session inconnue ou expirée.")

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
        return _control_error(e.status, e.french)
    return web.json_response({"ok": True})


def _theme_auth(request):
    """(player, guild id, None) for a valid edit token on a live session,
    else (None, None, error response)."""
    sid = request.match_info["sid"]
    player = session_player(sid)
    if player is None:
        return None, None, _control_error(404, "Session inconnue ou expirée.")
    guild_id = state.sessions[sid]
    if not check_edit_token(request.headers.get("X-Edit-Token"), guild_id):
        return (
            None,
            None,
            _control_error(
                403,
                "Lien d'édition invalide ou expiré. "
                "Relance !theme edit dans Discord.",
            ),
        )
    return player, guild_id, None


def _theme_saved(request, player, guild_id):
    touch(player)
    set_action(player, "🎨 Clip page theme updated via the web page")
    theme = theme_state(request.match_info["sid"], guild_id)
    return web.json_response({"ok": True, "theme": theme})


_SAVE_FAILED = "Enregistrement impossible sur le serveur."


async def web_theme(request):
    player, guild_id, error = _theme_auth(request)
    if error is not None:
        return error
    data, error = await _read_json(request)
    if error is not None:
        return error
    if data.get("skin") not in THEME_SKINS:
        return _control_error(400, "Thème inconnu.")
    if data.get("base") not in THEME_PRESETS:
        return _control_error(400, "Style de base inconnu.")
    if not all(valid_color(data.get(k)) for k in ("bg", "text", "accent")):
        return _control_error(400, "Couleur invalide (format #rrggbb).")
    name = clean_theme_name(data.get("name", ""))
    if name is None:
        return _control_error(
            400, f"Nom invalide ({THEME_NAME_MAX} caractères maximum)."
        )
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
        return _control_error(400, "Image trop lourde (512 Ko maximum).")
    ext = sniff_image(body)
    if ext is None:
        return _control_error(
            400, "Format non pris en charge (PNG, JPEG, WebP ou GIF)."
        )
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


def build_web_app():
    app = web.Application()
    app.router.add_get("/", web_landing)
    app.router.add_get("/api/{sid}/state", web_state)
    app.router.add_post("/api/{sid}/control", web_control)
    app.router.add_get("/api/{sid}/stats", web_stats)
    app.router.add_post("/api/{sid}/theme", web_theme)
    app.router.add_post("/api/{sid}/theme/logo", web_theme_logo)
    app.router.add_delete("/api/{sid}/theme/logo", web_theme_logo)
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
    runner = web.AppRunner(build_web_app(), access_log=None)
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
