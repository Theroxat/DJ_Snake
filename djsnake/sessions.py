"""Web sessions (expiry) and the status panel in the session's text channel."""

import asyncio
import secrets
import time

import discord

from . import state
from .config import (
    COMMAND_PREFIX,
    PUBLIC_BASE_URL,
    SESSION_CLEANUP_SECONDS,
    SESSION_IDLE_HOURS,
)
from .player import discard_tracks

# --- Sessions ---------------------------------------------------------------


def _session_expired(player):
    if player.in_voice():
        return False  # Being in voice counts as activity
    return state.clock() - player.last_active > SESSION_IDLE_HOURS * 3600


def expire_session(player):
    """End the session for good: its links stop working and its files go."""
    if player.sid:
        state.sessions.pop(player.sid, None)
        state.viewers.pop(player.sid, None)
        print(f"Web session expired: {player.sid}")
        player.sid = None
        player.expired = True
        refresh_panel(player)
    player.cancel_idle()
    if not player.in_voice():
        tracks = list(player.queue) + list(player.history)
        if player.now_playing:
            tracks.append(player.now_playing)
        player.queue.clear()
        player.history.clear()
        player.now_playing = None
        player.pending_downloads = []
        discard_tracks(tracks)


def touch(player):
    """Record activity. An already expired session is ended, not revived."""
    if player.sid and _session_expired(player):
        expire_session(player)
    player.last_active = state.clock()


def session_player(sid):
    """The player behind a live session id, or None (expires lazily)."""
    guild_id = state.sessions.get(sid)
    if guild_id is None:
        return None
    player = state.music_players.get(guild_id)
    if not player or player.sid != sid:
        state.sessions.pop(sid, None)
        state.viewers.pop(sid, None)
        return None
    if player.in_voice():
        player.last_active = state.clock()
    elif _session_expired(player):
        expire_session(player)
        return None
    return player


async def session_cleanup_loop():
    while True:
        await asyncio.sleep(SESSION_CLEANUP_SECONDS)
        try:
            for sid in list(state.sessions):
                session_player(sid)
        except Exception as e:
            print(f"Session cleanup error: {e}")


async def open_session(ctx, player, announce=True):
    """Reuse the guild's live session or start one, and post its status panel."""
    touch(player)
    created = False
    if not player.sid:
        player.sid = secrets.token_urlsafe(16)
        state.sessions[player.sid] = ctx.guild.id
        player.expired = False
        created = True
        print(f"Web session opened for {ctx.guild.name}: {player.sid}")
    if not (created or announce):
        return
    player.text_channel = ctx.channel
    await post_panel(player)


# --- Status panel -----------------------------------------------------------
# One message per session, edited in place instead of posting an announcement
# for every event. Every state change calls refresh_panel().

PANEL_MIN_INTERVAL = 1.5  # Seconds between edits (Discord rate limits)
_NO_MENTIONS = discord.AllowedMentions.none()


def short(title, limit=100):
    title = str(title)
    return title if len(title) <= limit else title[: limit - 1] + "…"


def _duration(seconds):
    seconds = max(0, round(seconds or 0))
    if seconds >= 60:
        return f"{round(seconds / 60)} min"
    return f"{seconds} s"


def render_panel(player):
    if player.expired:
        return "⌛ Session expired"
    lines = []
    if player.sid:
        lines.append(f"📺 Watch the clip live: {PUBLIC_BASE_URL}/{player.sid}")
    if not player.in_voice():
        lines.append(f"💤 Idle — {COMMAND_PREFIX}play to resume")
    elif player.lingering() and not player.now_playing and not player.queue:
        lines.append(
            f"💤 Queue empty — leaving in {_duration(player.idle_delay)} "
            "unless something is added"
        )
    elif player.now_playing:
        line = f"🎵 Now playing: **{short(player.now_playing['title'])}**"
        if player.paused_at is not None:
            line += " ⏸️ paused"
        lines.append(line)
    else:
        lines.append("🎵 Nothing playing yet")
    if player.queue:
        line = f"⏭️ Up next: {short(player.queue[0]['title'])}"
        if len(player.queue) > 1:
            line += f" and {len(player.queue) - 1} more"
        lines.append(line)
    if player.crossfade:
        lines.append("🔀 Crossfade on")
    if player.load_line:
        lines.append(player.load_line)
    elif len(player.pending_downloads) > 1:
        lines.append(f"📥 Downloading {len(player.pending_downloads)} tracks…")
    elif player.single_downloads or player.pending_downloads:
        lines.append("📥 Downloading…")
    if player.last_action:
        lines.append(player.last_action)
    return "\n".join(lines)


async def _send_panel(player):
    """Post the panel as a new message (caller holds panel_lock)."""
    text = render_panel(player)
    message = await player.text_channel.send(text, allowed_mentions=_NO_MENTIONS)
    player.panel_message = message
    player.panel_text = text
    player.panel_last_edit = time.monotonic()


async def post_panel(player):
    """Post a fresh panel at the bottom and delete the previous one."""
    if player.text_channel is None:
        return
    old = player.panel_message
    async with player.panel_lock:
        try:
            await _send_panel(player)
        except Exception as e:
            print(f"Could not post status panel: {e}")
            return
    if old is not None:
        try:
            await old.delete()
        except Exception:
            pass


async def _write_panel(player):
    async with player.panel_lock:
        text = render_panel(player)
        if player.panel_message is not None:
            if text == player.panel_text:
                return
            try:
                await player.panel_message.edit(
                    content=text, allowed_mentions=_NO_MENTIONS
                )
                player.panel_text = text
                return
            except discord.NotFound:
                player.panel_message = None  # Deleted by someone: post anew
            except Exception as e:
                print(f"Could not edit status panel: {e}")
                return
        if player.text_channel is None:
            return
        try:
            await _send_panel(player)
        except Exception as e:
            print(f"Could not post status panel: {e}")


async def _panel_worker(player):
    while player.panel_dirty:
        wait = player.panel_last_edit + PANEL_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        player.panel_dirty = False  # Changes during the edit trigger another
        await _write_panel(player)
        player.panel_last_edit = time.monotonic()


def refresh_panel(player):
    """Schedule a panel update. Coalesced: at most one edit per
    PANEL_MIN_INTERVAL, and the last state wins."""
    if player.text_channel is None:
        return
    player.panel_dirty = True
    if player.panel_task is not None and not player.panel_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    player.panel_task = loop.create_task(_panel_worker(player))


def set_action(player, text):
    """Show `text` as the panel's last-action line (None clears it)."""
    player.last_action = text
    if player.load_final:  # A finished load result lasts until the next change
        player.load_line, player.load_final = None, False
    refresh_panel(player)


async def ack(ctx):
    """React with a check mark on the user's command message."""
    message = getattr(ctx, "message", None)
    if message is None:
        return
    try:
        await message.add_reaction("✅")
    except Exception:
        pass
