"""Deezer and Apple Music links: URL parsing and track lists, read without any
credentials. Like Spotify, nothing is played from these services: each track
is matched on YouTube by spotify.find_youtube_match(), which works on any
`{title, artists, duration}` dict.

Deezer has a public JSON API (api.deezer.com). Apple Music songs and albums
come from the public iTunes Lookup API; playlists are not in it, so they are
read from the metadata embedded in the public playlist page. Everything
blocking here runs in an executor.
"""

import json
import re
from urllib.parse import parse_qs, urlparse

from . import spotify
from .spotify import SpotifyError

PROVIDERS = ("spotify", "deezer", "apple")
LABELS = {"spotify": "Spotify", "deezer": "Deezer", "apple": "Apple Music"}

DEEZER_HOSTS = {"deezer.com", "www.deezer.com"}
DEEZER_SHORT_HOSTS = {"link.deezer.com", "deezer.page.link", "dzr.page.link"}
_DEEZER_PATH_RE = re.compile(
    r"^(?:/[a-z]{2}(?:-[a-z]{2})?)?/(track|album|playlist)/(\d{1,20})/?$", re.I
)
DEEZER_API = "https://api.deezer.com/"

APPLE_HOSTS = {"music.apple.com", "embed.music.apple.com"}
_APPLE_PATH_RE = re.compile(
    r"^/([a-z]{2})/(album|playlist|song)/(?:([^/]{1,120})/)?"
    r"(\d{1,20}|pl\.[A-Za-z0-9-]{3,64})/?$",
    re.I,
)
_SLUG_RE = re.compile(r"^[A-Za-z0-9%._~-]{1,120}$")
ITUNES_LOOKUP = "https://itunes.apple.com/lookup?"


# --- URL parsing ---------------------------------------------------------------


def _parse(url):
    """(host, parsed) of an http(s) URL without credentials or a port, or
    None for anything else."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port  # Raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.username or port:
        return None
    return host, parsed


def check_deezer(url):
    """(kind, id) of a Deezer track, album or playlist link; None when it is
    not a Deezer link; SpotifyError for a Deezer link that cannot be used."""
    parts = _parse(url)
    if parts is None:
        return None
    host, parsed = parts
    if host in DEEZER_SHORT_HOSTS:
        raise SpotifyError("short_link")
    if host not in DEEZER_HOSTS:
        return None
    match = _DEEZER_PATH_RE.match(parsed.path)
    if not match:
        raise SpotifyError("bad_url", "Deezer URL")
    return match.group(1).lower(), match.group(2)


def check_apple(url):
    """(kind, ref) of an Apple Music song, album or playlist link, where ref
    is (storefront, slug, id, song_id); None when it is not an Apple Music
    link. An album link with ?i=<song id> is that song."""
    parts = _parse(url)
    if parts is None:
        return None
    host, parsed = parts
    if host not in APPLE_HOSTS:
        return None
    match = _APPLE_PATH_RE.match(parsed.path)
    if not match:
        raise SpotifyError("bad_url", "Apple Music URL")
    storefront, kind, slug, ref = match.groups()
    kind = kind.lower()
    if kind in ("album", "song") and not ref.isdigit():
        raise SpotifyError("bad_url", "Apple Music URL")
    if kind == "playlist" and ref.isdigit():
        raise SpotifyError("bad_url", "Apple Music URL")
    if slug is not None and not _SLUG_RE.match(slug):
        raise SpotifyError("bad_url", "Apple Music URL")
    song_id = None
    if kind == "album":
        song = parse_qs(parsed.query).get("i", [""])[0]
        if song:
            if not song.isdigit() or len(song) > 20:
                raise SpotifyError("bad_url", "Apple Music URL")
            kind, song_id = "song", song
    if kind == "song":
        ref = song_id or ref
    return kind, (storefront.lower(), slug or "-", ref, song_id)


def check_link(url):
    """Any metadata link `!play` understands: (kind, id, provider), or None.

    `id` is a string for Spotify and Deezer and a tuple for Apple Music; it
    is only meant to be handed back to get_tracks(). Raises SpotifyError for
    a link of those services that cannot be used.
    """
    ref = spotify.check_url(url)
    if ref is not None:
        return ref[0], ref[1], "spotify"
    ref = check_deezer(url)
    if ref is not None:
        return ref[0], ref[1], "deezer"
    ref = check_apple(url)
    if ref is not None:
        return ("track" if ref[0] == "song" else ref[0]), ref[1], "apple"
    return None


# --- Deezer --------------------------------------------------------------------


def _get_json(url):
    status, body = spotify._http(url)
    if status == 404:
        raise SpotifyError("link_not_found")
    if status != 200:
        raise SpotifyError("link_unreadable", f"HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as e:
        raise SpotifyError("link_unreadable", "JSON") from e
    return data


def _deezer_get(path):
    data = _get_json(DEEZER_API + path)
    error = data.get("error") if isinstance(data, dict) else None
    if error:
        # 800 "no data": missing or private. Anything else: the API changed.
        code = error.get("code") if isinstance(error, dict) else None
        raise SpotifyError(
            "link_not_found" if code in (800, 404) else "link_unreadable"
        )
    return data


def _deezer_track(item):
    if not isinstance(item, dict) or item.get("readable") is False:
        return None
    artist = (item.get("artist") or {}).get("name")
    duration = item.get("duration")
    return spotify.make_track(
        item.get("title"),
        [artist] if artist else [],
        duration * 1000 if isinstance(duration, (int, float)) else None,
        "deezer",
    )


def parse_deezer(kind, data):
    """Tracks of a Deezer API answer: a track object, or a {"data": [...]}
    list for an album or playlist."""
    if kind == "track":
        track = _deezer_track(data)
        return [track] if track else []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SpotifyError("link_unreadable", "no data list")
    return [t for t in map(_deezer_track, items) if t]


def fetch_deezer(kind, deezer_id, limit):
    if kind not in ("track", "album", "playlist") or not str(deezer_id).isdigit():
        raise ValueError("Invalid Deezer kind or id")
    if kind == "track":
        return parse_deezer(kind, _deezer_get(f"track/{deezer_id}"))
    return parse_deezer(
        kind, _deezer_get(f"{kind}/{deezer_id}/tracks?limit={int(limit)}")
    )


# --- Apple Music ---------------------------------------------------------------

_APPLE_DATA_RE = re.compile(
    r'<script[^>]*\bid="?serialized-server-data"?[^>]*>(.*?)</script>', re.S
)
_APPLE_LD_RE = re.compile(
    r'<script[^>]*\bid="?schema:music-playlist"?[^>]*>(.*?)</script>', re.S
)
_ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")


def _apple_track(title, artist, duration_ms):
    return spotify.make_track(title, [artist] if artist else [], duration_ms, "apple")


def parse_itunes_lookup(data, kind):
    """Songs of an iTunes Lookup answer (`entity=song` for an album)."""
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise SpotifyError("link_unreadable", "no results")
    tracks = []
    for item in results:
        if not isinstance(item, dict) or item.get("wrapperType") != "track":
            continue
        if item.get("kind", "song") != "song":
            continue  # Music videos and the like
        track = _apple_track(
            item.get("trackName"), item.get("artistName"), item.get("trackTimeMillis")
        )
        if track:
            tracks.append(track)
    return tracks


def _walk_tracks(node, out):
    if isinstance(node, dict):
        if "artistName" in node and "title" in node and "duration" in node:
            out.append(node)
        else:
            for value in node.values():
                _walk_tracks(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_tracks(value, out)


def _iso_ms(text):
    match = _ISO_DURATION_RE.match(text or "")
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(g or 0) for g in match.groups())
    return (hours * 3600 + minutes * 60 + seconds) * 1000


def parse_apple_playlist_html(html):
    """Tracks of an Apple Music playlist page. The page's server data has the
    artist and duration of every track; the schema.org block (title and
    duration only) is the fallback if that shape changes."""
    html = html or ""
    match = _APPLE_DATA_RE.search(html)
    if match:
        try:
            nodes = []
            _walk_tracks(json.loads(match.group(1)), nodes)
        except (ValueError, TypeError, RecursionError):
            nodes = []
        tracks = []
        for node in nodes:
            duration = node.get("duration")
            track = _apple_track(
                node.get("title"),
                node.get("artistName"),
                duration if isinstance(duration, (int, float)) else None,
            )
            if track:
                tracks.append(track)
        if tracks:
            return tracks
    match = _APPLE_LD_RE.search(html)
    if not match:
        raise SpotifyError("link_unreadable", "no playlist data")
    try:
        items = json.loads(match.group(1)).get("track") or []
    except (ValueError, AttributeError) as e:
        raise SpotifyError("link_unreadable", "bad JSON") from e
    tracks = []
    for item in items:
        if isinstance(item, dict):
            track = _apple_track(item.get("name"), None, _iso_ms(item.get("duration")))
            if track:
                tracks.append(track)
    return tracks


def fetch_apple(kind, ref, limit):
    storefront, slug, apple_id, _ = ref
    if kind == "playlist":
        # Rebuilt from validated parts; redirects are never followed, so the
        # slug must be the page's own.
        status, body = spotify._http(
            f"https://music.apple.com/{storefront}/playlist/{slug}/{apple_id}"
        )
        if status == 404:
            raise SpotifyError("link_not_found")
        if status != 200:
            raise SpotifyError("link_unreadable", f"HTTP {status}")
        return parse_apple_playlist_html(body.decode("utf-8", "replace"))
    entity = "&entity=song" if kind == "album" else ""
    data = _get_json(f"{ITUNES_LOOKUP}id={apple_id}&country={storefront}{entity}")
    return parse_itunes_lookup(data, kind)


# --- Entry point ---------------------------------------------------------------


def get_tracks(provider, kind, ref, limit):
    """Up to `limit` tracks `{title, artists, duration, source}` of a link
    (blocking). Raises SpotifyError, including "link_empty"."""
    if provider == "spotify":
        return spotify.get_tracks(kind, ref, limit)
    try:
        if provider == "deezer":
            tracks = fetch_deezer(kind, ref, limit)
        elif provider == "apple":
            tracks = fetch_apple(kind, ref, limit)
        else:
            raise ValueError(f"Unknown provider {provider!r}")
    except SpotifyError as e:
        if e.code != "spotify_unreadable":
            raise
        # spotify._http's network error: not a Spotify link, say so plainly.
        raise SpotifyError("link_unreadable", str(e)) from e
    tracks = tracks[:limit]
    if not tracks:
        raise SpotifyError("link_empty")
    return tracks
