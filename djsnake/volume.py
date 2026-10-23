"""Per-server volume: storage in DATA_DIR (volume.json) and the percent to
gain mapping. The gain itself is applied by the mixer."""

import json

from . import state
from .config import VOLUME_DEFAULT, VOLUME_FILE, VOLUME_MAX
from .themes import _write_atomic

# Stored as {guild id: percent}. Bad entries are dropped on load.


def valid_volume(value):
    """True for an int (not a bool) from 0 to VOLUME_MAX."""
    return type(value) is int and 0 <= value <= VOLUME_MAX


def parse_volume(text):
    """The percent in a command argument ("80" or "80%"), or None."""
    text = str(text).strip().rstrip("%").strip()
    if not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if valid_volume(value) else None


def percent_to_gain(percent):
    """Linear amplitude factor: 100 % is 1.0 (the normalised level)."""
    return percent / 100.0


def load_volumes():
    """(Re)load VOLUME_FILE into guild_volumes. A missing file is no error."""
    state.guild_volumes.clear()
    try:
        with open(VOLUME_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"⚠️  Could not read {VOLUME_FILE}: {e}")
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        try:
            guild_id = int(key)
        except ValueError:
            continue
        if valid_volume(value):
            state.guild_volumes[guild_id] = value


def save_volumes():
    """Persist guild_volumes. Raises OSError (e.g. DATA_DIR not writable)."""
    data = {str(gid): v for gid, v in state.guild_volumes.items()}
    _write_atomic(VOLUME_FILE, json.dumps(data, indent=2).encode("utf-8"))


def volume_for(guild_id):
    return state.guild_volumes.get(guild_id, VOLUME_DEFAULT)


load_volumes()
