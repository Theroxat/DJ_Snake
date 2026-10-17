"""Status panel: one message per session, edited in place and throttled."""

import asyncio
import types

import discord
import pytest

from djsnake import media, sessions
from djsnake.controls import control_jump, control_next, control_pause
from djsnake.playback import play_next, queue_playlist
from djsnake.sessions import ack, refresh_panel, set_action, touch
from fakes import make_track

WAIT = 1.0  # Comfortably more than the patched PANEL_MIN_INTERVAL


@pytest.fixture(autouse=True)
def fast_panel(monkeypatch):
    monkeypatch.setattr(sessions, "PANEL_MIN_INTERVAL", 0.4)


@pytest.fixture
def panel(player, channel):
    """The session's panel message, with the channel's logs cleared."""
    message = player.panel_message
    channel.log.clear()
    channel.edits.clear()
    channel.reactions.clear()
    return message


@pytest.fixture
async def playing(player, ctx, media_dir, panel):
    """T0 playing, T1-T4 queued."""
    for i in range(5):
        player.add_to_queue(make_track(media_dir, f"T{i}"))
    await play_next(ctx)
    return player


async def test_panel_is_posted_once_with_the_link(player, channel):
    assert len(channel.log) == 1
    assert player.panel_message is channel.messages[0]
    assert player.sid in player.panel_message.content
    assert "Nothing playing" in player.panel_message.content


async def test_playlist_progress_edits_the_panel_in_place(
    player, ctx, channel, panel, media_dir, monkeypatch
):
    monkeypatch.setattr(
        media, "download_media", lambda url: make_track(media_dir, "T" + url[-1])
    )
    entries = [{"id": f"aaaaaaaaaa{i}"} for i in range(5)]
    await queue_playlist(ctx, player, entries)
    await asyncio.sleep(WAIT)
    assert channel.log == []
    assert "Now playing: **T0**" in panel.content
    assert "Up next: T1 and 3 more" in panel.content
    assert "✅ 5/5 tracks loaded" in panel.content
    assert 0 < len(channel.edits) <= 3  # Five downloads, coalesced


async def test_burst_of_changes_is_coalesced_and_last_wins(player, channel, panel):
    await asyncio.sleep(0.5)
    for i in range(20):
        set_action(player, f"action {i}")
        await asyncio.sleep(0.01)
    await asyncio.sleep(WAIT)
    assert 1 <= len(channel.edits) <= 2
    assert "action 19" in panel.content
    assert channel.log == []


async def test_controls_are_shown_in_the_same_message(playing, channel, panel):
    p = playing
    await control_next(p)
    await asyncio.sleep(0.15)
    set_action(p, "⏭️ Skipped")
    await control_pause(p)
    refresh_panel(p)
    await asyncio.sleep(WAIT)
    assert "Now playing: **T1**" in panel.content
    assert "⏸️ paused" in panel.content
    assert "Skipped" in panel.content
    assert channel.log == []

    await control_jump(p, 1, p.queue[-1]["id"])
    await asyncio.sleep(0.15)
    set_action(p, "jumped")
    await asyncio.sleep(WAIT)
    assert channel.log == [] and p.panel_message is panel
    assert "Now playing: **T4**" in panel.content


async def test_ack_reacts_to_the_command_message(ctx, channel):
    await ack(ctx)
    assert channel.reactions == ["✅"]
    await ack(types.SimpleNamespace())  # No message: ignored


async def test_deleted_panel_is_posted_again(player, channel, panel):
    panel.fail_with = discord.NotFound(
        types.SimpleNamespace(status=404, reason="x"), "Unknown Message"
    )
    set_action(player, "after delete")
    await asyncio.sleep(WAIT)
    assert len(channel.log) == 1
    assert player.panel_message is not panel
    assert "after delete" in player.panel_message.content


async def test_other_edit_errors_are_ignored(player, channel, panel):
    panel.fail_with = RuntimeError("boom")
    set_action(player, "x")
    await asyncio.sleep(WAIT)
    assert channel.log == [] and player.panel_message is panel


async def test_idle_text_then_expiry(playing, panel, fake_clock):
    p = playing
    async with p.lock:
        p.stop_and_clear()
    await p.disconnect_voice_client()
    p.last_action = None
    refresh_panel(p)
    await asyncio.sleep(WAIT)
    assert "💤 Idle" in panel.content
    assert "Now playing" not in panel.content

    fake_clock(7 * 3600)
    touch(p)
    await asyncio.sleep(WAIT)
    assert panel.content == "⌛ Session expired"
