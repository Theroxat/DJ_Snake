"""Slash commands: every command is hybrid, and its reply paths work with an
interaction context (and still with a prefix one)."""

import types

import discord
import pytest
from discord import app_commands
from discord.ext import commands as ext_commands

from djsnake import app, commands, config, media, state
from djsnake.controls import ChannelContext, WebContext
from djsnake.playback import join_voice_channel
from fakes import FakeAuthor, make_ctx, make_slash_ctx
from conftest import GUILD_ID

NAMES = {
    "play",
    "next",
    "list",
    "seek",
    "pause",
    "resume",
    "stop",
    "reset",
    "crossfade",
    "theme",
    "loop",
    "shuffle",
    "volume",
    "autoplay",
    "playlist",
    "djrole",
    "weblink",
    "lastfm",
    "language",
}


@pytest.fixture
def slash(channel):
    return make_slash_ctx(GUILD_ID, channel, FakeAuthor(), command=commands.theme)


def test_every_command_is_hybrid_and_guild_only():
    prefix = {c.name for c in state.bot.commands} - {"help"}
    slash_names = {c.name for c in state.bot.tree.get_commands()}
    assert prefix == slash_names == NAMES
    for command in state.bot.tree.get_commands():
        assert command.guild_only


def test_crossfade_has_on_off_choices():
    mode = state.bot.tree.get_command("crossfade")._params["mode"]
    assert [c.value for c in mode.choices] == ["on", "off"]


def test_loop_and_autoplay_have_choices():
    mode = state.bot.tree.get_command("loop")._params["mode"]
    assert [c.value for c in mode.choices] == ["off", "track", "queue"]
    mode = state.bot.tree.get_command("autoplay")._params["mode"]
    assert [c.value for c in mode.choices] == ["on", "off"]


def test_playlist_is_a_slash_group():
    group = state.bot.tree.get_command("playlist")
    names = {c.name for c in group.commands}
    assert names == {"save", "add", "load", "list", "show", "delete"}


def test_parameters_are_described():
    for name, param in [
        ("play", "query"),
        ("seek", "position"),
        ("theme", "choice"),
        ("volume", "level"),
    ]:
        command = state.bot.tree.get_command(name)
        assert command._params[param].description != "…"


async def test_theme_autocomplete_suggests_presets_and_actions():
    everything = await commands.theme_autocomplete(None, "")
    assert [c.value for c in everything] == [*config.THEME_PRESETS, "reset", "edit"]
    found = await commands.theme_autocomplete(None, "SP")
    assert [c.value for c in found] == ["space"]
    assert await commands.theme_autocomplete(None, "zzz") == []


# --- Replies ---------------------------------------------------------------------


async def test_ack_on_a_slash_command_is_a_private_check(slash, channel):
    await commands.ack(slash)
    assert slash.replies == [("✅", True)]
    assert channel.reactions == []


async def test_ack_after_defer_replaces_the_thinking_message(slash):
    await commands.defer(slash)
    await commands.ack(slash)
    assert slash.interaction.deferred == [True]
    assert slash.interaction.edits == ["✅"]
    assert slash.replies == []


async def test_ack_on_a_prefix_command_still_reacts(ctx, channel):
    await commands.defer(ctx)  # No interaction: nothing to defer
    await commands.ack(ctx)
    assert channel.reactions == ["✅"]


async def test_list_error_is_private_on_slash_and_plain_on_prefix(slash, ctx, channel):
    await commands.list_queue.callback(slash)
    assert slash.replies == [("❌ The queue is empty.", True)]
    await commands.list_queue.callback(ctx)
    assert channel.log[-1] == "❌ The queue is empty."


async def test_pause_without_music_is_a_private_error(slash):
    await commands.pause.callback(slash)
    assert slash.replies == [("❌ No music is currently playing.", True)]


async def test_seek_invalid_position_is_private(slash):
    await commands.seek.callback(slash, "abc")
    assert slash.replies[-1] == ("❌ Invalid position. Use seconds or mm:ss.", True)


async def test_crossfade_on_a_slash_command(player, slash):
    await commands.crossfade.callback(slash, "on")
    assert player.crossfade
    assert slash.replies == [("✅", True)]
    await commands.crossfade.callback(slash, None)
    assert "Usage" in slash.replies[-1][0] and slash.replies[-1][1]


async def test_stop_defers_then_confirms(player, slash):
    await commands.stop.callback(slash)
    assert slash.interaction.deferred == [True]
    assert slash.interaction.edits == ["✅"]


async def test_theme_defers_and_sets_a_preset(slash):
    await commands.theme.callback(slash, "vinyl")
    assert slash.interaction.deferred == [True]
    assert slash.interaction.edits == ["✅"]
    assert state.guild_themes[GUILD_ID]["skin"] == "vinyl"


async def test_theme_unknown_choice_is_private(slash):
    await commands.theme.callback(slash, "bogus")
    assert slash.replies[-1][1] and "Unknown theme" in slash.replies[-1][0]


async def test_theme_edit_with_closed_dms_is_private(channel):
    ctx = make_slash_ctx(3, channel, FakeAuthor(dms_open=False))
    await commands.theme.callback(ctx, "edit")
    assert ctx.replies[-1][1] and "direct message" in ctx.replies[-1][0]


# --- /play -----------------------------------------------------------------------


@pytest.fixture
def play_env(monkeypatch):
    seen = {"order": []}

    async def join(ctx):
        seen["order"].append(("join", list(ctx.interaction.deferred)))
        return True

    async def queue(ctx, player, url):
        seen["queue_ctx"], seen["url"] = ctx, url

    monkeypatch.setattr(commands, "join_voice_channel", join)
    monkeypatch.setattr(commands, "download_and_queue_song", queue)
    monkeypatch.setattr(media, "get_playlist_info", lambda url: [])
    return seen


async def test_play_defers_before_joining_voice(slash, play_env):
    await commands.play.callback(slash, query="https://youtu.be/dQw4w9WgXcQ")
    assert play_env["order"] == [("join", [True])]  # Deferred when join ran


async def test_play_keeps_only_the_channel_for_the_long_job(slash, play_env):
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    await commands.play.callback(slash, query=url)
    assert play_env["url"] == url
    ctx = play_env["queue_ctx"]
    assert isinstance(ctx, ChannelContext)
    assert ctx.guild is slash.guild and ctx.channel is slash.channel
    assert ctx.author is slash.author and ctx.message is None
    assert slash.interaction.edits == ["✅"]  # Acknowledged once, privately
    # Its replies are channel messages (the interaction's would expire).
    await ctx.send("x", ephemeral=True)
    assert slash.channel.log[-1] == "x"


async def test_play_without_a_link_is_a_private_usage(slash, play_env):
    await commands.play.callback(slash, query=None)
    assert slash.interaction.deferred == [True]
    assert slash.replies[-1][1] and "Usage" in slash.replies[-1][0]
    assert "queue_ctx" not in play_env


async def test_play_with_a_prefix_context_is_unchanged(ctx, channel, monkeypatch):
    async def join(ctx):
        return True

    seen = {}

    async def queue(ctx, player, url):
        seen["ctx"] = ctx

    monkeypatch.setattr(commands, "join_voice_channel", join)
    monkeypatch.setattr(commands, "download_and_queue_song", queue)
    monkeypatch.setattr(media, "get_playlist_info", lambda url: [])
    await commands.play.callback(ctx, query="https://youtu.be/dQw4w9WgXcQ")
    assert channel.reactions == ["✅"]
    assert isinstance(seen["ctx"], ChannelContext)


async def test_join_error_is_private_for_the_user_and_ignored_by_web(channel):
    ctx = make_slash_ctx(GUILD_ID, channel, author=None)
    assert not await join_voice_channel(ctx)
    assert ctx.replies == [("❌ You must be in a voice channel!", True)]

    web = WebContext(GUILD_ID, types.SimpleNamespace(text_channel=channel))
    assert not await join_voice_channel(web)  # `ephemeral` must not reach the channel
    assert channel.log[-1] == "❌ You must be in a voice channel!"


# --- Errors ----------------------------------------------------------------------


async def test_prefix_missing_permission_names_the_command(channel):
    ctx = make_ctx(GUILD_ID, channel, command=commands.theme)
    await commands.on_command_error(
        ctx, ext_commands.MissingPermissions(["manage_guild"])
    )
    assert channel.log[-1].endswith(f"`{config.COMMAND_PREFIX}theme`.")


async def test_slash_missing_permission_in_a_hybrid_context(slash):
    await commands.on_command_error(
        slash, ext_commands.MissingPermissions(["manage_guild"])
    )
    content, ephemeral = slash.replies[-1]
    assert "Manage Server" in content and "`/theme`" in content and ephemeral


class FakeResponse:
    def __init__(self):
        self.sent, self.done = [], False

    def is_done(self):
        return self.done

    async def send_message(self, content, ephemeral=False):
        self.done = True
        self.sent.append((content, ephemeral))


def make_interaction(command_name="theme"):
    followup = types.SimpleNamespace(sent=[])

    async def send(content, ephemeral=False):
        followup.sent.append((content, ephemeral))

    followup.send = send
    return types.SimpleNamespace(
        command=types.SimpleNamespace(name=command_name),
        response=FakeResponse(),
        followup=followup,
    )


async def test_app_command_errors_get_the_same_messages():
    interaction = make_interaction()
    error = app_commands.MissingPermissions(["manage_guild"])
    await commands.on_app_command_error(interaction, error)
    content, ephemeral = interaction.response.sent[-1]
    assert "Manage Server" in content and "`/theme`" in content and ephemeral

    interaction = make_interaction()
    await commands.on_app_command_error(interaction, app_commands.NoPrivateMessage())
    assert interaction.response.sent[-1] == (
        "❌ This command only works in a server.",
        True,
    )


async def test_app_command_error_after_a_response_uses_the_followup():
    interaction = make_interaction()
    interaction.response.done = True
    await commands.on_app_command_error(interaction, RuntimeError("boom"))
    assert interaction.followup.sent == [("❌ Error: boom", True)]


async def test_unknown_app_command_is_silent():
    interaction = make_interaction()
    await commands.on_app_command_error(
        interaction, app_commands.CommandNotFound("x", [])
    )
    assert not interaction.response.sent


# --- Sync ------------------------------------------------------------------------


@pytest.fixture
def tree_sync(monkeypatch):
    calls = []

    async def sync(guild=None):
        calls.append(guild)
        return [object()] * 10

    monkeypatch.setattr(state.bot.tree, "sync", sync)
    return calls


async def test_sync_is_global_by_default(tree_sync, monkeypatch, capsys):
    monkeypatch.setattr(app, "SLASH_SYNC_GUILD", None)
    assert await app.sync_commands() == 10
    assert tree_sync == [None]
    assert "Synced 10 slash commands (globally)" in capsys.readouterr().out


async def test_sync_to_one_guild_when_configured(tree_sync, monkeypatch, capsys):
    monkeypatch.setattr(app, "SLASH_SYNC_GUILD", 42)
    assert await app.sync_commands() == 10
    assert tree_sync[0].id == 42
    assert "Synced 10 slash commands (guild 42)" in capsys.readouterr().out
    state.bot.tree.clear_commands(guild=tree_sync[0])


async def test_sync_failure_does_not_stop_startup(monkeypatch, capsys):
    async def sync(guild=None):
        raise discord.HTTPException(
            types.SimpleNamespace(status=500, reason="x"), "boom"
        )

    monkeypatch.setattr(state.bot.tree, "sync", sync)
    monkeypatch.setattr(app, "SLASH_SYNC_GUILD", None)
    assert await app.sync_commands() is None
    assert "Could not sync" in capsys.readouterr().out


def test_not_dj_message_names_the_slash_command():
    message = commands.error_message(commands.NotDJ(), "loop", slash=True)
    assert message.startswith("❌ Only DJs can use `/loop`")
    message = commands.error_message(commands.NotDJ(), "volume")
    assert "`!volume`" in message


def test_new_playback_commands_need_a_dj():
    from djsnake.permissions import DJ_COMMANDS, DJ_WHEN_SETTING

    assert {"loop", "shuffle"} <= DJ_COMMANDS
    assert {"volume", "autoplay"} == DJ_WHEN_SETTING
