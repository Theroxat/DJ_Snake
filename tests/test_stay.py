"""24/7 mode: the bot stays in its voice channel (idle linger skipped, saved
across restarts, rejoin after a restart or a drop, DJ gate)."""

import asyncio
import json
import types

import pytest

from djsnake import app, commands, config, permissions, playback, state, stay
from djsnake.playback import (
    join_voice_channel,
    play_next,
    recover_stay,
    restore_stay,
)
from djsnake.sessions import render_panel, session_player
from fakes import FakeVoiceClient, make_slash_ctx, make_track, settle
from test_permissions import DJ_ROLE, guild_ctx, member

GUILD_ID = 1
CHANNEL_ID = 99
DELAY = 0.2


@pytest.fixture(autouse=True)
def short_delays(monkeypatch):
    monkeypatch.setattr(config, "IDLE_DISCONNECT_SECONDS", DELAY)
    monkeypatch.setattr(config, "EMPTY_CHANNEL_GRACE_SECONDS", DELAY)


def voice_channel(connect=True, channel_id=CHANNEL_ID):
    return types.SimpleNamespace(
        id=channel_id,
        name="Lounge",
        members=[],
        connect=lambda **kw: None,
        permissions_for=lambda me: types.SimpleNamespace(connect=connect),
    )


def fake_guild(channel=None, voice_client=None):
    channels = {CHANNEL_ID: channel} if channel else {}
    return types.SimpleNamespace(
        id=GUILD_ID,
        name="Guild",
        me=object(),
        voice_client=voice_client,
        get_channel=channels.get,
    )


@pytest.fixture
def connects(monkeypatch):
    """Replace safe_voice_connect; `calls` lists the channels it was asked for."""
    calls = []

    async def fake_connect(channel, max_attempts=3):
        calls.append(channel)
        return FakeVoiceClient() if fake_connect.works else None

    fake_connect.works = True
    fake_connect.calls = calls
    monkeypatch.setattr(playback, "safe_voice_connect", fake_connect)
    return fake_connect


# --- storage -----------------------------------------------------------------


def test_persistence_round_trip():
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    assert stay.stay_on(GUILD_ID) and stay.stay_channel(GUILD_ID) == CHANNEL_ID
    state.guild_stay.clear()
    assert not stay.stay_on(GUILD_ID)
    stay.load_stay()
    assert stay.stay_on(GUILD_ID) and stay.stay_channel(GUILD_ID) == CHANNEL_ID
    stay.set_stay(GUILD_ID, False)  # Off keeps the channel, not the mode
    state.guild_stay.clear()
    stay.load_stay()
    assert not stay.stay_on(GUILD_ID)
    assert stay.stay_channel(GUILD_ID) == CHANNEL_ID


def test_load_drops_bad_entries(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    with open(stay.STAY_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "1": {"on": True, "channel": 5},
                "2": {"on": "yes", "channel": 5},
                "3": {"on": True, "channel": -1},
                "x": {"on": True},
                "4": 7,
            },
            f,
        )
    stay.load_stay()
    assert state.guild_stay == {
        1: {"on": True, "channel": 5},
        3: {"on": True, "channel": None},
    }


def test_env_default(monkeypatch):
    monkeypatch.setattr(stay, "STAY_247", True)
    assert stay.stay_on(GUILD_ID)
    stay.set_stay(GUILD_ID, False)  # An explicit choice wins
    assert not stay.stay_on(GUILD_ID)


def test_failed_save_restores(monkeypatch):
    def broken():
        raise OSError("read-only")

    monkeypatch.setattr(stay, "save_stay", broken)
    with pytest.raises(OSError):
        stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    assert GUILD_ID not in state.guild_stay


def test_remember_channel_only_when_on():
    stay.remember_channel(GUILD_ID, CHANNEL_ID)
    assert GUILD_ID not in state.guild_stay
    stay.set_stay(GUILD_ID, True)
    stay.remember_channel(GUILD_ID, CHANNEL_ID)
    assert stay.stay_channel(GUILD_ID) == CHANNEL_ID


# --- no idle disconnect ------------------------------------------------------


async def finish_one_track(player, ctx, media_dir):
    player.add_to_queue(make_track(media_dir, "A"))
    await play_next(ctx)
    player.voice_client.finish()
    await settle()


async def test_linger_skipped_when_on(player, ctx, media_dir):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    await finish_one_track(player, ctx, media_dir)
    assert not player.lingering() and player.idle_task is None
    await asyncio.sleep(DELAY + 0.2)
    assert player.voice_client.connected and player.in_voice()
    assert "🌙 24/7" in render_panel(player)
    assert "leaving in" not in render_panel(player)


async def test_linger_normal_when_off(player, ctx, media_dir):
    await finish_one_track(player, ctx, media_dir)
    assert player.lingering()
    await asyncio.sleep(DELAY + 0.2)
    assert player.voice_client is None or not player.voice_client.connected
    assert "24/7" not in render_panel(player)


async def test_turning_on_during_linger_keeps_voice(player, ctx, media_dir):
    await finish_one_track(player, ctx, media_dir)
    assert player.lingering()
    state.guild_stay[GUILD_ID] = {"on": True, "channel": None}  # Timer still runs
    await asyncio.sleep(DELAY + 0.2)
    assert player.voice_client.connected


async def test_session_survives_while_in_voice(player, fake_clock):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    fake_clock(config.SESSION_IDLE_HOURS * 3600 * 3)
    assert session_player(player.sid) is player


async def test_state_has_stay(player, client):
    r = await client.get(f"/api/{player.sid}/state")
    assert (await r.json())["stay"] is False
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    r = await client.get(f"/api/{player.sid}/state")
    assert (await r.json())["stay"] is True


# --- the command -------------------------------------------------------------


async def test_command_on_off(player, channel):
    player.voice_client.channel.id = CHANNEL_ID
    ctx = guild_ctx(channel, member(), "stay")
    await commands.stay.callback(ctx, "on")
    assert stay.stay_on(GUILD_ID) and stay.stay_channel(GUILD_ID) == CHANNEL_ID
    assert "24/7 mode on" in channel.log[-1]
    await commands.stay.callback(ctx, "off")
    assert not stay.stay_on(GUILD_ID)
    assert "24/7 mode off" in channel.log[-1]
    assert player.lingering()  # Back to the normal linger
    player.cancel_idle()


async def test_command_shows_state(player, channel):
    ctx = guild_ctx(channel, member(), "stay")
    await commands.stay.callback(ctx)
    assert "24/7 mode is off" in channel.log[-1]
    stay.set_stay(GUILD_ID, True)
    await commands.stay.callback(ctx, None)
    assert "24/7 mode is on" in channel.log[-1]


async def test_command_needs_a_dj_to_set(channel):
    state.guild_perms[GUILD_ID] = {"dj_role": DJ_ROLE}
    ctx = guild_ctx(channel, member(), "stay")
    await commands.stay.callback(ctx)  # Showing is open
    with pytest.raises(commands.NotDJ):
        await commands.stay.callback(ctx, "on")
    assert not stay.stay_on(GUILD_ID)
    ctx = guild_ctx(channel, member(roles=[DJ_ROLE]), "stay")
    await commands.stay.callback(ctx, "on")
    assert stay.stay_on(GUILD_ID)
    assert "stay" in permissions.DJ_WHEN_SETTING


async def test_command_on_joins_authors_channel(channel, connects):
    author = member()
    author.voice = types.SimpleNamespace(channel=voice_channel())
    ctx = guild_ctx(channel, author, "stay")
    ctx.guild.voice_client = None
    await commands.stay.callback(ctx, "on")
    assert connects.calls == [author.voice.channel]
    assert stay.stay_channel(GUILD_ID) == CHANNEL_ID
    assert "24/7 mode on" in channel.log[-1]


async def test_command_on_without_voice_waits(channel):
    author = member()
    author.voice = None
    ctx = guild_ctx(channel, author, "stay")
    await commands.stay.callback(ctx, "on")
    assert stay.stay_on(GUILD_ID) and stay.stay_channel(GUILD_ID) is None
    assert "next" in channel.log[-1]


async def test_command_save_failure(player, channel, monkeypatch):
    def broken():
        raise OSError("read-only")

    monkeypatch.setattr(stay, "save_stay", broken)
    await commands.stay.callback(guild_ctx(channel, member(), "stay"), "on")
    assert "could not be saved" in channel.log[-1]
    assert not stay.stay_on(GUILD_ID)


async def test_slash_stay(player, channel):
    ctx = make_slash_ctx(GUILD_ID, channel, member(), commands.stay)
    await commands.stay.callback(ctx, "on")
    assert stay.stay_on(GUILD_ID)


async def test_stop_keeps_voice_when_on(player, ctx):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    ctx.author = member()
    await commands.stop.callback(ctx)
    assert player.voice_client.connected


async def test_stop_leaves_when_off(player, ctx):
    ctx.author = member()
    await commands.stop.callback(ctx)
    assert player.voice_client is None or not player.voice_client.connected


async def test_join_remembers_channel(ctx, connects):
    stay.set_stay(GUILD_ID, True)
    ctx.author = member()
    ctx.author.voice = types.SimpleNamespace(channel=voice_channel())
    ctx.guild.voice_client = None
    assert await join_voice_channel(ctx)
    assert stay.stay_channel(GUILD_ID) == CHANNEL_ID


# --- restart rejoin ----------------------------------------------------------


async def test_restore_rejoins_without_playing(connects):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    channel = voice_channel()
    await restore_stay([fake_guild(channel)])
    assert connects.calls == [channel]
    player = state.music_players[GUILD_ID]
    assert player.in_voice() and not player.is_playing and not player.queue
    assert player.voice_client.played == []


async def test_restore_skips(connects):
    stay.set_stay(2, False, CHANNEL_ID)  # Off
    stay.set_stay(3, True)  # No remembered channel
    others = [types.SimpleNamespace(id=2), types.SimpleNamespace(id=3)]
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    await restore_stay(others + [fake_guild(None)])  # Channel deleted
    await restore_stay([fake_guild(voice_channel(connect=False))])  # No permission
    await restore_stay([fake_guild(voice_channel(), voice_client=FakeVoiceClient())])
    assert connects.calls == []
    assert GUILD_ID not in state.music_players


async def test_restore_survives_connect_failure(connects):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    connects.works = False
    await restore_stay([fake_guild(voice_channel())])
    assert GUILD_ID not in state.music_players
    assert stay.stay_on(GUILD_ID)  # Kept: the next restart may work


# --- dropped / kicked --------------------------------------------------------


async def test_recover_rejoins_once_then_gives_up(player, connects):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    guild = fake_guild(voice_channel())
    await recover_stay(guild)
    assert len(connects.calls) == 1 and stay.stay_on(GUILD_ID)
    await recover_stay(guild)  # Dropped again at once: no loop
    assert len(connects.calls) == 1
    assert not stay.stay_on(GUILD_ID)
    assert "turned off" in render_panel(player)


async def test_recover_allowed_again_later(player, connects, fake_clock):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    guild = fake_guild(voice_channel())
    await recover_stay(guild)
    fake_clock(config.STAY_RETRY_WINDOW + 1)
    await recover_stay(guild)
    assert len(connects.calls) == 2 and stay.stay_on(GUILD_ID)


async def test_recover_channel_deleted_turns_off(player, connects):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    await recover_stay(fake_guild(None))
    assert connects.calls == [] and not stay.stay_on(GUILD_ID)


async def test_recover_connect_failure_turns_off(player, connects):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    connects.works = False
    await recover_stay(fake_guild(voice_channel()))
    assert not stay.stay_on(GUILD_ID)


async def test_kick_event_with_channel_gone_turns_off(player, connects, monkeypatch):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    guild = fake_guild(None)
    me = types.SimpleNamespace(id=7, bot=True, guild=guild)
    monkeypatch.setattr(app, "bot", types.SimpleNamespace(user=me))
    real_sleep = asyncio.sleep

    async def quick(seconds):
        await real_sleep(0)

    monkeypatch.setattr(app.asyncio, "sleep", quick)
    old = types.SimpleNamespace(name="Lounge", guild=guild, id=CHANNEL_ID, members=[])
    await app.on_voice_state_update(
        me, types.SimpleNamespace(channel=old), types.SimpleNamespace(channel=None)
    )
    assert not stay.stay_on(GUILD_ID)
    assert player.voice_client is None


async def test_moved_bot_remembers_new_channel(player, monkeypatch):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    guild = fake_guild(None)
    me = types.SimpleNamespace(id=7, bot=True, guild=guild)
    monkeypatch.setattr(app, "bot", types.SimpleNamespace(user=me))
    old = types.SimpleNamespace(name="A", guild=guild, id=CHANNEL_ID, members=[])
    new = voice_channel(channel_id=CHANNEL_ID + 1)
    new.guild = guild
    await app.on_voice_state_update(
        me, types.SimpleNamespace(channel=old), types.SimpleNamespace(channel=new)
    )
    assert stay.stay_channel(GUILD_ID) == CHANNEL_ID + 1


async def _bot_left(monkeypatch, guild):
    me = types.SimpleNamespace(id=7, bot=True, guild=guild)
    monkeypatch.setattr(app, "bot", types.SimpleNamespace(user=me))
    real_sleep = asyncio.sleep

    async def quick(seconds):
        await real_sleep(0)

    monkeypatch.setattr(app.asyncio, "sleep", quick)
    old = types.SimpleNamespace(name="Lounge", guild=guild, id=CHANNEL_ID, members=[])
    await app.on_voice_state_update(
        me, types.SimpleNamespace(channel=old), types.SimpleNamespace(channel=None)
    )


async def test_reset_is_not_a_drop(player, ctx, connects, monkeypatch):
    """!reset disconnects on purpose: no 24/7 rejoin, and a second !reset
    within STAY_RETRY_WINDOW does not turn 24/7 off."""
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    guild = fake_guild(voice_channel())
    for _ in range(2):
        ctx.guild.voice_client = FakeVoiceClient()
        await commands.reset_voice.callback(ctx)
        await _bot_left(monkeypatch, guild)  # Also makes asyncio.sleep quick

    assert stay.stay_on(GUILD_ID)
    assert connects.calls == []


async def test_real_drops_still_recover(player, connects, monkeypatch):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    guild = fake_guild(voice_channel())
    await _bot_left(monkeypatch, guild)
    assert len(connects.calls) == 1 and stay.stay_on(GUILD_ID)
    await _bot_left(monkeypatch, guild)  # Dropped again at once: give up
    assert not stay.stay_on(GUILD_ID)


async def test_deliberate_mark_expires(fake_clock):
    from djsnake.player import left_deliberately, mark_deliberate_leave

    mark_deliberate_leave(GUILD_ID)
    assert left_deliberately(GUILD_ID) and not left_deliberately(GUILD_ID)
    mark_deliberate_leave(GUILD_ID)
    fake_clock(60)
    assert not left_deliberately(GUILD_ID)


async def test_stay_off_linger_uses_a_channel_context(player, channel, monkeypatch):
    stay.set_stay(GUILD_ID, True, CHANNEL_ID)
    seen = []
    monkeypatch.setattr(
        commands, "schedule_idle_disconnect", lambda c, p, *a: seen.append(c)
    )
    ctx = make_slash_ctx(GUILD_ID, channel, member(1, manager=True))
    await commands.stay.callback(ctx, "off")
    assert seen and isinstance(seen[0], commands.ChannelContext)
