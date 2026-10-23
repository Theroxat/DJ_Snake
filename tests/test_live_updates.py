"""Live updates: GET /api/{sid}/events (Server-Sent Events)."""

import asyncio
import json

import pytest

from djsnake import state, web
from djsnake.player import notify_state
from djsnake.sessions import expire_session


@pytest.fixture(autouse=True)
def fast_streams(monkeypatch):
    """Short timers so the tests run fast; a test changes one if it needs."""
    monkeypatch.setattr(web, "SSE_HEARTBEAT_SECONDS", 30)
    monkeypatch.setattr(web, "SSE_CHECK_SECONDS", 30)
    monkeypatch.setattr(web, "SSE_MIN_INTERVAL", 0.05)


async def open_stream(client, player, viewer=None, headers=None):
    query = f"?viewer={viewer}" if viewer else ""
    return await client.get(f"/api/{player.sid}/events{query}", headers=headers)


async def next_block(response, timeout=2.0):
    """The next SSE block (lines up to a blank line), or None at EOF."""
    try:
        raw = await asyncio.wait_for(response.content.readuntil(b"\n\n"), timeout)
    except asyncio.IncompleteReadError:
        return None
    return raw.decode() or None


async def next_event(response, timeout=2.0):
    """The next `data:` event, decoded (pings and `retry:` are skipped)."""
    while True:
        block = await next_block(response, timeout)
        if block is None:
            return None
        for line in block.splitlines():
            if line.startswith("data: "):
                return json.loads(line[len("data: ") :])


async def wait_for(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        assert loop.time() < end, "condition never met"
        await asyncio.sleep(0.02)


async def test_initial_event_is_the_full_state(client, player):
    r = await open_stream(client, player)
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")
    assert r.headers["Cache-Control"] == "no-cache"
    assert r.headers["X-Accel-Buffering"] == "no"
    event = await next_event(r)
    polled = await (await client.get(f"/api/{player.sid}/state")).json()
    assert set(event) == set(polled)
    assert event["active"] is True and event["in_voice"] is True
    r.close()


async def test_unknown_session_is_404(client):
    r = await client.get("/api/nope/events")
    assert r.status == 404
    assert await r.json() == {"active": False}


async def test_change_event(client, player):
    r = await open_stream(client, player)
    await next_event(r)
    key = player.add_pending("Some track")
    event = await next_event(r)
    assert event["downloading"] == [
        {"key": key, "title": "Some track", "source": "youtube"}
    ]
    player.drop_pending(key)
    assert (await next_event(r))["downloading"] == []
    r.close()


async def test_control_route_notifies(client, player):
    r = await open_stream(client, player)
    assert (await next_event(r))["crossfade"] is False
    resp = await client.post(
        f"/api/{player.sid}/control", json={"action": "crossfade", "enabled": True}
    )
    assert resp.status == 200
    assert (await next_event(r))["crossfade"] is True
    r.close()


async def test_bursts_are_coalesced(client, player, monkeypatch):
    monkeypatch.setattr(web, "SSE_MIN_INTERVAL", 0.3)
    r = await open_stream(client, player)
    await next_event(r)
    for i in range(50):
        player.add_pending(f"Track {i}")
    first = await next_event(r)
    # At most the first change plus one coalesced state for the rest.
    if first["downloading_total"] != 50:
        assert (await next_event(r))["downloading_total"] == 50
    with pytest.raises(asyncio.TimeoutError):
        await next_event(r, timeout=0.5)
    r.close()


async def test_unnotified_change_is_caught_by_the_diff(client, player, monkeypatch):
    monkeypatch.setattr(web, "SSE_CHECK_SECONDS", 0.1)
    r = await open_stream(client, player)
    await next_event(r)
    player.crossfade = True  # No notify_state()
    assert (await next_event(r))["crossfade"] is True
    r.close()


async def test_nothing_sent_without_change(client, player, monkeypatch):
    monkeypatch.setattr(web, "SSE_CHECK_SECONDS", 0.05)
    r = await open_stream(client, player)
    await next_event(r)
    with pytest.raises(asyncio.TimeoutError):
        await next_block(r, timeout=0.4)
    r.close()


async def test_heartbeat(client, player, monkeypatch):
    monkeypatch.setattr(web, "SSE_HEARTBEAT_SECONDS", 0.2)
    r = await open_stream(client, player)
    await next_event(r)
    assert (await next_block(r)).startswith(": ping")
    assert (await next_block(r)).startswith(": ping")
    r.close()


async def test_disconnect_cleans_up(client, player, monkeypatch):
    monkeypatch.setattr(web, "SSE_CHECK_SECONDS", 0.05)
    r = await open_stream(client, player, viewer="tab-aaaaaaaa")
    await next_event(r)
    assert len(state.streams[player.sid]) == 1
    assert len(player.watchers) == 1
    r.close()
    await wait_for(lambda: player.sid not in state.streams)
    assert not player.watchers


async def test_streams_count_as_viewers(client, player, monkeypatch):
    monkeypatch.setattr(web, "SSE_CHECK_SECONDS", 0.05)

    async def polled(viewer=None):
        headers = {"X-Viewer": viewer} if viewer else {}
        r = await client.get(f"/api/{player.sid}/state", headers=headers)
        return (await r.json())["viewers"]

    a = await open_stream(client, player, viewer="tab-aaaaaaaa")
    assert (await next_event(a))["viewers"] == 1
    anonymous = await open_stream(client, player, viewer="bad id!")
    assert (await next_event(anonymous))["viewers"] == 2
    assert (await next_event(a))["viewers"] == 2  # Told of the newcomer
    assert await polled() == 2
    assert await polled("tab-aaaaaaaa") == 2  # Streams and polls: once
    assert await polled("tab-bbbbbbbb") == 3  # A polling tab
    anonymous.close()
    await wait_for(lambda: len(state.streams[player.sid]) == 1)
    assert await polled() == 2
    a.close()
    await wait_for(lambda: player.sid not in state.streams)
    assert await polled() == 2  # Both polled within the window


async def test_session_expiry_ends_the_stream(client, player):
    r = await open_stream(client, player)
    await next_event(r)
    sid = player.sid
    player.voice_client = None  # Out of voice, so it can expire
    expire_session(player)
    assert await next_event(r) == {"active": False}
    assert await next_block(r) is None  # Closed
    await wait_for(lambda: sid not in state.streams)
    assert not player.watchers


async def test_reset_moves_the_stream_to_the_new_player(client, player):
    from djsnake.player import MusicPlayer
    from fakes import FakeVoiceClient

    r = await open_stream(client, player)
    await next_event(r)
    fresh = MusicPlayer()
    fresh.voice_client = FakeVoiceClient()
    fresh.sid = player.sid
    state.music_players[next(iter(state.music_players))] = fresh
    player.sid = None
    notify_state(player)  # The old player's last word wakes the stream
    await next_event(r)
    assert len(fresh.watchers) == 1 and not player.watchers
    fresh.crossfade = True
    notify_state(fresh)
    assert (await next_event(r))["crossfade"] is True
    r.close()


async def test_stream_caps(client, player, monkeypatch):
    monkeypatch.setattr(web, "MAX_STREAMS_PER_CLIENT", 1)
    r = await open_stream(client, player)
    await next_event(r)
    second = await open_stream(client, player)
    assert second.status == 429
    # Behind a local proxy, clients are told apart by X-Forwarded-For.
    other = await open_stream(
        client, player, headers={"X-Forwarded-For": "203.0.113.9"}
    )
    assert other.status == 200
    monkeypatch.setattr(web, "MAX_STREAMS_PER_SESSION", 2)
    third = await open_stream(
        client, player, headers={"X-Forwarded-For": "203.0.113.10"}
    )
    assert third.status == 429
    r.close()
    other.close()


async def test_shutdown_ends_streams(client, player):
    r = await open_stream(client, player)
    await next_event(r)
    await web._close_streams(client.app)
    assert await next_block(r) is None
    await wait_for(lambda: player.sid not in state.streams)
    refused = await open_stream(client, player)
    assert refused.status == 503
