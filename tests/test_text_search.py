"""Search by text: `!play daft punk one more time` and the clip page's add box.
The yt-dlp search is faked; scoring is the one used for Spotify tracks."""

import json
import os

import pytest

from djsnake import commands, controls, media, spotify
from fakes import make_track, settle

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "spotify")
with open(os.path.join(FIXTURES, "youtube_search.json"), encoding="utf-8") as f:
    SEARCH = json.load(f)
DAFT_ID = "FGBhQbmPwH8"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://youtu.be/dQw4w9WgXcQ", True),
        ("http://example.com/x", True),
        ("www.youtube.com/watch?v=x", True),
        ("youtube.com/watch?v=x", True),
        ("spotify:track:abc", True),
        ("https://youtu.be/x some words", True),
        ("daft punk one more time", False),
        ("despacito", False),
        ("ac/dc thunderstruck", False),
        ("mr. brightside", False),
        ("", False),
    ],
)
def test_looks_like_url(text, expected):
    assert media.looks_like_url(text) is expected


def test_find_text_match_scores_like_a_spotify_track(monkeypatch):
    queries = []

    def fake_search(query, count):
        queries.append((query, count))
        return SEARCH["Daft Punk - One More Time"]

    monkeypatch.setattr(media, "search_youtube", fake_search)
    url = spotify.find_text_match("  daft punk   one more time ")
    assert url == f"https://www.youtube.com/watch?v={DAFT_ID}"
    assert queries == [("daft punk one more time", 5)]


def test_find_text_match_none_when_nothing_scores_high_enough(monkeypatch):
    monkeypatch.setattr(
        media, "search_youtube", lambda q, c: SEARCH["Daft Punk - One More Time"]
    )
    assert spotify.find_text_match("quantum chromodynamics lecture") is None


def test_find_text_match_survives_a_search_failure(monkeypatch):
    def failing(query, count):
        raise RuntimeError("boom")

    monkeypatch.setattr(media, "search_youtube", failing)
    assert spotify.find_text_match("daft punk") is None


def test_text_search_keeps_version_penalties():
    live = {"id": "x" * 11, "title": "One More Time (Live)", "channel": "Someone"}
    studio = {"id": "y" * 11, "title": "One More Time", "channel": "Someone"}
    track = spotify.text_track("one more time")
    assert spotify.best_match(track, [live, studio]) is studio


@pytest.fixture
def search_env(monkeypatch, media_dir):
    downloads, searched = [], []

    def fake_search(query, count):
        searched.append(query)
        if "zzz" in query:
            return []
        return SEARCH["Daft Punk - One More Time"]

    def fake_download(url, **kwargs):
        downloads.append(url)
        track = make_track(media_dir, "T1")
        track["url"] = url
        return track

    monkeypatch.setattr(media, "search_youtube", fake_search)
    monkeypatch.setattr(media, "download_media", fake_download)
    return downloads, searched


@pytest.fixture
def web_add(client, player):
    async def add(url):
        r = await client.post(
            f"/api/{player.sid}/control", json={"action": "add", "url": url}
        )
        return r.status, await r.json()

    return add


async def test_web_add_plain_text_searches_and_queues(web_add, player, search_env):
    downloads, searched = search_env
    status, body = await web_add("daft punk one more time")
    await settle(0.3)
    assert (status, body) == (202, {"ok": True})
    assert searched == ["daft punk one more time"]
    assert downloads == [f"https://www.youtube.com/watch?v={DAFT_ID}"]
    assert player.now_playing["url"] == downloads[0]


async def test_web_add_text_without_match_says_so(web_add, player, channel, search_env):
    downloads, _ = search_env
    status, _ = await web_add("zzz nothing")
    await settle(0.3)
    assert status == 202
    assert downloads == []
    assert "No YouTube match found for **zzz nothing**" in channel.log[-1]
    assert not player.loading


async def test_web_add_text_needs_voice(web_add, player, search_env):
    player.voice_client = None
    status, body = await web_add("daft punk")
    assert status == 409
    assert body["error"] == controls.ControlError("not_in_voice").message


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201, "https://example.com/a"])
async def test_web_add_rejects_empty_long_and_foreign_links(web_add, search_env, bad):
    status, body = await web_add(bad)
    assert status == 400
    assert body["error"] == controls.ControlError("bad_url").message
    assert search_env[1] == []


async def test_web_add_youtube_link_does_not_search(web_add, player, search_env):
    status, _ = await web_add("https://youtu.be/dQw4w9WgXcQ")
    await settle(0.3)
    assert status == 202
    assert search_env[1] == []
    assert search_env[0] == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


async def test_play_command_searches_text(player, ctx, search_env, monkeypatch):
    async def joined(c):
        return True

    monkeypatch.setattr(commands, "join_voice_channel", joined)
    downloads, searched = search_env
    await commands.play.callback(ctx, "daft", rest="punk one more time")
    await settle(0.3)
    assert searched == ["daft punk one more time"]
    assert downloads == [f"https://www.youtube.com/watch?v={DAFT_ID}"]


async def test_play_command_text_without_match(
    player, ctx, channel, search_env, monkeypatch
):
    async def joined(c):
        return True

    monkeypatch.setattr(commands, "join_voice_channel", joined)
    await commands.play.callback(ctx, "zzz", rest="nothing")
    await settle(0.2)
    assert search_env[0] == []
    assert "No YouTube match found for **zzz nothing**" in channel.log[-1]


async def test_play_command_usage_and_long_text(player, ctx, channel, search_env):
    await commands.play.callback(ctx)
    assert "Usage" in channel.log[-1]
    await commands.play.callback(ctx, "a", rest="b" * 250)
    assert "limited to" in channel.log[-1]
    assert search_env[1] == []
