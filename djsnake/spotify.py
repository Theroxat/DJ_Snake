"""Spotify links: URL parsing, reading the track list and finding each track
on YouTube. Nothing is ever downloaded or played from Spotify.

The track list comes from the public embed page (no configuration, capped at
100 tracks by Spotify), or from the Web API when SPOTIFY_CLIENT_ID and
SPOTIFY_CLIENT_SECRET are set. Everything blocking here runs in an executor.
"""

import base64
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse

from . import media
from .config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_MAX_BYTES,
    SPOTIFY_TIMEOUT,
    YOUTUBE_SEARCH_RESULTS,
)

KINDS = ("playlist", "album", "track")
SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com"}
SHORT_LINK_HOSTS = {"spotify.link", "spotify.app.link"}
_ID_RE = re.compile(r"^[0-9A-Za-z]{22}$")
# /intl-fr/playlist/ID, /embed/album/ID, /track/ID/ (any of these prefixes)
_PATH_RE = re.compile(
    r"^(?:/intl-[a-z]{2}(?:-[a-z]{2})?)?(?:/embed)?/(playlist|album|track)/([^/]+)/?$",
    re.IGNORECASE,
)
_URI_RE = re.compile(r"^spotify:(playlist|album|track):([0-9A-Za-z]{22})$")
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
API_BASE = "https://api.spotify.com/v1/"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(Exception):
    """`code` is a ControlError code (see controls.py), which holds the
    message."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# --- URL parsing ---------------------------------------------------------------


def check_url(url):
    """Parse a link the user gave.

    Returns (kind, id) for a Spotify playlist, album or track, None if it is
    not a Spotify link at all, and raises SpotifyError("bad_url") for a
    Spotify link that cannot be used (or "spotify_short_link" for spotify.link,
    which would mean following a redirect to an arbitrary host). Only the id
    is kept: requests are always rebuilt from it, never from the user's URL.
    """
    if not isinstance(url, str) or len(url) > 2048:
        return None
    url = url.strip()
    if url.lower().startswith("spotify:"):
        match = _URI_RE.match(url)
        if not match:
            raise SpotifyError("bad_url", "Spotify URI")
        return match.group(1), match.group(2)
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port  # Raises on a malformed port
    except ValueError:
        return None
    if host in SHORT_LINK_HOSTS:
        raise SpotifyError("spotify_short_link")
    if host not in SPOTIFY_HOSTS:
        return None
    if parsed.scheme not in ("http", "https") or parsed.username or port:
        raise SpotifyError("bad_url", "Spotify URL")
    match = _PATH_RE.match(parsed.path)
    if not match or not _ID_RE.match(match.group(2)):
        raise SpotifyError("bad_url", "Spotify URL")
    return match.group(1).lower(), match.group(2)


def canonical_url(kind, spotify_id):
    return f"https://open.spotify.com/{kind}/{spotify_id}"


def embed_url(kind, spotify_id):
    return f"https://open.spotify.com/embed/{kind}/{spotify_id}"


def _check_ids(kind, spotify_id):
    if kind not in KINDS or not _ID_RE.match(spotify_id or ""):
        raise ValueError("Invalid Spotify kind or id")


# --- HTTP ----------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: every request goes to a fixed Spotify host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _read_capped(response):
    """Body of a response, read in chunks under one overall deadline: the
    socket timeout alone lets a server trickle bytes forever. Stops one byte
    past the size cap, which the caller then refuses."""
    deadline = time.monotonic() + SPOTIFY_TIMEOUT
    chunks, size = [], 0
    while size <= SPOTIFY_MAX_BYTES:
        if time.monotonic() > deadline:
            raise TimeoutError("response too slow")
        chunk = response.read(min(65536, SPOTIFY_MAX_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _http(url, headers=None, data=None):
    """(status, body bytes). Raises SpotifyError("spotify_unreadable") on a
    network failure or an oversized response."""
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept-Language": "en",
            **(headers or {}),
        },
    )
    try:
        with _opener.open(request, timeout=SPOTIFY_TIMEOUT) as response:
            status, body = response.status, _read_capped(response)
    except urllib.error.HTTPError as e:
        status, body = e.code, b""
    except Exception as e:
        raise SpotifyError("spotify_unreadable", f"network: {e}") from e
    if len(body) > SPOTIFY_MAX_BYTES:
        raise SpotifyError("spotify_unreadable", "response too large")
    return status, body


# --- Embed page (no configuration) --------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
# Artists in a track list's subtitle are joined with a comma and a no-break
# space, which keeps names such as "Tyler, The Creator" whole.
_ARTIST_SPLIT_RE = re.compile(", ")


def make_track(title, artists, duration_ms, source=None):
    """The shape every link provider hands to the YouTube matcher:
    {title, artists, duration (s)}, plus `source` for non-Spotify providers."""
    title = (title or "").strip()
    if not title:
        return None
    artists = [a.strip() for a in artists if a and a.strip()]
    try:
        duration = float(duration_ms) / 1000 if duration_ms else None
    except (TypeError, ValueError):
        duration = None
    track = {"title": title, "artists": artists, "duration": duration}
    if source:
        track["source"] = source
    return track


_track = make_track


def parse_embed_html(html, kind):
    """Track list from an embed page: [{title, artists, duration (s)}].

    Raises SpotifyError: "spotify_not_found" for a missing or private item,
    "spotify_unreadable" when the page no longer has the expected shape.
    """
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        raise SpotifyError("spotify_unreadable", "no __NEXT_DATA__")
    try:
        props = json.loads(match.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError) as e:
        raise SpotifyError("spotify_unreadable", f"bad JSON: {e}") from e
    if not isinstance(props, dict):
        raise SpotifyError("spotify_unreadable", "pageProps")
    if props.get("status") == 404:
        raise SpotifyError("spotify_not_found")
    try:
        entity = props["state"]["data"]["entity"]
    except (KeyError, TypeError) as e:
        raise SpotifyError("spotify_unreadable", "no entity") from e
    if not isinstance(entity, dict) or entity.get("type") != kind:
        raise SpotifyError("spotify_unreadable", "unexpected entity")

    tracks = []
    if kind == "track":
        artists = [a.get("name", "") for a in entity.get("artists") or []]
        track = _track(
            entity.get("title") or entity.get("name"), artists, entity.get("duration")
        )
        if track:
            tracks.append(track)
        return tracks

    track_list = entity.get("trackList")
    if not isinstance(track_list, list):
        raise SpotifyError("spotify_unreadable", "no trackList")
    for item in track_list:
        if not isinstance(item, dict) or item.get("entityType", "track") != "track":
            continue  # Podcast episodes and the like
        artists = _ARTIST_SPLIT_RE.split(item.get("subtitle") or "")
        track = _track(item.get("title"), artists, item.get("duration"))
        if track:
            tracks.append(track)
    return tracks


def fetch_embed_tracks(kind, spotify_id):
    _check_ids(kind, spotify_id)
    status, body = _http(embed_url(kind, spotify_id))
    if status == 404:
        raise SpotifyError("spotify_not_found")
    if status != 200:
        raise SpotifyError("spotify_unreadable", f"HTTP {status}")
    return parse_embed_html(body.decode("utf-8", "replace"), kind)


# --- Web API (optional, client credentials) ------------------------------------

_token_lock = threading.Lock()
_token = {"value": None, "expires": 0.0}


def api_configured():
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)


def _api_token():
    with _token_lock:
        if _token["value"] and time.monotonic() < _token["expires"]:
            return _token["value"]
        basic = base64.b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
        ).decode()
        status, body = _http(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=b"grant_type=client_credentials",
        )
        if status != 200:
            raise SpotifyError("spotify_unreadable", f"token HTTP {status}")
        try:
            data = json.loads(body)
            _token["value"] = data["access_token"]
            _token["expires"] = time.monotonic() + int(data["expires_in"]) - 60
        except (ValueError, KeyError, TypeError) as e:
            raise SpotifyError("spotify_unreadable", "token response") from e
        return _token["value"]


def _api_get(url):
    # Pagination links come from the API itself; still, only ever send the
    # token to the API host.
    if not url.startswith(API_BASE):
        raise SpotifyError("spotify_unreadable", "unexpected API URL")
    status, body = _http(url, headers={"Authorization": f"Bearer {_api_token()}"})
    if status in (400, 404):
        raise SpotifyError("spotify_not_found")
    if status != 200:
        raise SpotifyError("spotify_unreadable", f"API HTTP {status}")
    try:
        return json.loads(body)
    except ValueError as e:
        raise SpotifyError("spotify_unreadable", "API JSON") from e


def _api_track(item):
    if not isinstance(item, dict) or item.get("type", "track") != "track":
        return None
    artists = [a.get("name", "") for a in item.get("artists") or []]
    return _track(item.get("name"), artists, item.get("duration_ms"))


def fetch_api_tracks(kind, spotify_id, limit):
    _check_ids(kind, spotify_id)
    if kind == "track":
        track = _api_track(_api_get(f"{API_BASE}tracks/{spotify_id}"))
        return [track] if track else []
    if kind == "album":
        url = f"{API_BASE}albums/{spotify_id}/tracks?" + urlencode({"limit": 50})
    else:
        url = f"{API_BASE}playlists/{spotify_id}/tracks?" + urlencode(
            {
                "limit": 100,
                "additional_types": "track",
                "fields": "next,items(track(type,name,duration_ms,artists(name)))",
            }
        )
    tracks = []
    while url and len(tracks) < limit:
        page = _api_get(url)
        for item in page.get("items") or []:
            if kind == "playlist":
                item = (item or {}).get("track")
            track = _api_track(item)
            if track:
                tracks.append(track)
        url = page.get("next")
    return tracks[:limit]


def get_tracks(kind, spotify_id, limit):
    """Up to `limit` tracks of a Spotify playlist, album or track (blocking).

    Uses the Web API when configured and falls back to the embed page if it
    fails (Spotify hides some playlists from new API apps). Raises
    SpotifyError, including "spotify_empty" when nothing usable is listed.
    """
    tracks = None
    if api_configured():
        try:
            tracks = fetch_api_tracks(kind, spotify_id, limit)
        except SpotifyError as e:
            print(f"Spotify Web API failed ({e}), trying the embed page")
    if tracks is None:
        tracks = fetch_embed_tracks(kind, spotify_id)
    tracks = tracks[:limit]
    if not tracks:
        raise SpotifyError("spotify_empty")
    return tracks


# --- YouTube matching ----------------------------------------------------------

# Words that mark another version of a song. A candidate containing one is
# penalised unless the Spotify title has it too.
VERSION_PENALTIES = {
    "live": 40,
    "cover": 50,
    "remix": 40,
    "karaoke": 60,
    "instrumental": 50,
    "nightcore": 60,
    "sped up": 50,
    "slowed": 50,
    "reverb": 30,
    "8d": 50,
    "acoustic": 30,
    "bass boosted": 50,
    "reaction": 60,
    "tutorial": 60,
    "lesson": 60,
    "piano": 30,
    "extended": 25,
    "mashup": 50,
    "lyrics": 15,
    "lyric": 15,
    "letra": 15,
    "paroles": 15,
}
# Words that say nothing about which song it is.
_NOISE = {
    "official",
    "video",
    "music",
    "audio",
    "hd",
    "hq",
    "4k",
    "mv",
    "clip",
    "officiel",
    "visualizer",
    "version",
    "remastered",
    "remaster",
    "feat",
    "ft",
    "featuring",
    "with",
    "the",
    "a",
    "and",
}
_CHANNEL_WORDS = {"official", "music", "vevo", "tv", "channel"}
_OFFICIAL_VIDEO_RE = re.compile(
    r"\b(official (music )?video|clip officiel|video officielle|official mv)\b"
)
_OFFICIAL_AUDIO_RE = re.compile(r"\b(official (audio|visualizer|lyric video))\b")
MIN_SCORE = 40


def _normalize(text):
    """Lowercase ASCII words: accents stripped, '&' read as 'and'."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _tokens(text):
    return set(_normalize(text).split()) - _NOISE


def base_title(title):
    """The song title without "(feat. X)" or " - Remastered 2011" parts."""
    title = re.sub(r"\s*[\(\[](feat|ft|with)\.?\s[^\)\]]*[\)\]]", "", title, flags=re.I)
    return re.split(r"\s+-\s+", title, maxsplit=1)[0].strip() or title


def search_query(track):
    artist = track["artists"][0] if track["artists"] else ""
    title = base_title(track["title"])
    return f"{artist} - {title}" if artist else title


def _has_phrase(normalized, phrase):
    return re.search(rf"\b{re.escape(phrase)}\b", normalized) is not None


def score_candidate(track, candidate, rank=0):
    """Score a YouTube search result for a Spotify track; None rejects it.

    Signals: duration closeness (strongest), title and artist words, official
    uploads (an official music video first, since the clip page shows the
    video), and penalties for other versions (live, cover, remix...).
    """
    if candidate.get("live_status") in ("is_live", "is_upcoming"):
        return None
    title = candidate.get("title") or ""
    channel = candidate.get("channel") or ""
    cand_text = _normalize(title)
    channel_text = _normalize(channel)
    both_text = f"{cand_text} {channel_text}"
    channel_tokens = _tokens(channel) - _CHANNEL_WORDS
    artists = track["artists"]

    # Title words: most of the song's own words must be in the video title.
    song_tokens = _tokens(base_title(track["title"])) or _tokens(track["title"])
    if song_tokens:
        overlap = len(song_tokens & set(cand_text.split())) / len(song_tokens)
    else:
        overlap = 0.0
    if overlap < 0.5:
        return None
    score = 30 * overlap

    # Artist: in the title or the channel name.
    artist_hits = 0
    for artist in track["artists"]:
        artist_tokens = _tokens(artist)
        if artist_tokens and artist_tokens <= set(both_text.split()):
            artist_hits += 1
    score += 20 if artist_hits else -15

    official_video = bool(_OFFICIAL_VIDEO_RE.search(cand_text))
    if official_video:
        score += 25
    elif _OFFICIAL_AUDIO_RE.search(cand_text):
        score += 10
    if channel.endswith(" - Topic"):
        score += 10
    elif "vevo" in channel_text.split() or channel_text.endswith("vevo"):
        score += 15
    elif channel_tokens and any(channel_tokens == _tokens(a) for a in artists):
        score += 15  # The artist's own channel ("Queen Official" counts)

    spotify_text = _normalize(track["title"])
    for phrase, penalty in VERSION_PENALTIES.items():
        if _has_phrase(cand_text, phrase) and not _has_phrase(spotify_text, phrase):
            score -= penalty

    expected = track.get("duration")
    duration = candidate.get("duration")
    if expected and duration:
        if duration > 2 * expected or duration < expected / 2:
            return None
        diff = abs(duration - expected)
        if official_video and expected < duration <= expected + 90:
            # Music videos often add an intro or outro (SponsorBlock's
            # music_offtopic cut usually removes it): count it as close.
            diff = min(diff, 8)
        if diff <= 3:
            score += 40
        elif diff <= 8:
            score += 30
        elif diff <= 20:
            score += 15
        elif diff > 45:
            score -= 20 + min(30, (diff - 45) / 3)
    elif expected:
        score -= 10  # Unknown duration: cannot confirm it is the same take

    score += max(0, 5 - rank)  # YouTube's own ranking breaks ties
    return score


def best_match(track, candidates):
    """The best scoring candidate above MIN_SCORE, or None."""
    best, best_score = None, None
    for rank, candidate in enumerate(candidates):
        score = score_candidate(track, candidate, rank)
        if score is not None and score >= MIN_SCORE:
            if best_score is None or score > best_score:
                best, best_score = candidate, score
    return best


def find_youtube_match(track):
    """Canonical YouTube URL of the best match for a Spotify track, or None.
    Blocking: run it in an executor."""
    query = search_query(track)
    try:
        candidates = media.search_youtube(query, YOUTUBE_SEARCH_RESULTS)
    except Exception as e:
        print(f"YouTube search failed for {query!r}: {e}")
        return None
    match = best_match(track, candidates)
    if match is None:
        print(f"No YouTube match for {query!r}")
        return None
    print(f"Spotify {query!r} -> {match['title']!r} ({match.get('duration')}s)")
    return f"https://www.youtube.com/watch?v={match['id']}"


def describe(track):
    """'Title — Artist' for messages."""
    if track.get("artists"):
        return f"{track['title']} — {', '.join(track['artists'])}"
    return track["title"]
