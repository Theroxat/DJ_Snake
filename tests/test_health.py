"""GET /healthz and GET /metrics."""

import math

import pytest
from discord.ext import commands

from djsnake import state, web


@pytest.fixture
def connected(monkeypatch):
    """The bot is ready, with a live websocket at 42 ms."""
    monkeypatch.setattr(state.bot, "is_ready", lambda: True)
    monkeypatch.setattr(commands.Bot, "latency", property(lambda self: 0.042))


async def test_healthz_ok(client, connected, player):
    r = await client.get("/healthz")
    assert r.status == 200
    text = await r.text()
    body = await r.json()
    assert body["status"] == "ok"
    assert body["discord"] == "connected"
    assert body["latency_ms"] == 42.0
    assert body["players"] == 1
    assert {"guilds", "uptime_s", "version"} <= set(body)
    assert player.sid not in text  # No session ids


async def test_healthz_not_ready(client):
    r = await client.get("/healthz")
    assert r.status == 503
    body = await r.json()
    assert body["status"] == "unavailable"
    assert body["discord"] == "not ready"


async def test_healthz_websocket_closed(client, monkeypatch):
    monkeypatch.setattr(state.bot, "is_ready", lambda: True)
    monkeypatch.setattr(commands.Bot, "latency", property(lambda self: math.nan))
    r = await client.get("/healthz")
    assert r.status == 503
    assert (await r.json())["discord"] == "websocket closed"


async def test_metrics_disabled_by_default(client, connected):
    r = await client.get("/metrics")
    assert r.status == 404


async def test_metrics_enabled(client, connected, player, monkeypatch):
    monkeypatch.setattr(web, "METRICS_ENABLED", True)
    player.add_pending("A title", "youtube")
    state.counters["tracks_played"] = 3
    await client.get(f"/api/{player.sid}/state", headers={"X-Viewer": "viewer-0001"})
    r = await client.get("/metrics")
    assert r.status == 200
    assert r.content_type == "text/plain"
    text = await r.text()
    assert "djsnake_active_players 1\n" in text
    assert "djsnake_queued_tracks 0\n" in text
    assert "djsnake_downloads_in_progress 1\n" in text
    assert "djsnake_web_viewers 1\n" in text
    assert "djsnake_discord_latency_seconds 0.042000\n" in text
    assert "# TYPE djsnake_tracks_played_total counter" in text
    assert "djsnake_tracks_played_total 3\n" in text
