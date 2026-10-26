"""Optional Last.fm scrobbling.

Off unless LASTFM_API_KEY and LASTFM_API_SECRET are set. Each Discord user
links their own Last.fm account (`/lastfm link`); the session keys are stored
in DATA_DIR/lastfm.json (mode 0600) and never logged. For every linked user
in the voice channel while a track plays, the bot sends `track.updateNowPlaying`
when it starts and queues a `track.scrobble` when it ends if it was played for
half its length or 4 minutes (and the track lasts 30 s or more). The queue is
sent in batches of 50 by a background task that retries with a backoff, so a
Last.fm outage never touches playback. Imports `config`, `state`, `lyrics`
(for its title parser) and `themes` (atomic writes).
"""

import asyncio
import hashlib
import json
import os
import time

import aiohttp

from . import config, state
from .lyrics import lookup_fields
from .fileio import write_atomic

FILE = config.LASTFM_FILE
RETRY_START = 30.0  # Seconds before the first retry of a failed batch
RETRY_MAX = 600.0

# Last.fm error codes: the session key is dead / the call is temporary.
BAD_SESSION = {9}
TEMPORARY = {8, 11, 16, 29}
# Auth: the token is not authorised yet / is invalid or expired.
NOT_AUTHORISED = 14
BAD_TOKEN = {4, 15}

_sleep = asyncio.sleep
_flush_task = None


class LastfmError(Exception):
    """Last.fm answered with an error code."""

    def __init__(self, code, message=""):
        super().__init__(f"Last.fm error {code}: {message}")
        self.code = code


class LastfmUnavailable(Exception):
    """Last.fm could not be reached (network, timeout, server error)."""


def enabled():
    return bool(config.LASTFM_API_KEY and config.LASTFM_API_SECRET)


# --- API ----------------------------------------------------------------------


def sign(params, secret=None):
    """Last.fm API signature: md5 of the parameters sorted by name, each as
    name then value, followed by the shared secret. `format` and `callback`
    are never signed."""
    secret = config.LASTFM_API_SECRET if secret is None else secret
    text = "".join(
        f"{key}{params[key]}"
        for key in sorted(params)
        if key not in ("format", "callback")
    )
    return hashlib.md5((text + secret).encode("utf-8")).hexdigest()


async def _request(params):
    """POST the form to Last.fm: (HTTP status, decoded JSON or None). Raises
    LastfmUnavailable on a network failure. Tests replace this function."""
    timeout = aiohttp.ClientTimeout(total=config.LASTFM_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(config.LASTFM_API_URL, data=params) as response:
                try:
                    data = await response.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    data = None
                return response.status, data
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise LastfmUnavailable(type(e).__name__) from e


async def call(method, params=None):
    """Signed call to a Last.fm method; returns the decoded JSON. Raises
    LastfmError (the API refused) or LastfmUnavailable (try again later)."""
    body = {**(params or {}), "method": method, "api_key": config.LASTFM_API_KEY}
    body["api_sig"] = sign(body)
    body["format"] = "json"
    status, data = await _request(body)
    if isinstance(data, dict) and "error" in data:
        raise LastfmError(data["error"], data.get("message", ""))
    if status >= 500 or not isinstance(data, dict):
        raise LastfmUnavailable(f"HTTP {status}")
    return data


# --- Linked accounts ----------------------------------------------------------


def load_links():
    """(Re)load FILE into state.lastfm_links, dropping bad entries."""
    state.lastfm_links.clear()
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        print(f"⚠️  Could not read {FILE}")
        return
    if not isinstance(data, dict):
        return
    for key, record in data.items():
        if (
            key.isdigit()
            and isinstance(record, dict)
            and isinstance(record.get("key"), str)
            and record["key"]
            and isinstance(record.get("name"), str)
        ):
            state.lastfm_links[int(key)] = {
                "name": record["name"],
                "key": record["key"],
            }


def save_links():
    """Persist the links with owner-only permissions. Raises OSError."""
    data = {str(uid): record for uid, record in state.lastfm_links.items()}
    write_atomic(FILE, json.dumps(data, indent=2).encode("utf-8"))
    os.chmod(FILE, 0o600)


def link_of(user_id):
    return state.lastfm_links.get(user_id)


def unlink(user_id):
    """Forget the user's account and queued scrobbles. True if one was linked.
    Raises OSError when the file cannot be written (the link is still gone
    from memory)."""
    state.lastfm_pending_auth.pop(user_id, None)
    state.lastfm_queue.pop(user_id, None)
    if state.lastfm_links.pop(user_id, None) is None:
        return False
    save_links()
    return True


def auth_url(token):
    return f"{config.LASTFM_AUTH_URL}?api_key={config.LASTFM_API_KEY}&token={token}"


async def start_link(user_id):
    """Begin (or resume) linking. Returns ("url", url) to send the user to
    Last.fm, or ("linked", name) when a pending authorisation just completed."""
    pending = state.lastfm_pending_auth.get(user_id)
    if pending and time.time() - pending[1] > config.LASTFM_TOKEN_MINUTES * 60:
        pending = None
    if pending:
        try:
            return "linked", await complete_link(user_id)
        except LastfmError as e:
            if e.code == NOT_AUTHORISED:
                return "url", auth_url(pending[0])
            if e.code not in BAD_TOKEN:
                raise
    data = await call("auth.getToken")
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise LastfmUnavailable("no token")
    state.lastfm_pending_auth[user_id] = (token, time.time())
    return "url", auth_url(token)


async def complete_link(user_id):
    """Exchange the user's authorised token for a session key and store it.
    Returns the Last.fm user name. Raises LastfmError (code 14: the user has
    not authorised yet), LookupError (no link in progress) or OSError."""
    pending = state.lastfm_pending_auth.get(user_id)
    if not pending:
        raise LookupError("no link in progress")
    data = await call("auth.getSession", {"token": pending[0]})
    session = data.get("session") or {}
    key, name = session.get("key"), session.get("name")
    if not isinstance(key, str) or not key or not isinstance(name, str):
        raise LastfmUnavailable("no session")
    state.lastfm_pending_auth.pop(user_id, None)
    state.lastfm_links[user_id] = {"name": name, "key": key}
    save_links()
    return name


# --- What to scrobble ---------------------------------------------------------


def track_info(track):
    """{artist, track, album?, duration?} of a queued track, or None when no
    artist or title can be told. Spotify, Deezer and Apple Music tracks keep
    their own metadata; a YouTube one is parsed ("Artist - Title")."""
    if not track:
        return None
    artist, title, duration = lookup_fields(track)
    if not artist or not title:
        return None
    info = {"artist": artist, "track": title}
    if track.get("album"):
        info["album"] = str(track["album"])
    if duration and duration > 0:
        info["duration"] = int(duration)
    return info


def should_scrobble(duration, played):
    """Last.fm's rule: the track lasts 30 s or more and was played for half
    its length or 4 minutes. Without a known duration, only the 4 minutes."""
    if played is None:
        return False
    if not duration:
        return played >= config.LASTFM_MAX_THRESHOLD
    if duration < config.LASTFM_MIN_TRACK_SECONDS:
        return False
    return played >= min(duration / 2, config.LASTFM_MAX_THRESHOLD)


def _hears(member):
    """A human who is not deafened (by themselves or the server): a deafened
    member does not hear the track, so it is not scrobbled for them."""
    if getattr(member, "bot", False):
        return False
    voice = getattr(member, "voice", None)
    return not (getattr(voice, "self_deaf", False) or getattr(voice, "deaf", False))


def listeners(player):
    """Ids of the linked users in the bot's voice channel who hear it."""
    channel = getattr(player.voice_client, "channel", None)
    members = getattr(channel, "members", None) or []
    return {m.id for m in members if _hears(m) and m.id in state.lastfm_links}


def track_started(player):
    """A track begins (call with player.now_playing set): remember who
    listens and send "now playing" for each of them. Never raises."""
    try:
        if not enabled():
            return
        player.lastfm_listeners = listeners(player)
        info = track_info(player.now_playing)
        if not info or not player.lastfm_listeners:
            return
        _spawn(_now_playing(sorted(player.lastfm_listeners), info))
    except Exception as e:
        print(f"Last.fm start failed: {type(e).__name__}")


def track_ended(player, track, played, started_wall):
    """A track is retired after `played` seconds, started at `started_wall`
    (unix time). Queues a scrobble for every linked user who was in the
    channel at the start and still is. Never raises."""
    try:
        if not enabled():
            return
        info = track_info(track)
        if not info or not should_scrobble(info.get("duration"), played):
            return
        present = set(player.lastfm_listeners)
        if player.in_voice():
            present &= listeners(player)
        if not present:
            return
        entry = {**info, "timestamp": int(started_wall or time.time() - played)}
        for user_id in sorted(present):
            queue = state.lastfm_queue.setdefault(user_id, [])
            queue.append(dict(entry))
            del queue[: -config.LASTFM_MAX_PENDING]
        player.lastfm_listeners = set()
        _spawn_flush()
    except Exception as e:
        print(f"Last.fm scrobble failed: {type(e).__name__}")


def _spawn(coro):
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()  # No event loop: nothing to do in the background
        return None
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)
    return task


async def _now_playing(user_ids, info):
    for user_id in user_ids:
        link = link_of(user_id)
        if not link:
            continue
        params = {"artist": info["artist"], "track": info["track"], "sk": link["key"]}
        if "album" in info:
            params["album"] = info["album"]
        if "duration" in info:
            params["duration"] = info["duration"]
        try:
            await call("track.updateNowPlaying", params)
        except LastfmError as e:
            _on_error(user_id, e)
        except Exception:
            pass  # Best effort: a missed "now playing" is not retried


# --- Sending the queue --------------------------------------------------------


def _on_error(user_id, error):
    """A refused call: a dead session key unlinks the user."""
    if error.code in BAD_SESSION:
        try:
            unlink(user_id)
        except OSError:
            pass


async def _send_batch(user_id, batch):
    link = link_of(user_id)
    params = {"sk": link["key"]}
    for i, item in enumerate(batch):
        params[f"artist[{i}]"] = item["artist"]
        params[f"track[{i}]"] = item["track"]
        params[f"timestamp[{i}]"] = item["timestamp"]
        if "album" in item:
            params[f"album[{i}]"] = item["album"]
        if "duration" in item:
            params[f"duration[{i}]"] = item["duration"]
    await call("track.scrobble", params)


async def flush():
    """Send the queued scrobbles, 50 per call. A temporary failure keeps a
    user's batch for later. Returns True when nothing is left to retry."""
    clear = True
    for user_id in list(state.lastfm_queue):
        queue = state.lastfm_queue.get(user_id)
        while queue and link_of(user_id):
            batch = queue[: config.LASTFM_BATCH]
            try:
                await _send_batch(user_id, batch)
            except LastfmError as e:
                if e.code in TEMPORARY:
                    clear = False
                    break
                _on_error(user_id, e)
                # Any other refusal would fail again: drop the batch.
            except Exception:
                clear = False  # Network or server trouble: retry later
                break
            del queue[: len(batch)]
        if not link_of(user_id):
            state.lastfm_queue.pop(user_id, None)
        elif not queue:
            state.lastfm_queue.pop(user_id, None)
    return clear


def _spawn_flush():
    global _flush_task
    if _flush_task is not None and not _flush_task.done():
        return
    _flush_task = _spawn(_flush_loop())


async def _flush_loop():
    delay = RETRY_START
    while not await flush():
        await _sleep(delay)
        delay = min(delay * 2, RETRY_MAX)


def pending_count(user_id):
    return len(state.lastfm_queue.get(user_id, ()))


load_links()
