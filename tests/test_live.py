"""Live streams: YouTube lives, Twitch channels and internet radios, played
without a download (media.live_*, probe_stream, playback's live path)."""

import io
import sqlite3
import types
import urllib.error

import pytest

from djsnake import commands, config, controls, media, state, stats
from djsnake.controls import ControlError, control_seek
from djsnake.playback import play_next
from djsnake.sessions import render_panel
from fakes import make_track, settle, titles

STREAM = "https://radio.example/fip.mp3"


def live(source="radio", **fields):
    return media.live_track(
        "https://radio.example/fip.mp3", STREAM, "FIP", source, **fields
    )


async def start(ctx, player, *tracks):
    player.queue.extend(tracks)
    await play_next(ctx)


# --- Detection ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.twitch.tv/SomeChannel", "https://www.twitch.tv/somechannel"),
        ("https://twitch.tv/some_channel/", "https://www.twitch.tv/some_channel"),
        ("https://m.twitch.tv/abc", "https://www.twitch.tv/abc"),
        (
            "https://www.youtube.com/live/dQw4w9WgXcQ?si=x",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
    ],
)
def test_live_links_are_accepted_and_rebuilt(url, expected):
    assert media.parse_media_url(url) == ("single", expected)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.twitch.tv/directory",
        "https://www.twitch.tv/videos/123456",
        "https://www.twitch.tv/someone/clip/abc",
        "https://www.twitch.tv/a",  # Too short for a channel
        "https://twitch.tv.evil.example/someone",
    ],
)
def test_twitch_pages_that_are_not_a_channel_are_refused(url):
    assert media.parse_media_url(url) is None


def test_source_of_twitch():
    assert media.source_of("https://www.twitch.tv/abc") == "twitch"


@pytest.mark.parametrize(
    "url",
    [
        "https://icecast.radiofrance.fr/fip-midfi.mp3",
        "http://ice1.somafm.com/groovesalad-128-mp3",
        "https://somafm.com/groovesalad.pls",
        "https://radio.example:8000/live.m3u8",
        "http://radio.example:8000/stream",
        "http://radio.example:8000/;",
        "https://radio.example/listen.m3u?x=1",
    ],
)
def test_radio_links(url):
    assert media.parse_radio_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a",  # Not a stream path
        "https://example.com/",
        "http://127.0.0.1:8000/stream",  # Private addresses
        "http://10.0.0.5/radio.mp3",
        "http://[::1]/radio.mp3",
        "http://169.254.169.254/latest/stream",
        "ftp://radio.example/a.mp3",
        "https://user:pw@radio.example/a.mp3",
        "https://www.youtube.com/stream",  # Hosts with their own parser
        "https://www.twitch.tv/stream",
        "radio.example/a.mp3",
        "https://radio.example/a b.mp3",
    ],
)
def test_radio_links_refused(url):
    assert media.parse_radio_url(url) is None


def test_pick_live_format_prefers_audio_only():
    formats = [
        {"url": "https://t/160p.m3u8", "protocol": "m3u8_native", "height": 160},
        {
            "url": "https://t/audio.m3u8",
            "protocol": "m3u8_native",
            "vcodec": "none",
            "abr": 160,
        },
        {"url": "https://t/1080p.m3u8", "protocol": "m3u8_native", "height": 1080},
    ]
    assert media.pick_live_format(formats)["url"] == "https://t/audio.m3u8"


def test_pick_live_format_takes_the_smallest_muxed_360p():
    formats = [
        {"url": "https://y/91.m3u8", "protocol": "m3u8_native", "height": 144},
        {"url": "https://y/93.m3u8", "protocol": "m3u8_native", "height": 360},
        {"url": "https://y/95.m3u8", "protocol": "m3u8_native", "height": 720},
        {"url": "https://y/dash.mpd", "protocol": "http_dash_segments"},
        {"url": "rtmp://y/x", "protocol": "rtmp", "vcodec": "none"},
        {"url": "https://y/v.m3u8", "protocol": "m3u8", "acodec": "none"},
    ]
    assert media.pick_live_format(formats)["url"] == "https://y/93.m3u8"
    assert media.pick_live_format(formats[:1])["url"] == "https://y/91.m3u8"
    assert media.pick_live_format(formats[3:]) is None


LIVE_INFO = {
    "id": "dQw4w9WgXcQ",
    "title": "Lofi radio 2026-10-25 12:00",
    "fulltitle": "Lofi radio",
    "is_live": True,
    "live_status": "is_live",
    "thumbnail": "https://i.ytimg.com/vi/x/hq.jpg",
    "channel": "Lofi Girl",
    "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "formats": [
        {
            "url": "https://manifest.example/93.m3u8",
            "protocol": "m3u8_native",
            "height": 360,
            "http_headers": {"User-Agent": "Mozilla/5.0 (X11; Linux)"},
        }
    ],
}


class FakeYDL:
    """yt_dlp.YoutubeDL stand-in: calls the match filter like yt-dlp does
    before any download, and records whether a download would start."""

    downloads = []

    def __init__(self, params):
        self.params = params

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        info = dict(self.info)
        # As YoutubeDL.process_video_result(): before the format selection,
        # `incomplete` is the (truthy) set of format fields.
        self.params["match_filter"](info, incomplete={"url", "ext"})
        FakeYDL.downloads.append(url)
        raise RuntimeError("would download")


def test_download_media_turns_a_live_into_a_live_item(monkeypatch):
    FakeYDL.info, FakeYDL.downloads = LIVE_INFO, []
    monkeypatch.setattr(media.yt_dlp, "YoutubeDL", FakeYDL)
    titles_seen = []
    track = media.download_media(LIVE_INFO["webpage_url"], on_title=titles_seen.append)
    assert FakeYDL.downloads == []  # Stopped before the download
    assert track["live"] is True and track["filepath"] is None
    assert track["stream_url"] == "https://manifest.example/93.m3u8"
    assert track["title"] == "Lofi radio" == titles_seen[0]
    assert track["duration"] is None and track["end"] is None
    assert track["source"] == "youtube" and track["channel"] == "Lofi Girl"


def test_download_media_still_downloads_what_is_not_live(monkeypatch):
    FakeYDL.info = {**LIVE_INFO, "is_live": False, "live_status": "was_live"}
    FakeYDL.downloads = []
    monkeypatch.setattr(media.yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(media, "DOWNLOAD_ATTEMPTS", 1)
    with pytest.raises(RuntimeError):
        media.download_media(LIVE_INFO["webpage_url"])
    assert FakeYDL.downloads == [LIVE_INFO["webpage_url"]]


# --- Radio probe ----------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", ctype="audio/mpeg", **headers):
        super().__init__(body)
        message = types.SimpleNamespace()
        values = {
            "content-type": ctype,
            **{k.replace("_", "-"): v for k, v in headers.items()},
        }
        message.get = lambda key, default=None: values.get(key.lower(), default)
        message.get_content_type = lambda: ctype.split(";")[0].strip().lower()
        self.headers = message


@pytest.fixture
def web_of(monkeypatch):
    """Fake the HTTP side of the radio probe: url -> response or exception."""
    pages = {}

    def fake_open(url):
        media._check_public(url)
        answer = pages[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(
        media.socket,
        "getaddrinfo",
        lambda host, port: [
            (2, 1, 6, "", ("10.1.2.3" if "lan" in host else "93.184.216.34", 0))
        ],
    )
    monkeypatch.setattr(media, "_open", fake_open)
    return pages


def test_probe_icecast_stream(web_of):
    web_of[STREAM] = FakeResponse(ctype="audio/mpeg", icy_name="FIP")
    track = media.probe_stream(STREAM)
    assert track["live"] and track["stream_url"] == STREAM
    assert track["title"] == "FIP" and track["source"] == "radio"


def test_probe_hls_and_pls(web_of):
    hls = "https://radio.example/live.m3u8"
    web_of[hls] = FakeResponse(
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchunk.m3u8\n",
        ctype="application/vnd.apple.mpegurl",
    )
    assert media.probe_stream(hls)["stream_url"] == hls
    pls = "https://radio.example/station.pls"
    web_of[pls] = FakeResponse(
        b"[playlist]\nFile1=https://cdn.example/a\nTitle1=Groove Salad\n",
        ctype="audio/x-scpls",
    )
    web_of["https://cdn.example/a"] = FakeResponse(ctype="audio/aac")
    track = media.probe_stream(pls)
    assert track["stream_url"] == "https://cdn.example/a"
    assert track["url"] == pls and track["title"] == "Groove Salad"


def test_probe_refuses_pages_and_private_hops(web_of):
    page = "https://radio.example/stream"
    web_of[page] = FakeResponse(b"<html>", ctype="text/html")
    with pytest.raises(media.StreamError) as e:
        media.probe_stream(page)
    assert e.value.code == "not_a_stream"
    hop = "https://radio.example/hop.mp3"
    web_of[hop] = urllib.error.HTTPError(
        hop, 302, "Found", {"Location": "http://lan.example/admin"}, None
    )
    with pytest.raises(media.StreamError) as e:
        media.probe_stream(hop)
    assert e.value.code == "bad_url"
    m3u = "https://radio.example/a.m3u"
    web_of[m3u] = FakeResponse(b"http://lan.example/x\n", ctype="audio/x-mpegurl")
    with pytest.raises(media.StreamError) as e:
        media.probe_stream(m3u)
    assert e.value.code == "bad_url"


def test_icy_title(web_of):
    meta = b"StreamTitle='Daft Punk - One More Time';StreamUrl='';"
    meta += b"\0" * (-len(meta) % 16)
    body = b"x" * 32 + bytes([len(meta) // 16]) + meta
    web_of[STREAM] = FakeResponse(body, icy_metaint="32")
    assert media.read_icy_title(STREAM) == "Daft Punk - One More Time"
    web_of[STREAM] = FakeResponse(b"x" * 64)  # No icy-metaint
    assert media.read_icy_title(STREAM) is None
    assert media.parse_icy_metadata(b"StreamTitle='Caf\xe9';") == "Café"


# --- Playback ---------------------------------------------------------------------


async def test_live_pipeline_args(ctx, player, ffmpeg):
    track = live(http_headers={"User-Agent": "Mozilla/5.0 (X11)"})
    await start(ctx, player, track)
    assert player.voice_client.played == ["fip.mp3"]
    before, options = ffmpeg.last["before_options"], ffmpeg.last["options"]
    assert before.startswith(config.LIVE_RECONNECT)
    assert "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5" in before
    assert "-user_agent 'Mozilla/5.0 (X11)'" in before
    assert "-ss" not in before and "-t " not in before
    assert options.startswith("-vn -af loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=in")
    assert "measured_I" not in options
    assert player.mixer.remaining_seconds() is None  # Unknown length: no crossfade


async def test_seek_is_refused_on_a_live_item(ctx, player, client):
    await start(ctx, player, live())
    with pytest.raises(ControlError) as e:
        await control_seek(player, 30)
    assert e.value.code == "live_no_seek"
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "seek", "position": 30}
    )
    assert r.status == 409
    assert (await r.json())["error"] == ControlError("live_no_seek").message
    assert player.pending_action is None


async def test_queue_continues_after_the_stream_ends(ctx, player, media_dir):
    radio = live()
    await start(ctx, player, radio, make_track(media_dir, "B"))
    player.voice_client.finish()  # The stream ended (FFmpeg gave up)
    await settle()
    assert player.now_playing["title"] == "B"
    assert titles(player.history) == ["FIP"]
    assert "Live stream ended: FIP" in player.last_action


async def test_loop_track_does_not_replay_an_ended_live(ctx, player, media_dir):
    player.loop = "track"
    await start(ctx, player, live(), make_track(media_dir, "B"))
    player.voice_client.finish()
    await settle()
    assert player.now_playing["title"] == "B"


async def test_loop_queue_does_not_requeue_an_ended_live(ctx, player, media_dir):
    player.loop = "queue"
    await start(ctx, player, live(), make_track(media_dir, "B"))
    player.voice_client.finish()
    await settle()
    assert player.now_playing["title"] == "B" and titles(player.queue) == []


async def test_loop_queue_requeues_a_skipped_live(ctx, player, media_dir):
    player.loop = "queue"
    await start(ctx, player, live(), make_track(media_dir, "B"))
    await controls.control_next(player)
    await settle()
    assert player.now_playing["title"] == "B" and titles(player.queue) == ["FIP"]


async def test_state_fields_of_a_live_item(ctx, player, client, media_dir):
    radio = live()
    await start(ctx, player, radio, live())
    radio["live_title"] = "Daft Punk - One More Time"
    r = await client.get(f"/api/{player.sid}/state")
    s = await r.json()
    now = s["now_playing"]
    assert now["live"] is True and now["video_url"] is None
    assert now["end"] is None and now["duration"] is None and now["audio_only"]
    assert now["live_title"] == "Daft Punk - One More Time"
    assert s["queue"][0]["live"] is True
    r = await client.get(f"/media/{player.sid}/{radio['id']}")
    assert r.status == 404
    r = await client.get(f"/api/{player.sid}/lyrics/{radio['id']}")
    assert r.status == 404
    panel = render_panel(player)
    assert "FIP — Daft Punk - One More Time" in panel and "LIVE" in panel


async def test_live_max_hours_moves_on(ctx, player, media_dir, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_HOURS", 0.2 / 3600)
    await start(ctx, player, live(source="twitch"), make_track(media_dir, "B"))
    assert player.live_task is not None
    await settle(0.5)
    assert player.now_playing["title"] == "B"
    assert "stopped after" in player.last_action
    assert player.live_task is None


async def test_no_limit_and_no_watcher_left_behind(ctx, player, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_HOURS", 0.0)
    await start(ctx, player, live(source="youtube"))
    await settle(0.05)
    assert player.live_task.done()
    player.stop_and_clear()
    assert player.live_task is None


async def test_radio_title_is_read_while_it_plays(ctx, player, monkeypatch):
    monkeypatch.setattr(media, "read_icy_title", lambda url: "Artist - Song")
    await start(ctx, player, live())
    await settle(0.2)
    assert player.now_playing["live_title"] == "Artist - Song"
    player.stop_and_clear()
    assert player.live_task is None


async def test_live_listening_time_is_recorded(ctx, player, media_dir):
    await start(ctx, player, live(), make_track(media_dir, "B"))
    player.started_at -= 1800  # Half an hour of radio
    player.voice_client.finish()
    await settle()
    stats.flush()
    with sqlite3.connect(stats.DB_PATH) as conn:
        row = conn.execute(
            "SELECT title, duration, played_seconds, skipped FROM plays"
        ).fetchone()
    assert row[0] == "FIP" and row[1] is None and row[2] >= 1800 and row[3] == 0
    summary = stats.summary(1, "week")
    assert summary["totals"]["hours"] > 0
    assert summary["top_tracks"][0]["title"] == "FIP"


# --- Adding ---------------------------------------------------------------------


@pytest.fixture
def probe(monkeypatch):
    seen = []

    def fake_probe(url):
        seen.append(url)
        if "bad" in url:
            raise media.StreamError("not_a_stream")
        return media.live_track(url, url, "Station", "radio")

    monkeypatch.setattr(media, "probe_stream", fake_probe)
    return seen


async def test_web_add_radio(client, player, probe):
    url = "https://radio.example/station.mp3"
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "add", "url": url}
    )
    assert r.status == 202
    await settle()
    assert probe == [url]
    assert player.now_playing["live"] and player.now_playing["title"] == "Station"
    r = await client.post(
        f"/api/{player.sid}/control",
        json={"action": "add", "url": "https://radio.example/bad.mp3"},
    )
    assert r.status == 400
    assert (await r.json())["error"] == ControlError("not_a_stream").message


async def test_web_add_radio_needs_voice(client, player, probe):
    player.voice_client = None
    r = await client.post(
        f"/api/{player.sid}/control",
        json={"action": "add", "url": "https://radio.example/a.mp3"},
    )
    assert r.status == 409 and probe == []


async def test_play_command_radio(ctx, channel, player, probe, monkeypatch):
    async def join(c):
        return True

    monkeypatch.setattr(commands, "join_voice_channel", join)
    await commands.play.callback(ctx, query="https://radio.example/fip.mp3")
    await settle()
    assert player.now_playing["title"] == "Station" and player.now_playing["live"]
    await commands.play.callback(ctx, query="https://radio.example/bad.mp3")
    assert "not an audio stream" in channel.log[-1]


async def test_autoplay_never_seeds_from_a_live(ctx, player):
    from djsnake.playback import _autoplay_seed

    await start(ctx, player, live(video_id="dQw4w9WgXcQ"))
    assert _autoplay_seed(player) is None
    assert state.music_players[1] is player
