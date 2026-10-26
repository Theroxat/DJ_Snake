"""Web sessions (expiry) and the status panel in the session's text channel."""

import asyncio
import secrets
import time

import discord

from . import state
from .config import (
    PUBLIC_BASE_URL,
    SESSION_CLEANUP_SECONDS,
    SESSION_IDLE_HOURS,
    VOLUME_DEFAULT,
)
from .i18n import lang_for, t
from .permissions import web_control_restricted
from .player import discard_tracks, guild_of, notify_state
from .volume import volume_for
from .voteskip import web_status

# --- Sessions ---------------------------------------------------------------


def _session_expired(player):
    if player.in_voice():
        return False  # Being in voice counts as activity
    return state.clock() - player.last_active > SESSION_IDLE_HOURS * 3600


def _new_id():
    return secrets.token_urlsafe(16)


def _drop_session(sid):
    """Forget `sid` and every view-only id pointing at it."""
    state.sessions.pop(sid, None)
    state.viewers.pop(sid, None)
    for wid in [w for w, s in state.watch_ids.items() if s == sid]:
        del state.watch_ids[wid]


def resolve_link(given):
    """(sid, can_control) for the id in a page or API URL: a session id gives
    control, a view-only id gives (its session id, False); anything else
    (None, False). Ids are bearer secrets: every live id is compared with
    secrets.compare_digest, so the time taken does not depend on how much of
    a guess matches. Ids are never logged."""
    if not isinstance(given, str) or not 0 < len(given) <= 64:
        return None, False
    given = given.encode("utf-8", "replace")
    found, control = None, False
    for sid in list(state.sessions):
        if secrets.compare_digest(sid.encode(), given):
            found, control = sid, True
    for wid, sid in list(state.watch_ids.items()):
        if secrets.compare_digest(wid.encode(), given):
            found, control = sid, False
    return found, control


def rotate_control_link(player):
    """Give a live session a new control id (the view-only id stays): the
    old control link, maybe posted publicly, stops working."""
    if not player.sid or player.sid not in state.sessions:
        return
    old = player.sid
    guild_id = state.sessions[old]
    new = _new_id()
    state.sessions.pop(old)
    state.sessions[new] = guild_id
    if old in state.viewers:
        state.viewers[new] = state.viewers.pop(old)
    for wid, sid in list(state.watch_ids.items()):
        if sid == old:
            state.watch_ids[wid] = new
    player.sid = new
    # `!theme edit` links point at the old control id: retire them too.
    for token, (gid, _) in list(state.edit_tokens.items()):
        if gid == guild_id:
            state.edit_tokens.pop(token, None)
    refresh_panel(player)


def expire_session(player):
    """End the session for good: its links stop working and its files go."""
    if player.sid:
        _drop_session(player.sid)
        print("Web session expired")
        player.sid = None
        player.watch_id = None
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
        player.drop_autoplay_next()
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
        _drop_session(sid)
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
        player.sid = _new_id()
        player.watch_id = _new_id()
        state.sessions[player.sid] = ctx.guild.id
        state.watch_ids[player.watch_id] = player.sid
        player.expired = False
        created = True
        print(f"Web session opened for {ctx.guild.name}")  # Never log the ids
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


def player_lang(player):
    """The language of a player's guild (its session's, or BOT_LANGUAGE once
    the player is replaced by !reset)."""
    guild_id = state.sessions.get(player.sid) if player.sid else None
    if guild_id is None:
        guild_id = guild_of(player)
    return lang_for(guild_id)


def _duration(seconds, lang):
    seconds = max(0, round(seconds or 0))
    if seconds >= 60:
        return t("panel.minutes", lang, count=round(seconds / 60))
    return t("panel.seconds", lang, count=seconds)


def public_link(player):
    """The clip page URL a public message may show: the control link when the
    guild leaves control to everyone, else the view-only one (None without a
    session)."""
    guild_id = state.sessions.get(player.sid) if player.sid else None
    if guild_id is None:
        return None
    if web_control_restricted(guild_id):
        return f"{PUBLIC_BASE_URL}/{player.watch_id}" if player.watch_id else None
    return f"{PUBLIC_BASE_URL}/{player.sid}"


def _link_lines(player, lang=None):
    """The panel's links. The view-only link is always public; the control
    link only when the guild leaves control to everyone (WEB_CONTROL, no DJ
    role), else DJs get it by DM with `!weblink`."""
    guild_id = state.sessions.get(player.sid) if player.sid else None
    if guild_id is None:
        return []
    watch = f"{PUBLIC_BASE_URL}/{player.watch_id}" if player.watch_id else None
    if web_control_restricted(guild_id):
        lines = [t("panel.watch", lang, url=watch)] if watch else []
        lines.append(t("panel.weblink_hint", lang))
        return lines
    lines = [t("panel.watch_control", lang, url=f"{PUBLIC_BASE_URL}/{player.sid}")]
    if watch:
        # In <>: Discord shows no second preview for it.
        lines.append(t("panel.view_only_link", lang, url=watch))
    return lines


def render_panel(player):
    lang = player_lang(player)
    if player.expired:
        return t("panel.expired", lang)
    lines = []
    lines.extend(_link_lines(player, lang))
    if not player.in_voice():
        lines.append(t("panel.idle", lang))
    elif player.lingering() and not player.now_playing and not player.queue:
        delay = _duration(player.idle_delay, lang)
        lines.append(t("panel.lingering", lang, delay=delay))
    elif player.now_playing:
        title = short(player.now_playing["title"])
        line = t("panel.now_playing", lang, title=title)
        if player.paused_at is not None:
            line += t("panel.paused", lang)
        if player.now_playing.get("autoplay"):
            line += t("panel.autoplay_track", lang)
        lines.append(line)
    else:
        lines.append(t("panel.nothing_playing", lang))
    if player.queue:
        line = t("panel.up_next", lang, title=short(player.queue[0]["title"]))
        if len(player.queue) > 1:
            line += t("panel.up_next_more", lang, count=len(player.queue) - 1)
        lines.append(line)
    if player.crossfade:
        lines.append(t("panel.crossfade_on", lang))
    if player.loop != "off":
        lines.append(t("panel.loop", lang, mode=t(f"loop.mode.{player.loop}", lang)))
    volume = volume_for(state.sessions.get(player.sid))
    if volume != VOLUME_DEFAULT:
        lines.append(t("panel.volume", lang, volume=volume))
    if player.autoplay:
        lines.append(t("panel.autoplay_on", lang))
    votes = web_status(player, state.sessions.get(player.sid))
    if votes:
        lines.append(t("panel.votes", lang, **votes))
    if player.load_line:
        lines.append(player.load_line)
    elif len(player.pending_downloads) > 1:
        count = len(player.pending_downloads)
        lines.append(t("panel.downloading_many", lang, count=count))
    elif player.single_downloads or player.pending_downloads:
        lines.append(t("panel.downloading", lang))
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
    PANEL_MIN_INTERVAL, and the last state wins. It also wakes the page's
    live streams (notify_state), panel or not."""
    notify_state(player)
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
    """Confirm a command: a check mark reaction on the user's message, or for
    a slash command a private "✅" (its context has no real message)."""
    interaction = getattr(ctx, "interaction", None)
    try:
        if interaction is None:
            message = getattr(ctx, "message", None)
            if message is not None:
                await message.add_reaction("✅")
        elif interaction.response.is_done():
            # Deferred: the first reply replaces the "thinking" message.
            await interaction.edit_original_response(content="✅")
        else:
            await ctx.send("✅", ephemeral=True)
    except Exception:
        pass
