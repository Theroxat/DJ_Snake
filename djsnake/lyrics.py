"""Synced lyrics from LRCLIB (https://lrclib.net: free, no key).

Lyrics are fetched in the background when a track is queued (`prefetch()`),
never on the playback path, and cached per track: in memory, and on disk
under `{DATA_DIR}/lyrics/` by YouTube video id. The web route then reads the
cache (`get()`), waiting a few seconds at most for a fetch still running.

Timestamps are song time. The bot plays and reports `elapsed` in file time:
SponsorBlock's `music_offtopic` cut already removed talking intros from the
file, and the automix leading-silence trim only moves the seek point, not the
file's clock, so a line at 12.3 s in the LRC is sung at 12.3 s of file time.
"""

import asyncio
import collections
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from . import state
from .config import (
    LYRICS_DIR,
    LYRICS_ENABLED,
    LYRICS_MAX_BYTES,
    LYRICS_TIMEOUT,
    LYRICS_WAIT_SECONDS,
)
from .spotify import _opener, _read_capped
from .stats import clean_artist

API_BASE = "https://lrclib.net/api"
USER_AGENT = "DJ Snake Discord bot (https://github.com/Theroxat/DJ_Snake)"
CACHE_SIZE = 256  # Tracks kept in memory
# A search result whose duration is further than this from the track's is
# another version (live, extended, radio edit...).
MAX_DURATION_GAP = 30.0
MIN_LINES = 3  # Fewer lines of text is a stub, not lyrics

ENABLED = LYRICS_ENABLED

# Cache key (video id or track id) -> {"synced": [[ms, line]...] | None,
# "plain": str | None}, or None when LRCLIB has nothing for the track.
_cache = collections.OrderedDict()
# Cache key -> asyncio.Task of a fetch in progress.
_tasks = {}

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


# --- LRC ----------------------------------------------------------------------

_TIME_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]$", re.I)


def parse_lrc(text):
    """[[ms, line], ...] sorted by time from LRC text, or None if it has no
    timed line. A line may carry several time tags; `[offset:±ms]` (positive
    means earlier) is applied; other tags (`[ar:...]`) are skipped."""
    if not text or not isinstance(text, str):
        return None
    offset = 0
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        match = _OFFSET_RE.match(raw)
        if match:
            offset = int(match.group(1))
            continue
        stamps = []
        pos = 0
        while True:
            tag = _TIME_TAG_RE.match(raw, pos)
            if not tag:
                break
            minutes, seconds, frac = tag.groups()
            ms = (int(minutes) * 60 + int(seconds)) * 1000
            if frac:
                ms += int(frac.ljust(3, "0")[:3])
            stamps.append(ms)
            pos = tag.end()
        if not stamps:
            continue
        line = raw[pos:].strip()
        lines.extend([ms, line] for ms in stamps)
    if not lines:
        return None
    for entry in lines:
        entry[0] = max(0, entry[0] - offset)
    lines.sort(key=lambda entry: entry[0])
    return lines


# --- What to search for -------------------------------------------------------

# Bracketed noise in YouTube titles: "(Official Video)", "[4K]", "(Lyrics)".
_NOISE_WORDS = (
    r"official|officiel(?:le)?|video|audio|lyrics?|lyric video|visuali[sz]er|music video|clip"
    r"|hd|hq|4k|remaster(?:ed)?(?: \d{4})?|explicit|clean|mv|m/v|live session"
)
_BRACKET_RE = re.compile(r"\s*[\(\[【]([^\)\]】]*)[\)\]】]")
_NOISE_RE = re.compile(rf"\b({_NOISE_WORDS})\b", re.I)
_FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.I)
_DASH_RE = re.compile(r"\s+[-–—]\s+")
_QUOTES = "\"'“”‘’«»"


def _strip_noise(text):
    def drop(match):
        inside = match.group(1)
        if _NOISE_RE.search(inside) or re.match(r"^\s*(feat|ft)\b", inside, re.I):
            return ""
        return match.group(0)

    text = _BRACKET_RE.sub(drop, text)
    text = text.split(" | ")[0].split(" // ")[0]
    return text.strip()


def parse_title(title, channel=None):
    """(artist, track title) guessed from a YouTube video title such as
    "Daft Punk - Get Lucky (Official Video) ft. Pharrell". Without an
    "Artist - Title" split the artist is the cleaned channel name."""
    title = _strip_noise(str(title or ""))
    parts = _DASH_RE.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        artist, name = parts
    else:
        artist, name = clean_artist(channel), title
    name = _FEAT_RE.sub("", _strip_noise(name)).strip().strip(_QUOTES).strip()
    artist = _FEAT_RE.sub("", (artist or "").strip()).strip() or None
    if artist:
        # "A, B & C" -> "A": LRCLIB lists the main artist first.
        artist = re.split(r"\s*(?:,|&| x | and | vs\.? )\s*", artist)[0].strip()
    return artist or None, name or None


def lookup_fields(track):
    """(artist, title, duration in seconds or None) to search for. A Spotify
    track keeps Spotify's artist, title and duration (`lyrics_title`,
    `lyrics_duration`); a YouTube one parses its title."""
    duration = track.get("lyrics_duration") or track.get("duration")
    if track.get("lyrics_title"):
        artist = track.get("artist")
        title = track["lyrics_title"]
        if not artist:
            artist, _ = parse_title(track.get("title"), track.get("channel"))
    else:
        artist, title = parse_title(track.get("title"), track.get("channel"))
        if track.get("artist"):
            artist = track["artist"]
    try:
        duration = float(duration) if duration else None
    except (TypeError, ValueError):
        duration = None
    return artist, title, duration


# --- LRCLIB -------------------------------------------------------------------


class LyricsUnavailable(Exception):
    """LRCLIB could not be asked (network, server error): try again later."""


def _http(url):
    """(status, body bytes). Raises LyricsUnavailable on a network failure."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        # No redirect is followed (LRCLIB answers directly), and the body is
        # read under one deadline and a size cap, as for Spotify.
        with _opener.open(request, timeout=LYRICS_TIMEOUT) as response:
            body = _read_capped(response, LYRICS_MAX_BYTES, LYRICS_TIMEOUT)
            return response.status, body
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        raise LyricsUnavailable(str(e)) from e


def _get_json(path, params):
    status, body = _http(f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}")
    if status == 404:
        return None
    if status != 200 or len(body) > LYRICS_MAX_BYTES:
        raise LyricsUnavailable(f"HTTP {status}")
    try:
        return json.loads(body)
    except ValueError as e:
        raise LyricsUnavailable("bad JSON") from e


def _result(record):
    """{synced, plain} from an LRCLIB record, or None if it has no lyrics."""
    if not isinstance(record, dict) or record.get("instrumental"):
        return None
    synced = parse_lrc(record.get("syncedLyrics"))
    if synced is not None and sum(1 for _, line in synced if line) < MIN_LINES:
        synced = None  # A stub ("probe", one line): not worth showing
    plain = record.get("plainLyrics")
    plain = plain.strip() if isinstance(plain, str) else ""
    if sum(1 for line in plain.splitlines() if line.strip()) < MIN_LINES:
        plain = None
    if synced is None and plain is None:
        return None
    return {"synced": synced, "plain": plain}


def _best(records, duration):
    """The search result closest in duration, synced lyrics first."""
    best = None
    best_rank = None
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict) or _result(record) is None:
            continue
        gap = 0.0
        if duration and record.get("duration"):
            try:
                gap = abs(float(record["duration"]) - duration)
            except (TypeError, ValueError):
                gap = 0.0
            if gap > MAX_DURATION_GAP:
                continue
        rank = (_result(record)["synced"] is None, gap)
        if best_rank is None or rank < best_rank:
            best, best_rank = record, rank
    return best


def fetch(artist, title, duration=None):
    """{synced, plain} or None (nothing found). Blocking: run it in an
    executor. Raises LyricsUnavailable when LRCLIB cannot be reached."""
    if not title:
        return None
    if artist:
        params = {"track_name": title, "artist_name": artist}
        if duration:
            params["duration"] = round(duration)
            # Exact match (LRCLIB allows about 2 s of difference).
            found = _result(_get_json("get", params))
            if found is not None:
                return found
            del params["duration"]
        record = _best(_get_json("search", params), duration)
        if record is not None:
            return _result(record)
    record = _best(
        _get_json("search", {"q": f"{artist or ''} {title}".strip()}), duration
    )
    return _result(record) if record is not None else None


# --- Cache and background fetch ----------------------------------------------


def cache_key(track):
    video_id = track.get("video_id")
    if isinstance(video_id, str) and _VIDEO_ID_RE.match(video_id):
        return video_id
    return f"track:{track.get('id')}"


def _disk_path(key):
    if key.startswith("track:"):
        return None
    return os.path.join(LYRICS_DIR, f"{key}.json")


def _read_disk(key):
    path = _disk_path(key)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    synced = data.get("synced")
    plain = data.get("plain")
    if synced is not None and not (
        isinstance(synced, list)
        and all(
            isinstance(e, list)
            and len(e) == 2
            and isinstance(e[0], int)
            and isinstance(e[1], str)
            for e in synced
        )
    ):
        return None
    if plain is not None and not isinstance(plain, str):
        return None
    if synced is None and plain is None:
        return None
    return {"synced": synced, "plain": plain}


def _write_disk(key, result):
    path = _disk_path(key)
    if path is None or result is None:
        return
    try:
        os.makedirs(LYRICS_DIR, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        print(f"Could not cache lyrics: {e}")


def _remember(key, result):
    _cache[key] = result
    _cache.move_to_end(key)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


def _load(key, artist, title, duration):
    """Blocking: the disk cache, else LRCLIB. Raises LyricsUnavailable."""
    cached = _read_disk(key)
    if cached is not None:
        return cached
    result = fetch(artist, title, duration)
    _write_disk(key, result)
    return result


# At most this many lookups run at once (a long playlist prefetches every
# track; the default executor also runs the downloads).
MAX_CONCURRENT = 2
_limits = {}  # event loop -> its semaphore


def _limit():
    loop = asyncio.get_running_loop()
    sem = _limits.get(loop)
    if sem is None:
        _limits.clear()  # Only one loop runs (tests make a new one each)
        sem = _limits[loop] = asyncio.Semaphore(MAX_CONCURRENT)
    return sem


async def _run(key, track):
    artist, title, duration = lookup_fields(track)
    loop = asyncio.get_running_loop()
    try:
        async with _limit():
            result = await loop.run_in_executor(
                None, _load, key, artist, title, duration
            )
    except LyricsUnavailable as e:
        print(f"Lyrics unavailable for {title!r}: {e}")
        return UNAVAILABLE  # Not cached: a later request tries again
    except Exception as e:
        print(f"Lyrics lookup failed for {title!r}: {e}")
        return UNAVAILABLE
    else:
        _remember(key, result)
        return result
    finally:
        _tasks.pop(key, None)


def prefetch(track):
    """Start fetching a track's lyrics in the background (no-op when lyrics
    are off, cached or already being fetched). Never raises. A live stream
    has no song to look up: nothing is fetched for it."""
    if not ENABLED or not track or track.get("live"):
        return None
    try:
        key = cache_key(track)
        if key in _cache:
            return None
        task = _tasks.get(key)
        if task is None:
            task = asyncio.get_running_loop().create_task(_run(key, track))
            _tasks[key] = task
            state.background_tasks.add(task)
            task.add_done_callback(state.background_tasks.discard)
        return task
    except Exception as e:
        print(f"Could not start the lyrics lookup: {e}")
        return None


PENDING = "pending"
UNAVAILABLE = "unavailable"  # LRCLIB could not be reached: ask again later


async def get(track, wait=LYRICS_WAIT_SECONDS):
    """A track's lyrics: {synced, plain}, None (none found or lyrics off),
    UNAVAILABLE (LRCLIB could not be reached) or PENDING (still being
    fetched after `wait` seconds).
    Starts a fetch if none ran yet."""
    if not ENABLED or track.get("live"):
        return None
    key = cache_key(track)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    task = prefetch(track)
    if task is None:
        return _cache.get(key)
    try:
        return await asyncio.wait_for(asyncio.shield(task), wait)
    except asyncio.TimeoutError:
        return PENDING
    except Exception:
        return None
