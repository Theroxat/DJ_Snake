"""24/7 mode: per-server "stay in the voice channel", persisted in DATA_DIR
(stay.json). The idle linger (playback.py) never leaves a guild with it on,
and the bot rejoins the remembered channel after a restart."""

import json

from . import state
from .config import STAY_247, STAY_FILE
from .fileio import write_atomic

# Stored as {guild id: {"on": bool, "channel": voice channel id | None}}.
# A guild with no record follows the STAY_247 default (it still needs a
# remembered channel to rejoin at startup). Bad entries are dropped on load.


def load_stay():
    """(Re)load STAY_FILE into guild_stay. A missing file is no error."""
    state.guild_stay.clear()
    try:
        with open(STAY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"⚠️  Could not read {STAY_FILE}: {e}")
        return
    if not isinstance(data, dict):
        return
    for key, raw in data.items():
        try:
            guild_id = int(key)
        except ValueError:
            continue
        if not isinstance(raw, dict) or type(raw.get("on")) is not bool:
            continue
        channel = raw.get("channel")
        if type(channel) is not int or channel <= 0:
            channel = None
        state.guild_stay[guild_id] = {"on": raw["on"], "channel": channel}


def save_stay():
    """Persist guild_stay. Raises OSError (e.g. DATA_DIR not writable)."""
    data = {str(gid): record for gid, record in state.guild_stay.items()}
    write_atomic(STAY_FILE, json.dumps(data, indent=2).encode("utf-8"))


def stay_on(guild_id):
    record = state.guild_stay.get(guild_id)
    return record["on"] if record else STAY_247


def stay_channel(guild_id):
    """The voice channel id to rejoin, or None."""
    return state.guild_stay.get(guild_id, {}).get("channel")


def set_stay(guild_id, on, channel_id=None):
    """Turn 24/7 on or off and save, remembering `channel_id` (the old one
    is kept when None). Restores the previous record on a write failure."""
    previous = state.guild_stay.get(guild_id)
    kept = channel_id if channel_id is not None else stay_channel(guild_id)
    state.guild_stay[guild_id] = {"on": bool(on), "channel": kept}
    try:
        save_stay()
    except OSError:
        if previous is None:
            state.guild_stay.pop(guild_id, None)
        else:
            state.guild_stay[guild_id] = previous
        raise


def remember_channel(guild_id, channel_id):
    """The bot is now in `channel_id` of a 24/7 guild: remember it for the
    next restart. Best effort (a failed write is logged)."""
    if not stay_on(guild_id) or stay_channel(guild_id) == channel_id:
        return
    try:
        set_stay(guild_id, True, channel_id)
    except OSError as e:
        print(f"⚠️  Could not save {STAY_FILE}: {e}")


load_stay()
