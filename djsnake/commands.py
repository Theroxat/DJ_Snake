"""Discord commands and command hooks."""

import asyncio
import math

import discord
from discord import app_commands
from discord.ext import commands

from . import catalog, media, spotify, state
from .config import (
    COMMAND_PREFIX,
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
    control_crossfade,
    control_loop,
    control_next,
    control_pause,
    control_playlist_add,
    control_playlist_delete,
    control_playlist_get,
    control_playlist_list,
    control_playlist_save,
    control_resume,
    control_seek,
    control_shuffle,
    control_volume,
    playlist_entries,
    read_spotify,
    seek_label,
)
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
    refresh_panel,
    session_player,
    set_action,
    touch,
)
from .themes import issue_edit_token, reset_theme, save_themes, theme_for
from .volume import parse_volume, volume_for

bot = state.bot


def hybrid(name, description):
    """Register a command that works as `!name` and as `/name`.

    Every command goes through this decorator (see "Adding a command" in
    CLAUDE.md). It is guild-only. Put `@app_commands.describe(...)` /
    `@app_commands.choices(...)` under it; the `ctx` helpers below make the
    reply paths work for both kinds of context.
    """

    def decorate(func):
        command = bot.hybrid_command(name=name, description=description)(func)
        return commands.guild_only()(command)

    return decorate


async def defer(ctx):
    """Acknowledge a slash command that may take over 3 s (no-op for `!`).
    The thinking message is private; ack() or the first reply replaces it."""
    if getattr(ctx, "interaction", None) is not None:
        await ctx.defer(ephemeral=True)


async def fail(ctx, text):
    """Report a user mistake: private on a slash command, a plain reply on `!`."""
    await ctx.send(f"❌ {text}", ephemeral=True)


def error_message(error, command=None, slash=False):
    """The English message for a command error, or None to stay silent. Shared
    by the prefix handler and the slash command tree."""
    if isinstance(error, (commands.CommandNotFound, app_commands.CommandNotFound)):
        return None
    if isinstance(error, commands.MissingRequiredArgument):
        return f"❌ Missing argument. Try `{COMMAND_PREFIX}help`."
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return "❌ This command only works in a server."
    if isinstance(
        error, (commands.MissingPermissions, app_commands.MissingPermissions)
    ):
        return (
            "❌ You need the **Manage Server** permission to use "
            f"`{'/' if slash else COMMAND_PREFIX}{command}`."
        )
    return f"❌ Error: {error}"


class PlaylistChoiceView(discord.ui.View):
    """Buttons shown when a link contains both a specific song and a playlist,
    letting the user pick just the song or the whole playlist."""

    def __init__(self, author_id, timeout=30):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.choice = None  # "single" | "playlist" | None (timeout)

    async def interaction_check(self, interaction):
        # Only the person who ran the command may use the buttons.
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ This isn't your command.", ephemeral=True
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


@hybrid("play", "Queue a music link, or the best YouTube match for some text")
@app_commands.describe(
    query="YouTube, SoundCloud, Bandcamp, Spotify, Deezer or Apple Music link, "
    "or text to search on YouTube"
)
async def play(ctx, *, query: str = None):
    await defer(ctx)  # Joining voice and reading the link take over 3 s
    query = (query or "").strip()
    if not query:
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}play <link or search text>`")
        return

    text_search = not looks_like_url(query)
    # A link is read up to the first space, as before.
    url = query if text_search else query.split()[0]
    if text_search and len(query) > MAX_SEARCH_LENGTH:
        await fail(ctx, f"Search text is limited to {MAX_SEARCH_LENGTH} characters.")
        return

    try:
        # Spotify, Deezer or Apple Music
        spotify_ref = None if text_search else catalog.check_link(url)
    except spotify.SpotifyError as e:
        await fail(ctx, ControlError(e.code).message)
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
            await ctx.send(f"❌ An error occurred: {str(e)}")
        return

    try:
        await ack(ctx)
        # From here on only the channel is needed: an interaction's reply
        # token expires after 15 minutes, a playlist or the idle timer outlives it.
        ctx = ChannelContext(ctx)
        player.load_line, player.load_final = "🔎 Analyzing the link…", False
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
                view = PlaylistChoiceView(ctx.author.id)
                prompt = await ctx.send(
                    f"🎧 This link contains a playlist of **{len(playlist_entries)}** tracks.\n"
                    "Do you want just this track or the whole playlist?",
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
                    await ctx.send("❌ Unable to join voice channel!")
                    return

            # Limit based on configuration
            playlist_entries = playlist_entries[:MAX_PLAYLIST_SIZE]
            await queue_playlist(ctx, player, playlist_entries)
        else:
            # It's a single video
            if not player.should_stop:
                await download_and_queue_song(ctx, player, url)

    except Exception as e:
        await ctx.send(f"❌ An error occurred: {str(e)}")


async def _play_spotify(ctx, player, kind, ref, provider="spotify"):
    """!play with a Spotify, Deezer or Apple Music link: read its track list,
    then queue the YouTube match of each track."""
    try:
        await ack(ctx)
        ctx = ChannelContext(ctx)
        label = catalog.LABELS[provider]
        player.load_line, player.load_final = f"🔎 Reading the {label} link…", False
        refresh_panel(player)
        try:
            tracks = await read_spotify(kind, ref, provider)
        finally:
            player.load_line = None
            refresh_panel(player)
        if not player.should_stop:
            await queue_spotify(ctx, player, kind, tracks)
    except ControlError as e:
        await ctx.send(f"❌ {e.message}")
    except Exception as e:
        await ctx.send(f"❌ An error occurred: {str(e)}")


@hybrid("stop", "Stop, clear the queue and leave voice")
async def stop(ctx):
    await defer(ctx)
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await fail(ctx, "No music is currently playing.")
        return

    async with player.lock:
        retire_play(player, ctx.guild.id, "stop")
        player.stop_and_clear()
    await player.disconnect_voice_client()
    # The web session stays (same link on the next !play) until it expires.
    if not player.loading:
        player.load_line, player.load_final = None, False
    set_action(player, "⏹️ Stopped")
    await ack(ctx)


@hybrid("reset", "Force a clean voice reconnect")
async def reset_voice(ctx):
    """Force reset the voice connection"""
    await ctx.send("🔧 Resetting voice connection...")

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
    await ctx.send("✅ Voice connection reset!")


@hybrid("list", "Show the queue")
async def list_queue(ctx):
    player = state.music_players.get(ctx.guild.id)
    if not player or not player.queue:
        await fail(ctx, "The queue is empty.")
        return

    queue_items = [f"{i+1}. {song['title']}" for i, song in enumerate(player.queue)]
    message = "🎶 **Queue:**\n" + "\n".join(queue_items[:10])

    if len(player.queue) > 10:
        message += f"\n... and {len(player.queue) - 10} more tracks"

    await ctx.send(message)


_FAILED = object()


async def _run_command_control(ctx, control):
    """Run a control for a Discord command; returns its result (possibly
    None) or _FAILED after replying with the error."""
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await fail(ctx, "No music is currently playing.")
        return _FAILED
    try:
        result = await control(player)
    except ControlError as e:
        await fail(ctx, e.message)
        return _FAILED
    await ack(ctx)
    refresh_panel(player)
    return result


@hybrid("next", "Skip to the next track")
async def next_track(ctx):
    # Stopping triggers the after_playing callback, which plays the next track.
    if await _run_command_control(ctx, control_next) is not _FAILED:
        set_action(state.music_players[ctx.guild.id], "⏭️ Skipped")


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
        await fail(ctx, "Invalid position. Use seconds or mm:ss.")
        return

    def relative_seek(player):
        # The position counts from the automix start, as the page shows it.
        start = (player.now_playing or {}).get("start") or 0.0
        return control_seek(player, start + seconds)

    applied = await _run_command_control(ctx, relative_seek)
    if applied is not _FAILED:
        player = state.music_players[ctx.guild.id]
        set_action(player, seek_label(player, applied))


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
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}crossfade on|off`")
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
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}loop [off|track|queue]`")
        return
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    control_loop(player, mode)
    await ack(ctx)


@hybrid("shuffle", "Shuffle the upcoming queue once")
async def shuffle(ctx):
    if await _run_command_control(ctx, control_shuffle) is not _FAILED:
        set_action(state.music_players[ctx.guild.id], "🔀 Queue shuffled")


@hybrid("volume", "Show the server's volume, or set it (0-150 %, 100 = normal)")
@app_commands.describe(level="0 to 150 (percent); leave empty to show it")
async def volume(ctx, level: str = None):
    """Show the server's volume, or set it (0-150 %, 100 = normal)."""
    if level is None:
        await ctx.send(
            f"🔊 Volume: **{volume_for(ctx.guild.id)}%**\n"
            f"`{COMMAND_PREFIX}volume <0-150>` changes it (100 is the normal level).",
            ephemeral=True,
        )
        return
    percent = parse_volume(level)
    if percent is None:
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}volume [0-150]`")
        return
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    try:
        control_volume(player, ctx.guild.id, percent)
    except ControlError as e:
        await ctx.send(f"⚠️ {e.message}", ephemeral=True)
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
        status = "on" if player.autoplay else "off"
        await ctx.send(
            f"📻 Autoplay is {status}. Usage: `{COMMAND_PREFIX}autoplay on|off`",
            ephemeral=True,
        )
        return
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
    choice = (choice or "").strip().lower()
    player = state.music_players.get(guild_id)
    if not choice:
        current = theme_for(guild_id)["skin"]
        source = "server default" if guild_id in state.guild_themes else "fallback"
        await ctx.send(
            f"🎨 Clip page theme: **{current}** ({source})\n"
            f"Presets: {', '.join(THEME_SKINS)}\n"
            f"`{COMMAND_PREFIX}theme <preset>` sets it, "
            f"`{COMMAND_PREFIX}theme reset` restores `{WEB_THEME}`, "
            f"`{COMMAND_PREFIX}theme edit` sends you an editor link "
            "(custom colors, name, logo)."
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
                f"🎨 Theme editor for **{ctx.guild.name}** (valid "
                f"{THEME_EDIT_SECONDS // 60} min, do not share it):\n"
                f"{PUBLIC_BASE_URL}/{player.sid}?edit={token}"
            )
        except discord.HTTPException:
            state.edit_tokens.pop(token, None)
            await fail(
                ctx,
                "I can't send you a direct message. Allow DMs from server "
                f"members, then run `{COMMAND_PREFIX}theme edit` again.",
            )
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
            await fail(
                ctx,
                f"Unknown theme. Presets: {', '.join(THEME_SKINS)}, "
                "or `reset` / `edit`.",
            )
            return
    except OSError as e:
        print(f"Could not save theme: {e}")
        await fail(ctx, "Could not save the theme (is DATA_DIR writable?).")
        return
    await ack(ctx)
    if player:
        set_action(player, f"🎨 Clip page theme: {label}")


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


def _playlist_usage():
    p = COMMAND_PREFIX
    return (
        "❌ Usage: "
        f"`{p}playlist save|load|show|delete|add <name>` or `{p}playlist list`"
    )


def _fit(header, lines, limit=1900):
    """A message of `header` and as many `lines` as fit Discord's limit."""
    text = header
    for i, line in enumerate(lines):
        if len(text) + len(line) + 30 > limit:
            return text + f"\n... and {len(lines) - i} more"
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
        await fail(ctx, e.message)
        return _FAILED


@bot.hybrid_group(
    name="playlist",
    description="Saved playlists of this server",
    invoke_without_command=True,
)
@commands.guild_only()
async def playlist(ctx):
    await ctx.send(_playlist_usage(), ephemeral=True)


@playlist.command(name="save", description="Save the current track and the queue")
@app_commands.describe(name="Playlist name")
async def playlist_save(ctx, *, name: str = None):
    """Save the current track and the queue under a name."""
    if name is None:
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}playlist save <name>`")
        return
    player = state.music_players.get(ctx.guild.id)
    if player is None:
        await fail(ctx, "No music is currently playing.")
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
    set_action(player, f"💾 Saved playlist {result['name']} ({result['count']} tracks)")


@playlist.command(name="add", description="Add the current track to a playlist")
@app_commands.describe(name="Playlist name")
async def playlist_add(ctx, *, name: str = None):
    """Add the current track to a saved playlist."""
    if name is None:
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}playlist add <name>`")
        return
    player = state.music_players.get(ctx.guild.id)
    if player is None:
        await fail(ctx, "No music is currently playing.")
        return
    result = await _playlist_reply(
        ctx, control_playlist_add(player, ctx.guild.id, name)
    )
    if result is not _FAILED:
        await ack(ctx)
        set_action(
            player, f"💾 Added to playlist {result['name']} ({result['count']} tracks)"
        )


@playlist.command(name="load", description="Queue every track of a playlist")
@app_commands.describe(name="Playlist name")
async def playlist_load(ctx, *, name: str = None):
    """Queue every track of a saved playlist."""
    await defer(ctx)  # Joining voice takes over 3 s
    if name is None:
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}playlist load <name>`")
        return
    saved = await _playlist_reply(ctx, control_playlist_get(ctx.guild.id, name))
    if saved is _FAILED:
        return
    entries = playlist_entries(saved)
    if not entries:
        await fail(ctx, ControlError("playlist_empty").message)
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
    found = await _playlist_reply(ctx, control_playlist_list(ctx.guild.id))
    if found is _FAILED:
        return
    if not found:
        await fail(
            ctx, f"No saved playlists. Use `{COMMAND_PREFIX}playlist save <name>`."
        )
        return
    lines = [
        f"• **{p['name']}** ({p['tracks']} tracks, {_mmss(p['duration'])})"
        for p in found
    ]
    await ctx.send(
        _fit("📚 **Saved playlists:**", lines), allowed_mentions=_NO_MENTIONS
    )


@playlist.command(name="show", description="Show the tracks of a playlist")
@app_commands.describe(name="Playlist name")
async def playlist_show(ctx, *, name: str = None):
    """Show the tracks of a saved playlist."""
    if name is None:
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}playlist show <name>`")
        return
    saved = await _playlist_reply(ctx, control_playlist_get(ctx.guild.id, name))
    if saved is _FAILED:
        return
    lines = [
        f"{i + 1}. {t['title']} ({_mmss(t['duration'])})"
        for i, t in enumerate(saved["tracks"])
    ]
    header = f"📚 **{saved['name']}** ({len(lines)} tracks)"
    await ctx.send(_fit(header, lines), allowed_mentions=_NO_MENTIONS)


@playlist.command(name="delete", description="Delete a saved playlist")
@app_commands.describe(name="Playlist name")
async def playlist_delete(ctx, *, name: str = None):
    """Delete a saved playlist (its creator, or Manage Server)."""
    if name is None:
        await fail(ctx, f"Usage: `{COMMAND_PREFIX}playlist delete <name>`")
        return
    result = await _playlist_reply(
        ctx,
        control_playlist_delete(ctx.guild.id, name, ctx.author.id, _can_manage(ctx)),
    )
    if result is not _FAILED:
        await ack(ctx)


@bot.event
async def on_command_error(ctx, error):
    """Global command error handler to avoid ugly tracebacks."""
    message = error_message(
        error, ctx.command, slash=getattr(ctx, "interaction", None) is not None
    )
    if message is None:
        return
    if message.startswith("❌ Error:"):
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
    message = error_message(error, command, slash=True)
    if message is None:
        return
    if message.startswith("❌ Error:"):
        print(f"Slash command error in '{command}': {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass
