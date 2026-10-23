"""Synced lyrics: LRC parsing, title parsing, the LRCLIB lookup and the web
route. LRCLIB is faked through lyrics._http: no network."""

import asyncio
import json
import os
import urllib.parse

import pytest

from djsnake import lyrics
from djsnake.playback import spotify_fields
from fakes import make_track

# --- LRC ----------------------------------------------------------------------


def test_parse_lrc_basic():
    text = "[ar:Someone]\n[00:01.50] First line\n[00:03.2]Second\n[01:02.345] Third"
    assert lyrics.parse_lrc(text) == [
        [1500, "First line"],
        [3200, "Second"],
        [62345, "Third"],
    ]


def test_parse_lrc_repeated_tags_sorted():
    text = "[00:10.00][00:30.00] Chorus\n[00:20.00] Verse\n[00:40.00]"
    assert lyrics.parse_lrc(text) == [
        [10000, "Chorus"],
        [20000, "Verse"],
        [30000, "Chorus"],
        [40000, ""],  # Instrumental break: kept, the page shows a note
    ]


def test_parse_lrc_offset_and_clamp():
    # A positive offset shows lines earlier.
    text = "[offset:+500]\n[00:00.20] Early\n[00:02.00] Later"
    assert lyrics.parse_lrc(text) == [[0, "Early"], [1500, "Later"]]


@pytest.mark.parametrize("text", [None, "", "no tags here", "[ar:Artist]", 42])
def test_parse_lrc_nothing(text):
    assert lyrics.parse_lrc(text) is None


# --- Titles -------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, channel, expected",
    [
        ("Daft Punk - Get Lucky (Official Video)", None, ("Daft Punk", "Get Lucky")),
        (
            "Daft Punk – Get Lucky ft. Pharrell Williams [4K]",
            None,
            ("Daft Punk", "Get Lucky"),
        ),
        (
            "Justice - D.A.N.C.E. (Official Audio) | Ed Banger",
            None,
            ("Justice", "D.A.N.C.E."),
        ),
        ('Artist — "Quoted Song" (Lyrics)', None, ("Artist", "Quoted Song")),
        ("Duo A & Duo B - Song (feat. C)", None, ("Duo A", "Song")),
        (
            "Song Name (Remix)",
            "Some Artist - Topic",
            ("Some Artist", "Song Name (Remix)"),
        ),
        ("Just A Title", None, (None, "Just A Title")),
        ("", None, (None, None)),
    ],
)
def test_parse_title(title, channel, expected):
    assert lyrics.parse_title(title, channel) == expected


def test_lookup_fields_youtube_and_spotify():
    youtube = {"title": "Artist - Song (Official Video)", "duration": 215.4}
    assert lyrics.lookup_fields(youtube) == ("Artist", "Song", 215.4)
    spotify_track = {"title": "Song", "artists": ["Artist", "Guest"], "duration": 201.0}
    track = {"title": "ARTIST - SONG [MV]", "duration": 230.0}
    track.update(spotify_fields(spotify_track))
    assert lyrics.lookup_fields(track) == ("Artist", "Song", 201.0)


# --- LRCLIB -------------------------------------------------------------------


class FakeLrclib:
    """Stands in for lyrics._http: answers by endpoint and records calls."""

    def __init__(self, get=None, search=None, status=200):
        self.get, self.search, self.status = get, search, status
        self.calls = []

    def __call__(self, url):
        parsed = urllib.parse.urlsplit(url)
        assert parsed.netloc == "lrclib.net"
        endpoint = parsed.path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, dict(urllib.parse.parse_qsl(parsed.query))))
        if self.status != 200:
            return self.status, b""
        body = self.get if endpoint == "get" else self.search
        if body is None:
            return 404, b""
        return 200, json.dumps(body).encode()


RECORD = {
    "trackName": "Song",
    "artistName": "Artist",
    "duration": 200.0,
    "instrumental": False,
    "plainLyrics": "Line one\nLine two\nLine three",
    "syncedLyrics": "[00:05.00] Line one\n[00:09.50] Line two\n[00:12.00] Line three",
}
EXPECTED = {
    "synced": [[5000, "Line one"], [9500, "Line two"], [12000, "Line three"]],
    "plain": "Line one\nLine two\nLine three",
}


def test_fetch_exact_match(monkeypatch):
    fake = FakeLrclib(get=RECORD)
    monkeypatch.setattr(lyrics, "_http", fake)
    assert lyrics.fetch("Artist", "Song", 200.4) == EXPECTED
    assert fake.calls == [
        ("get", {"track_name": "Song", "artist_name": "Artist", "duration": "200"})
    ]


def test_fetch_search_picks_closest_synced(monkeypatch):
    plain_only = {**RECORD, "syncedLyrics": None, "duration": 230.0}
    far = {**RECORD, "duration": 400.0, "syncedLyrics": "[00:01.00] Live"}
    close = {**RECORD, "duration": 236.0}
    fake = FakeLrclib(get=None, search=[far, plain_only, close])
    monkeypatch.setattr(lyrics, "_http", fake)
    assert lyrics.fetch("Artist", "Song", 230.0) == EXPECTED
    assert [c[0] for c in fake.calls] == ["get", "search"]


def test_stub_lyrics_ignored(monkeypatch):
    stub = {**RECORD, "plainLyrics": "probe", "syncedLyrics": "[00:00.00] probe"}
    monkeypatch.setattr(lyrics, "_http", FakeLrclib(get=stub, search=[stub]))
    assert lyrics.fetch("Artist", "Song", 200) is None


def test_fetch_instrumental_and_nothing(monkeypatch):
    monkeypatch.setattr(
        lyrics, "_http", FakeLrclib(get={**RECORD, "instrumental": True}, search=[])
    )
    assert lyrics.fetch("Artist", "Song", 200) is None


def test_fetch_server_error_raises(monkeypatch):
    monkeypatch.setattr(lyrics, "_http", FakeLrclib(status=503))
    with pytest.raises(lyrics.LyricsUnavailable):
        lyrics.fetch("Artist", "Song", 200)


async def test_prefetch_caches_in_memory_and_on_disk(monkeypatch, data_dir):
    fake = FakeLrclib(get=RECORD)
    monkeypatch.setattr(lyrics, "_http", fake)
    track = {
        "id": "t1",
        "video_id": "abcdefghijk",
        "title": "Artist - Song",
        "duration": 200.0,
    }
    task = lyrics.prefetch(track)
    assert lyrics.prefetch(track) is task  # One fetch at a time per track
    assert await task == EXPECTED
    assert lyrics.prefetch(track) is None  # Cached
    assert os.path.isfile(data_dir / "lyrics" / "abcdefghijk.json")
    # A restart: memory is gone, the disk cache answers without LRCLIB.
    lyrics._cache.clear()
    monkeypatch.setattr(lyrics, "_http", FakeLrclib(status=500))
    assert await lyrics.get(track) == EXPECTED


async def test_prefetch_disabled(monkeypatch):
    monkeypatch.setattr(lyrics, "ENABLED", False)
    assert lyrics.prefetch({"id": "x", "title": "A - B"}) is None
    assert await lyrics.get({"id": "x", "title": "A - B"}) is None


async def test_unavailable_is_not_cached(monkeypatch):
    monkeypatch.setattr(lyrics, "_http", FakeLrclib(status=503))
    track = {"id": "t2", "title": "Artist - Song", "duration": 200.0}
    assert await lyrics.get(track) is None
    assert "track:t2" not in lyrics._cache  # Asked again next time
    monkeypatch.setattr(lyrics, "_http", FakeLrclib(get=RECORD))
    assert await lyrics.get(track) == EXPECTED


# --- Route --------------------------------------------------------------------


@pytest.fixture
def lrclib(monkeypatch):
    fake = FakeLrclib(get=RECORD)
    monkeypatch.setattr(lyrics, "_http", fake)
    return fake


async def test_route_returns_lyrics(client, player, media_dir, lrclib):
    track = make_track(media_dir, "A", title="Artist - Song (Official Video)")
    player.add_to_queue(track)
    r = await client.get(f"/api/{player.sid}/lyrics/{track['id']}")
    assert r.status == 200
    assert await r.json() == EXPECTED
    assert r.headers["Cache-Control"] == "no-store"


async def test_route_not_found(client, player, media_dir, lrclib):
    lrclib.get, lrclib.search = None, []
    track = make_track(media_dir, "A", title="Nobody - Nothing")
    player.add_to_queue(track)
    r = await client.get(f"/api/{player.sid}/lyrics/{track['id']}")
    assert r.status == 404


async def test_route_pending(client, player, media_dir, monkeypatch):
    release = asyncio.Event()

    async def slow_run(key, track):
        await release.wait()
        lyrics._remember(key, EXPECTED)
        return EXPECTED

    monkeypatch.setattr(lyrics, "_run", slow_run)
    track = make_track(media_dir, "A", title="Artist - Song")
    player.add_to_queue(track)

    async def get_with_short_wait(t, wait=0.05):
        return await real_get(t, wait)

    real_get = lyrics.get
    monkeypatch.setattr(lyrics, "get", get_with_short_wait)
    r = await client.get(f"/api/{player.sid}/lyrics/{track['id']}")
    assert r.status == 202
    assert await r.json() == {"pending": True}
    release.set()
    await asyncio.sleep(0.01)
    r = await client.get(f"/api/{player.sid}/lyrics/{track['id']}")
    assert r.status == 200


async def test_route_rejects_unknown_session_and_track(client, player, lrclib):
    r = await client.get("/api/not-a-session/lyrics/abcd")
    assert r.status == 404
    assert await r.json() == {"active": False}
    r = await client.get(f"/api/{player.sid}/lyrics/unknown")
    assert r.status == 404
    assert lrclib.calls == []


async def test_route_disabled(client, player, media_dir, lrclib, monkeypatch):
    monkeypatch.setattr(lyrics, "ENABLED", False)
    track = make_track(media_dir, "A", title="Artist - Song")
    player.add_to_queue(track)
    r = await client.get(f"/api/{player.sid}/lyrics/{track['id']}")
    assert r.status == 404
    assert lrclib.calls == []
    s = await (await client.get(f"/api/{player.sid}/state")).json()
    assert s["lyrics"] is False
