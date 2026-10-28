"""More link sources: SoundCloud and Bandcamp URLs, Deezer and Apple Music
links (parsing, track lists, loading cap). All offline: fixtures are trimmed
API answers and pages (tests/fixtures/catalog/)."""

import asyncio
import json
import os

import pytest

from djsnake import catalog, controls, media, spotify
from djsnake.spotify import SpotifyError
from fakes import make_track

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "catalog")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# --- SoundCloud and Bandcamp URLs ----------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://soundcloud.com/forss/flickermood",
            ("single", "https://soundcloud.com/forss/flickermood"),
        ),
        (
            "https://m.soundcloud.com/forss/flickermood/?utm_source=x#t=1",
            ("single", "https://soundcloud.com/forss/flickermood"),
        ),
        (
            "https://soundcloud.com/forss/flickermood/s-AbC123",
            ("single", "https://soundcloud.com/forss/flickermood/s-AbC123"),
        ),
        (
            "https://soundcloud.com/forss/sets/soulhack",
            ("playlist", "https://soundcloud.com/forss/sets/soulhack"),
        ),
        (
            "https://artist.bandcamp.com/track/some-song?from=embed",
            ("single", "https://artist.bandcamp.com/track/some-song"),
        ),
        (
            "https://artist.bandcamp.com/album/some-album/",
            ("playlist", "https://artist.bandcamp.com/album/some-album"),
        ),
        # YouTube still works through the same entry point.
        (
            "https://youtu.be/dQw4w9WgXcQ",
            ("single", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ),
    ],
)
def test_parse_media_url_accepts_and_rebuilds(url, expected):
    assert media.parse_media_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://on.soundcloud.com/AbCdE",  # Short link: an arbitrary redirect
        "https://soundcloud.com/forss",  # An artist page
        "https://soundcloud.com/forss/tracks",
        "https://soundcloud.com/discover/sets/charts",
        "https://soundcloud.com.evil.example/forss/flickermood",
        "https://evilsoundcloud.com/forss/flickermood",
        "https://user:pw@soundcloud.com/forss/flickermood",
        "https://soundcloud.com:8080/forss/flickermood",
        "ftp://soundcloud.com/forss/flickermood",
        "https://bandcamp.com/track/x",
        "https://artist.bandcamp.com/",
        "https://artist.bandcamp.com/merch/shirt",
        "https://a.b.bandcamp.com/track/x",
        "https://artist.bandcamp.com.evil.example/track/x",
        "https://music.example.com/track/x",  # Custom Bandcamp domains
        None,
    ],
)
def test_parse_media_url_rejects_other_links(url):
    assert media.parse_media_url(url) is None


def test_source_of_names_the_platform():
    assert media.source_of("https://soundcloud.com/a/b") == "soundcloud"
    assert media.source_of("https://api.soundcloud.com/tracks/1") == "soundcloud"
    assert media.source_of("https://artist.bandcamp.com/track/x") == "bandcamp"
    assert media.source_of("https://www.youtube.com/watch?v=x") == "youtube"
    assert media.source_of(None) == "youtube"


def test_entry_url_keeps_other_platforms_urls_and_rebuilds_youtube():
    sc = {"id": "123", "url": "https://api.soundcloud.com/tracks/123"}
    assert media.entry_url(sc) == "https://api.soundcloud.com/tracks/123"
    bc = {"id": "9", "url": "https://a.bandcamp.com/track/x"}
    assert media.entry_url(bc) == "https://a.bandcamp.com/track/x"
    assert media.entry_url({"id": "dQw4w9WgXcQ"}) == (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    assert media.entry_url(
        {"id": "dQw4w9WgXcQ", "url": "https://www.youtube.com/x"}
    ) == ("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert media.entry_url({}) is None


# --- Deezer and Apple Music URLs -----------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.deezer.com/en/album/302127", ("album", "302127")),
        ("https://www.deezer.com/fr/track/3135553?utm=x", ("track", "3135553")),
        ("https://deezer.com/playlist/908622995/", ("playlist", "908622995")),
        ("https://www.deezer.com/br/playlist/908622995", ("playlist", "908622995")),
    ],
)
def test_check_deezer(url, expected):
    assert catalog.check_deezer(url) == expected
    assert catalog.check_link(url) == (expected[0], expected[1], "deezer")


@pytest.mark.parametrize(
    "url, code",
    [
        ("https://link.deezer.com/s/abc", "short_link"),
        ("https://deezer.page.link/abc", "short_link"),
        ("https://www.deezer.com/en/artist/27", "bad_url"),
        ("https://www.deezer.com/en/album/notanumber", "bad_url"),
        ("https://user@www.deezer.com/album/1", None),
        ("https://www.deezer.com.evil.example/album/1", None),
    ],
)
def test_check_deezer_refuses(url, code):
    if code is None:
        assert catalog.check_deezer(url) is None
        return
    with pytest.raises(SpotifyError) as e:
        catalog.check_deezer(url)
    assert e.value.code == code


PL = "pl.f4d106fed2bd41149aaacabb233eb5eb"


def test_check_apple_album_song_and_playlist():
    assert catalog.check_apple(
        "https://music.apple.com/us/album/discovery/697194953"
    ) == ("album", ("us", "discovery", "697194953", None))
    # An album link with ?i= is that one song
    assert catalog.check_link(
        "https://music.apple.com/fr/album/one-more-time/697194953?i=697195787&l=en"
    ) == ("track", ("fr", "one-more-time", "697195787", "697195787"), "apple")
    assert catalog.check_apple("https://music.apple.com/us/song/x/697195787") == (
        "song",
        ("us", "x", "697195787", None),
    )
    assert catalog.check_link(
        f"https://music.apple.com/us/playlist/todays-hits/{PL}"
    ) == ("playlist", ("us", "todays-hits", PL, None), "apple")


@pytest.mark.parametrize(
    "url",
    [
        "https://music.apple.com/us/artist/daft-punk/5468295",
        "https://music.apple.com/us/album/x/pl.abc",
        "https://music.apple.com/us/playlist/x/12345",
        "https://music.apple.com/us/album/x/123?i=abc",
        "https://music.apple.com/us/album/bad slug/123",
        "https://music.apple.com/album/x/123",
    ],
)
def test_check_apple_refuses_unusable_links(url):
    with pytest.raises(SpotifyError) as e:
        catalog.check_apple(url)
    assert e.value.code == "bad_url"


@pytest.mark.parametrize(
    "url",
    [
        "https://music.apple.com.evil.example/us/album/x/123",
        "https://example.com/us/album/x/123",
        "https://itunes.apple.com/us/album/x/123",
    ],
)
def test_other_hosts_are_not_music_links(url):
    assert catalog.check_link(url) is None


def test_spotify_links_still_go_through_check_link():
    sid = "37i9dQZF1DXcBWIGoYBM5M"
    assert catalog.check_link(f"spotify:track:{sid}") == ("track", sid, "spotify")


# --- Parsing -------------------------------------------------------------------


def test_parse_deezer_album_skips_unreadable_and_untitled():
    data = json.loads(fixture("deezer_album_tracks.json"))
    assert catalog.parse_deezer("album", data) == [
        {
            "title": "One More Time",
            "artists": ["Daft Punk"],
            "duration": 320.0,
            "source": "deezer",
        },
        {
            "title": "Aerodynamic",
            "artists": ["Daft Punk"],
            "duration": 212.0,
            "source": "deezer",
        },
    ]


def test_parse_deezer_track():
    data = json.loads(fixture("deezer_track.json"))
    (track,) = catalog.parse_deezer("track", data)
    assert (track["title"], track["artists"], track["duration"]) == (
        "One More Time",
        ["Daft Punk"],
        320.0,
    )


def test_parse_deezer_unexpected_shape_is_unreadable():
    with pytest.raises(SpotifyError) as e:
        catalog.parse_deezer("album", {"hello": 1})
    assert e.value.code == "link_unreadable"


def test_parse_itunes_album_keeps_only_songs():
    tracks = catalog.parse_itunes_lookup(
        json.loads(fixture("itunes_album.json")), "album"
    )
    assert [(t["title"], t["duration"]) for t in tracks] == [
        ("One More Time", 320.357),
        ("Aerodynamic", 212.0),
    ]
    assert tracks[0]["source"] == "apple"


def test_parse_apple_playlist_reads_artists_from_the_page_data():
    tracks = catalog.parse_apple_playlist_html(fixture("apple_playlist.html"))
    assert [(t["title"], t["artists"]) for t in tracks] == [
        ("One More Time", ["Daft Punk"]),
        ("Aerodynamic", ["Daft Punk"]),
        ("Get Lucky", ["Daft Punk & Pharrell Williams"]),
    ]
    assert tracks[0]["duration"] == pytest.approx(320.357)


def test_parse_apple_playlist_falls_back_to_schema_org():
    html = fixture("apple_playlist.html")
    html = html.replace('id="serialized-server-data"', 'id="other"')
    tracks = catalog.parse_apple_playlist_html(html)
    assert [t["title"] for t in tracks] == [
        "One More Time",
        "Aerodynamic",
        "Digital Love",
    ]
    assert tracks[0]["duration"] == 320.0 and tracks[0]["artists"] == []


def test_parse_apple_playlist_without_data_is_unreadable():
    with pytest.raises(SpotifyError) as e:
        catalog.parse_apple_playlist_html("<html></html>")
    assert e.value.code == "link_unreadable"


# --- Fetching (the HTTP layer is replaced) --------------------------------------


@pytest.fixture
def http(monkeypatch):
    """Replace spotify._http: urls are looked up in `routes` by substring."""
    routes, calls = {}, []

    def fake(url, headers=None, data=None):
        calls.append(url)
        for part, answer in routes.items():
            if part in url:
                return answer
        return 404, b""

    monkeypatch.setattr(spotify, "_http", fake)
    fake.routes, fake.calls = routes, calls
    return fake


def test_get_tracks_deezer_album_requests_a_capped_list(http):
    http.routes["api.deezer.com/album/302127/tracks"] = (
        200,
        fixture("deezer_album_tracks.json").encode(),
    )
    tracks = catalog.get_tracks("deezer", "album", "302127", 1)
    assert [t["title"] for t in tracks] == ["One More Time"]  # Cut to the limit
    assert http.calls == ["https://api.deezer.com/album/302127/tracks?limit=1"]


def test_get_tracks_deezer_missing_item(http):
    http.routes["api.deezer.com/"] = 200, fixture("deezer_error.json").encode()
    with pytest.raises(SpotifyError) as e:
        catalog.get_tracks("deezer", "track", "99999999999", 30)
    assert e.value.code == "link_not_found"


def test_get_tracks_empty_list(http):
    http.routes["api.deezer.com/"] = 200, b'{"data": []}'
    with pytest.raises(SpotifyError) as e:
        catalog.get_tracks("deezer", "playlist", "1", 30)
    assert e.value.code == "link_empty"


def test_get_tracks_apple_album_uses_the_lookup_api(http):
    http.routes["itunes.apple.com/lookup"] = 200, fixture("itunes_album.json").encode()
    ref = ("us", "discovery", "697194953", None)
    tracks = catalog.get_tracks("apple", "album", ref, 30)
    assert len(tracks) == 2
    assert http.calls == [
        "https://itunes.apple.com/lookup?id=697194953&country=us&entity=song&limit=30"
    ]


def test_get_tracks_apple_playlist_rebuilds_the_page_url(http):
    http.routes["music.apple.com/"] = 200, fixture("apple_playlist.html").encode()
    ref = ("us", "daft-mix", PL, None)
    tracks = catalog.get_tracks("apple", "playlist", ref, 2)
    assert len(tracks) == 2
    assert http.calls == [f"https://music.apple.com/us/playlist/daft-mix/{PL}"]


def test_get_tracks_server_errors_are_unreadable(http):
    http.routes["api.deezer.com/"] = 503, b""
    with pytest.raises(SpotifyError) as e:
        catalog.get_tracks("deezer", "track", "1", 30)
    assert e.value.code == "link_unreadable"


# --- Loading flow: the shared YouTube matching and the cap ----------------------

ID = "302127"
DISCOVERY = [
    {"title": "One More Time", "artists": ["Daft Punk"], "duration": 320.0},
    {"title": "Nothing Like It", "artists": ["Nobody"], "duration": 200.0},
    {"title": "Aerodynamic", "artists": ["Daft Punk"], "duration": 212.0},
]


@pytest.fixture
def source_env(monkeypatch, media_dir):
    downloads = []

    def fake_get_tracks(provider, kind, ref, limit):
        tracks = [dict(t, source=provider) for t in DISCOVERY]
        return tracks[:1] if kind == "track" else tracks[:limit]

    def fake_search(query, count):
        if "Nothing" in query:
            return []
        title = query.split(" - ", 1)[1]
        return [
            {
                "id": (title.replace(" ", "x") * 2)[:11],
                "title": query,
                "duration": 320 if "One" in title else 212,
                "channel": "Daft Punk",
            }
        ]

    def fake_download(url, **kwargs):
        downloads.append(url)
        track = make_track(media_dir, f"T{len(downloads)}")
        track["url"] = url
        return track

    monkeypatch.setattr(catalog, "get_tracks", fake_get_tracks)
    monkeypatch.setattr(media, "search_youtube", fake_search)
    monkeypatch.setattr(media, "download_media", fake_download)
    return downloads


@pytest.fixture
def web_add(client, player):
    async def add(url):
        r = await client.post(
            f"/api/{player.sid}/control", json={"action": "add", "url": url}
        )
        return r.status, await r.json()

    return add


async def test_web_add_deezer_album_matches_each_track_on_youtube(
    web_add, player, source_env
):
    status, body = await web_add(f"https://www.deezer.com/en/album/{ID}")
    await asyncio.sleep(0.4)
    assert (status, body) == (202, {"ok": True})
    assert source_env == [
        "https://www.youtube.com/watch?v=OnexMorexTi",
        "https://www.youtube.com/watch?v=Aerodynamic",
    ]
    assert player.load_line == "✅ 2/3 tracks loaded (1 not found on YouTube)"


async def test_web_add_apple_song(web_add, player, source_env):
    status, _ = await web_add("https://music.apple.com/us/album/x/1?i=697195787")
    await asyncio.sleep(0.3)
    assert status == 202
    assert source_env == ["https://www.youtube.com/watch?v=OnexMorexTi"]


async def test_deezer_playlist_respects_max_size(
    web_add, player, monkeypatch, source_env
):
    monkeypatch.setattr(controls, "MAX_PLAYLIST_SIZE", 2)
    monkeypatch.setattr(controls, "MAX_PLAYLIST_FULL", 2)
    await web_add("https://www.deezer.com/playlist/908622995")
    await asyncio.sleep(0.4)
    assert player.load_line == "✅ 1/2 tracks loaded (1 not found on YouTube)"


async def test_pending_entries_carry_the_provider(player):
    from djsnake.playback import pending_label

    assert pending_label({"spotify": dict(DISCOVERY[0], source="deezer")}) == (
        "Daft Punk – One More Time",
        "deezer",
    )
    assert pending_label({"spotify": DISCOVERY[0]})[1] == "spotify"
    assert pending_label({"title": "x", "url": "https://a.bandcamp.com/track/x"}) == (
        "x",
        "bandcamp",
    )


@pytest.mark.parametrize(
    "code, status",
    [("link_not_found", 404), ("link_unreadable", 502), ("link_empty", 400)],
)
async def test_web_add_link_errors_use_the_control_message(
    web_add, monkeypatch, code, status
):
    def failing(provider, kind, ref, limit):
        raise SpotifyError(code)

    monkeypatch.setattr(catalog, "get_tracks", failing)
    got_status, body = await web_add("https://www.deezer.com/album/1")
    assert got_status == status
    assert body == {"ok": False, "error": controls.ControlError(code).message}


async def test_web_add_soundcloud_set_is_listed_with_its_url(
    web_add, player, monkeypatch, media_dir
):
    downloads = []
    seen = []

    def fake_info(url):
        seen.append(url)
        return [
            {"id": "1", "title": "A", "url": "https://api.soundcloud.com/tracks/1"},
            {"id": "2", "title": "B", "url": "https://api.soundcloud.com/tracks/2"},
            {"id": "3", "title": "C", "url": "https://api.soundcloud.com/tracks/3"},
        ]

    def fake_download(url, **kwargs):
        downloads.append(url)
        track = make_track(media_dir, f"S{len(downloads)}")
        track["url"] = url
        return track

    monkeypatch.setattr(controls, "MAX_PLAYLIST_SIZE", 2)
    monkeypatch.setattr(controls, "MAX_PLAYLIST_FULL", 2)
    monkeypatch.setattr(media, "get_playlist_info", fake_info)
    monkeypatch.setattr(media, "download_media", fake_download)
    status, _ = await web_add("https://soundcloud.com/forss/sets/soulhack?si=1")
    await asyncio.sleep(0.4)
    assert status == 202
    assert seen == ["https://soundcloud.com/forss/sets/soulhack"]
    assert downloads == [
        "https://api.soundcloud.com/tracks/1",
        "https://api.soundcloud.com/tracks/2",
    ]


async def test_web_add_rejects_unknown_hosts(web_add):
    status, body = await web_add("https://example.com/forss/flickermood")
    assert status == 400 and "SoundCloud" in body["error"]


@pytest.mark.parametrize(
    "provider, kind, ref",
    [("deezer", "album", "302127"), ("apple", "album", ("us", "x", "1", None))],
)
def test_network_failure_is_not_worded_for_spotify(monkeypatch, provider, kind, ref):
    def down(url, headers=None, data=None):
        raise SpotifyError("spotify_unreadable", "network: down")

    monkeypatch.setattr(spotify, "_http", down)
    with pytest.raises(SpotifyError) as e:
        catalog.get_tracks(provider, kind, ref, 10)
    assert e.value.code == "link_unreadable"
    assert "Spotify" not in controls.ControlError(e.value.code).message
