"""YouTube media: URL parsing, yt-dlp search and download, automix analysis
and FFmpeg playback options. Everything blocking here runs in an executor."""

import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import socket
import subprocess
import time
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit

import yt_dlp
from yt_dlp.postprocessor import ModifyChaptersPP, SponsorBlockPP

from . import cookies
from .config import (
    ANALYSIS_TIMEOUT,
    AUTOPLAY_CANDIDATES,
    AUTOPLAY_MAX_SECONDS,
    CROSSFADE_MIN_TRACK,
    DOWNLOAD_ATTEMPTS,
    EXTRACTOR_ARGS,
    FADE_IN_SECONDS,
    FADE_OUT_SECONDS,
    LIVE_ICY_DEADLINE,
    LIVE_ICY_MAX_METAINT,
    LIVE_PLAYLIST_MAX_BYTES,
    LIVE_PROBE_DEADLINE,
    LIVE_PROBE_TIMEOUT,
    LIVE_PROTOCOLS,
    LIVE_RECONNECT,
    LIVE_STREAM_RECONNECTS,
    LIVE_STREAM_TIMEOUT,
    LOUDNORM_KEYS,
    LOUDNORM_TARGET,
    MAX_LEADING_TRIM,
    MAX_PLAYLIST_FULL,
    MEDIA_FORMAT,
    MIN_PLAYED_SECONDS,
    PERMANENT_ERROR_MARKERS,
    REMOTE_COMPONENTS,
    RETRY_BACKOFF_SECONDS,
    SEEK_FADE_IN_SECONDS,
    SILENCE_FILTER,
    SPONSORBLOCK_REMOVE,
    TEMP_DIR,
)


def get_video_id_from_url(url):
    """Return the specific video id (the `v=` param) if the URL points to one,
    else None. A URL like watch?v=XXX&list=YYY targets a specific song *inside*
    a playlist/radio, which is the ambiguous case we want to ask the user about."""
    try:
        query = parse_qs(urlparse(url).query)
        video_id = query.get("v", [None])[0]
        return video_id or None
    except Exception:
        return None


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")


_URL_LIKE_RE = re.compile(r"^([a-z][a-z0-9+.-]*:|www\.|[\w-]+(\.[\w-]+)+(/|$))", re.I)


def looks_like_url(text):
    """True when `text` is meant as a link (scheme, "www." or a domain), so
    anything else is searched on YouTube as plain text."""
    return bool(_URL_LIKE_RE.match((text or "").strip().split(" ")[0]))


def parse_youtube_url(url):
    """Validate a URL from the web page and rebuild it canonically.

    Returns ("single", url) or ("playlist", url), or None for anything that is
    not a YouTube video or playlist link. Only the rebuilt URL reaches yt-dlp,
    so the request cannot point it at another host.
    """
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        parsed.port  # Raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.port:
        return None
    query = parse_qs(parsed.query)
    if host == "youtu.be":
        video_id = parsed.path.strip("/")
    elif host in YOUTUBE_HOSTS:
        video_id = query.get("v", [""])[0]
        for prefix in ("/shorts/", "/live/"):  # /live/<id>: a live stream link
            if not video_id and parsed.path.startswith(prefix):
                video_id = parsed.path[len(prefix) :].strip("/")
        if not video_id:
            list_id = query.get("list", [""])[0]
            if _LIST_ID_RE.match(list_id):
                return ("playlist", f"https://www.youtube.com/playlist?list={list_id}")
            return None
    else:
        return None
    if _VIDEO_ID_RE.match(video_id):
        return ("single", f"https://www.youtube.com/watch?v={video_id}")
    return None


SOUNDCLOUD_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
_SC_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.~%-]{1,200}$")
# First path parts that are SoundCloud pages, not a user; second parts that are
# a user's tabs, not a track.
_SC_RESERVED_USERS = {
    "discover",
    "search",
    "you",
    "stream",
    "upload",
    "charts",
    "pages",
    "feed",
    "settings",
    "notifications",
    "messages",
    "jobs",
    "terms-of-use",
}
_SC_USER_TABS = {
    "sets",
    "tracks",
    "albums",
    "likes",
    "reposts",
    "popular-tracks",
    "following",
    "followers",
    "comments",
    "spotlight",
}
_BANDCAMP_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.bandcamp\.com$")
_BANDCAMP_PATH_RE = re.compile(r"^/(track|album)/([A-Za-z0-9_%.~-]{1,200})/?$")


def _parse_soundcloud(parsed):
    segments = [seg for seg in parsed.path.split("/") if seg]
    if not 2 <= len(segments) <= 4 or not all(map(_SC_SEGMENT_RE.match, segments)):
        return None
    if segments[0].lower() in _SC_RESERVED_USERS:
        return None
    if segments[1] == "sets" and len(segments) >= 3:
        kind = "playlist"  # /user/sets/name[/secret-token]
    elif segments[1] not in _SC_USER_TABS and len(segments) <= 3:
        kind = "single"  # /user/track[/secret-token]
    else:
        return None
    return kind, "https://soundcloud.com/" + "/".join(segments)


def _parse_bandcamp(host, parsed):
    if not _BANDCAMP_HOST_RE.match(host):
        return None
    match = _BANDCAMP_PATH_RE.match(parsed.path)
    if not match:
        return None
    kind = "single" if match.group(1) == "track" else "playlist"
    return kind, f"https://{host}/{match.group(1)}/{match.group(2)}"


TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
_TWITCH_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")
# Twitch pages that are not a channel.
_TWITCH_RESERVED = {
    "directory",
    "videos",
    "settings",
    "search",
    "downloads",
    "jobs",
    "store",
    "turbo",
    "subscriptions",
    "inventory",
    "wallet",
    "drops",
    "friends",
    "messages",
    "login",
    "signup",
    "logout",
    "prime",
    "bits",
    "broadcast",
    "moderator",
    "popout",
    "team",
    "products",
    "privacy",
}


def _parse_twitch(parsed):
    """A Twitch channel (`twitch.tv/<channel>`): its live stream. VODs and
    clips are not accepted."""
    segments = [seg for seg in parsed.path.split("/") if seg]
    if len(segments) != 1 or not _TWITCH_CHANNEL_RE.match(segments[0]):
        return None
    channel = segments[0].lower()
    if channel in _TWITCH_RESERVED:
        return None
    return "single", f"https://www.twitch.tv/{channel}"


def parse_media_url(url):
    """Validate a URL from the web page and rebuild it canonically: a YouTube
    link (see parse_youtube_url), a SoundCloud or Bandcamp track, set or
    album, or a Twitch channel. Returns ("single" | "playlist", url) or None.
    Short links (on.soundcloud.com) and custom Bandcamp domains are refused:
    the host has to be known before yt-dlp is given the URL. Internet radio
    links are checked apart, see parse_radio_url()."""
    parsed_youtube = parse_youtube_url(url)
    if parsed_youtube is not None:
        return parsed_youtube
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        parsed.port  # Raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.port:
        return None
    if host in SOUNDCLOUD_HOSTS:
        return _parse_soundcloud(parsed)
    if host in TWITCH_HOSTS:
        return _parse_twitch(parsed)
    return _parse_bandcamp(host, parsed)


def parse_play_url(url):
    """parse_media_url() for `!play`: the same allowlist and canonical URL,
    except that a YouTube video link inside a playlist or mix (`v=` and
    `list=`) keeps both ids, so `!play` can ask which one to play. Returns
    the rebuilt URL or None."""
    parsed = parse_media_url(url)
    if parsed is None:
        return None
    kind, clean = parsed
    if kind == "single" and clean.startswith("https://www.youtube.com/watch?v="):
        try:
            list_id = parse_qs(urlparse(url.strip()).query).get("list", [""])[0]
        except ValueError:
            list_id = ""
        if _LIST_ID_RE.match(list_id):
            clean = f"{clean}&list={list_id}"
    return clean


def source_of(url):
    """Platform of a track URL: youtube, soundcloud, bandcamp or twitch (the
    page labels the download with it)."""
    host = (urlparse(url or "").hostname or "").lower()
    if host in SOUNDCLOUD_HOSTS or host.endswith(".soundcloud.com"):
        return "soundcloud"
    if host == "bandcamp.com" or host.endswith(".bandcamp.com"):
        return "bandcamp"
    if host in TWITCH_HOSTS:
        return "twitch"
    return "youtube"


def entry_url(entry):
    """URL of a flat playlist entry. Another platform's entries carry their
    own page URL (their ids are not YouTube ids); YouTube ones are rebuilt
    from the id. Returns None when there is nothing to download."""
    for key in ("url", "webpage_url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith("http"):
            if source_of(value) != "youtube":
                return value
    video_id = entry.get("id") or entry.get("url") or entry.get("webpage_url")
    if not video_id:
        return None
    if isinstance(video_id, str) and video_id.startswith("http"):
        return video_id
    return f"https://www.youtube.com/watch?v={video_id}"


def get_playlist_info(url, limit=None):
    """Extract playlist information without downloading (at most `limit`
    entries, default MAX_PLAYLIST_FULL + 1: one more than can ever be loaded,
    so a caller can tell a list of exactly MAX_PLAYLIST_FULL from a longer one)."""
    try:
        print(f"Extracting playlist from: {url}")

        # Configuration to extract playlist
        ydl_opts = {
            "extract_flat": "in_playlist",  # Important: use 'in_playlist' for playlists
            "quiet": True,  # Reduce log spam
            "no_warnings": True,
            "ignoreerrors": True,  # Continue even if some videos have errors
            "playlistend": limit or MAX_PLAYLIST_FULL + 1,
            "remote_components": REMOTE_COMPONENTS,
            "extractor_args": EXTRACTOR_ARGS,
            **cookies.options(),
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            if info is None:
                print("No information extracted")
                return []

            # Debug: display the type of info received
            info_type = info.get("_type", "video")
            print(f"Info type: {info_type}")
            print(f"Title: {info.get('title', 'Not found')}")

            # Handle different types of results
            if info_type == "playlist" or "entries" in info:
                entries = info.get("entries", [])

                if entries:
                    print(f"Number of entries found: {len(entries)}")

                    # Filter None entries and extract necessary info
                    valid_entries = []
                    for entry in entries:
                        if entry is not None:
                            processed_entry = {
                                "id": entry.get("id"),
                                "title": entry.get("title", "Unknown title"),
                                "url": entry.get("url")
                                or entry.get("webpage_url")
                                or f"https://www.youtube.com/watch?v={entry.get('id')}",
                                "webpage_url": entry.get("webpage_url")
                                or entry.get("url"),
                                "duration": entry.get("duration"),
                            }
                            if processed_entry["id"]:
                                valid_entries.append(processed_entry)

                    print(f"Number of valid entries: {len(valid_entries)}")
                    return valid_entries
                else:
                    print("Empty playlist or not found")
                    return []
            else:
                # It's a single video, not a playlist
                print("URL detected as single video, not a playlist")
                return []

    except Exception as e:
        print(f"Error extracting playlist: {e}")
        return []


def search_youtube(query, count):
    """Flat YouTube search (no download): up to `count` videos as
    [{id, title, duration, channel, live_status}]. Blocking, raises on failure.
    The query is only ever used after the "ytsearchN:" prefix."""
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "remote_components": REMOTE_COMPONENTS,
        "extractor_args": EXTRACTOR_ARGS,
        **cookies.options(),
    }
    query = " ".join(str(query).split())[:200]
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{int(count)}:{query}", download=False)
    results = []
    for entry in (info or {}).get("entries") or []:
        if not entry or not _VIDEO_ID_RE.match(entry.get("id") or ""):
            continue
        results.append(
            {
                "id": entry["id"],
                "title": entry.get("title") or "",
                "duration": entry.get("duration"),
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "live_status": entry.get("live_status"),
            }
        )
    return results


_NON_MUSIC_RE = re.compile(
    r"\b(podcast|interview|reaction|react|tutorial|trailer|news|full album|"
    r"full episode|livestream|live stream|compilation|hours?|asmr|gameplay|"
    r"walkthrough|review|documentary)\b",
    re.IGNORECASE,
)


def related_tracks(video_id):
    """Videos YouTube relates to `video_id`: its "RD<id>" mix, read flat (no
    download), as [{id, title, duration, channel, live_status}]. Blocking,
    raises on failure."""
    if not _VIDEO_ID_RE.match(video_id or ""):
        raise ValueError("invalid video id")
    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "playlistend": AUTOPLAY_CANDIDATES,
        "remote_components": REMOTE_COMPONENTS,
        "extractor_args": EXTRACTOR_ARGS,
        **cookies.options(),
    }
    url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    results = []
    for entry in (info or {}).get("entries") or []:
        if not entry or not _VIDEO_ID_RE.match(entry.get("id") or ""):
            continue
        results.append(
            {
                "id": entry["id"],
                "title": entry.get("title") or "",
                "duration": entry.get("duration"),
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "live_status": entry.get("live_status"),
            }
        )
    return results


def choose_related(candidates, exclude):
    """Candidates autoplay may play, in mix order: not in `exclude` (video ids
    played recently), no live streams, nothing over AUTOPLAY_MAX_SECONDS and
    no title that looks like something other than a song."""
    picks = []
    for entry in candidates:
        if entry["id"] in exclude or entry["id"] in (p["id"] for p in picks):
            continue
        if entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        duration = entry.get("duration")
        if duration is not None and duration > AUTOPLAY_MAX_SECONDS:
            continue
        if _NON_MUSIC_RE.search(entry.get("title") or ""):
            continue
        picks.append(entry)
    return picks


def _remove_offtopic(ydl, info, filepath):
    """Cut SponsorBlock "music_offtopic" segments out of the file, in place.

    Best effort: no segments, an unreachable API or a failed cut leaves the
    file as downloaded. The cut is a stream copy; yt-dlp's concat output keeps
    audio and video aligned and writes the moov atom first (faststart).
    """
    pp_info = dict(info)
    for download in info.get("requested_downloads") or []:
        if download.get("filepath") == filepath:
            pp_info.update(download)
    pp_info["filepath"] = filepath
    try:
        _, pp_info = SponsorBlockPP(ydl, categories=SPONSORBLOCK_REMOVE).run(pp_info)
        cutter = ModifyChaptersPP(ydl, remove_sponsor_segments=SPONSORBLOCK_REMOVE)
        leftovers, _ = cutter.run(pp_info)
        for leftover in leftovers:
            if os.path.exists(leftover):
                os.remove(leftover)
    except Exception as e:
        print(f"SponsorBlock skipped: {e}")


def _parse_analysis(stderr):
    """Read silencedetect and loudnorm (print_format=json) output.

    Returns {"duration", "start", "end", "loudnorm"}; any part that cannot be
    read gets its neutral default (no trim, no normalisation).
    """
    duration = None
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if match:
        h, m, sec = match.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(sec)

    silences = []  # [start, end or None]
    for kind, value in re.findall(r"silence_(start|end): (-?\d+(?:\.\d+)?)", stderr):
        if kind == "start":
            silences.append([float(value), None])
        elif silences and silences[-1][1] is None:
            silences[-1][1] = float(value)

    start, end = 0.0, duration
    if silences and silences[0][0] <= 0.05 and silences[0][1] is not None:
        start = min(silences[0][1], MAX_LEADING_TRIM)
    if silences and duration:
        tail_start, tail_end = silences[-1]
        if tail_start > 0.05 and (tail_end is None or tail_end >= duration - 0.1):
            end = tail_start
    if end is not None and end - start < MIN_PLAYED_SECONDS:
        start, end = 0.0, duration  # Mostly silence: play it untouched

    loudnorm = None
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if blocks:
        try:
            raw = json.loads(blocks[-1])
            values = {key: float(raw[key]) for key in LOUDNORM_KEYS}
            if all(math.isfinite(v) for v in values.values()):
                loudnorm = values
        except (ValueError, KeyError, TypeError):
            pass

    return {"duration": duration, "start": start, "end": end, "loudnorm": loudnorm}


def analyse_media(filepath):
    """One audio-only ffmpeg pass: loudness (loudnorm first pass) and silence.

    Blocking (a few seconds per track): call it from an executor thread.
    Never raises; a failure yields the defaults.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-vn",
        "-sn",
        "-dn",
        "-i",
        filepath,
        "-af",
        f"{SILENCE_FILTER},loudnorm={LOUDNORM_TARGET}:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=ANALYSIS_TIMEOUT,
        )
        return _parse_analysis(proc.stderr)
    except Exception as e:
        print(f"Audio analysis failed: {e}")
        return _parse_analysis("")


class TrackTooLong(Exception):
    """The track lasts more than the `max_seconds` given to download_media();
    nothing was downloaded. `duration` is in seconds."""

    def __init__(self, duration):
        super().__init__(f"track lasts {duration} s")
        self.duration = duration


def _duration_filter(max_seconds, seen):
    """A yt-dlp match_filter refusing, before any download, a track longer
    than `max_seconds`; the refused duration goes in `seen`."""

    def check(info, incomplete=False):
        duration = info.get("duration")
        if isinstance(duration, (int, float)) and duration > max_seconds:
            seen.append(duration)
            return f"longer than {max_seconds} s"
        return None

    return check


def _download_once(url, on_title=None, max_seconds=None):
    """Single download attempt. Raises on any failure. `on_title` is called
    (from this thread) with the title as soon as yt-dlp knows it. With
    `max_seconds`, a longer track is refused from its metadata before the
    download starts (TrackTooLong)."""
    # Ensure temp directory exists
    os.makedirs(TEMP_DIR, exist_ok=True)

    # Simplified but robust configuration
    ydl_opts = {
        "format": MEDIA_FORMAT,
        "merge_output_format": "mp4",
        # Moov atom first, so browsers can start and seek before the end.
        "postprocessor_args": {"merger": ["-movflags", "+faststart"]},
        "noplaylist": True,
        "outtmpl": os.path.join(TEMP_DIR, "%(id)s.%(ext)s"),
        "quiet": False,
        "remote_components": REMOTE_COMPONENTS,
        "extractor_args": EXTRACTOR_ARGS,
        **cookies.options(),
    }
    if on_title is not None:
        ydl_opts["progress_hooks"] = [
            lambda d: on_title((d.get("info_dict") or {}).get("title"))
        ]
    # A live stream cannot be downloaded: stop before the download starts
    # and play it as a live item instead (see live_from_info).
    ydl_opts["match_filter"] = _refuse_live

    too_long = []
    if max_seconds:
        too_long_check = _duration_filter(max_seconds, too_long)

        def live_then_length(info, *, incomplete=False):
            # A live is refused first (it plays as a live item, no length).
            _refuse_live(info, incomplete=incomplete)
            return too_long_check(info, incomplete=incomplete)

        ydl_opts["match_filter"] = live_then_length

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print(f"Downloading: {url}")
            info = ydl.extract_info(url, download=True)
            if too_long:
                raise TrackTooLong(too_long[0])
            filepath = _find_download(info)
            if source_of(url) == "youtube":  # SponsorBlock only knows YouTube
                _remove_offtopic(ydl, info, filepath)
    except LiveStream as live:
        track = live_from_info(live.info, url)
        if on_title is not None:
            on_title(track["title"])
        return track

    analysis = analyse_media(filepath)
    duration = analysis["duration"] or info.get("duration")
    duration = float(duration) if duration else None
    end = analysis["end"] if analysis["end"] is not None else duration
    print(
        f"Automix: start={analysis['start']:.2f}s end={end}s "
        f"loudnorm={'yes' if analysis['loudnorm'] else 'no'}"
    )
    return {
        "id": secrets.token_hex(4),
        "filepath": filepath,
        "title": info.get("title", "Unknown"),
        "duration": duration,  # Of the file, after the SponsorBlock cut
        "start": analysis["start"],  # File time where playback starts
        "end": end,  # File time where it stops (None: unknown, play it all)
        "loudnorm": analysis["loudnorm"],  # Measured values, or None
        "thumbnail": info.get("thumbnail"),
        # No video stream (SoundCloud, Bandcamp): the page shows the artwork.
        "audio_only": _is_audio_only(info, filepath),
        "url": info.get("webpage_url") or url,
        # For the stats (stats.artist_of cleans the channel name).
        "video_id": info.get("id"),
        "channel": info.get("channel") or info.get("uploader"),
    }


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".wav", ".flac"}


def _is_audio_only(info, filepath):
    for download in info.get("requested_downloads") or [info]:
        if download.get("vcodec") == "none":
            return True
    return os.path.splitext(filepath)[1].lower() in AUDIO_EXTENSIONS


def _find_download(info):
    """Path of the downloaded file. Raises if there is none."""

    def usable(filepath):
        return filepath and os.path.exists(filepath) and os.path.getsize(filepath) > 0

    # yt-dlp reports the final (merged) path
    for download in info.get("requested_downloads") or []:
        if usable(download.get("filepath")):
            return download["filepath"]

    # Fallback: find the downloaded file
    video_id = info.get("id", "")
    temp_files = os.listdir(TEMP_DIR)
    for f in temp_files:
        if video_id in f and usable(os.path.join(TEMP_DIR, f)):
            return os.path.join(TEMP_DIR, f)

    # If not found by ID, take the most recent
    if temp_files:
        latest_file = max(
            temp_files,
            key=lambda f: os.path.getctime(os.path.join(TEMP_DIR, f)),
        )
        if usable(os.path.join(TEMP_DIR, latest_file)):
            return os.path.join(TEMP_DIR, latest_file)

    raise RuntimeError("File not found after download")


def playback_options(
    track, offset, fade_in=None, fade_out=FADE_OUT_SECONDS, extra_filters=""
):
    """FFmpeg options to play `track` from `offset` (file time, seconds).

    Returns (offset, kwargs for FFmpegPCMAudio); the offset is clamped to the
    automix window [start, end - 1]. Input -ss/-t keep FFmpeg in file time
    while the filters see a timeline starting at 0, so the fade-out is placed
    relative to the seek point. `fade_in` (None: automatic) and `fade_out`
    are overridden by the crossfade (both in file seconds). `extra_filters`
    (filters.chain(): the session's audio filter) goes after the fades.
    """
    # The file carries video for the web page; Discord only needs audio.
    end = track.get("end")
    if end is None:  # Not analysed: play the whole file, untouched
        kwargs = {"options": f"-vn -af {extra_filters}" if extra_filters else "-vn"}
        if offset > 0:
            kwargs["before_options"] = f"-ss {offset:.3f}"
        return offset, kwargs

    start = track.get("start") or 0.0
    offset = max(start, min(offset, end - 1.0))
    remaining = end - offset
    filters = []
    measured = track.get("loudnorm")
    if measured:
        filters.append(
            f"loudnorm={LOUDNORM_TARGET}"
            f":measured_I={measured['input_i']}"
            f":measured_TP={measured['input_tp']}"
            f":measured_LRA={measured['input_lra']}"
            f":measured_thresh={measured['input_thresh']}"
            f":offset={measured['target_offset']}"
            ":linear=true"
        )
    if fade_in is None:
        fade_in = FADE_IN_SECONDS if offset <= start + 0.05 else SEEK_FADE_IN_SECONDS
    fade_in = min(fade_in, remaining / 2)
    fade_out = min(fade_out, remaining)
    filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    filters.append(f"afade=t=out:st={remaining - fade_out:.3f}:d={fade_out:.3f}")
    if extra_filters:
        filters.append(extra_filters)

    before = f"-ss {offset:.3f} " if offset > 0 else ""
    return offset, {
        "before_options": f"{before}-t {remaining:.3f}",
        "options": f"-vn -af {','.join(filters)}",
    }


def crossfade_eligible(track):
    """True if `track` is long enough (and analysed) to take part in a
    crossfade."""
    end = track.get("end") if track else None
    if end is None:
        return False
    return end - (track.get("start") or 0.0) >= CROSSFADE_MIN_TRACK


def _is_permanent_failure(message):
    """True when retrying cannot help (deleted, private, blocked...)."""
    lowered = message.lower()
    return any(marker in lowered for marker in PERMANENT_ERROR_MARKERS)


_ERROR_KINDS = (
    (
        "age",
        (
            "confirm your age",
            "age-restricted",
            "age restricted",
            "inappropriate for some users",
        ),
    ),
    ("members", ("members-only", "members only", "join this channel")),
    ("private", ("private video",)),
    ("signin", ("sign in", "log in", "login required")),
)


def classify_error(message):
    """The kind of access error in a yt-dlp message ("age", "members",
    "private" or "signin"), or None. The bot check ("confirm you're not a
    bot") is not one: cookies do not fix it, the PO token provider does."""
    lowered = str(message).lower().replace("’", "'")
    if "not a bot" in lowered:
        return None
    for kind, markers in _ERROR_KINDS:
        if any(marker in lowered for marker in markers):
            return kind
    return None


def download_media(url, on_title=None, max_seconds=None):
    """Download one track (video+audio mp4), retrying transient YouTube refusals.

    Runs in an executor thread (see download_and_queue_song), so the blocking
    sleep between attempts does not stall the bot's event loop. With
    `max_seconds`, a longer track raises TrackTooLong before downloading.
    """
    last_error = None

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return _download_once(url, on_title, max_seconds)
        except TrackTooLong:
            raise
        except Exception as e:
            last_error = str(e)
            print(
                f"Download error (attempt {attempt}/{DOWNLOAD_ATTEMPTS}): "
                f"{last_error}"
            )

            if _is_permanent_failure(last_error):
                print("Permanent failure, not retrying.")
                break
            if attempt == DOWNLOAD_ATTEMPTS:
                break

            delay = RETRY_BACKOFF_SECONDS[attempt - 1]
            print(f"Retrying in {delay}s...")
            time.sleep(delay)

    raise RuntimeError(f"Error during download: {last_error}")


# --- Live streams -------------------------------------------------------------
#
# YouTube lives, Twitch channels and internet radios are never downloaded:
# a live item has no file (`filepath` None) and `live: True`, and FFmpeg reads
# its `stream_url` as it comes (live_options). No automix analysis, no
# SponsorBlock, no seek, no crossfade, no known duration.


class LiveStream(Exception):
    """Raised by the yt-dlp match filter when the link is live right now."""

    def __init__(self, info):
        super().__init__("live stream")
        self.info = info


def is_live_info(info):
    return bool(info.get("is_live")) or info.get("live_status") == "is_live"


def _refuse_live(info, *, incomplete=False):
    """yt-dlp match_filter: called before the format selection (with
    `incomplete` the set of format fields not known yet) and again before
    the download. Whether the video is live is known either way."""
    if is_live_info(info):
        raise LiveStream(info)
    return None


_LIVE_PROTOCOLS = ("http", "https", "m3u8", "m3u8_native")


def pick_live_format(formats):
    """The format FFmpeg should read for a live stream: an audio-only one
    (Twitch's "audio_only") with the best bitrate, else the smallest muxed
    one of 360p or more (YouTube lives only offer muxed HLS; below 360p the
    audio is 48 kb/s). Only plain HTTP(S) or HLS URLs. None if nothing fits."""
    usable = [
        f
        for f in formats or []
        if str(f.get("url") or "").startswith(("http://", "https://"))
        and (f.get("protocol") or "https") in _LIVE_PROTOCOLS
        and f.get("acodec") != "none"
    ]
    audio = [f for f in usable if f.get("vcodec") == "none"]
    if audio:
        return max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0)
    if not usable:
        return None
    by_size = sorted(usable, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    for f in by_size:
        if (f.get("height") or 0) >= 360:
            return f
    return by_size[-1]


def live_track(url, stream_url, title, source, **fields):
    """A live queue item: no file, no duration, never analysed."""
    track = {
        "id": secrets.token_hex(4),
        "filepath": None,
        "live": True,
        "stream_url": stream_url,
        "http_headers": {},
        "title": title or "Live",
        "duration": None,
        "start": 0.0,
        "end": None,
        "loudnorm": None,
        "thumbnail": None,
        # No video file: the page shows the artwork and a LIVE badge.
        "audio_only": True,
        "url": url,
        "video_id": None,
        "channel": None,
        "source": source,
        "live_title": None,  # A radio's current ICY StreamTitle
        # When stream_url was read (YouTube/Twitch URLs expire, see
        # playback.refresh_live_url)
        "resolved_at": time.monotonic(),
    }
    track.update(fields)
    return track


def live_from_info(info, url):
    """The live item for a YouTube live or a Twitch channel, from the info
    yt-dlp extracted (no second request). Raises if no format can be read."""
    fmt = pick_live_format(info.get("formats"))
    if fmt is None and str(info.get("url") or "").startswith(("http://", "https://")):
        fmt = info
    if fmt is None:
        raise RuntimeError("No playable format for this live stream")
    return live_track(
        info.get("webpage_url") or url,
        check_live_url(fmt["url"]),
        info.get("fulltitle") or info.get("title"),
        source_of(url),
        http_headers=dict(fmt.get("http_headers") or info.get("http_headers") or {}),
        thumbnail=info.get("thumbnail"),
        video_id=info.get("id"),
        channel=info.get("channel") or info.get("uploader"),
    )


def live_options(track):
    """FFmpeg options for a live item: audio only, a one-pass loudnorm (no
    measurement is possible) and a short fade-in. No -ss/-t: it plays until
    it ends or is skipped. A radio comes through a pipe (RadioStream) and
    FFmpeg may open nothing but that pipe; a YouTube/Twitch live is read by
    FFmpeg itself, reconnecting on errors, restricted to HTTP(S) and HLS."""
    filters = f"loudnorm={LOUDNORM_TARGET},afade=t=in:st=0:d={FADE_IN_SECONDS:.3f}"
    options = f"-vn -af {filters}"
    if track.get("source") == "radio":
        return {"before_options": "-protocol_whitelist pipe", "options": options}
    before = f"{LIVE_RECONNECT} -protocol_whitelist {LIVE_PROTOCOLS}"
    agent = (track.get("http_headers") or {}).get("User-Agent")
    if agent:
        before += f" -user_agent {shlex.quote(str(agent))}"
    return {"before_options": before, "options": options}


def check_live_url(url):
    """A yt-dlp stream URL FFmpeg may open: http(s), and not an IP literal
    outside the public internet. Raises RuntimeError."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RuntimeError("Unexpected live stream URL")
    try:
        if not ipaddress.ip_address(parsed.hostname.strip("[]")).is_global:
            raise RuntimeError("Live stream URL on a private address")
    except ValueError:
        pass  # A host name (the platform's CDN)
    return url


def resolve_live(url):
    """Extract a YouTube live or Twitch channel again (no download): its
    stream URL expires, so it is read just before it plays. Returns a fresh
    live item; raises when it is not live (any more) or cannot be read.
    Blocking: run it in an executor."""
    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "remote_components": REMOTE_COMPONENTS,
        "extractor_args": EXTRACTOR_ARGS,
        **cookies.options(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or not is_live_info(info):
        raise RuntimeError("Not live any more")
    return live_from_info(info, url)


class RadioStream:
    """File-like reader of an internet radio, handed to FFmpeg's stdin
    (`FFmpegPCMAudio(..., pipe=True)`). It connects on the first read (on
    discord.py's pipe thread, never on the event loop), each connection
    pinned to an address checked by _resolve_public() and each redirect
    checked again, asks no ICY metadata (the audio stays clean), and
    reconnects after an error or a silent LIVE_STREAM_TIMEOUT, giving up
    (end of stream) after LIVE_STREAM_RECONNECTS failures in a row."""

    def __init__(self, url):
        self.name = url  # What logs and tests show
        self._url = url
        self._response = None
        self._closed = False
        self._failures = 0

    def _connect(self):
        _, response = _follow(self._url, icy=False, opener=_stream_open)
        return response

    def read(self, size=-1):
        size = 65536 if size is None or size < 0 else size
        while not self._closed:
            if self._response is None:
                try:
                    self._response = self._connect()
                except Exception as e:
                    if not self._retry(e):
                        return b""
                    continue
            try:
                data = self._response.read(size)
            except Exception as e:
                data, error = b"", e
            else:
                error = None
            if data:
                self._failures = 0
                return data
            response, self._response = self._response, None
            if response is not None:
                response.close()
            if not self._retry(error or "stream ended"):
                return b""
        return b""

    def _retry(self, reason):
        """Count a failure; False once the stream should end."""
        if self._closed:
            return False
        self._failures += 1
        if self._failures > LIVE_STREAM_RECONNECTS:
            print(f"Radio stream gave up after {self._failures - 1} reconnects")
            return False
        print(f"Radio stream: reconnecting ({type(reason).__name__})")
        time.sleep(min(5, self._failures))
        return not self._closed

    def close(self):
        self._closed = True
        response, self._response = self._response, None
        if response is not None:
            response.close()


def _stream_open(url, icy=False, deadline=None):
    return _request(url, icy=icy, deadline=deadline, timeout=LIVE_STREAM_TIMEOUT)


# --- Internet radio links ----------------------------------------------------


class StreamError(Exception):
    """A radio link that cannot be played; `code` is a ControlError code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# Hosts with their own parser (or none): never probed as a radio.
_NOT_RADIO_HOSTS = (
    YOUTUBE_HOSTS
    | SOUNDCLOUD_HOSTS
    | TWITCH_HOSTS
    | {"youtu.be", "open.spotify.com", "play.spotify.com", "spotify.link"}
    | {"www.deezer.com", "deezer.com", "deezer.page.link", "link.deezer.com"}
    | {"music.apple.com", "itunes.apple.com"}
)
_PLAYLIST_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/x-mpegurl",
    "audio/mpegurl",
    "audio/x-scpls",
    "application/pls+xml",
}
_GENERIC_TYPES = {"text/plain", "application/octet-stream", "binary/octet-stream"}
_STREAM_TYPES = {"application/ogg"}
_RADIO_HEADERS = {"User-Agent": "DJSnake/1.0 (radio check)", "Icy-MetaData": "1"}


# The last path segment of a radio link: a stream or playlist file
# ("fip-midfi.mp3", "groovesalad-128-mp3", "live.m3u8", "radio.pls") or a
# usual mount point ("/stream", "/listen", "/live", Shoutcast's "/;").
_RADIO_PATH_RE = re.compile(
    r"([-_.](mp3|aac|aacp|ogg|oga|opus|flac|m3u8?|pls)|^(stream|listen|live|;))$",
    re.IGNORECASE,
)


def parse_radio_url(url, check_path=True):
    """A link that may be an internet radio (Icecast, Shoutcast, .pls or
    .m3u; an HLS .m3u8 is accepted here and refused by probe_stream): http(s), no credentials, not a host with its own parser, not a
    private IP, and a path that looks like a stream (_RADIO_PATH_RE). Returns
    the cleaned URL or None. probe_stream() then checks the content type and
    refuses hosts that resolve to private addresses. Redirects and playlist
    entries skip the path check (`check_path=False`)."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    url = url.strip()
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        parsed.port  # Raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or parsed.username or not host:
        return None
    if host in _NOT_RADIO_HOSTS or host.endswith(".bandcamp.com"):
        return None
    if any(c.isspace() for c in url):
        return None
    try:
        if not ipaddress.ip_address(host.strip("[]")).is_global:
            return None
    except ValueError:
        pass  # A name: checked when it is resolved
    # urlsplit: Shoutcast's "/;" is a path here, not URL parameters.
    segment = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    if check_path and not _RADIO_PATH_RE.search(segment):
        return None
    return url


def _resolve_public(host, port):
    """The address to connect to for `host`: every address it resolves to
    must be public (global), else StreamError("bad_url"): the bot must not be
    used to reach its own network (pot-provider, cloud metadata, the LAN).
    The caller then connects to that very address (no second resolution, so
    a DNS rebinding between the check and the connection cannot happen)."""
    host = (host or "").strip("[]")
    try:
        infos = socket.getaddrinfo(host, port)
    except OSError:
        raise StreamError("stream_unreachable")
    addresses = []
    for info in infos:
        try:
            address = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            raise StreamError("bad_url")
        if not address.is_global:
            raise StreamError("bad_url")
        addresses.append(str(address))
    if not addresses:
        raise StreamError("stream_unreachable")
    return addresses[0]


def _check_public(url):
    """Refuse a URL whose host resolves to a non-public address. Returns the
    checked address."""
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _resolve_public(parsed.hostname, port)


class _Headers:
    """The response headers (lower-case names, the first value wins)."""

    def __init__(self, pairs):
        self._values = {}
        for name, value in pairs:
            self._values.setdefault(name.lower(), value)

    def get(self, name, default=None):
        return self._values.get(name.lower(), default)

    def get_content_type(self):
        value = self.get("content-type") or ""
        return value.split(";")[0].strip().lower() or "application/octet-stream"


_STATUS_RE = re.compile(rb"^(?:HTTP/\d(?:\.\d)?|ICY)\s+(\d{3})")
_MAX_HEADER_BYTES = 32 * 1024


class _Response:
    """A minimal HTTP/1.0 (or Shoutcast "ICY") response read from a socket
    connected to an address checked by _resolve_public(). `deadline` (a
    time.monotonic() value, or None) bounds every read."""

    def __init__(self, sock, status, headers, buffered, deadline=None, timeout=None):
        self._sock, self.status, self.headers = sock, status, headers
        self._buffer, self._deadline = buffered, deadline
        self._timeout = timeout or LIVE_PROBE_TIMEOUT

    def _recv(self, size):
        timeout = self._timeout
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("radio request deadline")
            timeout = min(timeout, left)
        self._sock.settimeout(timeout)
        return self._sock.recv(size)

    def read(self, size=-1):
        """Up to `size` bytes (all of it with -1: only for small bodies)."""
        if size is None or size < 0:
            chunks = [self._buffer]
            self._buffer = b""
            while True:
                chunk = self._recv(65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        if self._buffer:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
            return data
        return self._recv(size)

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)  # Wakes a reader thread
            except OSError:
                pass
            sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _request(url, icy=False, deadline=None, timeout=None):
    """GET `url` with the connection pinned to its checked public address:
    HTTP/1.0 (no chunked body), TLS with SNI and certificate checks for
    https, no redirect followed (a 3xx comes back as is). Returns a
    _Response; raises StreamError or OSError."""
    import ssl

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    https = parsed.scheme == "https"
    port = parsed.port or (443 if https else 80)
    address = _resolve_public(host, port)
    connect_timeout = timeout or LIVE_PROBE_TIMEOUT
    if deadline is not None:
        connect_timeout = max(0.1, min(connect_timeout, deadline - time.monotonic()))
    sock = socket.create_connection((address, port), timeout=connect_timeout)
    try:
        if https:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        hostport = host if parsed.port is None else f"{host}:{parsed.port}"
        if ":" in host:
            hostport = f"[{host}]" if parsed.port is None else f"[{host}]:{port}"
        lines = [f"GET {target} HTTP/1.0", f"Host: {hostport}"]
        lines += [f"{k}: {v}" for k, v in _RADIO_HEADERS.items()]
        if icy:
            lines.append("Icy-MetaData: 1")
        lines.append("Connection: close")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        if any(c in target for c in "\r\n"):
            raise StreamError("bad_url")
        sock.sendall(request)
        response = _Response(sock, 0, None, b"", deadline, timeout)
        head = b""
        while b"\r\n\r\n" not in head and b"\n\n" not in head:
            chunk = response._recv(4096)
            if not chunk:
                break
            head += chunk
            if len(head) > _MAX_HEADER_BYTES:
                raise StreamError("not_a_stream")
        split = head.find(b"\r\n\r\n")
        cut = split + 4 if split >= 0 else head.find(b"\n\n") + 2
        if cut < 2:
            raise StreamError("stream_unreachable")
        lines = head[:cut].decode("latin-1").splitlines()
        match = _STATUS_RE.match(lines[0].encode("latin-1")) if lines else None
        if match is None:
            raise StreamError("stream_unreachable")
        pairs = []
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                pairs.append((name.strip(), value.strip()))
        response.status = int(match.group(1))
        response.headers = _Headers(pairs)
        response._buffer = head[cut:]
        return response
    except BaseException:
        sock.close()
        raise


def _open(url, icy=True, deadline=None):
    """Open `url` for the radio probe (tests replace this function)."""
    return _request(url, icy=icy, deadline=deadline)


def _follow(url, icy=True, deadline=None, opener=None):
    """GET `url`, following up to 3 redirects. Every hop is checked again
    (parse_radio_url and a pinned public address), so a redirect can never
    lead to a private address. Returns (final url, a 200 response)."""
    opener = opener or _open
    current = url
    for _ in range(4):
        try:
            response = opener(current, icy=icy, deadline=deadline)
        except StreamError:
            raise
        except Exception:
            raise StreamError("stream_unreachable")
        if 300 <= response.status < 400:
            location = response.headers.get("location")
            response.close()
            if not location:
                raise StreamError("stream_unreachable")
            current = parse_radio_url(urljoin(current, location), check_path=False)
            if current is None:
                raise StreamError("bad_url")
            continue
        if response.status != 200:
            response.close()
            raise StreamError("stream_unreachable")
        return current, response
    raise StreamError("stream_unreachable")


def _clean_name(value):
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = "".join(c for c in str(value) if c.isprintable()).strip()
    return value[:200] or None


def parse_playlist_file(body):
    """First stream URL and title of a .pls or plain .m3u playlist."""
    text = body.decode("utf-8", "replace")
    url = title = None
    if "[playlist]" in text.lower():
        for line in text.splitlines():
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if key == "file1" and url is None:
                url = value.strip()
            elif key == "title1" and title is None:
                title = value.strip()
    else:
        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("#EXTINF:") and title is None:
                title = line.partition(",")[2].strip()
            elif line and not line.startswith("#"):
                url = line
                break
    return url, _clean_name(title)


_HLS_TYPES = {"application/vnd.apple.mpegurl", "application/x-mpegurl"}


def probe_stream(url, depth=0, deadline=None):
    """Check that `url` is an audio stream and build its live item: an
    Icecast/Shoutcast stream (content-type audio/*), or a .pls/.m3u file
    pointing to one (followed once). HLS (.m3u8) radios are refused: FFmpeg
    would fetch their segments from any host. Redirects are followed up to 3
    times, each hop checked again, and the whole probe ends within
    LIVE_PROBE_DEADLINE. Blocking: run it in an executor. Raises
    StreamError."""
    if depth > 1:
        raise StreamError("not_a_stream")
    if deadline is None:
        deadline = time.monotonic() + LIVE_PROBE_DEADLINE
    current, response = _follow(url, deadline=deadline)
    with response:
        ctype = response.headers.get_content_type()
        name = _clean_name(response.headers.get("icy-name"))
        path = urlparse(current).path.lower()
        playlist_ext = path.endswith((".m3u", ".m3u8", ".pls"))
        if ctype in _PLAYLIST_TYPES or (ctype in _GENERIC_TYPES and playlist_ext):
            try:
                body = _read_capped(response, LIVE_PLAYLIST_MAX_BYTES + 1)
            except StreamError:
                raise
            except Exception:
                raise StreamError("stream_unreachable")
            if len(body) > LIVE_PLAYLIST_MAX_BYTES:
                raise StreamError("not_a_stream")
            if b"#EXT-X-" in body or (ctype in _HLS_TYPES and b"#EXTM3U" in body):
                raise StreamError("radio_hls")
            entry, title = parse_playlist_file(body)
            entry = parse_radio_url(urljoin(current, entry or ""), check_path=False)
            if not entry or entry == current:
                raise StreamError("not_a_stream")
            track = probe_stream(entry, depth + 1, deadline)
            track["url"] = url
            if title and track["title"] == urlparse(entry).hostname:
                track["title"] = title
            return track
        if ctype.startswith("audio/") or ctype in _STREAM_TYPES:
            title = name or urlparse(current).hostname
            return live_track(url, current, title, "radio")
    raise StreamError("not_a_stream")


def _read_capped(response, limit):
    data = b""
    while len(data) < limit:
        chunk = response.read(min(65536, limit - len(data)))
        if not chunk:
            break
        data += chunk
    return data


_STREAM_TITLE_RE = re.compile(rb"StreamTitle='(.*?)';", re.DOTALL)


def parse_icy_metadata(block):
    """The StreamTitle of one ICY metadata block (bytes), or None."""
    match = _STREAM_TITLE_RE.search(block or b"")
    if not match:
        return None
    raw = match.group(1)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return _clean_name(text)


def _read_exactly(response, size):
    data = b""
    while len(data) < size:
        chunk = response.read(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def read_icy_title(stream_url):
    """What a radio plays now (ICY "StreamTitle"), or None: one request that
    reads up to `icy-metaint` audio bytes and the metadata block after them.
    Blocking, never raises."""
    deadline = time.monotonic() + LIVE_ICY_DEADLINE
    try:
        _, response = _follow(stream_url, icy=True, deadline=deadline)
        with response:
            metaint = int(response.headers.get("icy-metaint") or 0)
            if not 0 < metaint <= LIVE_ICY_MAX_METAINT:
                return None
            if len(_read_exactly(response, metaint)) < metaint:
                return None
            length = _read_exactly(response, 1)
            if not length:
                return None
            return parse_icy_metadata(_read_exactly(response, length[0] * 16))
    except Exception:
        return None
