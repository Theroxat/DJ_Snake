"""Web API contract and the controls behind it, with a fake voice client."""

import asyncio
import os
import types

import pytest

from djsnake import media, state
from djsnake.controls import control_seek
from djsnake.playback import play_next, queue_playlist
from djsnake.player import file_in_use
from djsnake.sessions import open_session
from fakes import FakeVoiceClient, make_track, settle, titles

STATE_KEYS = {
    "active",
    "in_voice",
    "guild",
    "channel",
    "loading",
    "paused",
    "can_previous",
    "crossfade",
    "crossfading",
    "volume",
    "server_time",
    "viewers",
    "now_playing",
    "queue",
    "downloading",
    "downloading_total",
    "theme",
}
NOW_PLAYING_KEYS = {
    "id",
    "title",
    "duration",
    "thumbnail",
    "url",
    "video_url",
    "elapsed",
    "start",
    "end",
}
QUEUE_KEYS = {"id", "title", "duration", "thumbnail", "url"}


@pytest.fixture
def api(client, player):
    """Helpers bound to the player's session: state() and control(**body)."""

    async def get_state():
        r = await client.get(f"/api/{player.sid}/state")
        return r.status, await r.json()

    async def control(**body):
        r = await client.post(f"/api/{player.sid}/control", json=body)
        return r.status, await r.json()

    async def raw_control(data):
        r = await client.post(f"/api/{player.sid}/control", data=data)
        return r.status

    return types.SimpleNamespace(
        state=get_state, control=control, raw_control=raw_control
    )


@pytest.fixture
async def tracks(player, ctx, media_dir):
    """A playing, B C D queued; E F created but not queued."""
    made = {name: make_track(media_dir, name) for name in "ABCDEF"}
    for name in "ABCD":
        player.add_to_queue(made[name])
    await play_next(ctx)
    return made


def test_session_link_posted(player, channel):
    assert any(player.sid in message for message in channel.log)


# --- State ---------------------------------------------------------------------


async def test_state_shape(api, tracks):
    status, s = await api.state()
    assert status == 200
    assert set(s) == STATE_KEYS
    assert set(s["now_playing"]) == NOW_PLAYING_KEYS
    assert s["now_playing"]["title"] == "A"
    assert titles(s["queue"]) == ["B", "C", "D"]
    assert set(s["queue"][0]) == QUEUE_KEYS


async def test_unknown_session_state_is_inactive(client):
    r = await client.get("/api/nosuchsid/state")
    assert r.status == 404
    assert await r.json() == {"active": False}


# --- Next / previous -----------------------------------------------------------


async def test_next_plays_the_following_track(api, player, tracks):
    status, body = await api.control(action="next")
    await settle()
    _, s = await api.state()
    assert (status, body) == (200, {"ok": True})
    assert s["now_playing"]["title"] == "B"
    assert titles(player.history) == ["A"]


async def test_previous_early_goes_back_to_the_last_track(api, client, player, tracks):
    await api.control(action="next")
    await settle()
    status, _ = await api.control(action="previous")
    await settle()
    _, s = await api.state()
    assert status == 200
    assert s["now_playing"]["title"] == "A"
    assert titles(s["queue"]) == ["B", "C", "D"]
    assert player.history == []
    r = await client.get(f"/media/{player.sid}/{tracks['A']['id']}")
    assert r.status == 200


async def test_media_is_never_cached_by_a_shared_cache(client, player, tracks):
    url = f"/media/{player.sid}/{tracks['A']['id']}"
    r = await client.get(url, headers={"Range": "bytes=0-1"})
    assert r.status == 206
    assert r.headers["Cache-Control"] == "private, no-cache"
    # The page's retry adds a cache-busting query string: same file.
    r = await client.get(url + "?r=1")
    assert r.status == 200
    assert r.headers["Cache-Control"] == "private, no-cache"


async def test_previous_after_five_seconds_restarts_the_track(api, player, tracks):
    await api.control(action="next")
    await settle()
    player.started_at -= 10
    status, _ = await api.control(action="previous")
    await settle()
    _, s = await api.state()
    assert status == 200
    assert s["now_playing"]["title"] == "B"
    assert s["now_playing"]["elapsed"] < 1
    assert titles(player.history) == ["A"]


# --- Pause / resume ------------------------------------------------------------


async def test_pause_freezes_elapsed_and_resume_excludes_paused_time(
    api, player, tracks
):
    vc = player.voice_client
    status, _ = await api.control(action="pause")
    _, s1 = await api.state()
    await asyncio.sleep(0.5)
    _, s2 = await api.state()
    assert status == 200 and s1["paused"] and vc.is_paused()
    assert s1["now_playing"]["elapsed"] == s2["now_playing"]["elapsed"]

    status, _ = await api.control(action="resume")
    await asyncio.sleep(0.2)
    _, s3 = await api.state()
    frozen = s2["now_playing"]["elapsed"]
    assert status == 200 and not s3["paused"] and vc.is_playing()
    assert frozen + 0.15 < s3["now_playing"]["elapsed"] < frozen + 0.4


# --- Seek ----------------------------------------------------------------------


async def test_seek_restarts_the_track_at_the_position(
    api, player, channel, ffmpeg, tracks
):
    vc = player.voice_client
    played, messages = len(vc.played), len(channel.log)
    status, body = await api.control(action="seek", position=30)
    await settle()
    _, s = await api.state()
    assert (status, body) == (200, {"ok": True})
    assert s["now_playing"]["title"] == "A"
    assert 30 <= s["now_playing"]["elapsed"] < 30.4
    assert ffmpeg.last == {"options": "-vn", "before_options": "-ss 30.000"}
    # Not a skip: no history push, file kept, one restart, no announcement.
    assert player.history == []
    assert os.path.exists(tracks["A"]["filepath"])
    assert len(vc.played) == played + 1
    assert len(channel.log) == messages
    assert titles(s["queue"]) == ["B", "C", "D"]


async def test_seek_is_clamped_to_duration_minus_one(api, tracks):
    status, _ = await api.control(action="seek", position=1000)
    await settle()
    _, s = await api.state()
    assert status == 200
    assert 99 <= s["now_playing"]["elapsed"] < 99.4


async def test_seek_while_paused_stays_paused(api, player, tracks):
    await api.control(action="pause")
    status, _ = await api.control(action="seek", position=50.5)
    await settle()
    _, s1 = await api.state()
    await asyncio.sleep(0.3)
    _, s2 = await api.state()
    assert status == 200 and s1["paused"] and s2["paused"]
    assert player.voice_client.is_paused()
    assert s1["now_playing"]["elapsed"] == s2["now_playing"]["elapsed"] == 50.5

    await api.control(action="resume")
    await asyncio.sleep(0.2)
    _, s = await api.state()
    assert not s["paused"]
    assert 50.6 < s["now_playing"]["elapsed"] < 51


@pytest.mark.parametrize("position", [-1, "abc", True, None, [3]])
async def test_seek_rejects_bad_positions(api, player, tracks, position):
    played = len(player.voice_client.played)
    status, body = await api.control(action="seek", position=position)
    await settle()
    assert (status, body) == (400, {"ok": False, "error": "Invalid position."})
    assert len(player.voice_client.played) == played


async def test_seek_rejects_infinity(api, tracks):
    assert await api.raw_control('{"action":"seek","position":Infinity}') == 400


# --- Queue edits ---------------------------------------------------------------


async def test_move_reorders_the_queue(api, player, tracks):
    for name in "EF":
        player.add_to_queue(tracks[name])
    status, _ = await api.control(action="move", id=tracks["F"]["id"], index=0)
    _, s = await api.state()
    assert status == 200
    assert titles(s["queue"]) == ["F", "B", "C", "D", "E"]


@pytest.mark.parametrize("index", [9, -1, True])
async def test_move_rejects_bad_indexes(api, tracks, index):
    status, body = await api.control(action="move", id=tracks["B"]["id"], index=index)
    assert status == 400 and body["ok"] is False


async def test_remove_drops_the_track_and_its_file(api, player, tracks):
    player.add_to_queue(tracks["E"])
    status, _ = await api.control(action="remove", id=tracks["E"]["id"])
    _, s = await api.state()
    assert status == 200
    assert "E" not in titles(s["queue"])
    assert not os.path.exists(tracks["E"]["filepath"])


async def test_remove_unknown_track_is_404(api, tracks):
    status, body = await api.control(action="remove", id="nope")
    assert status == 404 and body["ok"] is False


async def test_jump_plays_the_track_now(api, player, channel, ffmpeg, tracks):
    messages = len(channel.log)
    status, _ = await api.control(action="jump", id=tracks["D"]["id"])
    await settle()
    _, s = await api.state()
    assert status == 200
    assert s["now_playing"]["title"] == "D"
    assert titles(s["queue"]) == ["B", "C"]  # Earlier tracks keep their order
    assert titles(player.history) == ["A"]  # Current one goes to history
    assert os.path.exists(tracks["A"]["filepath"])
    assert ffmpeg.last == {"options": "-vn"}
    assert channel.log[messages:] == []


async def test_jump_unknown_track_is_404(api, tracks):
    status, body = await api.control(action="jump", id="nope")
    assert status == 404 and body["ok"] is False


async def test_jump_without_id_is_400(api, tracks):
    status, _ = await api.control(action="jump")
    assert status == 400


# --- Request validation --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com@evil.com/watch?v=dQw4w9WgXcQ",
        "http://127.0.0.1:4416/",
        "file:///etc/passwd",
        "https://www.youtube.com/channel/UC123",
    ],
)
async def test_add_rejects_non_youtube_links(api, tracks, url):
    status, body = await api.control(action="add", url=url)
    assert status == 400 and body["ok"] is False


async def test_unknown_action_is_400(api, tracks):
    status, _ = await api.control(action="bogus")
    assert status == 400


async def test_non_json_body_is_400(api, tracks):
    assert await api.raw_control("not json") == 400


async def test_body_over_4kb_is_400(api, tracks):
    body = '{"action":"next","p":"' + "x" * 5000 + '"}'
    assert await api.raw_control(body) == 400


async def test_control_on_unknown_session_is_404(client):
    r = await client.post("/api/nosuchsid/control", json={"action": "next"})
    assert r.status == 404
    assert (await r.json())["ok"] is False


# --- Add from the web page -----------------------------------------------------


@pytest.fixture
def downloads(monkeypatch, media_dir):
    """Fake media.download_media: records URLs, fails for ccccccccccc."""
    calls = []

    def fake_download(url, **kwargs):
        calls.append(url)
        if "ccccccccccc" in url:
            raise RuntimeError("Video unavailable")
        track = make_track(media_dir, "G")
        track["url"] = url
        return track

    monkeypatch.setattr(media, "download_media", fake_download)
    return calls


async def test_add_single_track(api, channel, tracks, downloads):
    status, body = await api.control(
        action="add", url="https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz"
    )
    await settle()
    _, s = await api.state()
    assert (status, body) == (202, {"ok": True})
    assert downloads == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
    assert s["queue"][-1]["title"] == "G"
    assert len(channel.log) == 1  # Only the panel: no announcement


async def test_add_playlist(api, player, channel, tracks, downloads, monkeypatch):
    monkeypatch.setattr(
        media,
        "get_playlist_info",
        lambda url: [
            {"id": "aaaaaaaaaaa", "title": "P1"},
            {"id": "ccccccccccc", "title": "Gone"},
            {"id": "bbbbbbbbbbb", "title": "P2"},
        ],
    )
    messages = len(channel.log)
    status, _ = await api.control(
        action="add", url="https://music.youtube.com/playlist?list=PLabc"
    )
    await asyncio.sleep(0.4)
    assert status == 202
    assert len(downloads) == 3
    assert not player.loading
    assert channel.log[messages:] == []
    assert player.load_line == "✅ 2/3 tracks loaded (1 unavailable)"


async def test_playlist_stopped_midway_says_so_in_the_panel(
    player, ctx, monkeypatch, media_dir
):
    sent = []

    async def quiet_send(content=None, **kwargs):
        sent.append(content)

    def stopping_download(url, **kwargs):
        player.epoch += 1  # Like !stop during the first download
        return make_track(media_dir, "S")

    monkeypatch.setattr(media, "download_media", stopping_download)
    pctx = types.SimpleNamespace(
        guild=ctx.guild, channel=ctx.channel, author=None, send=quiet_send
    )
    await queue_playlist(pctx, player, [{"id": "aaaaaaaaaaa"}, {"id": "bbbbbbbbbbb"}])
    assert sent == []
    assert player.load_line == "⏹️ Loading stopped (0/2)"


async def test_playlist_loads_without_posting_messages(
    player, ctx, downloads, monkeypatch
):
    async def failing_send(content=None, **kwargs):
        raise AssertionError("queue_playlist must not post messages")

    pctx = types.SimpleNamespace(
        guild=ctx.guild, channel=ctx.channel, author=None, send=failing_send
    )
    await queue_playlist(pctx, player, [{"id": "aaaaaaaaaaa"}])
    assert player.load_line == "✅ 1/1 tracks loaded"


# --- History -------------------------------------------------------------------


async def test_history_is_capped_and_evicted_files_deleted(
    api, player, tracks, media_dir
):
    for i in range(5):
        player.add_to_queue(make_track(media_dir, f"Q{i}"))
    for _ in range(7):
        await api.control(action="next")
        await settle()
    assert len(player.history) == 5
    assert titles(player.history) == ["C", "D", "Q0", "Q1", "Q2"]
    for name in "AB":
        assert not os.path.exists(tracks[name]["filepath"])
        assert not file_in_use(tracks[name]["filepath"])


# --- Leaving voice, rejoining, expiry ------------------------------------------


async def leave_voice(player):
    async with player.lock:
        player.stop_and_clear()
    await player.disconnect_voice_client()
    player.should_stop = False


async def test_session_survives_leaving_voice(api, player, tracks, media_dir):
    await leave_voice(player)
    status, s = await api.state()
    assert status == 200 and s["active"] and not s["in_voice"]
    assert s["now_playing"] is None and s["queue"] == []
    assert sorted(os.listdir(media_dir)) == ["E.mp4", "F.mp4"]  # Never queued


@pytest.mark.parametrize(
    "body",
    [
        {"action": "add", "url": "https://youtu.be/dQw4w9WgXcQ"},
        {"action": "next"},
        {"action": "seek", "position": 10},
        {"action": "jump", "id": "whatever"},
    ],
    ids=["add", "next", "seek", "jump"],
)
async def test_controls_out_of_voice_are_409(api, player, tracks, body):
    await leave_voice(player)
    status, response = await api.control(**body)
    assert status == 409 and response["ok"] is False


async def test_rejoin_reuses_the_session_and_reposts_the_panel(
    api, player, ctx, channel, tracks
):

    sid = player.sid
    await leave_voice(player)
    channel.log.clear()
    player.voice_client = FakeVoiceClient()
    await open_session(ctx, player, announce=True)
    assert player.sid == sid
    assert any(sid in message for message in channel.log)
    assert len(channel.deletes) == 1  # The old panel
    assert channel.messages[-1] is player.panel_message
    assert not channel.messages[-1].deleted

    status, body = await api.control(action="seek", position=10)
    assert status == 409 and body["ok"] is False  # In voice, nothing playing


async def test_idle_session_expires(api, player, ctx, tracks, media_dir, fake_clock):

    sid = player.sid
    await leave_voice(player)
    for name in "EF":
        os.remove(tracks[name]["filepath"])
    player.history.append(make_track(media_dir, "H2"))
    fake_clock(7 * 3600)
    status, s = await api.state()
    assert (status, s) == (404, {"active": False})
    assert os.listdir(media_dir) == []  # History files deleted
    assert player.sid is None
    assert sid not in state.sessions

    player.voice_client = FakeVoiceClient()
    await open_session(ctx, player, announce=True)
    assert player.sid and player.sid != sid


# --- Automix window in playback and state -----------------------------------------


async def test_automix_window_in_playback_and_state(
    client, player, ctx, ffmpeg, media_dir
):
    track = make_track(media_dir, "AM", start=5.0, end=90.0, loudnorm=None)
    player.add_to_queue(track)
    await play_next(ctx)
    assert ffmpeg.last["before_options"] == "-ss 5.000 -t 85.000"

    r = await client.get(f"/api/{player.sid}/state")
    s = (await r.json())["now_playing"]
    assert (s["start"], s["end"], s["duration"]) == (5.0, 90.0, 100.0)
    assert 5.0 <= s["elapsed"] < 5.5  # File time

    await control_seek(player, 60)
    await settle()
    r = await client.get(f"/api/{player.sid}/state")
    s = (await r.json())["now_playing"]
    assert ffmpeg.last["before_options"] == "-ss 60.000 -t 30.000"
    assert 60.0 <= s["elapsed"] < 60.5
