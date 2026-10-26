"""Playlist export and import: file formats, validation, link sources and
permissions. No network: media listing and YouTube matching are faked."""

import json
import types

import pytest

from djsnake import catalog, commands, controls, media, playlist_io, playlists, spotify
from djsnake.config import MAX_SAVED_TRACKS
from djsnake.controls import ControlError
from fakes import make_ctx, make_track

GUILD_ID = 1
ALICE, BOB, ADMIN = 101, 102, 103


def yt(n):
    return f"https://www.youtube.com/watch?v=vid{n:08d}"


def tr(n, **fields):
    track = {"url": yt(n), "title": f"Track {n}", "duration": 60.0}
    track.update(fields)
    return track


def author(user_id, admin=False):
    return types.SimpleNamespace(
        id=user_id,
        display_name=f"user{user_id}",
        guild_permissions=types.SimpleNamespace(manage_guild=admin),
    )


def ctx_for(channel, user_id=ALICE, admin=False):
    return make_ctx(GUILD_ID, channel, author=author(user_id, admin), name="Guild")


class FakeAttachment:
    def __init__(self, data, filename="list.json", size=None):
        self.data, self.filename = data, filename
        self.size = len(data) if size is None else size

    async def read(self):
        return self.data


# --- Formats -------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["json", "m3u"])
def test_export_then_import_gives_the_same_entries(fmt):
    tracks = [tr(1), tr(2, title="Ça, va ? é", duration=185.0)]
    filename, data = playlist_io.export_bytes("My mix", "101", tracks, fmt)
    assert filename == f"My_mix.{fmt}"
    got, skipped, truncated = playlist_io.parse_import(data, filename)
    assert got == tracks
    assert (skipped, truncated) == (0, False)


def test_txt_round_trip_keeps_the_urls():
    filename, data = playlist_io.export_bytes("x", None, [tr(1), tr(2)], "txt")
    assert data.decode().split() == [yt(1), yt(2)]
    got, _, _ = playlist_io.parse_import(data, filename)
    assert [t["url"] for t in got] == [yt(1), yt(2)]
    assert got[0]["title"] == "?"


def test_json_schema_is_versioned():
    _, data = playlist_io.export_bytes("Mix", "101", [tr(1)], "json")
    doc = json.loads(data)
    assert doc["schema"] == "djsnake.playlist"
    assert doc["version"] == 1
    assert doc["name"] == "Mix"
    assert doc["created_by"] == "101"
    assert doc["entries"] == [{"url": yt(1), "title": "Track 1", "duration": 60.0}]


def test_m3u_layout():
    _, data = playlist_io.export_bytes("Mix", None, [tr(1)], "m3u")
    lines = data.decode().splitlines()
    assert lines[0] == "#EXTM3U"
    assert "#EXTINF:60,Track 1" in lines
    assert lines[-1] == yt(1)


def test_m3u_title_cannot_inject_lines():
    track = tr(1, title="Evil\nhttps://evil.test/x")
    _, data = playlist_io.export_bytes("m", None, [track], "m3u")
    got, skipped, _ = playlist_io.parse_import(data, "m.m3u")
    assert [t["url"] for t in got] == [yt(1)]
    assert skipped == 0


def doc(entries, **fields):
    base = {"schema": "djsnake.playlist", "version": 1, "entries": entries}
    base.update(fields)
    return json.dumps(base).encode()


@pytest.mark.parametrize(
    "data",
    [
        b"not json at all {",
        b"[1, 2]",
        json.dumps({"entries": []}).encode(),
        json.dumps({"schema": "other", "version": 1, "entries": []}).encode(),
        json.dumps(
            {"schema": "djsnake.playlist", "version": 2, "entries": []}
        ).encode(),
        json.dumps(
            {"schema": "djsnake.playlist", "version": True, "entries": []}
        ).encode(),
        json.dumps(
            {"schema": "djsnake.playlist", "version": 1, "entries": {}}
        ).encode(),
        b"\xff\xfe\x00bad utf8",
    ],
)
def test_bad_files_are_refused(data):
    with pytest.raises(playlist_io.ImportFileError) as e:
        playlist_io.parse_import(data, "x.json")
    assert e.value.code == "import_bad_file"


def test_too_big_file_is_refused():
    data = b"#" * (playlist_io.MAX_FILE_BYTES + 1)
    with pytest.raises(playlist_io.ImportFileError) as e:
        playlist_io.parse_import(data, "x.txt")
    assert e.value.code == "import_too_big"


def test_disallowed_urls_are_dropped_and_counted():
    entries = [
        {"url": yt(1), "title": "ok"},
        {"url": "https://evil.test/a.mp3", "title": "evil"},
        {"url": "http://169.254.169.254/latest", "title": "ssrf"},
        {"url": "file:///etc/passwd"},
        {"url": "https://open.spotify.com/track/" + "a" * 22},
        {"url": "https://www.youtube.com/playlist?list=PL" + "a" * 16},
        {"url": 5},
        "string",
        {"url": yt(1), "title": "duplicate"},
        {"url": "https://youtu.be/vid00000002", "title": "short"},
    ]
    got, skipped, truncated = playlist_io.parse_import(doc(entries), "x.json")
    assert [t["url"] for t in got] == [yt(1), yt(2)]
    assert skipped == 8
    assert truncated is False


def test_file_with_no_usable_url_is_an_error():
    with pytest.raises(playlist_io.ImportFileError) as e:
        playlist_io.parse_import(b"https://evil.test/x\n", "x.txt")
    assert e.value.code == "import_nothing"


def test_cap_truncates_and_reports():
    lines = "\n".join(yt(i) for i in range(MAX_SAVED_TRACKS + 5)).encode()
    got, skipped, truncated = playlist_io.parse_import(lines, "x.txt")
    assert len(got) == MAX_SAVED_TRACKS
    assert (skipped, truncated) == (5, True)


def test_titles_and_durations_are_sanitised():
    entries = [{"url": yt(1), "title": "a\x00b" + "x" * 500, "duration": "nope"}]
    got, _, _ = playlist_io.parse_import(doc(entries), "x.json")
    assert len(got[0]["title"]) <= playlists.TEXT_MAX
    assert "\x00" not in got[0]["title"]
    assert got[0]["duration"] is None


def test_format_is_sniffed_without_extension():
    _, data = playlist_io.export_bytes("m", None, [tr(1)], "m3u")
    got, _, _ = playlist_io.parse_import(data, "upload")
    assert got[0]["title"] == "Track 1"


# --- Export controls -----------------------------------------------------------


async def test_export_saved_playlist_and_formats():
    playlists.save(GUILD_ID, "Mix", [tr(1), tr(2)], creator_id=ALICE)
    name, data, count = await controls.control_playlist_export(GUILD_ID, "mix", "JSON")
    assert (name, count) == ("Mix.json", 2)
    assert json.loads(data)["created_by"] == str(ALICE)
    with pytest.raises(ControlError) as e:
        await controls.control_playlist_export(GUILD_ID, "mix", "mp3")
    assert e.value.code == "export_bad_format"
    with pytest.raises(ControlError) as e:
        await controls.control_playlist_export(GUILD_ID, "nope", "json")
    assert e.value.code == "playlist_not_found"


async def test_export_queue_is_reserved_and_needs_tracks(player, media_dir):
    with pytest.raises(ControlError) as e:
        await controls.control_playlist_export(GUILD_ID, "queue", "json", player)
    assert e.value.code == "playlist_empty"
    player.add_to_queue(make_track(media_dir, "A", duration=90.0))
    player.add_to_queue(make_track(media_dir, "B", duration=90.0))
    name, data, count = await controls.control_playlist_export(
        GUILD_ID, " Queue ", "txt", player
    )
    assert (name, count) == ("queue.txt", 2)


def test_queue_is_not_a_valid_saved_name():
    with pytest.raises(playlists.PlaylistError) as e:
        playlists.save(GUILD_ID, "Queue", [tr(1)])
    assert e.value.code == "bad_playlist_name"


async def test_export_command_sends_a_file(channel):
    playlists.save(GUILD_ID, "Mix", [tr(1)])
    sent = []

    async def send(content=None, **kwargs):
        sent.append((content, kwargs))

    ctx = ctx_for(channel)
    ctx.send = send
    await commands.playlist_export.callback(ctx, name="Mix", format="m3u")
    text, kwargs = sent[0]
    assert "Mix" in text and "m3u" in text
    assert kwargs["file"].filename == "Mix.m3u"
    await commands.playlist_export.callback(ctx, name="Mix", format="exe")
    assert "Unknown format" in sent[1][0]


# --- Import from a file --------------------------------------------------------


async def test_import_file_command_round_trip(channel):
    playlists.save(GUILD_ID, "Src", [tr(1), tr(2)])
    _, data, _ = await controls.control_playlist_export(GUILD_ID, "src", "json")
    ctx = ctx_for(channel)
    await commands.playlist_import.callback(
        ctx, name="Copy", file=FakeAttachment(data, "Src.json")
    )
    assert (
        playlists.get(GUILD_ID, "Copy")["tracks"]
        == playlists.get(GUILD_ID, "Src")["tracks"]
    )
    assert "Imported **Copy** (2 tracks)" in channel.log[-1]


async def test_import_file_reports_skips(channel):
    entries = [{"url": yt(1)}, {"url": "https://evil.test/x"}]
    await commands.playlist_import.callback(
        ctx_for(channel), name="P", file=FakeAttachment(doc(entries))
    )
    assert "1 skipped" in channel.log[-1]
    assert len(playlists.get(GUILD_ID, "P")["tracks"]) == 1


async def test_import_oversize_attachment_is_not_read(channel):
    att = FakeAttachment(b"x", size=playlist_io.MAX_FILE_BYTES + 1)

    async def boom():
        raise AssertionError("must not download")

    att.read = boom
    await commands.playlist_import.callback(ctx_for(channel), name="P", file=att)
    assert "too big" in channel.log[-1]
    assert not playlists.list_all(GUILD_ID)


async def test_import_bad_file_and_usage(channel):
    await commands.playlist_import.callback(
        ctx_for(channel), name="P", file=FakeAttachment(b"{nope", "a.json")
    )
    assert "not a valid playlist" in channel.log[-1]
    await commands.playlist_import.callback(ctx_for(channel), name="P")
    assert "Usage" in channel.log[-1]
    assert not playlists.list_all(GUILD_ID)


async def test_prefix_attachment_is_used(channel):
    ctx = ctx_for(channel)
    ctx.message.attachments = [FakeAttachment(yt(1).encode(), "a.txt")]
    await commands.playlist_import.callback(ctx, name="P")
    assert len(playlists.get(GUILD_ID, "P")["tracks"]) == 1


async def test_import_permissions_match_save(channel):
    file = lambda: FakeAttachment(yt(1).encode(), "a.txt")  # noqa: E731
    await commands.playlist_import.callback(ctx_for(channel, ALICE), "mix", file=file())
    await commands.playlist_import.callback(ctx_for(channel, BOB), "MIX", file=file())
    assert "Manage Server" in channel.log[-1]
    n = len(channel.log)
    await commands.playlist_import.callback(
        ctx_for(channel, ADMIN, True), "mix", file=file()
    )
    assert "Imported" in channel.log[-1] and len(channel.log) > n
    assert playlists.get(GUILD_ID, "mix")["creator_id"] == ALICE


async def test_import_checks_permission_before_slow_work(channel, monkeypatch):
    playlists.save(GUILD_ID, "mix", [tr(1)], creator_id=ALICE)
    called = []
    monkeypatch.setattr(
        controls.media, "get_playlist_info", lambda *a, **k: called.append(1) or []
    )
    await commands.playlist_import.callback(
        ctx_for(channel, BOB),
        name="mix",
        url="https://www.youtube.com/playlist?list=PL" + "a" * 16,
    )
    assert "Manage Server" in channel.log[-1]
    assert not called


# --- Import from links ---------------------------------------------------------

PL_URL = "https://www.youtube.com/playlist?list=PL" + "a" * 16


def flat(n, **fields):
    entry = {"id": f"vid{n:08d}", "title": f"Flat {n}", "url": yt(n), "duration": 30}
    entry.update(fields)
    return entry


async def test_import_youtube_playlist(channel, monkeypatch):
    seen = {}

    def fake_info(url, limit=None):
        seen.update(url=url, limit=limit)
        return [
            flat(1),
            flat(2),
            flat(1),
            {"id": "x", "title": "bad", "url": "https://evil.test/a"},
        ]

    monkeypatch.setattr(media, "get_playlist_info", fake_info)
    await commands.playlist_import.callback(ctx_for(channel), "yt", url=PL_URL)
    got = playlists.get(GUILD_ID, "yt")["tracks"]
    assert [t["url"] for t in got] == [yt(1), yt(2)]
    assert got[0]["duration"] == 30.0
    assert seen["limit"] > MAX_SAVED_TRACKS  # One more, to detect truncation
    assert "Imported **yt** (2 tracks)" in channel.log[-1]
    assert "2 skipped" in channel.log[-1]


async def test_import_soundcloud_set_and_bandcamp_album(monkeypatch):
    def fake_info(url, limit=None):
        return [
            {
                "id": "1",
                "title": "SC",
                "url": "https://soundcloud.com/artist/track-one",
            },
            {
                "id": "2",
                "title": "BC",
                "url": "https://band.bandcamp.com/track/song",
            },
        ]

    monkeypatch.setattr(media, "get_playlist_info", fake_info)
    for name, url in [
        ("sc", "https://soundcloud.com/artist/sets/mix"),
        ("bc", "https://band.bandcamp.com/album/disc"),
    ]:
        result = await controls.control_playlist_import_url(GUILD_ID, name, url)
        assert result["count"] == 2
    got = playlists.get(GUILD_ID, "sc")["tracks"]
    assert got[0]["url"] == "https://soundcloud.com/artist/track-one"
    # And such tracks load back
    entries = controls.playlist_entries(playlists.get(GUILD_ID, "bc"))
    assert len(entries) == 2


async def test_import_truncates_to_the_cap(monkeypatch):
    monkeypatch.setattr(
        media,
        "get_playlist_info",
        lambda url, limit=None: [flat(i) for i in range(MAX_SAVED_TRACKS + 1)],
    )
    result = await controls.control_playlist_import_url(GUILD_ID, "big", PL_URL)
    assert result["count"] == MAX_SAVED_TRACKS
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "url,code",
    [
        ("https://evil.test/playlist", "bad_url"),
        ("https://www.youtube.com/watch?v=vid00000001", "import_not_list"),
        ("not a link", "bad_url"),
    ],
)
async def test_import_url_rejections(url, code):
    with pytest.raises(ControlError) as e:
        await controls.control_playlist_import_url(GUILD_ID, "p", url)
    assert e.value.code == code
    assert not playlists.list_all(GUILD_ID)


async def test_import_empty_listing(monkeypatch):
    monkeypatch.setattr(media, "get_playlist_info", lambda url, limit=None: [])
    with pytest.raises(ControlError) as e:
        await controls.control_playlist_import_url(GUILD_ID, "p", PL_URL)
    assert e.value.code == "import_nothing"


@pytest.mark.parametrize(
    "url,provider",
    [
        ("https://open.spotify.com/playlist/" + "a" * 22, "spotify"),
        ("https://www.deezer.com/playlist/123", "deezer"),
        ("https://music.apple.com/us/playlist/x/pl.abc123", "apple"),
        ("https://open.spotify.com/album/" + "b" * 22, "spotify"),
    ],
)
async def test_import_metadata_sources_match_on_youtube(
    channel, monkeypatch, url, provider
):
    calls = []

    def fake_tracks(prov, kind, ref, limit):
        calls.append((prov, limit))
        return [
            spotify.make_track("One", ["A"], 100000),
            spotify.make_track("Two", ["B", "C"], 120000),
            spotify.make_track("Missing", ["D"], 90000),
        ]

    def fake_match(track):
        return None if track["title"] == "Missing" else yt(len(track["title"]))

    monkeypatch.setattr(catalog, "get_tracks", fake_tracks)
    monkeypatch.setattr(spotify, "find_youtube_match", fake_match)
    await commands.playlist_import.callback(ctx_for(channel), "meta", url=url)
    assert calls == [(provider, MAX_SAVED_TRACKS)]
    got = playlists.get(GUILD_ID, "meta")["tracks"]
    assert [t["title"] for t in got] == ["A – One", "B, C – Two"]
    assert got[0]["duration"] == 100.0
    assert all(t["url"].startswith("https://www.youtube.com/watch?v=") for t in got)
    # The progress message is edited into the summary
    assert "Imported **meta** (2 tracks)" in channel.edits[-1]
    assert "1 skipped" in channel.edits[-1]
    assert "Matching tracks" in channel.log[0]


async def test_import_metadata_single_track_and_no_match(monkeypatch):
    with pytest.raises(ControlError) as e:
        await controls.control_playlist_import_url(
            GUILD_ID, "p", "https://open.spotify.com/track/" + "a" * 22
        )
    assert e.value.code == "import_not_list"
    monkeypatch.setattr(
        catalog, "get_tracks", lambda *a: [spotify.make_track("One", ["A"], 1000)]
    )
    monkeypatch.setattr(spotify, "find_youtube_match", lambda track: None)
    with pytest.raises(ControlError) as e:
        await controls.control_playlist_import_url(
            GUILD_ID, "p", "https://open.spotify.com/playlist/" + "a" * 22
        )
    assert e.value.code == "import_nothing"
    assert not playlists.list_all(GUILD_ID)


async def test_import_unreadable_link_reports_the_reason(channel, monkeypatch):
    def failing(*a):
        raise spotify.SpotifyError("link_not_found")

    monkeypatch.setattr(catalog, "get_tracks", failing)
    await commands.playlist_import.callback(
        ctx_for(channel), "p", url="https://www.deezer.com/playlist/9"
    )
    assert "not found" in channel.log[-1]


async def test_import_replaces_an_existing_playlist_for_its_creator(monkeypatch):
    playlists.save(GUILD_ID, "mix", [tr(9)], creator_id=ALICE)
    monkeypatch.setattr(media, "get_playlist_info", lambda url, limit=None: [flat(1)])
    await controls.control_playlist_import_url(GUILD_ID, "mix", PL_URL, author_id=ALICE)
    assert [t["url"] for t in playlists.get(GUILD_ID, "mix")["tracks"]] == [yt(1)]
    with pytest.raises(ControlError) as e:
        await controls.control_playlist_import_url(
            GUILD_ID, "mix", PL_URL, author_id=BOB
        )
    assert e.value.code == "playlist_exists"
