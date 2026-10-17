"""Spotify links: URL parsing, the embed page parser, YouTube match scoring
and the loading flow. All offline: fixtures are saved, trimmed embed pages and
yt-dlp search results (tests/fixtures/spotify/)."""

import asyncio
import json
import os
import types

import pytest

from djsnake import controls, media, spotify
from djsnake.spotify import SpotifyError
from fakes import make_track, settle

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "spotify")
ID = "37i9dQZF1DXcBWIGoYBM5M"


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# --- URL parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (f"https://open.spotify.com/playlist/{ID}", ("playlist", ID)),
        (f"https://open.spotify.com/playlist/{ID}?si=abc123&pi=x", ("playlist", ID)),
        (f"http://open.spotify.com/album/{ID}/", ("album", ID)),
        (f"https://open.spotify.com/intl-fr/track/{ID}", ("track", ID)),
        (f"https://open.spotify.com/intl-pt-br/album/{ID}", ("album", ID)),
        (f"https://open.spotify.com/embed/playlist/{ID}", ("playlist", ID)),
        (f"  https://OPEN.SPOTIFY.COM/Track/{ID}  ", ("track", ID)),
        (f"spotify:playlist:{ID}", ("playlist", ID)),
        (f"spotify:track:{ID}", ("track", ID)),
    ],
)
def test_check_url_accepts_spotify_links(url, expected):
    assert spotify.check_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://evil.com/playlist/" + ID,
        f"https://open.spotify.com.evil.com/playlist/{ID}",
        "http://127.0.0.1:4416/",
        "file:///etc/passwd",
        "not a url",
        None,
        "x" * 3000,
    ],
)
def test_check_url_ignores_other_links(url):
    assert spotify.check_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/artist/{ID}",
        f"https://open.spotify.com/show/{ID}",
        "https://open.spotify.com/playlist/short",
        f"https://open.spotify.com/playlist/{ID}x",
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5-",
        f"https://open.spotify.com/playlist/{ID}/../../etc",
        f"https://user@open.spotify.com/playlist/{ID}",
        f"https://open.spotify.com:8080/playlist/{ID}",
        f"ftp://open.spotify.com/playlist/{ID}",
        f"spotify:artist:{ID}",
        "spotify:playlist:../../x",
        f"spotify:user:someone:playlist:{ID}",
    ],
)
def test_check_url_rejects_bad_spotify_links(url):
    with pytest.raises(SpotifyError) as info:
        spotify.check_url(url)
    assert info.value.code == "bad_url"


@pytest.mark.parametrize(
    "url", ["https://spotify.link/AbCdEf123", "https://spotify.app.link/xyz"]
)
def test_short_links_are_refused_not_followed(url, monkeypatch):
    monkeypatch.setattr(spotify, "_http", _no_network)
    with pytest.raises(SpotifyError) as info:
        spotify.check_url(url)
    assert info.value.code == "spotify_short_link"


def test_requests_are_rebuilt_from_the_id():
    assert (
        spotify.embed_url("album", ID) == f"https://open.spotify.com/embed/album/{ID}"
    )
    assert spotify.canonical_url("track", ID) == f"https://open.spotify.com/track/{ID}"
    with pytest.raises(ValueError):
        spotify.fetch_embed_tracks("album", "../../evil")


def _no_network(*args, **kwargs):
    raise AssertionError("no network in tests")


# --- Embed page parser ---------------------------------------------------------


def test_parse_embed_playlist():
    tracks = spotify.parse_embed_html(fixture("embed_playlist.html"), "playlist")
    assert len(tracks) == 4
    assert tracks[0] == {
        "title": "expectations (feat. Chappell Roan)",
        "artists": ["Olivia Rodrigo", "Chappell Roan"],
        "duration": 223.935,
    }
    assert tracks[1]["artists"] == ["ADÉLA"]


def test_parse_embed_album():
    tracks = spotify.parse_embed_html(fixture("embed_album.html"), "album")
    assert [t["title"] for t in tracks] == [
        "One More Time",
        "Aerodynamic",
        "Digital Love",
        "Harder, Better, Faster, Stronger",
    ]
    assert all(t["artists"] == ["Daft Punk"] for t in tracks)
    assert tracks[0]["duration"] == pytest.approx(320.357)


def test_parse_embed_track():
    tracks = spotify.parse_embed_html(fixture("embed_track.html"), "track")
    assert tracks == [
        {"title": "Blinding Lights", "artists": ["The Weeknd"], "duration": 200.04}
    ]


def test_artist_names_with_commas_stay_whole():
    page = fixture("embed_playlist.html").replace("Ashe", "Tyler, The Creator")
    tracks = spotify.parse_embed_html(page, "playlist")
    assert tracks[2]["artists"] == ["Tyler, The Creator"]


def test_non_track_items_are_skipped():
    page = fixture("embed_playlist.html").replace(
        '"entityType": "track"', '"entityType": "episode"', 1
    )
    tracks = spotify.parse_embed_html(page, "playlist")
    assert len(tracks) == 3 and tracks[0]["title"] == "Nicole Kidman"


def test_parse_embed_not_found():
    with pytest.raises(SpotifyError) as info:
        spotify.parse_embed_html(fixture("embed_not_found.html"), "playlist")
    assert info.value.code == "spotify_not_found"


@pytest.mark.parametrize(
    "html, kind",
    [
        ("<html>no data</html>", "playlist"),
        (
            '<script id="__NEXT_DATA__" type="application/json">{oops</script>',
            "playlist",
        ),
        (
            '<script id="__NEXT_DATA__" type="application/json">'
            '{"props": {"pageProps": {"other": 1}}}</script>',
            "album",
        ),
        (None, "album"),
    ],
)
def test_parse_embed_changed_page_is_unreadable(html, kind):
    with pytest.raises(SpotifyError) as info:
        spotify.parse_embed_html(html, kind)
    assert info.value.code == "spotify_unreadable"


def test_parse_embed_wrong_kind_is_unreadable():
    with pytest.raises(SpotifyError) as info:
        spotify.parse_embed_html(fixture("embed_album.html"), "playlist")
    assert info.value.code == "spotify_unreadable"


def test_fetch_embed_uses_only_the_embed_url(monkeypatch):
    calls = []

    def fake_http(url, headers=None, data=None):
        calls.append(url)
        return 200, fixture("embed_album.html").encode()

    monkeypatch.setattr(spotify, "_http", fake_http)
    tracks = spotify.get_tracks("album", ID, limit=2)
    assert calls == [f"https://open.spotify.com/embed/album/{ID}"]
    assert len(tracks) == 2


def test_fetch_embed_http_404(monkeypatch):
    monkeypatch.setattr(spotify, "_http", lambda url, **kw: (404, b""))
    with pytest.raises(SpotifyError) as info:
        spotify.get_tracks("playlist", ID, limit=30)
    assert info.value.code == "spotify_not_found"


def test_empty_playlist(monkeypatch):
    page = fixture("embed_playlist.html")
    head, rest = page.split('"trackList": [', 1)
    empty = head + '"trackList": []}}}}}}' + rest[rest.index("</script>") :]
    assert spotify.parse_embed_html(empty, "playlist") == []
    monkeypatch.setattr(spotify, "_http", lambda url, **kw: (200, empty.encode()))
    with pytest.raises(SpotifyError) as info:
        spotify.get_tracks("playlist", ID, limit=30)
    assert info.value.code == "spotify_empty"


def test_web_api_when_configured(monkeypatch):
    monkeypatch.setattr(spotify, "SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setattr(spotify, "SPOTIFY_CLIENT_SECRET", "secret")
    monkeypatch.setitem(spotify._token, "value", None)
    pages = {
        "first": {
            "items": [
                {
                    "track": {
                        "type": "track",
                        "name": "A",
                        "duration_ms": 1000,
                        "artists": [{"name": "X"}],
                    }
                },
                {"track": {"type": "episode", "name": "Pod"}},
                {"track": None},
            ],
            "next": "https://api.spotify.com/v1/playlists/p/tracks?offset=100",
        },
        "second": {
            "items": [
                {
                    "track": {
                        "type": "track",
                        "name": "B",
                        "duration_ms": 2000,
                        "artists": [{"name": "Y"}, {"name": "Z"}],
                    }
                }
            ],
            "next": None,
        },
    }
    calls = []

    def fake_http(url, headers=None, data=None):
        calls.append(url)
        if url == spotify.TOKEN_URL:
            assert headers["Authorization"].startswith("Basic ")
            return 200, b'{"access_token": "tok", "expires_in": 3600}'
        assert headers["Authorization"] == "Bearer tok"
        page = pages["second" if "offset=100" in url else "first"]
        return 200, json.dumps(page).encode()

    monkeypatch.setattr(spotify, "_http", fake_http)
    tracks = spotify.get_tracks("playlist", ID, limit=30)
    assert tracks == [
        {"title": "A", "artists": ["X"], "duration": 1.0},
        {"title": "B", "artists": ["Y", "Z"], "duration": 2.0},
    ]
    assert calls[0] == spotify.TOKEN_URL
    assert calls[1].startswith(f"https://api.spotify.com/v1/playlists/{ID}/tracks?")


def test_web_api_never_follows_foreign_next_links(monkeypatch):
    monkeypatch.setattr(spotify, "_api_token", lambda: "tok")
    with pytest.raises(SpotifyError):
        spotify._api_get("https://evil.com/v1/x")


def test_web_api_failure_falls_back_to_embed(monkeypatch):
    monkeypatch.setattr(spotify, "SPOTIFY_CLIENT_ID", "id")
    monkeypatch.setattr(spotify, "SPOTIFY_CLIENT_SECRET", "secret")

    def failing_api(kind, spotify_id, limit):
        raise SpotifyError("spotify_not_found")

    monkeypatch.setattr(spotify, "fetch_api_tracks", failing_api)
    monkeypatch.setattr(
        spotify, "_http", lambda url, **kw: (200, fixture("embed_track.html").encode())
    )
    assert spotify.get_tracks("track", ID, limit=30)[0]["title"] == "Blinding Lights"


def test_oversized_response_is_refused(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return b"x" * n

    monkeypatch.setattr(spotify._opener, "open", lambda req, timeout: Response())
    with pytest.raises(SpotifyError) as info:
        spotify._http("https://open.spotify.com/embed/album/" + ID)
    assert info.value.code == "spotify_unreadable"


# --- YouTube match scoring -----------------------------------------------------

SEARCH = json.loads(fixture("youtube_search.json"))


def song(title, artists, duration):
    return {"title": title, "artists": artists, "duration": duration}


@pytest.mark.parametrize(
    "track, query, expected_id",
    [
        # Official video and official audio both match: the video wins.
        (
            song("One More Time", ["Daft Punk"], 320.357),
            "Daft Punk - One More Time",
            "FGBhQbmPwH8",
        ),
        # The video runs a minute longer (intro), still preferred.
        (
            song("Blinding Lights", ["The Weeknd"], 200.04),
            "The Weeknd - Blinding Lights",
            "4NRXx6U8ABQ",
        ),
        # "Remastered" in the video title is not a different version.
        (
            song("Bohemian Rhapsody - Remastered 2011", ["Queen"], 354.32),
            "Queen - Bohemian Rhapsody",
            "fJ9rUzIMcZQ",
        ),
        (
            song(
                "expectations (feat. Chappell Roan)",
                ["Olivia Rodrigo", "Chappell Roan"],
                223.935,
            ),
            "Olivia Rodrigo - expectations",
            "gsZauX8gwxo",
        ),
    ],
)
def test_best_match_on_real_search_results(track, query, expected_id):
    assert spotify.search_query(track) == query
    assert spotify.best_match(track, SEARCH[query])["id"] == expected_id


def test_audio_upload_wins_when_there_is_no_video():
    results = [
        c for c in SEARCH["Daft Punk - One More Time"] if "Video" not in c["title"]
    ]
    track = song("One More Time", ["Daft Punk"], 320.357)
    assert spotify.best_match(track, results)["id"] == "A2VpR8HahKc"


def test_topic_channel_beats_a_lyrics_upload():
    track = song("Digital Love", ["Daft Punk"], 301.0)
    candidates = [
        {
            "id": "a" * 11,
            "title": "Daft Punk - Digital Love (Lyrics)",
            "duration": 300,
            "channel": "Lyrics Hub",
        },
        {
            "id": "b" * 11,
            "title": "Digital Love",
            "duration": 301,
            "channel": "Daft Punk - Topic",
        },
    ]
    assert spotify.best_match(track, candidates)["id"] == "b" * 11


@pytest.mark.parametrize(
    "variant",
    ["Live", "Cover", "Remix", "Karaoke", "Nightcore", "Sped Up", "8D Audio"],
)
def test_other_versions_are_penalised(variant):
    track = song("Halo", ["Beyoncé"], 261.0)
    base = {
        "id": "b" * 11,
        "title": "Beyonce - Halo",
        "duration": 262,
        "channel": "Some Uploader",
    }
    other = dict(base, id="v" * 11, title=f"Beyonce - Halo ({variant})")
    assert spotify.score_candidate(track, other) < spotify.score_candidate(track, base)
    assert spotify.best_match(track, [other, base])["id"] == "b" * 11


def test_versions_named_in_the_spotify_title_are_not_penalised():
    track = song("Halo - Live", ["Beyoncé"], 300.0)
    live = {
        "id": "v" * 11,
        "title": "Beyoncé - Halo (Live)",
        "duration": 301,
        "channel": "Beyoncé",
    }
    plain = {
        "id": "b" * 11,
        "title": "Beyoncé - Halo",
        "duration": 301,
        "channel": "Beyoncé",
    }
    assert spotify.score_candidate(track, live) == spotify.score_candidate(track, plain)


def test_duration_more_than_double_is_rejected():
    track = song("Halo", ["Beyoncé"], 200.0)
    long = {
        "id": "l" * 11,
        "title": "Beyoncé - Halo (Official Video)",
        "duration": 401,
        "channel": "Beyoncé",
    }
    assert spotify.score_candidate(track, long) is None
    assert spotify.best_match(track, [long]) is None


def test_closer_duration_wins():
    track = song("Halo", ["Beyoncé"], 261.0)
    near = {"id": "n" * 11, "title": "Beyoncé - Halo", "duration": 262, "channel": "x"}
    far = dict(near, id="f" * 11, duration=330)
    assert spotify.best_match(track, [far, near])["id"] == "n" * 11


def test_unrelated_titles_are_rejected():
    track = song("Halo", ["Beyoncé"], 261.0)
    other = {
        "id": "o" * 11,
        "title": "Beyoncé - Crazy In Love",
        "duration": 261,
        "channel": "Beyoncé",
    }
    assert spotify.best_match(track, [other]) is None


def test_live_streams_are_rejected():
    track = song("Halo", ["Beyoncé"], 261.0)
    stream = {
        "id": "s" * 11,
        "title": "Beyoncé - Halo",
        "duration": None,
        "channel": "Beyoncé",
        "live_status": "is_live",
    }
    assert spotify.score_candidate(track, stream) is None


def test_find_youtube_match_builds_a_canonical_url(monkeypatch):
    queries = []

    def fake_search(query, count):
        queries.append((query, count))
        return SEARCH["Daft Punk - One More Time"]

    monkeypatch.setattr(media, "search_youtube", fake_search)
    url = spotify.find_youtube_match(song("One More Time", ["Daft Punk"], 320.0))
    assert url == "https://www.youtube.com/watch?v=FGBhQbmPwH8"
    assert queries == [("Daft Punk - One More Time", 5)]


def test_find_youtube_match_survives_a_search_failure(monkeypatch):
    def failing(query, count):
        raise RuntimeError("Sign in to confirm you're not a bot")

    monkeypatch.setattr(media, "search_youtube", failing)
    assert spotify.find_youtube_match(song("A", ["B"], 100.0)) is None


# --- Loading flow (web add and !play), media mocked ----------------------------

ALBUM = [
    song("One More Time", ["Daft Punk"], 320.0),
    song("Nothing Like It", ["Nobody"], 200.0),
    song("Aerodynamic", ["Daft Punk"], 212.0),
]


@pytest.fixture
def spotify_env(monkeypatch, media_dir):
    """get_tracks returns ALBUM; the search finds every track but "Nothing
    Like It"; download_media records URLs."""
    downloads = []

    def fake_get_tracks(kind, spotify_id, limit):
        assert spotify_id == ID
        return [ALBUM[0]] if kind == "track" else ALBUM[:limit]

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

    def fake_download(url):
        downloads.append(url)
        track = make_track(media_dir, f"T{len(downloads)}")
        track["url"] = url
        return track

    monkeypatch.setattr(spotify, "get_tracks", fake_get_tracks)
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


async def test_web_add_spotify_album(web_add, player, channel, spotify_env):
    status, body = await web_add(f"https://open.spotify.com/album/{ID}?si=x")
    await asyncio.sleep(0.4)
    assert (status, body) == (202, {"ok": True})
    assert spotify_env == [
        "https://www.youtube.com/watch?v=OnexMorexTi",
        "https://www.youtube.com/watch?v=Aerodynamic",
    ]
    assert not player.loading
    assert player.load_line == "✅ 2/3 tracks loaded (1 not found on YouTube)"


async def test_web_add_spotify_track(web_add, player, spotify_env):
    status, _ = await web_add(f"spotify:track:{ID}")
    await settle(0.3)
    assert status == 202
    assert spotify_env == ["https://www.youtube.com/watch?v=OnexMorexTi"]
    assert player.now_playing["url"] == spotify_env[0]


@pytest.mark.parametrize(
    "code, status",
    [("spotify_not_found", 404), ("spotify_unreadable", 502), ("spotify_empty", 400)],
)
async def test_web_add_spotify_errors_are_french(
    web_add, monkeypatch, spotify_env, code, status
):
    def failing(kind, spotify_id, limit):
        raise SpotifyError(code)

    monkeypatch.setattr(spotify, "get_tracks", failing)
    got_status, body = await web_add(f"https://open.spotify.com/playlist/{ID}")
    assert got_status == status
    assert body == {"ok": False, "error": controls.ControlError(code).french}
    assert spotify_env == []


async def test_web_add_short_link_is_refused(web_add, spotify_env):
    status, body = await web_add("https://spotify.link/AbCdEf")
    assert status == 400 and "spotify.link" in body["error"]


async def test_spotify_playlist_respects_max_size(
    web_add, player, monkeypatch, spotify_env
):
    monkeypatch.setattr(controls, "MAX_PLAYLIST_SIZE", 2)
    await web_add(f"https://open.spotify.com/playlist/{ID}")
    await asyncio.sleep(0.4)
    assert player.load_line == "✅ 1/2 tracks loaded (1 not found on YouTube)"


async def test_spotify_loading_stops_with_the_epoch(player, ctx, monkeypatch):
    searched = []

    def stopping_search(track):
        searched.append(track["title"])
        player.epoch += 1  # Like !stop during the first search
        return None

    monkeypatch.setattr(spotify, "find_youtube_match", stopping_search)
    pctx = types.SimpleNamespace(
        guild=ctx.guild, channel=ctx.channel, author=None, send=_no_send
    )
    from djsnake.playback import queue_spotify

    await queue_spotify(pctx, player, "album", ALBUM)
    assert searched == ["One More Time"]
    assert player.load_line == "⏹️ Loading stopped (0/3)"


async def _no_send(content=None, **kwargs):
    raise AssertionError("no message expected")


async def test_single_track_without_match_says_so(player, ctx, channel, monkeypatch):
    monkeypatch.setattr(spotify, "find_youtube_match", lambda track: None)
    from djsnake.playback import queue_spotify

    await queue_spotify(ctx, player, "track", [song("Halo", ["Beyoncé"], 261.0)])
    assert "No YouTube match found for **Halo — Beyoncé**" in channel.log[-1]
