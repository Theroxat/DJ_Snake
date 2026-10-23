"""Who may control the music: the per-guild DJ role and the gates that use it.

Two gates, each one central list:

- Discord: `DJ_COMMANDS` names the commands that need a DJ (the global
  `bot.check` in commands.py runs `can_run_dj_command()` for every command).
- Web: every `POST /api/{id}/control` action needs the control link, so a
  view-only link is refused before any action is read (see web.py).
"""

import json

from . import state
from .config import PERMISSIONS_FILE, WEB_CONTROL
from .themes import _write_atomic

# Discord commands that change playback for everyone. A new mutating command
# only has to be added here. `!play` stays open: adding a track is how anyone
# takes part, it never interrupts what plays, and DJs can still skip or remove
# it. `!list`, `!weblink` and `!help` only read.
DJ_COMMANDS = frozenset(
    {"next", "pause", "resume", "seek", "stop", "reset", "crossfade"}
)
# Commands the member who queued the current track may always use on it.
REQUESTER_COMMANDS = frozenset({"next"})


def load_perms():
    """(Re)load PERMISSIONS_FILE into guild_perms. A missing file is no error."""
    state.guild_perms.clear()
    try:
        with open(PERMISSIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"⚠️  Could not read {PERMISSIONS_FILE}: {e}")
        return
    if not isinstance(data, dict):
        return
    for key, raw in data.items():
        try:
            guild_id = int(key)
        except ValueError:
            continue
        role = raw.get("dj_role") if isinstance(raw, dict) else None
        if type(role) is int and role > 0:
            state.guild_perms[guild_id] = {"dj_role": role}


def save_perms():
    """Persist guild_perms. Raises OSError (e.g. DATA_DIR not writable)."""
    data = {str(gid): record for gid, record in state.guild_perms.items()}
    _write_atomic(PERMISSIONS_FILE, json.dumps(data, indent=2).encode("utf-8"))


def dj_role(guild_id):
    """The guild's DJ role id, or None (no role: everyone is a DJ)."""
    return state.guild_perms.get(guild_id, {}).get("dj_role")


def set_dj_role(guild_id, role_id):
    """Set (or with None clear) the DJ role and save; restores on failure."""
    previous = state.guild_perms.get(guild_id)
    if role_id is None:
        state.guild_perms.pop(guild_id, None)
    else:
        state.guild_perms[guild_id] = {"dj_role": int(role_id)}
    try:
        save_perms()
    except OSError:
        if previous is None:
            state.guild_perms.pop(guild_id, None)
        else:
            state.guild_perms[guild_id] = previous
        raise


def web_control_restricted(guild_id):
    """True when the control link is only for DJs (not posted publicly)."""
    return WEB_CONTROL == "dj" or dj_role(guild_id) is not None


def is_manager(member):
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def is_dj(member, guild_id, strict=False):
    """Manage Server or the DJ role. Without a DJ role everyone is a DJ,
    unless `strict` (then only managers are)."""
    if is_manager(member):
        return True
    role = dj_role(guild_id)
    if role is None:
        return not strict
    return any(r.id == role for r in getattr(member, "roles", ()))


def can_run_dj_command(member, guild_id, command, player):
    """The Discord gate: may `member` run `command` (a name)?"""
    if command not in DJ_COMMANDS or is_dj(member, guild_id):
        return True
    if command in REQUESTER_COMMANDS and player is not None:
        track = player.now_playing or {}
        requester = track.get("requester_id")
        return requester is not None and requester == getattr(member, "id", None)
    return False


load_perms()
