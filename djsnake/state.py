"""Shared runtime state: the bot instance, players, sessions and themes.

Other modules reach it as module attributes (`state.music_players`), never
through `from .state import ...`, so tests can patch it in one place.
"""

import time

import discord
from discord.ext import commands

from .config import COMMAND_PREFIX

# Configure intents with all necessary permissions
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.guild_messages = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


def clock():
    """Wall clock for session expiry (a function so tests can fake it)."""
    return time.time()


# Process start (monotonic), for /healthz uptime.
started_at = time.monotonic()
# Monotonic counters exposed by /metrics.
counters = {"tracks_played": 0}

# Guild id -> MusicPlayer
music_players = {}
# Web session id (the control link's id) -> guild id. With `watch_ids`, the
# only way a request reaches a player: look ids up with
# sessions.resolve_link() (constant-time compare).
sessions = {}
# View-only link id -> web session id (one per session, dropped with it).
watch_ids = {}
# Guild id -> {"dj_role": role id}, persisted (see permissions.py).
guild_perms = {}

# Guild id -> stored theme record (see themes.py).
guild_themes = {}
# Guild id -> volume percent (see volume.py); absent means VOLUME_DEFAULT.
guild_volumes = {}
# Guild id -> vote-to-skip threshold percent (see voteskip.py); absent means
# config.VOTESKIP_PERCENT.
guild_voteskip = {}
# Guild id -> {field: value} limit overrides (see limits.py).
guild_limits = {}
# (guild id, member id) -> clock() of the last accepted /play (cooldown).
play_times = {}
# Guild id -> {"on": bool, "channel": voice channel id | None}, persisted
# (see stay.py).
guild_stay = {}
# Guild id -> clock() of the last 24/7 rejoin after a drop (see playback.py).
stay_retries = {}
# Guild id -> clock() when the bot left voice on purpose (!reset, !stop, a
# channel switch): the voice-state handler then does not take it for a drop.
deliberate_leaves = {}
# Guild ids with a `/playlist import` running: one at a time per guild.
importing = set()
# Guild id -> language code chosen with `!language` (see i18n.py).
guild_languages = {}
# `!theme edit` links: token -> (guild id, expiry on clock()). In memory only.
edit_tokens = {}

# Web session id -> {viewer id: last seen on clock()}, in poll order (see
# web.py). Dropped with the session.
viewers = {}
# Web session id -> set of open live update streams (web.LiveStream).
streams = {}

# Strong references to fire-and-forget tasks, so they are not collected.
background_tasks = set()
