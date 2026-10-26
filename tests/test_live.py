"""Live streams: YouTube lives, Twitch channels and internet radios, played
without a download (media.live_*, probe_stream, playback's live path)."""

import io
import sqlite3
import types

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
    def __init__(self, body=b"", ctype="audio/mpeg", status=200, **headers):
        super().__init__(body)
        self.status = status
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

    def fake_open(url, **kwargs):
        media._check_public(url)
        answer = pages[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(
        media.socket,
        "getaddrinfo",
        lambda host, port=None: [
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


def test_probe_refuses_hls_and_reads_pls(web_of):
    """HLS radios are refused: FFmpeg would fetch their segments from any
    host named in the playlist."""
    hls = "https://radio.example/live.m3u8"
    web_of[hls] = FakeResponse(
        b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchunk.m3u8\n",
        ctype="application/vnd.apple.mpegurl",
    )
    with pytest.raises(media.StreamError) as e:
        media.probe_stream(hls)
    assert e.value.code == "radio_hls"
    assert ControlError("radio_hls").status == 400
    disguised = "https://radio.example/list.m3u"
    web_of[disguised] = FakeResponse(
        b"#EXTM3U\n#EXT-X-TARGETDURATION:6\nhttp://10.0.0.1/seg.ts\n",
        ctype="audio/x-mpegurl",
    )
    with pytest.raises(media.StreamError) as e:
        media.probe_stream(disguised)
    assert e.value.code == "radio_hls"
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
    web_of[hop] = FakeResponse(status=302, location="http://lan.example/admin")
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


async def test_radio_is_piped_and_ffmpeg_may_open_nothing_else(
    ctx, player, ffmpeg, monkeypatch
):
    made = []

    class Spy(media.RadioStream):
        def __init__(self, url):
            super().__init__(url)
            made.append(self)

    monkeypatch.setattr(media, "RadioStream", Spy)
    await start(ctx, player, live())
    assert player.voice_client.played == ["fip.mp3"]
    call = ffmpeg.last
    assert call["pipe"] is True
    assert call["before_options"] == "-protocol_whitelist pipe"
    assert "-reconnect" not in call["before_options"]
    reader = made[-1]
    assert reader.name == STREAM and not reader._closed
    player.mixer.cleanup()
    assert reader._closed  # The connection goes with the source


async def test_live_pipeline_args(ctx, player, ffmpeg):
    track = live(source="twitch", http_headers={"User-Agent": "Mozilla/5.0 (X11)"})
    await start(ctx, player, track)
    assert player.voice_client.played == ["fip.mp3"]
    before, options = ffmpeg.last["before_options"], ffmpeg.last["options"]
    assert before.startswith(config.LIVE_RECONNECT)
    assert "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5" in before
    assert "-protocol_whitelist http,https,tcp,tls,crypto" in before
    assert "pipe" not in before and "file" not in before
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


async def test_live_plays_are_not_in_the_stats(ctx, player, media_dir):
    """Hours of radio would top the charts and inflate the listening time."""
    await start(ctx, player, live(), make_track(media_dir, "B"))
    player.started_at -= 1800  # Half an hour of radio
    player.voice_client.finish()
    await settle()
    player.voice_client.finish()  # B ends too: the database has a row
    await settle()
    stats.flush()
    with sqlite3.connect(stats.DB_PATH) as conn:
        titles_in_db = [r[0] for r in conn.execute("SELECT title FROM plays")]
    assert "FIP" not in titles_in_db
    summary = stats.summary(1, "week")
    assert all(t["title"] != "FIP" for t in summary["top_tracks"])


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


# --- SSRF hardening ---------------------------------------------------------------


def addrinfo(*ips):
    return [(2, 1, 6, "", (ip, 0)) for ip in ips]


@pytest.mark.parametrize(
    "ips",
    [
        ("127.0.0.1",),
        ("10.0.0.7",),
        ("169.254.169.254",),
        ("172.18.0.3",),  # A Compose network (pot-provider)
        ("93.184.216.34", "192.168.1.10"),  # One private answer is enough
        ("::1",),
        ("fd00::1",),
    ],
)
def test_private_resolutions_are_refused(monkeypatch, ips):
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda h, p=None: addrinfo(*ips))
    with pytest.raises(media.StreamError) as e:
        media._resolve_public("radio.example", 443)
    assert e.value.code == "bad_url"


def test_connection_is_pinned_to_the_checked_address(monkeypatch):
    """DNS rebinding: the name resolves to a public address for the check and
    to a private one afterwards. The bot resolves once and connects to the
    address it checked, so the second answer is never used."""
    answers = [addrinfo("93.184.216.34"), addrinfo("127.0.0.1")]
    lookups, connects = [], []

    def getaddrinfo(host, port=None):
        lookups.append(host)
        return answers[min(len(lookups) - 1, 1)]

    def create_connection(address, timeout=None):
        connects.append(address)
        raise OSError("stop here")

    monkeypatch.setattr(media.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(media.socket, "create_connection", create_connection)
    with pytest.raises(OSError):
        media._request("http://rebind.example/stream.mp3")
    assert lookups == ["rebind.example"]
    assert connects == [("93.184.216.34", 80)]


def test_redirects_to_private_hosts_are_refused(web_of):
    for location in (
        "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:47823/",
        "file:///etc/passwd",
    ):
        hop = "https://radio.example/hop.mp3"
        web_of[hop] = FakeResponse(status=302, location=location)
        with pytest.raises(media.StreamError) as e:
            media.probe_stream(hop)
        assert e.value.code in ("bad_url", "stream_unreachable"), location


def test_pot_provider_name_resolving_privately_is_refused(monkeypatch):
    monkeypatch.setattr(
        media.socket, "getaddrinfo", lambda h, p=None: addrinfo("172.18.0.2")
    )
    with pytest.raises(media.StreamError) as e:
        media._check_public("http://pot-provider:4416/x.mp3")
    assert e.value.code == "bad_url"


def test_yt_dlp_live_urls_on_private_ips_are_refused():
    with pytest.raises(RuntimeError):
        media.check_live_url("http://10.0.0.1/live.m3u8")
    with pytest.raises(RuntimeError):
        media.check_live_url("file:///etc/passwd")
    assert media.check_live_url("https://cdn.example/a.m3u8")


class FakeSock:
    def __init__(self, chunks=()):
        self.chunks, self.sent, self.closed = list(chunks), b"", False

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        return self.chunks.pop(0) if self.chunks else b""

    def sendall(self, data):
        self.sent += data

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def test_request_speaks_http_1_0_and_reads_icy(monkeypatch):
    monkeypatch.setattr(
        media.socket, "getaddrinfo", lambda h, p=None: addrinfo("93.184.216.34")
    )
    sock = FakeSock(
        [b"ICY 200 OK\r\nicy-name: FIP\r\ncontent-type: audio/mpeg\r\n\r\nabc"]
    )
    monkeypatch.setattr(media.socket, "create_connection", lambda a, timeout=None: sock)
    response = media._request("http://radio.example:8000/;", icy=True)
    assert response.status == 200 and response.headers.get("icy-name") == "FIP"
    assert response.read(10) == b"abc"
    assert sock.sent.startswith(b"GET /; HTTP/1.0\r\nHost: radio.example:8000\r\n")
    assert b"Icy-MetaData: 1" in sock.sent


def test_reads_stop_at_the_deadline():
    response = media._Response(FakeSock([b"x"] * 100), 200, None, b"", deadline=0.0)
    with pytest.raises(TimeoutError):
        response.read(10)


def test_icy_title_has_an_overall_deadline(monkeypatch):
    calls = []

    def slow_follow(url, icy=True, deadline=None, opener=None):
        calls.append(deadline)
        raise TimeoutError("radio request deadline")

    monkeypatch.setattr(media, "_follow", slow_follow)
    assert media.read_icy_title(STREAM) is None
    assert calls and calls[0] is not None


def test_radio_stream_reconnects_then_gives_up(monkeypatch):
    monkeypatch.setattr(media.time, "sleep", lambda s: None)
    monkeypatch.setattr(media, "LIVE_STREAM_RECONNECTS", 2)
    opened = []

    def stream_open(url, icy=False, deadline=None):
        opened.append((url, icy))
        if len(opened) == 1:
            return FakeResponse(b"audio")
        raise OSError("down")

    monkeypatch.setattr(media, "_stream_open", stream_open)
    reader = media.RadioStream(STREAM)
    assert reader.read(5) == b"audio"
    assert reader.read(5) == b""  # Ended, reconnects failed
    assert opened[0] == (STREAM, False)  # No ICY metadata in the audio
    assert len(opened) == 3


def test_radio_stream_redirect_is_checked_again(monkeypatch):
    monkeypatch.setattr(media.time, "sleep", lambda s: None)
    monkeypatch.setattr(media, "LIVE_STREAM_RECONNECTS", 0)
    monkeypatch.setattr(
        media,
        "_stream_open",
        lambda url, **kw: FakeResponse(status=301, location="http://10.0.0.1/a.mp3"),
    )
    assert media.RadioStream(STREAM).read(5) == b""


async def test_expired_live_url_is_extracted_again(ctx, player, ffmpeg, monkeypatch):
    seen = []

    def resolve(url):
        seen.append(url)
        return media.live_track(url, "https://cdn.example/fresh.m3u8", "T", "twitch")

    monkeypatch.setattr(media, "resolve_live", resolve)
    track = live(source="twitch")
    track["url"] = "https://www.twitch.tv/somechannel"
    track["resolved_at"] -= config.LIVE_REFRESH_SECONDS + 1
    await start(ctx, player, track)
    assert seen == ["https://www.twitch.tv/somechannel"]
    assert ffmpeg.calls and track["stream_url"] == "https://cdn.example/fresh.m3u8"


async def test_fresh_live_and_radio_urls_are_not_extracted_again(
    ctx, player, monkeypatch
):
    seen = []
    monkeypatch.setattr(media, "resolve_live", lambda url: seen.append(url))
    old_radio = live()
    old_radio["resolved_at"] -= 3600
    await start(ctx, player, live(source="twitch"))
    await controls.control_next(player)
    player.queue.append(old_radio)
    await settle()
    assert seen == []


async def test_live_limit_does_not_count_pauses(ctx, player, media_dir, monkeypatch):
    monkeypatch.setattr(config, "LIVE_MAX_HOURS", 0.3 / 3600)
    await start(ctx, player, live(source="twitch"), make_track(media_dir, "B"))
    await controls.control_pause(player)
    await settle(0.5)  # Longer than the limit, but paused
    assert player.now_playing["live"]
    await controls.control_resume(player)
    await settle(0.6)
    assert player.now_playing["title"] == "B"
