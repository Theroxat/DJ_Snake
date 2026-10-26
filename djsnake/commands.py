"""Discord commands and command hooks."""

import asyncio
import math

import discord
from discord import app_commands
from discord.ext import commands

from . import catalog, i18n, media, spotify, state
from .config import (
    COMMAND_PREFIX,
    HISTORY_LOG_SIZE,
    LOOP_MODES,
    MAX_PLAYLIST_SIZE,
    MAX_SEARCH_LENGTH,
    PUBLIC_BASE_URL,
    THEME_EDIT_SECONDS,
    THEME_PRESETS,
    THEME_SKINS,
    WEB_THEME,
)
from .controls import (
    ChannelContext,
    ControlError,
    control_autoplay,
    control_clear,
    control_crossfade,
    control_loop,
    control_move,
    control_next,
    control_pause,
    control_playlist_add,
    control_playlist_delete,
    control_playlist_get,
    control_playlist_list,
    control_playlist_save,
    control_previous,
    control_remove,
    control_resume,
    control_seek,
    control_shuffle,
    control_volume,
    control_voteskip,
    control_voteskip_threshold,
    playlist_entries,
    read_spotify,
    seek_label,
)
from .i18n import ctx_lang, lang_for, language_name, t
from .media import get_video_id_from_url, looks_like_url
from .playback import (
    download_and_queue_song,
    join_voice_channel,
    queue_playlist,
    queue_search,
    queue_spotify,
)
from .player import MusicPlayer, discard_tracks, retire_play
from .sessions import (
    ack,
    open_session,
    player_lang,
    public_link,
    refresh_panel,
    rotate_control_link,
    session_player,
    set_action,
    short,
    touch,
)
from .permissions import (
    OWN_TRACK_COMMANDS,
    REQUESTER_COMMANDS,
    can_run_dj_command,
    dj_role,
    is_dj,
    owns_track,
    set_dj_role,
    web_control_restricted,
)
from .themes import issue_edit_token, reset_theme, save_themes, theme_for
from .volume import parse_volume, volume_for
from .voteskip import parse_percent, threshold_for

bot = state.bot


def hybrid(name, description, **kwargs):
    """Register a command that works as `!name` and as `/name`.

    Every command goes through this decorator (see "Adding a command" in
    CLAUDE.md). It is guild-only. Put `@app_commands.describe(...)` /
    `@app_commands.choices(...)` under it; the `ctx` helpers below make the
    reply paths work for both kinds of context.
    """

    def decorate(func):
        command = bot.hybrid_command(name=name, description=description, **kwargs)(func)
        return commands.guild_only()(command)

    return decorate


async def defer(ctx):
    """Acknowledge a slash command that may take over 3 s (no-op for `!`).
    The thinking message is private; ack() or the first reply replaces it."""
    if getattr(ctx, "interaction", None) is not None:
        await ctx.defer(ephemeral=True)


async def fail(ctx, text, **kwargs):
    """Report a user mistake: private on a slash command, a plain reply on `!`."""
    await ctx.send(f"❌ {text}", ephemeral=True, **kwargs)


async def fail_t(ctx, key, **params):
    """fail() with the `key` message (which starts with ❌) in the guild's
    language."""
    await ctx.send(t(key, ctx_lang(ctx), **params), ephemeral=True)


def error_message(error, command=None, slash=False, guild=None):
    """The message for a command error in the guild's language, or None to
    stay silent. Shared by the prefix handler and the slash command tree."""
    lang = lang_for(getattr(guild, "id", None))
    prefix = "/" if slash else COMMAND_PREFIX
    if isinstance(error, (commands.HybridCommandError, commands.CommandInvokeError)):
        error = getattr(error, "original", error)
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, NotDJ):
        name = getattr(command, "name", command) or ""
        extra = ""
        if name in REQUESTER_COMMANDS:
            extra = t("cmd.not_dj_requester", lang)
        elif name in OWN_TRACK_COMMANDS:
            extra = t("cmd.not_dj_owner", lang)
        need = _dj_requirement(guild, lang)
        hint = ""
        if name == "next" and guild is not None and dj_role(guild.id) is not None:
            hint = t("cmd.not_dj_voteskip", lang, prefix=prefix)
        return t(
            "cmd.not_dj",
            lang,
            prefix=prefix,
            name=name,
            need=need,
            extra=extra,
            hint=hint,
        )
    if isinstance(error, (commands.CommandNotFound, app_commands.CommandNotFound)):
        return None
    if isinstance(error, commands.MissingRequiredArgument):
        return t("cmd.missing_argument", lang)
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return t("cmd.guild_only", lang)
    if isinstance(
        error, (commands.MissingPermissions, app_commands.MissingPermissions)
    ):
        return t("cmd.missing_permission", lang, prefix=prefix, command=command)
    return t("cmd.error_generic", lang, error=error)


class PlaylistChoiceView(discord.ui.View):
    """Buttons shown when a link contains both a specific song and a playlist,
    letting the user pick just the song or the whole playlist."""

    def __init__(self, author_id, timeout=30, lang=None):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.lang = lang
        self.choice = None  # "single" | "playlist" | None (timeout)
        self.single.label = t("cmd.choice_single", lang)
        self.playlist.label = t("cmd.choice_playlist", lang)

    async def interaction_check(self, interaction):
        # Only the person who ran the command may use the buttons.
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                t("cmd.not_your_command", self.lang), ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="This track only", emoji="🎵", style=discord.ButtonStyle.primary
    )
    async def single(self, interaction, button):
        self.choice = "single"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(
        label="Whole playlist", emoji="📋", style=discord.ButtonStyle.secondary
    )
    async def playlist(self, interaction, button):
        self.choice = "playlist"
        await interaction.response.defer()
        self.stop()


class NotDJ(commands.CheckFailure):
    """A member without the DJ role ran one of DJ_COMMANDS."""


def _dj_requirement(guild, lang=None):
    """How to become a DJ, for error messages."""
    if guild is None:
        return t("cmd.dj_need_role_unknown", lang)
    role_id = dj_role(guild.id)
    role = guild.get_role(role_id) if role_id is not None else None
    if role is not None:
        return t("cmd.dj_need_role", lang, role=role.name)
    return t("cmd.dj_need_manager", lang)


@bot.check
async def dj_gate(ctx):
    """The one Discord permission gate: every command in DJ_COMMANDS needs a
    DJ (see permissions.py); the member who queued the playing track may
    still skip it."""
    if ctx.guild is None or ctx.command is None:
        return True
    player = state.music_players.get(ctx.guild.id)
    if can_run_dj_command(ctx.author, ctx.guild.id, ctx.command.name, player):
        return True
    raise NotDJ()


def require_dj(ctx):
    """For DJ_WHEN_SETTING commands: raise NotDJ unless the author may change
    the value (showing it stays open to everyone)."""
    if ctx.guild is not None and not is_dj(ctx.author, ctx.guild.id):
        raise NotDJ()


def require_dj_or_owner(ctx, track):
    """For OWN_TRACK_COMMANDS: raise NotDJ unless the author is a DJ or queued
    `track` themselves."""
    if ctx.guild is None or is_dj(ctx.author, ctx.guild.id):
        return
    if not owns_track(ctx.author, track):
        raise NotDJ()


@bot.before_invoke
async def record_command_activity(ctx):
    if ctx.guild and ctx.guild.id in state.music_players:
        touch(state.music_players[ctx.guild.id])


@hybrid("play", "Queue a music link, or the best YouTube match for some text")
@app_commands.describe(
    query="YouTube, SoundCloud, Bandcamp, Spotify, Deezer or Apple Music link, "
    "or text to search on YouTube"
)
async def play(ctx, *, query: str = None):
    await defer(ctx)  # Joining voice and reading the link take over 3 s
    lang = ctx_lang(ctx)
    query = (query or "").strip()
    if not query:
        await fail_t(ctx, "cmd.play_usage")
        return

    text_search = not looks_like_url(query)
    # A link is read up to the first space, as before.
    url = query if text_search else query.split()[0]
    if text_search and len(query) > MAX_SEARCH_LENGTH:
        await fail_t(ctx, "cmd.search_too_long", max=MAX_SEARCH_LENGTH)
        return

    try:
        # Spotify, Deezer or Apple Music
        spotify_ref = None if text_search else catalog.check_link(url)
    except spotify.SpotifyError as e:
        await fail(ctx, ControlError(e.code).text(lang))
        return
    if not text_search and spotify_ref is None:
        # As the page's add: only allowlisted hosts, rebuilt canonically,
        # ever reach yt-dlp.
        url = media.parse_play_url(url)
        if url is None:
            await fail(ctx, ControlError("bad_url").text(lang))
            return

    if not await join_voice_channel(ctx):
        return

    if ctx.guild.id not in state.music_players:
        state.music_players[ctx.guild.id] = MusicPlayer()

    player = state.music_players[ctx.guild.id]
    player.should_stop = False

    if spotify_ref is not None:
        await _play_spotify(ctx, player, *spotify_ref)
        return

    if text_search:
        await ack(ctx)
        ctx = ChannelContext(ctx)  # The search outlives the interaction token
        try:
            await queue_search(ctx, player, query)
        except Exception as e:
            await ctx.send(t("cmd.error", lang, error=e))
        return

    try:
        await ack(ctx)
        # From here on only the channel is needed: an interaction's reply
        # token expires after 15 minutes, a playlist or the idle timer outlives it.
        ctx = ChannelContext(ctx)
        player.load_line = t("cmd.analyzing_link", lang)
        player.load_final = False
        refresh_panel(player)

        # Try to extract as playlist
        loop = asyncio.get_running_loop()
        key = player.add_pending(None)  # Shown on the page as a pending link
        try:
            playlist_entries = await loop.run_in_executor(
                None, media.get_playlist_info, url
            )
        finally:
            player.drop_pending(key)
            player.load_line = None
            refresh_panel(player)

        if playlist_entries:
            # It's a playlist
            # If the link targets a specific song *and* carries a playlist/radio
            # (e.g. watch?v=XXX&list=RD...), ask the user what they want.
            specific_video_id = get_video_id_from_url(url)
            if specific_video_id:
                view = PlaylistChoiceView(ctx.author.id, lang=lang)
                prompt = await ctx.send(
                    t("cmd.playlist_prompt", lang, count=len(playlist_entries)),
                    view=view,
                )
                await view.wait()
                try:
                    await prompt.delete()
                except Exception:
                    pass

                # Default to the single track on timeout (choice is None).
                if view.choice != "playlist":
                    single_url = f"https://www.youtube.com/watch?v={specific_video_id}"
                    if not player.should_stop:
                        await download_and_queue_song(ctx, player, single_url)
                    return
                # Otherwise fall through and load the whole playlist.

            # Make sure we're connected
            if not player.voice_client or not player.voice_client.is_connected():
                if not await join_voice_channel(ctx):
                    await ctx.send(t("cmd.cannot_join", lang))
                    return

            # Limit based on configuration
            playlist_entries = playlist_entries[:MAX_PLAYLIST_SIZE]
            await queue_playlist(ctx, player, playlist_entries)
        else:
            # It's a single video
            if not player.should_stop:
                await download_and_queue_song(ctx, player, url)

    except Exception as e:
        await ctx.send(t("cmd.error", lang, error=e))


async def _play_spotify(ctx, player, kind, ref, provider="spotify"):
    """!play with a Spotify, Deezer or Apple Music link: read its track list,
    then queue the YouTube match of each track."""
    lang = ctx_lang(ctx)
    try:
        await ack(ctx)
        ctx = ChannelContext(ctx)
        label = catalog.LABELS[provider]
        player.load_line = t("cmd.reading_link", lang, service=label)
        player.load_final = False
        refresh_panel(player)
        try:
            tracks = await read_spotify(kind, ref, provider)
        finally:
            player.load_line = None
            refresh_panel(player)
        if not player.should_stop:
            await queue_spotify(ctx, player, kind, tracks)
    except ControlError as e:
        await ctx.send(f"❌ {e.text(lang)}")
    except Exception as e:
        await ctx.send(t("cmd.error", lang, error=e))


@hybrid("stop", "Stop, clear the queue and leave voice")
async def stop(ctx):
    await defer(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await fail(ctx, ControlError("nothing_playing").text(ctx_lang(ctx)))
        return

    async with player.lock:
        retire_play(player, ctx.guild.id, "stop")
        player.stop_and_clear()
    await player.disconnect_voice_client()
    # The web session stays (same link on the next !play) until it expires.
    if not player.loading:
        player.load_line, player.load_final = None, False
    set_action(player, t("action.stopped", player_lang(player)))
    await ack(ctx)


@hybrid("reset", "Force a clean voice reconnect")
async def reset_voice(ctx):
    """Force reset the voice connection"""
    lang = ctx_lang(ctx)
    await ctx.send(t("cmd.reset_start", lang))

    player = state.music_players.get(ctx.guild.id)

    # Disconnect completely
    if ctx.guild.voice_client:
        await ctx.guild.voice_client.disconnect(force=True)

    # Clean the player. The web session survives a reset: same link.
    if player:
        player.cancel_idle()
        player.should_stop = True
        player.epoch += 1
        await player.disconnect_voice_client()
        fresh = MusicPlayer()
        fresh.sid = player.sid
        fresh.watch_id = player.watch_id
        fresh.text_channel = player.text_channel
        fresh.last_active = player.last_active
        fresh.history = player.history
        fresh.played = player.played
        fresh.panel_message = player.panel_message
        fresh.panel_text = player.panel_text
        fresh.expired = player.expired
        fresh.crossfade = player.crossfade
        fresh.loop = player.loop
        fresh.autoplay = player.autoplay
        fresh.recent_ids = player.recent_ids
        if player.panel_task is not None:
            player.panel_task.cancel()
        state.music_players[ctx.guild.id] = fresh
        leftovers = list(player.queue)
        if player.now_playing:
            leftovers.append(player.now_playing)
        player.queue = []
        player.history = []
        player.now_playing = None
        player.drop_autoplay_next()
        discard_tracks(leftovers)

    # Wait a bit (the voice state update arrives in the meantime)
    await asyncio.sleep(2)
    if player:
        refresh_panel(state.music_players[ctx.guild.id])
    await ctx.send(t("cmd.reset_done", lang))


@hybrid("list", "Show the queue")
async def list_queue(ctx):
    lang = ctx_lang(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player or not player.queue:
        await fail_t(ctx, "cmd.queue_empty")
        return

    queue_items = [f"{i+1}. {song['title']}" for i, song in enumerate(player.queue)]
    message = t("cmd.queue_title", lang) + "\n" + "\n".join(queue_items[:10])

    if len(player.queue) > 10:
        message += t("cmd.queue_more", lang, count=len(player.queue) - 10)

    await ctx.send(message)


_FAILED = object()


async def _run_command_control(ctx, control):
    """Run a control for a Discord command; returns its result (possibly
    None) or _FAILED after replying with the error."""
    lang = ctx_lang(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await fail(ctx, ControlError("nothing_playing").text(lang))
        return _FAILED
    try:
        result = await control(player)
    except ControlError as e:
        await fail(ctx, e.text(lang))
        return _FAILED
    await ack(ctx)
    refresh_panel(player)
    return result


@hybrid("next", "Skip to the next track")
async def next_track(ctx):
    # Stopping triggers the after_playing callback, which plays the next track.
    if await _run_command_control(ctx, control_next) is not _FAILED:
        player = state.music_players[ctx.guild.id]
        set_action(player, t("action.skipped", player_lang(player)))


@hybrid("voteskip", "Vote to skip the current track", aliases=["vs"])
@app_commands.describe(
    setting="threshold to set the share of votes needed (DJ only)",
    percent="1 to 100 (with threshold)",
)
@app_commands.choices(
    setting=[app_commands.Choice(name="threshold", value="threshold")]
)
async def voteskip(ctx, setting: str = None, percent: str = None):
    """Vote to skip the current track, or set how many votes it takes."""
    lang = ctx_lang(ctx)
    if setting is not None:
        if setting.lower() != "threshold":
            await fail_t(ctx, "cmd.voteskip_usage")
            return
        if percent is None:
            value = threshold_for(ctx.guild.id)
            await ctx.send(t("cmd.voteskip_info", lang, percent=value), ephemeral=True)
            return
        value = parse_percent(percent)
        if value is None:
            await fail_t(ctx, "cmd.voteskip_usage")
            return
        require_dj(ctx)
        try:
            control_voteskip_threshold(ctx.guild.id, value)
        except ControlError as e:
            await ctx.send(f"⚠️ {e.text(lang)}", ephemeral=True)
            return
        await ack(ctx)
        return
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await fail(ctx, ControlError("nothing_playing").text(lang))
        return
    touch(player)
    if can_run_dj_command(ctx.author, ctx.guild.id, "next", player):
        # A DJ, or the requester of this track: skip at once, as !next.
        await next_track.callback(ctx)
        return
    try:
        await control_voteskip(player, ctx.guild.id, ctx.author, ctx.channel)
    except ControlError as e:
        await fail(ctx, e.text(lang))
        return
    await ack(ctx)


def parse_position(text):
    """Seconds from "90", "1:30" or "1:02:03"; None if invalid."""
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(not math.isfinite(p) or p < 0 for p in parts):
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


@hybrid("seek", "Jump to a position in the current track")
@app_commands.describe(
    position="Seconds (90) or mm:ss (1:30), as the clip page shows it"
)
async def seek(ctx, position: str):
    seconds = parse_position(position)
    if seconds is None:
        await fail_t(ctx, "cmd.bad_position")
        return

    def relative_seek(player):
        # The position counts from the automix start, as the page shows it.
        start = (player.now_playing or {}).get("start") or 0.0
        return control_seek(player, start + seconds)

    applied = await _run_command_control(ctx, relative_seek)
    if applied is not _FAILED:
        player = state.music_players[ctx.guild.id]
        set_action(player, seek_label(player, applied))


def _queue_position(player, position):
    """The queued track at the 1-based `position` (as /list shows it)."""
    if not player.queue:
        raise ControlError("queue_empty")
    if position is None or not 1 <= position <= len(player.queue):
        raise ControlError("bad_index")
    return player.queue[position - 1]


async def _queue_autocomplete(interaction, current):
    """Queued tracks as "position. title", for /remove and /move."""
    player = (
        state.music_players.get(interaction.guild.id) if interaction.guild else None
    )
    current = (current or "").strip().lower()
    choices = []
    for i, track in enumerate(player.queue if player else [], 1):
        label = f"{i}. {track['title']}"
        if current and current not in label.lower():
            continue
        choices.append(app_commands.Choice(name=short(label), value=i))
        if len(choices) == 25:
            break
    return choices


@hybrid("remove", "Remove a track from the queue (position shown by /list)")
@app_commands.describe(position="Position in the queue, as /list shows it (1 = next)")
async def remove(ctx, position: int):
    lang = ctx_lang(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await fail(ctx, ControlError("nothing_playing").text(lang))
        return
    try:
        track = _queue_position(player, position)
        require_dj_or_owner(ctx, track)
        removed = await control_remove(player, track["id"])
    except ControlError as e:
        await fail(ctx, e.text(lang))
        return
    await ack(ctx)
    set_action(player, t("action.removed", lang, title=short(removed["title"])))


@remove.autocomplete("position")
async def remove_autocomplete(interaction, current):
    return await _queue_autocomplete(interaction, current)


@hybrid("move", "Move a queued track to another position")
@app_commands.describe(
    from_position="Current position of the track, as /list shows it",
    to_position="New position in the queue (1 = next)",
)
async def move(ctx, from_position: int, to_position: int):
    lang = ctx_lang(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await fail(ctx, ControlError("nothing_playing").text(lang))
        return
    try:
        track = _queue_position(player, from_position)
        _queue_position(player, to_position)
        await control_move(player, track["id"], to_position - 1)
    except ControlError as e:
        await fail(ctx, e.text(lang))
        return
    await ack(ctx)
    label = t("action.moved", lang, title=short(track["title"]), position=to_position)
    set_action(player, label)


@move.autocomplete("from_position")
async def move_from_autocomplete(interaction, current):
    return await _queue_autocomplete(interaction, current)


@move.autocomplete("to_position")
async def move_to_autocomplete(interaction, current):
    return await _queue_autocomplete(interaction, current)


@hybrid("previous", "Go back to the previous track (or restart this one)")
async def previous(ctx):
    action = await _run_command_control(ctx, control_previous)
    if action is not _FAILED:
        player = state.music_players[ctx.guild.id]
        key = "action.restarted" if action == "restart" else "action.previous"
        set_action(player, t(key, player_lang(player)))


@hybrid("clear", "Empty the upcoming queue (the current track keeps playing)")
async def clear(ctx):
    removed = await _run_command_control(ctx, control_clear)
    if removed is not _FAILED:
        player = state.music_players[ctx.guild.id]
        set_action(player, t("action.cleared", player_lang(player), count=len(removed)))


def progress_bar(elapsed, total, width=15):
    """A text bar: a marker on a line, placed by elapsed / total."""
    if not total or total <= 0:
        return "▬" * width
    ratio = min(1.0, max(0.0, (elapsed or 0.0) / total))
    spot = min(width - 1, int(ratio * width))
    return "▬" * spot + "🔘" + "▬" * (width - spot - 1)


def now_playing_embed(player, guild_id, lang=None):
    """The /nowplaying embed for the playing track."""
    track = player.now_playing
    start = track.get("start") or 0.0
    end = track.get("end") or track.get("duration")
    total = max(0.0, end - start) if end else None
    elapsed = min(max(0.0, (player.elapsed() or 0.0) - start), total or 1e12)
    embed = discord.Embed(title=short(track["title"], 256), color=0x5865F2)
    if track.get("url"):
        embed.url = track["url"]
    times = f"{_mmss(elapsed)} / {_mmss(total)}" if total else _mmss(elapsed)
    paused = t("np.paused", lang) if player.paused_at is not None else ""
    embed.description = f"{progress_bar(elapsed, total)}\n`{times}`{paused}"
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])
    requester = track.get("requester") or t("np.web", lang)
    embed.add_field(name=t("np.requester", lang), value=short(requester, 100))
    source = media.source_of(track.get("url"))
    embed.add_field(name=t("np.source", lang), value=t(f"np.source.{source}", lang))
    on, off = t("np.on", lang), t("np.off", lang)
    embed.add_field(name=t("np.loop", lang), value=t(f"loop.mode.{player.loop}", lang))
    embed.add_field(name=t("np.autoplay", lang), value=on if player.autoplay else off)
    embed.add_field(name=t("np.volume", lang), value=f"{volume_for(guild_id)} %")
    link = public_link(player)
    if link:
        embed.add_field(name=t("np.clip_page", lang), value=link, inline=False)
    return embed


@hybrid("nowplaying", "Show the playing track and its progress", aliases=["np"])
async def nowplaying(ctx):
    lang = ctx_lang(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player or not player.now_playing or not player.is_playing:
        await fail(ctx, ControlError("nothing_playing").text(lang))
        return
    await ctx.send(embed=now_playing_embed(player, ctx.guild.id, lang))


HISTORY_DEFAULT = 10
HISTORY_MAX = HISTORY_LOG_SIZE


@hybrid("history", "Show the last tracks played in this session")
@app_commands.describe(count="How many tracks to show (default 10, max 25)")
async def history(ctx, count: int = HISTORY_DEFAULT):
    lang = ctx_lang(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player or not player.played:
        await fail_t(ctx, "cmd.history_empty")
        return
    count = min(max(1, count), HISTORY_MAX)
    lines = []
    for i, track in enumerate(reversed(player.played[-count:]), 1):
        line = f"{i}. {short(track['title'] or '?', 80)}"
        if track.get("duration"):
            line += f" `{_mmss(track['duration'])}`"
        if track.get("requester"):
            line += f" — {short(track['requester'], 32)}"
        lines.append(line)
    await ctx.send(
        _fit(t("cmd.history_title", lang), lines, lang),
        allowed_mentions=_NO_MENTIONS,
    )


@hybrid("crossfade", "Turn crossfade on or off for this session")
@app_commands.describe(mode="on or off")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def crossfade(ctx, mode: str = None):
    mode = (mode or "").lower()
    if mode not in ("on", "off"):
        await fail_t(ctx, "cmd.crossfade_usage")
        return
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    control_crossfade(player, mode == "on")
    await ack(ctx)


@hybrid("loop", "Set the loop mode (off, track, queue); empty cycles")
@app_commands.describe(mode="off, track or queue (leave empty to cycle)")
@app_commands.choices(mode=[app_commands.Choice(name=m, value=m) for m in LOOP_MODES])
async def loop_command(ctx, mode: str = None):
    """Set the loop mode (off, track, queue); with no argument, cycle."""
    mode = mode.lower() if mode else None
    if mode is not None and mode not in LOOP_MODES:
        await fail_t(ctx, "cmd.loop_usage")
        return
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    control_loop(player, mode)
    await ack(ctx)


@hybrid("shuffle", "Shuffle the upcoming queue once")
async def shuffle(ctx):
    if await _run_command_control(ctx, control_shuffle) is not _FAILED:
        player = state.music_players[ctx.guild.id]
        set_action(player, t("action.shuffled", player_lang(player)))


@hybrid("volume", "Show the server's volume, or set it (0-150 %, 100 = normal)")
@app_commands.describe(level="0 to 150 (percent); leave empty to show it")
async def volume(ctx, level: str = None):
    """Show the server's volume, or set it (0-150 %, 100 = normal)."""
    lang = ctx_lang(ctx)
    if level is None:
        info = t("cmd.volume_info", lang, volume=volume_for(ctx.guild.id))
        await ctx.send(info, ephemeral=True)
        return
    percent = parse_volume(level)
    if percent is None:
        await fail_t(ctx, "cmd.volume_usage")
        return
    require_dj(ctx)
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    try:
        control_volume(player, ctx.guild.id, percent)
    except ControlError as e:
        await ctx.send(f"⚠️ {e.text(lang)}", ephemeral=True)
        return
    await ack(ctx)


@hybrid("autoplay", "Keep playing related tracks when the queue runs out")
@app_commands.describe(mode="on or off (leave empty to show the setting)")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ]
)
async def autoplay(ctx, mode: str = None):
    """Keep playing related tracks when the queue runs out."""
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    mode = (mode or "").lower()
    if mode not in ("on", "off"):
        lang = ctx_lang(ctx)
        key = "cmd.autoplay_info_on" if player.autoplay else "cmd.autoplay_info_off"
        await ctx.send(t(key, lang), ephemeral=True)
        return
    require_dj(ctx)
    control_autoplay(player, mode == "on")
    await ack(ctx)


@hybrid("theme", "Show or set the clip page theme of this server")
@commands.has_guild_permissions(manage_guild=True)
@app_commands.describe(
    choice="A preset, reset or edit (leave empty to show the current theme)"
)
async def theme(ctx, choice: str = None):
    """Show or set the clip page's default theme for this server."""
    await defer(ctx)
    guild_id = ctx.guild.id
    lang = ctx_lang(ctx)
    choice = (choice or "").strip().lower()
    player = state.music_players.get(guild_id)
    if not choice:
        current = theme_for(guild_id)["skin"]
        if guild_id in state.guild_themes:
            source = t("cmd.theme_source_server", lang)
        else:
            source = t("cmd.theme_source_fallback", lang)
        await ctx.send(
            t(
                "cmd.theme_info",
                lang,
                current=current,
                source=source,
                presets=", ".join(THEME_SKINS),
                default=WEB_THEME,
            )
        )
        return

    if choice == "edit":
        player = _live_session(guild_id)
        # The editor lives on the session's page: open one if needed (as a
        # join does, without joining voice).
        await open_session(ctx, player, announce=False)
        token = issue_edit_token(guild_id)
        try:
            await ctx.author.send(
                t(
                    "cmd.theme_editor_dm",
                    lang,
                    guild=ctx.guild.name,
                    minutes=THEME_EDIT_SECONDS // 60,
                    url=f"{PUBLIC_BASE_URL}/{player.sid}?edit={token}",
                )
            )
        except discord.HTTPException:
            state.edit_tokens.pop(token, None)
            await fail_t(ctx, "cmd.theme_dm_failed")
            return
        await ack(ctx)
        return

    try:
        if choice == "reset":
            reset_theme(guild_id)
            label = WEB_THEME
        elif choice in THEME_SKINS:
            state.guild_themes.setdefault(guild_id, {})["skin"] = choice
            save_themes()
            label = choice
        else:
            await fail_t(ctx, "cmd.theme_unknown", presets=", ".join(THEME_SKINS))
            return
    except OSError as e:
        print(f"Could not save theme: {e}")
        await fail_t(ctx, "cmd.theme_save_failed")
        return
    await ack(ctx)
    if player:
        set_action(player, t("action.theme", player_lang(player), theme=label))


def _language_list():
    return ", ".join(f"`{code}` ({language_name(code)})" for code in i18n.LANGUAGES)


@bot.hybrid_command(
    name="language",
    aliases=["lang", "langue"],
    description="Show or set the language of the bot and the clip page",
)
@commands.guild_only()
@commands.has_guild_permissions(manage_guild=True)
@app_commands.describe(choice="A language code (en, fr), or reset")
async def language(ctx, choice: str = None):
    """Show or set the language of the bot and the clip page here."""
    guild_id = ctx.guild.id
    lang = ctx_lang(ctx)
    choice = (choice or "").strip().lower()
    if not choice:
        if guild_id in state.guild_languages:
            source = t("cmd.theme_source_server", lang)
        else:
            source = t("cmd.theme_source_fallback", lang)
        await ctx.send(
            t(
                "cmd.language_info",
                lang,
                current=language_name(lang),
                source=source,
                languages=_language_list(),
                default=i18n.DEFAULT_LANGUAGE,
            )
        )
        return
    if choice == "reset":
        code = None
    else:
        code = i18n.normalize(choice)
        if code is None:
            await fail_t(ctx, "cmd.language_unknown", languages=_language_list())
            return
    try:
        i18n.set_language(guild_id, code)
    except OSError as e:
        print(f"Could not save language: {e}")
        await fail_t(ctx, "cmd.language_save_failed")
        return
    await ack(ctx)
    player = state.music_players.get(guild_id)
    if player:
        new = player_lang(player)
        set_action(player, t("action.language", new, language=language_name(new)))


def _live_session(guild_id):
    """The guild's player with a live web session, opened by the caller with
    open_session() if needed (an expired sid is dropped here)."""
    player = state.music_players.setdefault(guild_id, MusicPlayer())
    if player.sid and session_player(player.sid) is None:
        player.sid = None  # Expired: open_session makes a fresh one
        player.watch_id = None
    return player


@hybrid("djrole", "Show or set the role that may control the music")
@commands.has_guild_permissions(manage_guild=True)
@app_commands.describe(choice="A role (name, ID or mention), or off")
async def djrole(ctx, *, choice: str = None):
    """Show or set the role that may control the music (Manage Server)."""
    guild_id = ctx.guild.id
    lang = ctx_lang(ctx)
    choice = (choice or "").strip()
    if not choice:
        role_id = dj_role(guild_id)
        if role_id is None:
            current = t("cmd.djrole_none", lang)
        else:
            role = ctx.guild.get_role(role_id)
            current = f"**{role.name}**" if role else t("cmd.djrole_deleted", lang)
        await ctx.send(
            t("cmd.djrole_info", lang, current=current),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    role = None
    if choice.lower() != "off":
        try:
            role = await commands.RoleConverter().convert(ctx, choice)
        except commands.BadArgument:
            await fail_t(ctx, "cmd.djrole_not_found")
            return
        if role.is_default():
            await ctx.send(
                t("cmd.djrole_everyone", lang),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

    try:
        set_dj_role(guild_id, role.id if role else None)
    except OSError as e:
        print(f"Could not save DJ role: {e}")
        await fail_t(ctx, "cmd.djrole_save_failed")
        return
    player = state.music_players.get(guild_id)
    if player:
        # Who may control changed: the control link (maybe posted publicly,
        # maybe held by former DJs) stops working, the view-only link stays.
        if player.sid and session_player(player.sid) is not None:
            rotate_control_link(player)
        panel_lang = player_lang(player)
        if role:
            set_action(player, t("action.djrole", panel_lang, role=role.name))
        else:
            set_action(player, t("action.djrole_removed", panel_lang))
    await ack(ctx)


@hybrid("weblink", "Get the clip page links by direct message")
async def weblink(ctx):
    """DM the clip page's control link (DJs only when control is limited)."""
    guild_id = ctx.guild.id
    if web_control_restricted(guild_id) and not is_dj(
        ctx.author, guild_id, strict=True
    ):
        raise NotDJ()
    player = _live_session(guild_id)
    # As `!theme edit`: open a session (and its panel) without joining voice.
    await open_session(ctx, player, announce=False)
    try:
        await ctx.author.send(
            t(
                "cmd.weblink_dm",
                ctx_lang(ctx),
                guild=ctx.guild.name,
                control=f"{PUBLIC_BASE_URL}/{player.sid}",
                watch=f"{PUBLIC_BASE_URL}/{player.watch_id}",
            )
        )
    except discord.HTTPException:
        await fail_t(ctx, "cmd.weblink_dm_failed")
        return
    await ack(ctx)


@hybrid("pause", "Pause playback")
async def pause(ctx):
    await _run_command_control(ctx, control_pause)


@hybrid("resume", "Resume playback")
async def resume(ctx):
    await _run_command_control(ctx, control_resume)


@theme.autocomplete("choice")
async def theme_autocomplete(interaction, current):
    options = THEME_PRESETS + ("reset", "edit")
    current = current.strip().lower()
    return [app_commands.Choice(name=o, value=o) for o in options if current in o][:25]


# --- Saved playlists ------------------------------------------------------------

_NO_MENTIONS = discord.AllowedMentions.none()


def _can_manage(ctx):
    perms = getattr(ctx.author, "guild_permissions", None)
    return bool(getattr(perms, "manage_guild", False))


def _fit(header, lines, lang=None, limit=1900):
    """A message of `header` and as many `lines` as fit Discord's limit."""
    text = header
    for i, line in enumerate(lines):
        if len(text) + len(line) + 30 > limit:
            return text + t("cmd.queue_more", lang, count=len(lines) - i)
        text += "\n" + line
    return text


def _mmss(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


async def _playlist_reply(ctx, call):
    """Await `call`; returns its result, or _FAILED after replying with the
    error."""
    try:
        return await call
    except ControlError as e:
        await fail(ctx, e.text(ctx_lang(ctx)))
        return _FAILED


@bot.hybrid_group(
    name="playlist",
    description="Saved playlists of this server",
    invoke_without_command=True,
)
@commands.guild_only()
async def playlist(ctx):
    await fail_t(ctx, "cmd.playlist_usage")


@playlist.command(name="save", description="Save the current track and the queue")
@app_commands.describe(name="Playlist name")
async def playlist_save(ctx, *, name: str = None):
    """Save the current track and the queue under a name."""
    if name is None:
        await fail_t(ctx, "cmd.playlist_usage_name", sub="save")
        return
    player = state.music_players.get(ctx.guild.id)
    if player is None:
        await fail(ctx, ControlError("nothing_playing").text(ctx_lang(ctx)))
        return
    result = await _playlist_reply(
        ctx,
        control_playlist_save(
            player, ctx.guild.id, name, ctx.author.id, _can_manage(ctx)
        ),
    )
    if result is _FAILED:
        return
    await ack(ctx)
    set_action(
        player,
        t(
            "action.playlist_saved",
            player_lang(player),
            name=result["name"],
            count=result["count"],
        ),
    )


@playlist.command(name="add", description="Add the current track to a playlist")
@app_commands.describe(name="Playlist name")
async def playlist_add(ctx, *, name: str = None):
    """Add the current track to a saved playlist."""
    if name is None:
        await fail_t(ctx, "cmd.playlist_usage_name", sub="add")
        return
    player = state.music_players.get(ctx.guild.id)
    if player is None:
        await fail(ctx, ControlError("nothing_playing").text(ctx_lang(ctx)))
        return
    result = await _playlist_reply(
        ctx, control_playlist_add(player, ctx.guild.id, name)
    )
    if result is not _FAILED:
        await ack(ctx)
        set_action(
            player,
            t(
                "action.playlist_added",
                player_lang(player),
                name=result["name"],
                count=result["count"],
            ),
        )


@playlist.command(name="load", description="Queue every track of a playlist")
@app_commands.describe(name="Playlist name")
async def playlist_load(ctx, *, name: str = None):
    """Queue every track of a saved playlist."""
    await defer(ctx)  # Joining voice takes over 3 s
    if name is None:
        await fail_t(ctx, "cmd.playlist_usage_name", sub="load")
        return
    saved = await _playlist_reply(ctx, control_playlist_get(ctx.guild.id, name))
    if saved is _FAILED:
        return
    entries = playlist_entries(saved)
    if not entries:
        await fail(ctx, ControlError("playlist_empty").text(ctx_lang(ctx)))
        return
    if not await join_voice_channel(ctx):
        return
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    player.should_stop = False
    await ack(ctx)
    # The loading outlives a slash command's 15-minute reply token
    await queue_playlist(ChannelContext(ctx), player, entries)


@playlist.command(name="list", description="List the saved playlists")
async def playlist_list(ctx):
    """List the server's saved playlists."""
    lang = ctx_lang(ctx)
    found = await _playlist_reply(ctx, control_playlist_list(ctx.guild.id))
    if found is _FAILED:
        return
    if not found:
        await fail_t(ctx, "cmd.playlist_none")
        return
    lines = [
        t(
            "cmd.playlist_line",
            lang,
            name=p["name"],
            count=p["tracks"],
            duration=_mmss(p["duration"]),
        )
        for p in found
    ]
    header = t("cmd.playlist_list_title", lang)
    await ctx.send(_fit(header, lines, lang), allowed_mentions=_NO_MENTIONS)


@playlist.command(name="show", description="Show the tracks of a playlist")
@app_commands.describe(name="Playlist name")
async def playlist_show(ctx, *, name: str = None):
    """Show the tracks of a saved playlist."""
    if name is None:
        await fail_t(ctx, "cmd.playlist_usage_name", sub="show")
        return
    saved = await _playlist_reply(ctx, control_playlist_get(ctx.guild.id, name))
    if saved is _FAILED:
        return
    lang = ctx_lang(ctx)
    lines = [
        f"{i + 1}. {track['title']} ({_mmss(track['duration'])})"
        for i, track in enumerate(saved["tracks"])
    ]
    header = t("cmd.playlist_show_title", lang, name=saved["name"], count=len(lines))
    await ctx.send(_fit(header, lines, lang), allowed_mentions=_NO_MENTIONS)


@playlist.command(name="delete", description="Delete a saved playlist")
@app_commands.describe(name="Playlist name")
async def playlist_delete(ctx, *, name: str = None):
    """Delete a saved playlist (its creator, or Manage Server)."""
    if name is None:
        await fail_t(ctx, "cmd.playlist_usage_name", sub="delete")
        return
    result = await _playlist_reply(
        ctx,
        control_playlist_delete(ctx.guild.id, name, ctx.author.id, _can_manage(ctx)),
    )
    if result is not _FAILED:
        await ack(ctx)


# Errors with their own message (anything else is also logged).
_KNOWN_ERRORS = (
    NotDJ,
    commands.CommandNotFound,
    app_commands.CommandNotFound,
    commands.MissingRequiredArgument,
    commands.NoPrivateMessage,
    app_commands.NoPrivateMessage,
    commands.MissingPermissions,
    app_commands.MissingPermissions,
)


@bot.event
async def on_command_error(ctx, error):
    """Global command error handler to avoid ugly tracebacks."""
    message = error_message(
        error,
        ctx.command,
        slash=getattr(ctx, "interaction", None) is not None,
        guild=ctx.guild,
    )
    if message is None:
        return
    if not isinstance(error, _KNOWN_ERRORS):
        print(f"Command error in '{ctx.command}': {error}")
    try:
        await ctx.send(message, ephemeral=True)
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction, error):
    """Errors raised before a hybrid command's context exists, and any pure
    slash command, get the same messages."""
    command = interaction.command.name if interaction.command else None
    guild = getattr(interaction, "guild", None)
    message = error_message(error, command, slash=True, guild=guild)
    if message is None:
        return
    if not isinstance(error, _KNOWN_ERRORS):
        print(f"Slash command error in '{command}': {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


# Commands listed by !help, in this order; any other visible command follows
# (its `help.cmd.<name>` line, or just its name).
HELP_ORDER = (
    "play",
    "list",
    "next",
    "voteskip",
    "pause",
    "resume",
    "seek",
    "crossfade",
    "stop",
    "reset",
    "loop",
    "shuffle",
    "remove",
    "move",
    "previous",
    "clear",
    "nowplaying",
    "history",
    "volume",
    "autoplay",
    "playlist",
    "theme",
    "djrole",
    "weblink",
    "language",
    "help",
)


def help_line(name, lang):
    key = f"help.cmd.{name}"
    text = t(key, lang)
    return text if text != key else f"`{COMMAND_PREFIX}{name}`"


class HelpCommand(commands.HelpCommand):
    """!help in the guild's language: one line per command."""

    async def send_bot_help(self, mapping):
        lang = ctx_lang(self.context)
        names = [c.name for c in self.context.bot.commands if not c.hidden]
        names.sort(key=lambda n: (HELP_ORDER.index(n) if n in HELP_ORDER else 99, n))
        lines = [t("help.title", lang)] + [help_line(n, lang) for n in names]
        await self.get_destination().send("\n".join(lines))

    async def send_command_help(self, command):
        await self.get_destination().send(
            help_line(command.name, ctx_lang(self.context))
        )

    def command_not_found(self, string):
        return t("help.unknown_command", ctx_lang(self.context), name=string)

    async def send_error_message(self, error):
        await self.get_destination().send(
            error, allowed_mentions=discord.AllowedMentions.none()
        )


bot.help_command = HelpCommand()
