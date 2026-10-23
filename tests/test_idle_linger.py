"""Idle linger: once the queue runs out the bot stays in voice for a while
(IDLE_DISCONNECT_SECONDS) before leaving, so a late add plays at once."""

import asyncio
import types

import pytest

from djsnake import commands, config, media, state
from djsnake.app import _check_lingering_alone
from djsnake.controls import control_add
from djsnake.playback import play_next, schedule_idle_disconnect
from djsnake.player import MusicPlayer
from djsnake.sessions import render_panel
from fakes import FakeVoiceClient, make_ctx, make_track, settle

DELAY = 0.3
GRACE = 0.1


@pytest.fixture(autouse=True)
def short_delays(monkeypatch):
    monkeypatch.setattr(config, "IDLE_DISCONNECT_SECONDS", DELAY)
    monkeypatch.setattr(config, "EMPTY_CHANNEL_GRACE_SECONDS", GRACE)


def human(name="Alice"):
    return types.SimpleNamespace(name=name, bot=False)


@pytest.fixture
async def idle(player, ctx, media_dir):
    """One track played to its end: the queue is empty, the bot lingers."""
    vc = player.voice_client
    vc.channel.members = [human()]
    player.add_to_queue(make_track(media_dir, "A"))
    await play_next(ctx)
    vc.finish()
    await settle()
    assert player.lingering()
    return player


def idle_tasks(player):
    return [
        t
        for t in state.background_tasks
        if not t.done() and t.get_coro().__name__ == "_idle_disconnect"
    ]


async def test_lingers_then_leaves(idle, ctx):
    vc = idle.voice_client
    assert vc.connected and idle.in_voice() and not idle.is_playing
    assert "💤 Queue empty — leaving in 0 s" in render_panel(idle)
    await asyncio.sleep(DELAY + 0.2)
    assert not vc.connected and not idle.in_voice()
    assert not idle.lingering() and idle.idle_task is None
    assert "💤 Idle" in render_panel(idle)


async def test_panel_says_minutes(player, monkeypatch, ctx):
    monkeypatch.setattr(config, "IDLE_DISCONNECT_SECONDS", 300)
    player.voice_client.channel.members = [human()]
    await play_next(ctx)  # Nothing queued: start lingering
    assert "leaving in 5 min unless something is added" in render_panel(player)
    player.cancel_idle()


async def test_state_in_voice_while_lingering(idle, client):
    r = await client.get(f"/api/{idle.sid}/state")
    data = await r.json()
    assert data["in_voice"] is True and data["now_playing"] is None


async def test_web_add_cancels_and_plays(idle, client, media_dir, monkeypatch):
    monkeypatch.setattr(
        media, "download_media", lambda url, **kw: make_track(media_dir, "W")
    )
    r = await client.post(
        f"/api/{idle.sid}/control",
        json={"action": "add", "url": "https://youtu.be/dQw4w9WgXcQ"},
    )
    assert r.status == 202
    await settle()
    assert idle.now_playing["title"] == "W" and idle.is_playing
    assert not idle.lingering()
    await asyncio.sleep(DELAY + 0.2)
    assert idle.voice_client.connected


async def test_play_command_cancels_and_plays(idle, channel, media_dir, monkeypatch):
    async def joined(ctx):
        return True

    monkeypatch.setattr(commands, "join_voice_channel", joined)
    monkeypatch.setattr(media, "get_playlist_info", lambda url: None)
    monkeypatch.setattr(
        media, "download_media", lambda url, **kw: make_track(media_dir, "P")
    )
    author = types.SimpleNamespace(display_name="Bob", voice=None, id=7)
    await commands.play.callback(
        make_ctx(1, channel, author=author), query="https://youtu.be/dQw4w9WgXcQ"
    )
    assert idle.now_playing["title"] == "P" and not idle.lingering()
    await asyncio.sleep(DELAY + 0.2)
    assert idle.voice_client.connected


async def test_stop_leaves_at_once(idle, ctx):
    vc = idle.voice_client
    await commands.stop.callback(ctx)
    assert not vc.connected and idle.voice_client is None
    await settle(0.01)
    assert idle.idle_task is None and idle_tasks(idle) == []


async def test_empty_channel_leaves_early(idle, ctx, monkeypatch):
    monkeypatch.setattr(config, "IDLE_DISCONNECT_SECONDS", 60)
    schedule_idle_disconnect(ctx, idle)  # A long timer, someone listening
    vc = idle.voice_client
    alice = vc.channel.members.pop()
    member = types.SimpleNamespace(
        bot=False, guild=types.SimpleNamespace(id=1), name=alice.name
    )
    _check_lingering_alone(member, types.SimpleNamespace(channel=vc.channel))
    assert idle.idle_delay == GRACE
    await asyncio.sleep(GRACE + 0.2)
    assert not vc.connected


async def test_alone_when_queue_ends_uses_grace(player, ctx, monkeypatch):
    monkeypatch.setattr(config, "IDLE_DISCONNECT_SECONDS", 60)
    player.voice_client.channel.members = []
    await play_next(ctx)
    assert player.idle_delay == GRACE
    await asyncio.sleep(GRACE + 0.2)
    assert not player.voice_client.connected


async def test_stale_timer_after_reset_does_nothing(idle, ctx):
    vc = idle.voice_client
    fresh = MusicPlayer()
    fresh.voice_client = FakeVoiceClient()
    state.music_players[1] = fresh  # What !reset does, timer left running
    await asyncio.sleep(DELAY + 0.2)
    assert vc.connected and fresh.voice_client.connected


async def test_reset_cancels_the_timer(idle, ctx, monkeypatch):
    ctx.guild.voice_client = None
    real_sleep = asyncio.sleep
    monkeypatch.setattr(commands.asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))
    task = idle.idle_task
    await commands.reset_voice.callback(ctx)
    monkeypatch.undo()
    await settle()
    assert task.cancelled() and state.music_players[1] is not idle


async def test_new_play_bumps_generation_so_old_timer_is_stale(idle, ctx, media_dir):
    task = idle.idle_task
    idle.add_to_queue(make_track(media_dir, "B"))
    await play_next(ctx)
    await settle(0.01)
    assert task.cancelled() and idle.is_playing


async def test_no_double_timers(idle, ctx):
    first = idle.idle_task
    for _ in range(3):
        await play_next(ctx)  # Idle player, empty queue: re-arms
    await settle(0.01)
    assert first.cancelled()
    assert idle_tasks(idle) == [idle.idle_task]


async def test_pending_download_keeps_bot_waiting(idle):
    idle.single_downloads = 1
    await asyncio.sleep(DELAY + 0.1)
    assert idle.voice_client.connected and idle.lingering()
    idle.single_downloads = 0
    await asyncio.sleep(DELAY + 0.2)
    assert not idle.voice_client.connected


async def test_kick_cancels_the_timer(idle, monkeypatch):
    from djsnake import app

    vc = idle.voice_client
    task = idle.idle_task
    vc.connected = False
    guild = types.SimpleNamespace(id=1, voice_client=None)
    before = types.SimpleNamespace(channel=types.SimpleNamespace(name="G", guild=guild))
    after = types.SimpleNamespace(channel=None)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(app.asyncio, "sleep", lambda s: real_sleep(0))
    member = state.bot.user
    await app.on_voice_state_update(member, before, after)
    monkeypatch.undo()
    await settle(0.01)
    assert task.cancelled() and idle.voice_client is None


async def test_control_add_allowed_while_lingering(idle, media_dir, monkeypatch):
    monkeypatch.setattr(
        media, "download_media", lambda url, **kw: make_track(media_dir, "Z")
    )
    await control_add(idle, 1, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    await settle()
    assert idle.now_playing["title"] == "Z"
