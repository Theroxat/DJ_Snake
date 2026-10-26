"""Optional YouTube cookies (YTDLP_COOKIES) for age-restricted and members-only
videos. yt-dlp writes back to its cookie file, so the user's file is copied once
to a private temporary file; the original is never modified and may be mounted
read-only. The contents are never logged."""

import os
import shutil
import tempfile

from . import config

_HEADERS = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
_state = {"ready": False, "path": None}


def validate(path):
    """None when `path` looks like a readable Netscape cookie file, else a
    short reason (never the file's content)."""
    if not os.path.isfile(path):
        return "file not found (is it mounted in the container?)"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(65536)
    except OSError as e:
        return f"not readable ({e.strerror or type(e).__name__})"
    lines = [line.strip() for line in head.splitlines() if line.strip()]
    if not lines:
        return "file is empty"
    if lines[0].startswith(_HEADERS):
        return None
    for line in lines:
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        if len(line.split("\t")) == 7:
            return None
        break
    return "not in Netscape cookies.txt format (7 tab-separated fields per line)"


def setup(path=None):
    """Validate and copy the cookie file to a private temp file (0600).
    Returns the copy's path, or None when cookies are not configured or
    unusable (a warning says why). Calling it again re-copies."""
    path = config.YTDLP_COOKIES if path is None else path
    _state["ready"] = True
    _state["path"] = None
    if not path:
        return None
    problem = validate(path)
    if problem:
        print(f"⚠️  YTDLP_COOKIES={path!r} ignored: {problem}.")
        return None
    try:
        fd, copy = tempfile.mkstemp(prefix="dj-snake-cookies-", suffix=".txt")
        os.chmod(copy, 0o600)
        with os.fdopen(fd, "wb") as dst, open(path, "rb") as src:
            shutil.copyfileobj(src, dst)
    except OSError as e:
        print(f"⚠️  YTDLP_COOKIES={path!r} ignored: copy failed ({e.strerror}).")
        return None
    _state["path"] = copy
    print("YouTube cookies loaded (YTDLP_COOKIES).")
    return copy


def cookie_path():
    """The private cookie copy, or None. Set up on first use."""
    if not _state["ready"]:
        setup()
    return _state["path"]


def enabled():
    return cookie_path() is not None


def options():
    """yt-dlp options to merge into every call: {"cookiefile": ...} or {}."""
    path = cookie_path()
    return {"cookiefile": path} if path else {}
