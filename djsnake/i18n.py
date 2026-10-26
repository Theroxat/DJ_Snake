"""User-facing text in several languages, with no dependency.

Every message lives in `locales/{code}.json` (a flat `{key: text}` map) and is
looked up with `t(key, lang, **params)`. `en` and `fr` hold every key; the
other languages (`es`, `de`) are best effort: a key they lack falls back to
English, key by key.
Texts are `str.format` templates; `{prefix}` is always the command prefix.
A `count` parameter picks the plural form when the catalog has `key.one`
and `key.other`. The language of a guild is set with `!language` and
persisted to `{DATA_DIR}/languages.json`; guilds that never chose one use
BOT_LANGUAGE.
"""

import json
import os
import tempfile

from . import state
from .config import BOT_LANGUAGE, COMMAND_PREFIX, LANGUAGES_FILE

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
FALLBACK = "en"


def _load_catalogs():
    catalogs = {}
    for name in sorted(os.listdir(LOCALES_DIR)):
        if name.endswith(".json"):
            with open(os.path.join(LOCALES_DIR, name), encoding="utf-8") as f:
                catalogs[name[: -len(".json")]] = json.load(f)
    return catalogs


CATALOGS = _load_catalogs()
# Language codes, English first.
LANGUAGES = tuple(sorted(CATALOGS, key=lambda code: (code != FALLBACK, code)))


def normalize(lang):
    """A supported language code for "fr", "FR", "fr-CA", "fr_FR"..., or None."""
    if not isinstance(lang, str):
        return None
    code = lang.strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in CATALOGS else None


DEFAULT_LANGUAGE = normalize(BOT_LANGUAGE)
if DEFAULT_LANGUAGE is None:
    print(f"⚠️  BOT_LANGUAGE={BOT_LANGUAGE!r} is not supported; using 'en'.")
    DEFAULT_LANGUAGE = FALLBACK


def _plural_one(lang, count):
    """True when `count` takes the singular form in `lang`: `count == 1`
    (en, es, de: "one" vs "other"), except French where 0 and 1 are singular."""
    if lang == "fr":
        return abs(count) < 2  # 0 and 1 are singular in French
    return count == 1


def _lookup(key, lang, params):
    catalog = CATALOGS.get(lang) or CATALOGS[FALLBACK]
    if "count" in params:
        form = "one" if _plural_one(lang, params["count"]) else "other"
        plural = f"{key}.{form}"
        if plural in catalog or plural in CATALOGS[FALLBACK]:
            key = plural
    if key in catalog:
        return catalog[key], True
    return CATALOGS[FALLBACK].get(key), False


def t(key, lang=None, **params):
    """The text of `key` in `lang` (default: BOT_LANGUAGE), filled with
    `params`. Falls back to English, then to the key itself (unformatted),
    so a literal string passes through unchanged."""
    lang = normalize(lang) or DEFAULT_LANGUAGE
    text, found = _lookup(key, lang, params)
    if text is None:
        return key
    values = {"prefix": COMMAND_PREFIX, **params}
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError) as e:
        if not found and lang == FALLBACK:
            raise
        print(f"Bad {lang} translation for {key!r}: {e}")
        return t(key, FALLBACK, **params)


def language_name(lang):
    """The language's own name ("English", "Français")."""
    return t("language.name", lang)


# --- Per-guild language -----------------------------------------------------


def lang_for(guild_id):
    """The language of a guild (None: no guild, so BOT_LANGUAGE)."""
    return state.guild_languages.get(guild_id) or DEFAULT_LANGUAGE


def ctx_lang(ctx):
    """The language for a command context (or a WebContext)."""
    guild = getattr(ctx, "guild", None)
    return lang_for(getattr(guild, "id", None))


def load_languages():
    """(Re)load LANGUAGES_FILE into guild_languages. A missing file is no
    error; unknown codes and bad ids are dropped."""
    state.guild_languages.clear()
    try:
        with open(LANGUAGES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"Could not read {LANGUAGES_FILE}: {e}")
        return
    if not isinstance(data, dict):
        return
    for key, code in data.items():
        try:
            guild_id = int(key)
        except ValueError:
            continue
        code = normalize(code)
        if code is not None:
            state.guild_languages[guild_id] = code


def _save():
    """Write guild_languages atomically (temp file and os.replace)."""
    data = {str(gid): code for gid, code in state.guild_languages.items()}
    directory = os.path.dirname(LANGUAGES_FILE)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, LANGUAGES_FILE)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def set_language(guild_id, lang):
    """Set (or with None, forget) a guild's language and save. Raises
    OSError when DATA_DIR is not writable, leaving memory unchanged."""
    previous = state.guild_languages.get(guild_id)
    if lang is None:
        state.guild_languages.pop(guild_id, None)
    else:
        state.guild_languages[guild_id] = lang
    try:
        _save()
    except OSError:
        if previous is None:
            state.guild_languages.pop(guild_id, None)
        else:
            state.guild_languages[guild_id] = previous
        raise


load_languages()
