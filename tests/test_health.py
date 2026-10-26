"""GET /healthz and GET /metrics."""

import math

import pytest
from discord.ext import commands

from djsnake import state, web


@pytest.fixture
def connected(monkeypatch):
    """The bot is ready, with a live websocket at 42 ms."""
    monkeypatch.setattr(state.bot, "is_ready", lambda: True)
    monkeypatch.setattr(commands.AutoShardedBot, "latency", property(lambda s: 0.042))


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
    monkeypatch.setattr(
        commands.AutoShardedBot, "latency", property(lambda s: math.nan)
    )
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


class FakeShard:
    def __init__(self, latency, closed=False):
        self.latency = latency
        self._closed = closed

    def is_closed(self):
        return self._closed


def fake_shards(monkeypatch, *shards):
    monkeypatch.setattr(state.bot, "is_ready", lambda: True)
    monkeypatch.setattr(
        commands.AutoShardedBot,
        "shards",
        property(lambda self: dict(enumerate(shards))),
    )


def test_bot_is_autosharded():
    assert isinstance(state.bot, commands.AutoShardedBot)


async def test_healthz_all_shards_up(client, monkeypatch):
    fake_shards(monkeypatch, FakeShard(0.03), FakeShard(0.05))
    r = await client.get("/healthz")
    assert r.status == 200
    body = await r.json()
    assert body["status"] == "ok"
    assert body["latency_ms"] == 40.0
    assert [s["status"] for s in body["shards"]] == ["up", "up"]


async def test_healthz_one_shard_down_is_degraded(client, monkeypatch):
    fake_shards(monkeypatch, FakeShard(0.03), FakeShard(math.nan, closed=True))
    r = await client.get("/healthz")
    assert r.status == 200
    body = await r.json()
    assert body["status"] == "degraded"
    assert body["discord"] == "degraded"
    assert body["latency_ms"] == 30.0
    assert [s["status"] for s in body["shards"]] == ["up", "down"]
    assert body["shards"][1]["latency_ms"] is None


async def test_healthz_all_shards_down(client, monkeypatch):
    fake_shards(monkeypatch, FakeShard(math.nan, True), FakeShard(math.nan))
    r = await client.get("/healthz")
    assert r.status == 503
    assert (await r.json())["discord"] == "websocket closed"


async def test_metrics_per_shard(client, monkeypatch):
    monkeypatch.setattr(web, "METRICS_ENABLED", True)
    fake_shards(monkeypatch, FakeShard(0.03), FakeShard(math.nan, closed=True))
    text = await (await client.get("/metrics")).text()
    assert "djsnake_shards 2\n" in text
    assert 'djsnake_shard_up{shard="0"} 1\n' in text
    assert 'djsnake_shard_up{shard="1"} 0\n' in text
    assert 'djsnake_shard_latency_seconds{shard="0"} 0.030000\n' in text
    assert 'djsnake_shard_latency_seconds{shard="1"} +Inf\n' in text
    assert "NaN" not in text
    assert "djsnake_discord_latency_seconds 0.030000\n" in text


@pytest.mark.parametrize(
    "raw,expected",
    [(None, None), ("", None), (" 4 ", 4), ("0", None), ("-2", None), ("abc", None)],
)
def test_parse_shard_count(raw, expected):
    from djsnake import config

    assert config.parse_shard_count(raw) == expected


async def test_a_shard_resuming_is_not_down(client, monkeypatch):
    """A resume closes the old websocket while the latency stays finite: the
    shard stays up for SHARD_RESUME_GRACE, then counts down."""
    web._shard_closed_since.clear()
    fake_shards(monkeypatch, FakeShard(0.03), FakeShard(0.05, closed=True))
    body = await (await client.get("/healthz")).json()
    assert body["status"] == "ok"
    assert [s["status"] for s in body["shards"]] == ["up", "up"]
    web._shard_closed_since[1] -= web.SHARD_RESUME_GRACE + 1
    body = await (await client.get("/healthz")).json()
    assert body["status"] == "degraded" and body["discord"] == "degraded"
    assert [s["status"] for s in body["shards"]] == ["up", "down"]
    web._shard_closed_since.clear()
