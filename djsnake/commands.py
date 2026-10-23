"""Discord commands and command hooks."""

import asyncio
import math

import discord
from discord.ext import commands

from . import i18n, media, spotify, state
from .config import (
    COMMAND_PREFIX,
    MAX_PLAYLIST_SIZE,
    PUBLIC_BASE_URL,
    THEME_EDIT_SECONDS,
    THEME_SKINS,
    WEB_THEME,
)
from .controls import (
    ControlError,
    control_crossfade,
    control_next,
    control_pause,
    control_resume,
    control_seek,
    read_spotify,
    seek_label,
)
from .i18n import ctx_lang, language_name, t
from .media import get_video_id_from_url
from .playback import (
    download_and_queue_song,
    join_voice_channel,
    queue_playlist,
    queue_spotify,
)
from .player import MusicPlayer, discard_tracks, retire_play
from .sessions import (
    ack,
    open_session,
    player_lang,
    refresh_panel,
    session_player,
    set_action,
    touch,
)
from .themes import issue_edit_token, reset_theme, save_themes, theme_for

bot = state.bot


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


@bot.before_invoke
async def record_command_activity(ctx):
    if ctx.guild and ctx.guild.id in state.music_players:
        touch(state.music_players[ctx.guild.id])


@bot.command(name="play")
async def play(ctx, url: str = None):
    lang = ctx_lang(ctx)
    if url is None:
        await ctx.send(t("cmd.play_usage", lang))
        return

    try:
        spotify_ref = spotify.check_url(url)
    except spotify.SpotifyError as e:
        await ctx.send(f"❌ {ControlError(e.code).text(lang)}")
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

    try:
        await ack(ctx)
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


async def _play_spotify(ctx, player, kind, spotify_id):
    """!play with a Spotify link: read its track list, then queue the
    YouTube match of each track."""
    lang = ctx_lang(ctx)
    try:
        await ack(ctx)
        player.load_line = t("cmd.reading_spotify", lang)
        player.load_final = False
        refresh_panel(player)
        try:
            tracks = await read_spotify(kind, spotify_id)
        finally:
            player.load_line = None
            refresh_panel(player)
        if not player.should_stop:
            await queue_spotify(ctx, player, kind, tracks)
    except ControlError as e:
        await ctx.send(f"❌ {e.text(lang)}")
    except Exception as e:
        await ctx.send(t("cmd.error", lang, error=e))


@bot.command(name="stop")
async def stop(ctx):
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await ctx.send(f"❌ {ControlError('nothing_playing').text(ctx_lang(ctx))}")
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


@bot.command(name="reset")
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
        fresh.text_channel = player.text_channel
        fresh.last_active = player.last_active
        fresh.history = player.history
        fresh.panel_message = player.panel_message
        fresh.panel_text = player.panel_text
        fresh.expired = player.expired
        fresh.crossfade = player.crossfade
        if player.panel_task is not None:
            player.panel_task.cancel()
        state.music_players[ctx.guild.id] = fresh
        leftovers = list(player.queue)
        if player.now_playing:
            leftovers.append(player.now_playing)
        player.queue = []
        player.history = []
        player.now_playing = None
        discard_tracks(leftovers)

    # Wait a bit (the voice state update arrives in the meantime)
    await asyncio.sleep(2)
    if player:
        refresh_panel(state.music_players[ctx.guild.id])
    await ctx.send(t("cmd.reset_done", lang))


@bot.command(name="list")
async def list_queue(ctx):
    lang = ctx_lang(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player or not player.queue:
        await ctx.send(t("cmd.queue_empty", lang))
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
        await ctx.send(f"❌ {ControlError('nothing_playing').text(lang)}")
        return _FAILED
    try:
        result = await control(player)
    except ControlError as e:
        await ctx.send(f"❌ {e.text(lang)}")
        return _FAILED
    await ack(ctx)
    refresh_panel(player)
    return result


@bot.command(name="next")
async def next_track(ctx):
    # Stopping triggers the after_playing callback, which plays the next track.
    if await _run_command_control(ctx, control_next) is not _FAILED:
        player = state.music_players[ctx.guild.id]
        set_action(player, t("action.skipped", player_lang(player)))


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


@bot.command(name="seek")
async def seek(ctx, position: str):
    seconds = parse_position(position)
    if seconds is None:
        await ctx.send(t("cmd.bad_position", ctx_lang(ctx)))
        return

    def relative_seek(player):
        # The position counts from the automix start, as the page shows it.
        start = (player.now_playing or {}).get("start") or 0.0
        return control_seek(player, start + seconds)

    applied = await _run_command_control(ctx, relative_seek)
    if applied is not _FAILED:
        player = state.music_players[ctx.guild.id]
        set_action(player, seek_label(player, applied))


@bot.command(name="crossfade")
async def crossfade(ctx, mode: str = None):
    mode = (mode or "").lower()
    if mode not in ("on", "off"):
        await ctx.send(t("cmd.crossfade_usage", ctx_lang(ctx)))
        return
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    control_crossfade(player, mode == "on")
    await ack(ctx)


@bot.command(name="theme")
@commands.has_guild_permissions(manage_guild=True)
async def theme(ctx, choice: str = None):
    """Show or set the clip page's default theme for this server."""
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
        player = state.music_players.setdefault(guild_id, MusicPlayer())
        if player.sid and session_player(player.sid) is None:
            player.sid = None  # Expired: open_session makes a fresh one
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
            await ctx.send(t("cmd.theme_dm_failed", lang))
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
            presets = ", ".join(THEME_SKINS)
            await ctx.send(t("cmd.theme_unknown", lang, presets=presets))
            return
    except OSError as e:
        print(f"Could not save theme: {e}")
        await ctx.send(t("cmd.theme_save_failed", lang))
        return
    await ack(ctx)
    if player:
        set_action(player, t("action.theme", player_lang(player), theme=label))


def _language_list():
    return ", ".join(f"`{code}` ({language_name(code)})" for code in i18n.LANGUAGES)


@bot.command(name="language", aliases=["lang", "langue"])
@commands.has_guild_permissions(manage_guild=True)
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
            await ctx.send(t("cmd.language_unknown", lang, languages=_language_list()))
            return
    try:
        i18n.set_language(guild_id, code)
    except OSError as e:
        print(f"Could not save language: {e}")
        await ctx.send(t("cmd.language_save_failed", lang))
        return
    await ack(ctx)
    player = state.music_players.get(guild_id)
    if player:
        new = player_lang(player)
        set_action(player, t("action.language", new, language=language_name(new)))


@bot.command(name="pause")
async def pause(ctx):
    await _run_command_control(ctx, control_pause)


@bot.command(name="resume")
async def resume(ctx):
    await _run_command_control(ctx, control_resume)


@bot.event
async def on_command_error(ctx, error):
    """Global command error handler to avoid ugly tracebacks."""
    if isinstance(error, commands.CommandNotFound):
        return
    lang = ctx_lang(ctx)
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(t("cmd.missing_argument", lang))
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send(t("cmd.guild_only", lang))
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(t("cmd.missing_permission", lang, command=ctx.command))
        return
    print(f"Command error in '{ctx.command}': {error}")
    try:
        await ctx.send(t("cmd.error_generic", lang, error=error))
    except Exception:
        pass


# Commands listed by !help, in this order; any other visible command follows
# (its `help.cmd.<name>` line, or just its name).
HELP_ORDER = (
    "play",
    "list",
    "next",
    "pause",
    "resume",
    "seek",
    "crossfade",
    "stop",
    "reset",
    "theme",
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
