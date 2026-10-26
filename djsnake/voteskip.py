"""Vote to skip: members without the DJ role can ask to skip the track.

The threshold is per guild (`/voteskip threshold`, persisted in
`{DATA_DIR}/voteskip.json` as `{guild id: percent}`), default
`config.VOTESKIP_PERCENT`. Who counts: the humans in the bot's voice channel
who are not deafened (a deafened member does not hear the track, so does not
count in the total and cannot vote). A vote is the member's id in
`player.votes`; it only counts while that member is still an eligible
listener, so leaving, joining or (un)deafening is handled by recomputing from
the channel (`status()`), never by editing the set. The votes begin again when
a new track starts (`MusicPlayer.reset_votes()`).
"""

import json

from . import state
from .config import VOTESKIP_FILE, VOTESKIP_PERCENT
from .themes import _write_atomic


def valid_percent(value):
    """True for an int (not a bool) from 1 to 100."""
    return type(value) is int and 1 <= value <= 100


def parse_percent(text):
    """The percent in a command argument ("60" or "60%"), or None."""
    text = str(text).strip().rstrip("%").strip()
    if not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if valid_percent(value) else None


def load_thresholds():
    """(Re)load VOTESKIP_FILE into guild_voteskip. A missing file is no error."""
    state.guild_voteskip.clear()
    try:
        with open(VOTESKIP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"⚠️  Could not read {VOTESKIP_FILE}: {e}")
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        try:
            guild_id = int(key)
        except ValueError:
            continue
        if valid_percent(value):
            state.guild_voteskip[guild_id] = value


def save_thresholds():
    """Persist guild_voteskip. Raises OSError (e.g. DATA_DIR not writable)."""
    data = {str(gid): v for gid, v in state.guild_voteskip.items()}
    _write_atomic(VOTESKIP_FILE, json.dumps(data, indent=2).encode("utf-8"))


def threshold_for(guild_id):
    return state.guild_voteskip.get(guild_id, VOTESKIP_PERCENT)


def set_threshold(guild_id, percent):
    """Store and save the threshold; it stays applied if saving fails (OSError
    is then raised)."""
    state.guild_voteskip[guild_id] = percent
    save_thresholds()


def is_listener(member):
    """A human who is not deafened (by themselves or the server)."""
    if getattr(member, "bot", False):
        return False
    voice = getattr(member, "voice", None)
    return not (getattr(voice, "self_deaf", False) or getattr(voice, "deaf", False))


def listeners(channel):
    """The members of `channel` who count toward the total."""
    return [m for m in (getattr(channel, "members", None) or []) if is_listener(m)]


def votes_needed(total, percent):
    """Votes needed out of `total` listeners: strictly more than `percent` of
    them (3 of 5 at 50 %), at least 1 and at most `total` (100 % means
    everyone)."""
    needed = total * percent // 100 + 1
    return max(1, min(needed, total))


def status(player, guild_id):
    """(count, needed) for the current track: the votes of members who are
    still eligible listeners, and the votes needed now."""
    channel = getattr(player.voice_client, "channel", None)
    present = listeners(channel)
    count = sum(1 for m in present if getattr(m, "id", None) in player.votes)
    return count, votes_needed(len(present), threshold_for(guild_id))


def web_status(player, guild_id):
    """`{count, needed}` for the page and the panel, or None when no vote is
    active."""
    if not player.votes or player.now_playing is None or not player.in_voice():
        return None
    count, needed = status(player, guild_id)
    return {"count": count, "needed": needed} if count > 0 else None


load_thresholds()
