"""Bot events, startup and the entry point."""

import asyncio
import os

import discord

from . import commands  # noqa: F401  (registers the commands)
from . import state, stats
from .config import COMMAND_PREFIX
from .playback import idle_channel_emptied
from .sessions import refresh_panel, session_cleanup_loop
from .web import start_web_server

bot = state.bot


@bot.event
async def on_ready():
    print(f"{bot.user} is connected and ready!")
    print(f"Bot ID: {bot.user.id}")
    print(f"Connected to {len(bot.guilds)} guilds")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{COMMAND_PREFIX}help for command list",
        )
    )


@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state changes for cleanup"""
    _record_listeners(member, after)
    _check_lingering_alone(member, before)
    if member == bot.user and before.channel and not after.channel:
        print(f"Bot was disconnected from voice channel: {before.channel.name}")

        guild = before.channel.guild
        # The client's own handler runs in a separate task, so give it time to
        # settle. A stale event from an old disconnect (!stop then !play) then
        # sees a live client and is ignored; a real kick sees it disconnected.
        await asyncio.sleep(1.5)
        if guild.voice_client and guild.voice_client.is_connected():
            return
        if guild.id in state.music_players:
            player = state.music_players[guild.id]
            player.cancel_idle()
            player.voice_client = None
            player.is_playing = False
            refresh_panel(player)
            # The web session stays; it expires after SESSION_IDLE_HOURS.


def _check_lingering_alone(member, before):
    """A human left the bot's channel: an idle bot left alone leaves soon."""
    try:
        if member.bot or before.channel is None:
            return
        player = state.music_players.get(member.guild.id)
        if not player or not player.lingering() or not player.in_voice():
            return
        if before.channel != player.voice_client.channel:
            return
        idle_channel_emptied(_GuildContext(member.guild), player)
    except Exception as e:
        print(f"Idle check failed: {e}")


class _GuildContext:
    """Enough of a context for the idle timer (it only reads the guild)."""

    def __init__(self, guild):
        self.guild = guild


def _record_listeners(member, after):
    """Someone joined the bot's voice channel: maybe a new listener peak."""
    try:
        voice_client = member.guild.voice_client
        if (
            member.bot
            or after.channel is None
            or not voice_client
            or not voice_client.is_connected()
            or after.channel != voice_client.channel
        ):
            return
        stats.record_listeners(
            member.guild.id, stats.count_listeners(voice_client.channel)
        )
    except Exception as e:
        print(f"Listener count failed: {e}")


async def setup_hook():
    await start_web_server()
    task = asyncio.get_running_loop().create_task(session_cleanup_loop())
    state.background_tasks.add(task)


bot.setup_hook = setup_hook


def get_discord_token():
    """Get Discord token from environment variables or token file (fallback)"""
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        try:
            with open("token", "r") as f:
                TOKEN = f.read().strip()
            print(
                "⚠️  Warning: Using token file. Consider using DISCORD_TOKEN environment variable for better security."
            )
        except FileNotFoundError:
            print("❌ Error: No Discord token found!")
            print(
                "Please set DISCORD_TOKEN environment variable or create a 'token' file."
            )
            print("Get your token from: https://discord.com/developers/applications")
            exit(1)
    return TOKEN


def main():
    print("Starting Discord Music Bot (fixed version)")
    print("-" * 50)
    TOKEN = get_discord_token()
    try:
        bot.run(TOKEN)
    finally:
        stats.flush(timeout=5)  # Best effort: keep queued stats on shutdown
