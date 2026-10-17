"""YouTube media: URL parsing, yt-dlp search and download, automix analysis
and FFmpeg playback options. Everything blocking here runs in an executor."""

import json
import math
import os
import re
import secrets
import subprocess
import time
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.postprocessor import ModifyChaptersPP, SponsorBlockPP

from .config import (
    ANALYSIS_TIMEOUT,
    CROSSFADE_MIN_TRACK,
    DOWNLOAD_ATTEMPTS,
    EXTRACTOR_ARGS,
    FADE_IN_SECONDS,
    FADE_OUT_SECONDS,
    LOUDNORM_KEYS,
    LOUDNORM_TARGET,
    MAX_LEADING_TRIM,
    MAX_PLAYLIST_SIZE,
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
        if not video_id and parsed.path.startswith("/shorts/"):
            video_id = parsed.path[len("/shorts/") :].strip("/")
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


def get_playlist_info(url):
    """Extract playlist information without downloading."""
    try:
        print(f"Extracting playlist from: {url}")

        # Configuration to extract playlist
        ydl_opts = {
            "extract_flat": "in_playlist",  # Important: use 'in_playlist' for playlists
            "quiet": True,  # Reduce log spam
            "no_warnings": True,
            "ignoreerrors": True,  # Continue even if some videos have errors
            "playlistend": MAX_PLAYLIST_SIZE,  # Limit based on config
            "remote_components": REMOTE_COMPONENTS,
            "extractor_args": EXTRACTOR_ARGS,
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


def _download_once(url):
    """Single download attempt. Raises on any failure."""
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
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        print(f"Downloading: {url}")
        info = ydl.extract_info(url, download=True)
        filepath = _find_download(info)
        _remove_offtopic(ydl, info, filepath)

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
        "url": info.get("webpage_url") or url,
    }


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


def playback_options(track, offset, fade_in=None, fade_out=FADE_OUT_SECONDS):
    """FFmpeg options to play `track` from `offset` (file time, seconds).

    Returns (offset, kwargs for FFmpegPCMAudio); the offset is clamped to the
    automix window [start, end - 1]. Input -ss/-t keep FFmpeg in file time
    while the filters see a timeline starting at 0, so the fade-out is placed
    relative to the seek point. `fade_in` (None: automatic) and `fade_out`
    are overridden by the crossfade.
    """
    # The file carries video for the web page; Discord only needs audio.
    end = track.get("end")
    if end is None:  # Not analysed: play the whole file, untouched
        kwargs = {"options": "-vn"}
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


def download_media(url):
    """Download one track (video+audio mp4), retrying transient YouTube refusals.

    Runs in an executor thread (see download_and_queue_song), so the blocking
    sleep between attempts does not stall the bot's event loop.
    """
    last_error = None

    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return _download_once(url)
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
