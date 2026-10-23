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


# Guild id -> MusicPlayer
music_players = {}
# Web session id -> guild id. The only way a request reaches a player.
sessions = {}

# Guild id -> stored theme record (see themes.py).
guild_themes = {}
# Guild id -> language code chosen with `!language` (see i18n.py).
guild_languages = {}
# `!theme edit` links: token -> (guild id, expiry on clock()). In memory only.
edit_tokens = {}

# Web session id -> {viewer id: last seen on clock()}, in poll order (see
# web.py). Dropped with the session.
viewers = {}

# Strong references to fire-and-forget tasks, so they are not collected.
background_tasks = set()
