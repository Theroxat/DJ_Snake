"""The "En téléchargement" list: player.pending_downloads and its state field."""

import asyncio
import threading
import time
import types

import pytest

from djsnake import media, spotify
from djsnake.playback import download_and_queue_song, queue_playlist, queue_spotify
from djsnake.sessions import expire_session
from djsnake.web import MAX_DOWNLOADING
from fakes import make_track, settle, titles

URL = "https://www.youtube.com/watch?v=aaaaaaaaaaa"


def pending(player):
    return [(item["title"], item["source"]) for item in player.pending_downloads]


@pytest.fixture
def downloads(monkeypatch, media_dir, player):
    """Fake download_media: snapshots pending_downloads at each call, names
    the track through on_title, fails for ccccccccccc, and waits on `gate`
    when it is cleared."""
    rec = types.SimpleNamespace(seen=[], gate=threading.Event(), on_stop=None)
    rec.gate.set()

    def fake_download(url, on_title=None):
        rec.seen.append(pending(player))
        if on_title is not None:
            on_title("Real title")
            on_title("Again")  # Only the first one counts
        rec.gate.wait(5)
        if rec.on_stop:
            rec.on_stop()
        if "ccccccccccc" in url:
            raise RuntimeError("Video unavailable")
        return make_track(media_dir, "D" + url[-1])

    monkeypatch.setattr(media, "download_media", fake_download)
    return rec


def quiet_ctx(ctx):
    async def send(content=None, **kwargs):
        pass

    return types.SimpleNamespace(
        guild=ctx.guild, channel=ctx.channel, author=None, send=send
    )


async def test_single_link_is_listed_named_then_removed(player, ctx, downloads):
    downloads.gate.clear()
    job = asyncio.ensure_future(download_and_queue_song(ctx, player, URL))
    await settle()
    assert downloads.seen == [[(None, "youtube")]]
    assert pending(player) == [("Real title", "youtube")]  # From yt-dlp
    downloads.gate.set()
    await job
    assert player.pending_downloads == []
    assert player.now_playing["title"] == "Da"


async def test_failed_download_is_removed(player, ctx, downloads):
    track = await download_and_queue_song(ctx, player, URL[:-11] + "c" * 11, quiet=True)
    assert track is None
    assert downloads.seen == [[(None, "youtube")]]
    assert player.pending_downloads == []


async def test_stop_clears_the_list_and_the_job_queues_nothing(player, ctx, downloads):
    downloads.on_stop = player.stop_and_clear  # !stop during the download
    track = await download_and_queue_song(ctx, player, URL)
    assert track is None
    assert player.pending_downloads == [] and player.queue == []


async def test_playlist_lists_every_entry_then_drops_them_one_by_one(
    player, ctx, downloads
):
    entries = [
        {"id": "aaaaaaaaaaa", "title": "P1"},
        {"id": "ccccccccccc", "title": "Gone"},
        {"title": "No id"},
        {"id": "bbbbbbbbbbb", "title": "P2"},
    ]
    await queue_playlist(quiet_ctx(ctx), player, entries)
    # Flat titles are known, so yt-dlp does not rename them.
    assert downloads.seen == [
        [
            ("P1", "youtube"),
            ("Gone", "youtube"),
            ("No id", "youtube"),
            ("P2", "youtube"),
        ],
        [("Gone", "youtube"), ("No id", "youtube"), ("P2", "youtube")],
        [("P2", "youtube")],
    ]
    assert player.pending_downloads == []
    assert titles([player.now_playing] + player.queue) == ["Da", "Db"]


async def test_playlist_stopped_midway_leaves_nothing(player, ctx, downloads):
    downloads.on_stop = lambda: setattr(player, "epoch", player.epoch + 1)
    entries = [{"id": "aaaaaaaaaaa", "title": "P1"}, {"id": "bbbbbbbbbbb"}]
    await queue_playlist(quiet_ctx(ctx), player, entries)
    assert downloads.seen == [[("P1", "youtube"), (None, "youtube")]]
    assert player.pending_downloads == []


def song(title, artists):
    return {"title": title, "artists": artists, "duration": 200.0}


async def test_spotify_list_not_found_entries_are_dropped(
    player, ctx, downloads, monkeypatch
):
    searched = []

    def fake_match(track):
        searched.append(pending(player))
        if track["title"] == "Missing":
            return None
        return "https://www.youtube.com/watch?v=aaaaaaaaaaa"

    monkeypatch.setattr(spotify, "find_youtube_match", fake_match)
    tracks = [song("Missing", ["A", "B"]), song("Found", ["C"])]
    await queue_spotify(quiet_ctx(ctx), player, "album", tracks)
    assert searched[0] == [("A, B – Missing", "spotify"), ("C – Found", "spotify")]
    assert searched[1] == [("C – Found", "spotify")]
    # The Spotify label is kept: yt-dlp's title does not replace it.
    assert downloads.seen == [[("C – Found", "spotify")]]
    assert player.pending_downloads == []
    assert player.load_line == "✅ 1/2 tracks loaded (1 not found on YouTube)"


async def test_spotify_single_not_found_is_dropped(player, ctx, monkeypatch):
    seen = []

    def fake_match(track):
        seen.append(pending(player))
        return None

    monkeypatch.setattr(spotify, "find_youtube_match", fake_match)
    await queue_spotify(ctx, player, "track", [song("Halo", ["Beyoncé"])])
    assert seen == [[("Beyoncé – Halo", "spotify")]]
    assert player.pending_downloads == []


async def test_expiry_clears_the_list(player):
    player.add_pending("X")
    await player.disconnect_voice_client()
    expire_session(player)
    assert player.pending_downloads == []


# --- Web ----------------------------------------------------------------------


async def get_state(client, player):
    r = await client.get(f"/api/{player.sid}/state")
    return await r.json()


async def test_web_add_shows_the_link_until_it_is_queued(client, player, downloads):
    downloads.gate.clear()
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "add", "url": URL}
    )
    assert r.status == 202
    await settle()
    s = await get_state(client, player)
    assert s["downloading_total"] == 1
    assert s["downloading"][0]["title"] == "Real title"
    assert s["downloading"][0]["source"] == "youtube"
    assert downloads.seen == [[(None, "youtube")]]
    downloads.gate.set()
    await settle(0.3)
    s = await get_state(client, player)
    assert (s["downloading"], s["downloading_total"]) == ([], 0)
    assert s["now_playing"]["title"] == "Da"


async def test_web_playlist_add_lists_the_link_while_it_is_read(
    client, player, downloads, monkeypatch
):
    seen = []

    def fake_info(url):
        seen.append(pending(player))
        time.sleep(0.05)
        return [{"id": "aaaaaaaaaaa", "title": "P1"}, {"id": "bbbbbbbbbbb"}]

    monkeypatch.setattr(media, "get_playlist_info", fake_info)
    r = await client.post(
        f"/api/{player.sid}/control",
        json={"action": "add", "url": "https://www.youtube.com/playlist?list=PLx"},
    )
    assert r.status == 202
    await settle(0.4)
    assert seen == [[(None, "youtube")]]
    assert downloads.seen[0] == [("P1", "youtube"), (None, "youtube")]
    assert player.pending_downloads == []


async def test_state_caps_the_list_and_counts_all(client, player):
    for i in range(MAX_DOWNLOADING + 7):
        player.add_pending(f"T{i}", "spotify" if i % 2 else "youtube")
    s = await get_state(client, player)
    assert len(s["downloading"]) == MAX_DOWNLOADING
    assert s["downloading_total"] == MAX_DOWNLOADING + 7
    assert set(s["downloading"][0]) == {"key", "title", "source"}
    assert [d["title"] for d in s["downloading"][:2]] == ["T0", "T1"]
    assert s["downloading"][1]["source"] == "spotify"


async def test_state_is_empty_out_of_voice(client, player):
    player.add_pending("T")
    await player.disconnect_voice_client()
    s = await get_state(client, player)
    assert (s["downloading"], s["downloading_total"]) == ([], 0)
