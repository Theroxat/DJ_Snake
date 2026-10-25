"""Anti-abuse limits on what may be queued, per guild.

Four limits, each with an environment default (config.py) that a guild
overrides with `/limits` (Manage Server), stored in `{DATA_DIR}/limits.json`:

- `queue_size`: tracks waiting in the queue (never 0).
- `per_user`: tracks one member may have waiting (0 = unlimited). The clip
  page has no identity, so its adds share the "web" bucket.
- `track_minutes`: longest single track (0 = unlimited). The bot downloads
  whole files and never plays live streams, so there is no live item to
  exempt; an unknown duration passes.
- `cooldown`: seconds between one member's `/play` calls (0 = none).

Managers and members with the DJ role (`is_dj(strict=True)`) bypass the
per-user limit and the cooldown, never the queue size or the duration.
"""

import json
import math

from . import config, state
from .permissions import is_dj
from .themes import _write_atomic

FIELDS = ("queue_size", "per_user", "track_minutes", "cooldown")
WEB_BUCKET = "web"
# Autoplay is the bot's own pick: only the queue size applies to it.
AUTOPLAY_BUCKET = "autoplay"
# Generous bounds, so a typo cannot overflow anything.
MAXIMUM = {
    "queue_size": 10000,
    "per_user": 10000,
    "track_minutes": 1440,
    "cooldown": 86400,
}
# Reasons a track is refused (also the suffix of `limits.<reason>` messages).
QUEUE_FULL, USER_FULL, TOO_LONG = "queue_full", "user_full", "too_long"

# Stored as {guild id: {field: int}}: only what differs from the defaults.
# `state.play_times` holds the cooldown clocks, in memory only.


def defaults():
    return {
        "queue_size": config.MAX_QUEUE_SIZE,
        "per_user": config.MAX_TRACKS_PER_USER,
        "track_minutes": config.MAX_TRACK_MINUTES,
        "cooldown": config.PLAY_COOLDOWN_SECONDS,
    }


def valid_value(field, value):
    """True for an int (not a bool) within the field's range."""
    low = 1 if field == "queue_size" else 0
    return type(value) is int and low <= value <= MAXIMUM[field]


def parse_value(field, text):
    """The number in a command argument, or None if invalid."""
    text = str(text).strip()
    if not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if valid_value(field, value) else None


def limits_for(guild_id):
    """The effective limits of a guild: its overrides over the defaults."""
    result = defaults()
    result.update(state.guild_limits.get(guild_id, {}))
    return result


def load_limits():
    """(Re)load LIMITS_FILE into guild_limits. A missing file is no error."""
    state.guild_limits.clear()
    try:
        with open(config.LIMITS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        print(f"⚠️  Could not read {config.LIMITS_FILE}: {e}")
        return
    if not isinstance(data, dict):
        return
    for key, raw in data.items():
        try:
            guild_id = int(key)
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        record = {f: v for f, v in raw.items() if f in FIELDS and valid_value(f, v)}
        if record:
            state.guild_limits[guild_id] = record


def save_limits():
    """Persist guild_limits. Raises OSError (e.g. DATA_DIR not writable)."""
    data = {str(gid): record for gid, record in state.guild_limits.items()}
    _write_atomic(config.LIMITS_FILE, json.dumps(data, indent=2).encode("utf-8"))


def set_limit(guild_id, field, value):
    """Set one limit (None goes back to the default) and save; restores the
    previous record if saving fails."""
    previous = state.guild_limits.get(guild_id)
    record = dict(previous or {})
    if value is None:
        record.pop(field, None)
    else:
        record[field] = value
    if record:
        state.guild_limits[guild_id] = record
    else:
        state.guild_limits.pop(guild_id, None)
    try:
        save_limits()
    except OSError:
        if previous is None:
            state.guild_limits.pop(guild_id, None)
        else:
            state.guild_limits[guild_id] = previous
        raise


def bypasses(member, guild_id):
    """DJs and managers skip the per-user limit and the cooldown. Without a
    DJ role only managers do (everyone being a DJ would void the limits)."""
    return member is not None and is_dj(member, guild_id, strict=True)


def bucket_of(requester_id):
    """The per-user counting key: the member's id, or "web" for the page."""
    return requester_id if requester_id is not None else WEB_BUCKET


def queued_by(player, bucket):
    return sum(
        1 for track in player.queue if bucket_of(track.get("requester_id")) == bucket
    )


def check_enqueue(player, guild_id, bucket, bypass=False, duration=None):
    """Why a track may not enter the queue, as (reason, params), or None.
    `bucket` is a member id, WEB_BUCKET or AUTOPLAY_BUCKET; `duration` is in
    seconds when known."""
    limit = limits_for(guild_id)
    if len(player.queue) >= limit["queue_size"]:
        return QUEUE_FULL, {"max": limit["queue_size"]}
    if bucket == AUTOPLAY_BUCKET:
        return None
    if (
        limit["per_user"]
        and not bypass
        and queued_by(player, bucket) >= limit["per_user"]
    ):
        return USER_FULL, {"max": limit["per_user"]}
    if limit["track_minutes"] and duration:
        if duration > limit["track_minutes"] * 60:
            return TOO_LONG, {
                "max": limit["track_minutes"],
                "minutes": math.ceil(duration / 60),
            }
    return None


def cooldown_remaining(guild_id, member_id, bypass=False):
    """Seconds (rounded up) before the member may `/play` again, 0 if now."""
    wait = limits_for(guild_id)["cooldown"]
    if not wait or bypass:
        return 0
    last = state.play_times.get((guild_id, member_id))
    if last is None:
        return 0
    return max(0, math.ceil(last + wait - state.clock()))


def start_cooldown(guild_id, member_id, bypass=False):
    """Record a `/play` call and forget cooldowns that already ran out."""
    if not limits_for(guild_id)["cooldown"] or bypass:
        return
    now = state.clock()
    for key in [k for k, at in state.play_times.items() if now - at > 86400]:
        del state.play_times[key]
    state.play_times[(guild_id, member_id)] = now


load_limits()
