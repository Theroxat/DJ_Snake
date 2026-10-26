"""Export and import of saved playlists as files (JSON, M3U or plain text).

Pure functions, no network and no Discord. Export writes the stored
`{url, title, duration}` entries; import validates a user-supplied file and
returns clean entries. A file is never trusted: it is size-capped, parsed
strictly, and every URL goes through `media.parse_media_url()` (the same
allowlist as the web add), so a file can never make the bot fetch an
arbitrary host. Spotify, Deezer and Apple Music links are not accepted in a
file (they would need a YouTube match; use `/playlist import <name> <url>`).
"""

import json

from . import media
from .config import MAX_SAVED_TRACKS
from .playlists import TEXT_MAX, URL_MAX

FORMATS = ("json", "m3u", "txt")
SCHEMA = "djsnake.playlist"
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 256 * 1024
MAX_LINES = 5000  # Lines or entries looked at, whatever the cap on tracks


class ImportFileError(Exception):
    """A file that cannot be imported. `code` is a ControlError code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _clean_text(text):
    """One line of plain text: control characters and newlines dropped."""
    return "".join(c if c.isprintable() else " " for c in str(text)).strip()


def _duration(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if 0 < value < 100 * 3600 else None


# --- Export --------------------------------------------------------------------


def export_bytes(name, created_by, tracks, fmt="json"):
    """(filename, bytes) of a playlist in `fmt`. `created_by` is a display
    string or None."""
    fmt = (fmt or "json").lower()
    if fmt not in FORMATS:
        raise ValueError(fmt)
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]
    stem = stem or "playlist"
    if fmt == "json":
        body = json.dumps(
            {
                "schema": SCHEMA,
                "version": SCHEMA_VERSION,
                "name": name,
                "created_by": created_by,
                "entries": [
                    {
                        "url": t["url"],
                        "title": t.get("title"),
                        "duration": t.get("duration"),
                    }
                    for t in tracks
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    elif fmt == "m3u":
        lines = ["#EXTM3U", f"#PLAYLIST:{_clean_text(name)}"]
        for t in tracks:
            seconds = _duration(t.get("duration"))
            seconds = int(round(seconds)) if seconds else -1
            lines.append(f"#EXTINF:{seconds},{_clean_text(t.get('title') or '')}")
            lines.append(t["url"])
        body = "\n".join(lines)
    else:
        body = "\n".join(t["url"] for t in tracks)
    return f"{stem}.{fmt}", (body + "\n").encode("utf-8")


# --- Import --------------------------------------------------------------------


def detect_format(filename, text):
    """The format from the extension, else from the content."""
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext == "m3u8":
        return "m3u"
    if ext in FORMATS:
        return ext
    head = text.lstrip()[:8]
    if head.startswith("{"):
        return "json"
    if head.upper().startswith("#EXTM3U"):
        return "m3u"
    return "txt"


def _raw_json(text):
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):  # Deeply nested arrays overflow
        raise ImportFileError("import_bad_file")
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ImportFileError("import_bad_file")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ImportFileError("import_bad_file")
    if not 1 <= version <= SCHEMA_VERSION:
        raise ImportFileError("import_bad_file")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ImportFileError("import_bad_file")
    raw = []
    for entry in entries[:MAX_LINES]:
        if isinstance(entry, dict):
            raw.append((entry.get("url"), entry.get("title"), entry.get("duration")))
        else:
            raw.append((None, None, None))
    return raw


def _raw_m3u(text):
    raw, title, duration = [], None, None
    for line in text.splitlines()[:MAX_LINES]:
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            head, _, title = line[8:].partition(",")
            try:
                duration = float(head.split()[0])
            except (ValueError, IndexError):
                duration = None
            title = title.strip() or None
        elif not line.startswith("#"):
            raw.append((line, title, duration))
            title = duration = None
    return raw


def _raw_txt(text):
    lines = (line.strip() for line in text.splitlines()[:MAX_LINES])
    return [(line, None, None) for line in lines if line and not line.startswith("#")]


def parse_import(data, filename=""):
    """Validate an uploaded file (bytes). Returns `(tracks, skipped,
    truncated)`: clean `{url, title, duration}` entries (canonical,
    deduplicated, at most MAX_SAVED_TRACKS), the number of rejected or
    duplicate entries, and whether the cap cut the list. Raises
    ImportFileError."""
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError("import_too_big")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError("import_bad_file")
    if "\x00" in text:
        raise ImportFileError("import_bad_file")
    fmt = detect_format(filename, text)
    raw = {"json": _raw_json, "m3u": _raw_m3u, "txt": _raw_txt}[fmt](text)
    tracks, seen, skipped, truncated = [], set(), 0, False
    for url, title, duration in raw:
        parsed = media.parse_media_url(url) if isinstance(url, str) else None
        if (
            parsed is None
            or parsed[0] != "single"
            or parsed[1] in seen
            or len(parsed[1]) > URL_MAX
        ):
            skipped += 1
            continue
        if len(tracks) >= MAX_SAVED_TRACKS:
            truncated = True
            skipped += 1
            continue
        seen.add(parsed[1])
        title = _clean_text(title)[:TEXT_MAX] if isinstance(title, str) else ""
        tracks.append(
            {"url": parsed[1], "title": title or "?", "duration": _duration(duration)}
        )
    if not tracks:
        raise ImportFileError("import_nothing")
    return tracks, skipped, truncated
