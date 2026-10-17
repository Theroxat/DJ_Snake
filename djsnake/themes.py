"""Clip page themes: per-guild storage, logos and `!theme edit` tokens."""

import json
import os
import re
import secrets
import tempfile
import time

from . import state
from .config import (
    LOGO_DIR,
    LOGO_TYPES,
    PERSO_DEFAULTS,
    THEME_EDIT_SECONDS,
    THEME_NAME_MAX,
    THEME_PRESETS,
    THEME_SKINS,
    THEMES_FILE,
    WEB_THEME,
)

# Each guild picks the clip page's default theme (viewers can still override
# it locally). Stored in THEMES_FILE as {guild id: {skin, base, bg, text,
# accent, name, logo}}, logo being {"ext", "v"} or null; the logo itself is
# LOGO_DIR/<guild id>.<ext>. Every path comes from the guild id and a fixed
# extension, never from a request.

_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")


def valid_color(value):
    return isinstance(value, str) and _COLOR_RE.fullmatch(value) is not None


def clean_theme_name(value):
    """The stripped community name, or None if it is not acceptable."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > THEME_NAME_MAX or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def _clean_theme(raw):
    """A stored theme record with every bad or missing field dropped."""
    record = {}
    if not isinstance(raw, dict):
        return record
    if raw.get("skin") in THEME_SKINS:
        record["skin"] = raw["skin"]
    if raw.get("base") in THEME_PRESETS:
        record["base"] = raw["base"]
    for key in ("bg", "text", "accent"):
        if valid_color(raw.get(key)):
            record[key] = raw[key].lower()
    name = clean_theme_name(raw.get("name"))
    if name:
        record["name"] = name
    logo = raw.get("logo")
    if (
        isinstance(logo, dict)
        and logo.get("ext") in LOGO_TYPES
        and type(logo.get("v")) is int
    ):
        record["logo"] = {"ext": logo["ext"], "v": logo["v"]}
    return record


def load_themes():
    """(Re)load THEMES_FILE into guild_themes. A missing file is no error."""
    state.guild_themes.clear()
    try:
        with open(THEMES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"⚠️  Could not read {THEMES_FILE}: {e}")
        return
    if not isinstance(data, dict):
        return
    for key, raw in data.items():
        try:
            guild_id = int(key)
        except ValueError:
            continue
        record = _clean_theme(raw)
        if record:
            state.guild_themes[guild_id] = record


def _write_atomic(path, data):
    """Write bytes to `path` through a temp file and os.replace()."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_themes():
    """Persist guild_themes. Raises OSError (e.g. DATA_DIR not writable)."""
    data = {str(gid): record for gid, record in state.guild_themes.items()}
    _write_atomic(THEMES_FILE, json.dumps(data, indent=2).encode("utf-8"))


def theme_for(guild_id):
    """The guild's full theme, defaults filled in."""
    theme = {"skin": WEB_THEME, **PERSO_DEFAULTS, "name": "", "logo": None}
    theme.update(state.guild_themes.get(guild_id, {}))
    return theme


def theme_state(sid, guild_id):
    """The `theme` field of the web state."""
    theme = theme_for(guild_id)
    logo = theme.pop("logo")
    theme["logo_url"] = f"/theme-logo/{sid}?v={logo['v']}" if logo else None
    return theme


def logo_path(guild_id, ext):
    return os.path.join(LOGO_DIR, f"{int(guild_id)}.{ext}")


def sniff_image(data):
    """The extension of a PNG, JPEG, WebP or GIF by its magic bytes, else None.
    SVG (scriptable) and everything else is refused."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _remove_logo_files(guild_id, keep=None):
    for ext in LOGO_TYPES:
        if ext != keep:
            try:
                os.remove(logo_path(guild_id, ext))
            except FileNotFoundError:
                pass


def set_logo(guild_id, data, ext):
    """Store the guild's logo (replacing any previous one) and save."""
    _write_atomic(logo_path(guild_id, ext), data)
    _remove_logo_files(guild_id, keep=ext)
    record = state.guild_themes.setdefault(guild_id, {})
    record["logo"] = {"ext": ext, "v": int(time.time() * 1000)}
    save_themes()


def remove_logo(guild_id):
    record = state.guild_themes.get(guild_id)
    if record and record.pop("logo", None) is not None:
        save_themes()
    _remove_logo_files(guild_id)


def reset_theme(guild_id):
    """Back to WEB_THEME: drops the guild's colors, name and logo."""
    if state.guild_themes.pop(guild_id, None) is not None:
        save_themes()
    _remove_logo_files(guild_id)


def _prune_edit_tokens():
    now = state.clock()
    for token, (_, expires) in list(state.edit_tokens.items()):
        if expires <= now:
            state.edit_tokens.pop(token, None)


def issue_edit_token(guild_id):
    _prune_edit_tokens()
    token = secrets.token_urlsafe(24)
    state.edit_tokens[token] = (guild_id, state.clock() + THEME_EDIT_SECONDS)
    return token


def check_edit_token(given, guild_id):
    """True if `given` is a live edit token for this guild (constant-time)."""
    _prune_edit_tokens()
    if not isinstance(given, str) or not 0 < len(given) <= 128:
        return False
    given = given.encode("utf-8", "replace")
    ok = False
    for token, (gid, _) in list(state.edit_tokens.items()):
        if secrets.compare_digest(token.encode(), given) and gid == guild_id:
            ok = True
    return ok


load_themes()
