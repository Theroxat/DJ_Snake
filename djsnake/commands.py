"""Discord commands and command hooks."""

import asyncio
import math

import discord
from discord.ext import commands

from . import media, spotify, state
from .config import (
    COMMAND_PREFIX,
    LOOP_MODES,
    MAX_PLAYLIST_SIZE,
    MAX_SEARCH_LENGTH,
    PUBLIC_BASE_URL,
    THEME_EDIT_SECONDS,
    THEME_SKINS,
    WEB_THEME,
)
from .controls import (
    ControlError,
    control_crossfade,
    control_loop,
    control_next,
    control_pause,
    control_resume,
    control_seek,
    control_shuffle,
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

bot = state.bot


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


@bot.command(name="play")
async def play(ctx, url: str = None, *, rest: str = ""):
    # `url` is the first word, `rest` the other words (a text search).
    query = f"{url or ''} {rest}".strip()
    if not query:
        await ctx.send(
            f"❌ Usage: `{COMMAND_PREFIX}play <YouTube or Spotify link or search text>`"
        )
        return

    query = query.strip()
    text_search = not looks_like_url(query)
    # A link is read up to the first space, as before.
    url = query if text_search else query.split()[0]
    if text_search and len(query) > MAX_SEARCH_LENGTH:
        await ctx.send(f"❌ Search text is limited to {MAX_SEARCH_LENGTH} characters.")
        return

    try:
        spotify_ref = None if text_search else spotify.check_url(url)
    except spotify.SpotifyError as e:
        await ctx.send(f"❌ {ControlError(e.code).message}")
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
        try:
            await queue_search(ctx, player, query)
        except Exception as e:
            await ctx.send(f"❌ An error occurred: {str(e)}")
        return

    try:
        await ack(ctx)
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


async def _play_spotify(ctx, player, kind, spotify_id):
    """!play with a Spotify link: read its track list, then queue the
    YouTube match of each track."""
    try:
        await ack(ctx)
        player.load_line, player.load_final = "🔎 Reading the Spotify link…", False
        refresh_panel(player)
        try:
            tracks = await read_spotify(kind, spotify_id)
        finally:
            player.load_line = None
            refresh_panel(player)
        if not player.should_stop:
            await queue_spotify(ctx, player, kind, tracks)
    except ControlError as e:
        await ctx.send(f"❌ {e.message}")
    except Exception as e:
        await ctx.send(f"❌ An error occurred: {str(e)}")


@bot.command(name="stop")
async def stop(ctx):
    player = state.music_players.get(ctx.guild.id)
    if not player:
        await ctx.send("❌ No music is currently playing.")
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


@bot.command(name="reset")
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
    await ctx.send("✅ Voice connection reset!")


@bot.command(name="list")
async def list_queue(ctx):
    player = state.music_players.get(ctx.guild.id)
    if not player or not player.queue:
        await ctx.send("❌ The queue is empty.")
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
        await ctx.send("❌ No music is currently playing.")
        return _FAILED
    try:
        result = await control(player)
    except ControlError as e:
        await ctx.send(f"❌ {e.message}")
        return _FAILED
    await ack(ctx)
    refresh_panel(player)
    return result


@bot.command(name="next")
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


@bot.command(name="seek")
async def seek(ctx, position: str):
    seconds = parse_position(position)
    if seconds is None:
        await ctx.send("❌ Invalid position. Use seconds or mm:ss.")
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
        await ctx.send(f"❌ Usage: `{COMMAND_PREFIX}crossfade on|off`")
        return
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    control_crossfade(player, mode == "on")
    await ack(ctx)


@bot.command(name="loop")
async def loop_command(ctx, mode: str = None):
    """Set the loop mode (off, track, queue); with no argument, cycle."""
    mode = mode.lower() if mode else None
    if mode is not None and mode not in LOOP_MODES:
        await ctx.send(f"❌ Usage: `{COMMAND_PREFIX}loop [off|track|queue]`")
        return
    player = state.music_players.setdefault(ctx.guild.id, MusicPlayer())
    touch(player)
    control_loop(player, mode)
    await ack(ctx)


@bot.command(name="shuffle")
async def shuffle(ctx):
    if await _run_command_control(ctx, control_shuffle) is not _FAILED:
        set_action(state.music_players[ctx.guild.id], "🔀 Queue shuffled")


@bot.command(name="theme")
@commands.has_guild_permissions(manage_guild=True)
async def theme(ctx, choice: str = None):
    """Show or set the clip page's default theme for this server."""
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
            await ctx.send(
                "❌ I can't send you a direct message. Allow DMs from server "
                f"members, then run `{COMMAND_PREFIX}theme edit` again."
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
            await ctx.send(
                f"❌ Unknown theme. Presets: {', '.join(THEME_SKINS)}, "
                "or `reset` / `edit`."
            )
            return
    except OSError as e:
        print(f"Could not save theme: {e}")
        await ctx.send("❌ Could not save the theme (is DATA_DIR writable?).")
        return
    await ack(ctx)
    if player:
        set_action(player, f"🎨 Clip page theme: {label}")


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
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Try `{COMMAND_PREFIX}help`.")
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send("❌ This command only works in a server.")
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(
            f"❌ You need the **Manage Server** permission to use "
            f"`{COMMAND_PREFIX}{ctx.command}`."
        )
        return
    print(f"Command error in '{ctx.command}': {error}")
    try:
        await ctx.send(f"❌ Error: {error}")
    except Exception:
        pass
