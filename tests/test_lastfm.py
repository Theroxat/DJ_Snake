"""Last.fm scrobbling: signature, linking, thresholds, who is scrobbled and
the batched, retried queue. The HTTP client (lastfm._request) is faked."""

import hashlib
import json
import os
import time
import types

import pytest

from djsnake import commands, config, lastfm, state
from djsnake.player import MusicPlayer, retire_play
from fakes import FakeAuthor, FakeVoiceClient, make_ctx, make_slash_ctx, make_track
from conftest import GUILD_ID


class FakeLastfm:
    """Records every request body and answers per method."""

    def __init__(self):
        self.calls = []
        self.answers = {}  # method -> dict, Exception or callable(body)

    async def __call__(self, body):
        self.calls.append(dict(body))
        answer = self.answers.get(body["method"], {})
        if callable(answer):
            answer = answer(body)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict) and "error" in answer:
            return 200, answer
        return 200, answer

    def methods(self):
        return [c["method"] for c in self.calls]


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(config, "LASTFM_API_KEY", "key123")
    monkeypatch.setattr(config, "LASTFM_API_SECRET", "secret456")
    fake = FakeLastfm()
    monkeypatch.setattr(lastfm, "_request", fake)
    return fake


def member(user_id, bot=False):
    return types.SimpleNamespace(id=user_id, bot=bot)


def link(user_id, name="user", key="sk"):
    state.lastfm_links[user_id] = {"name": name, "key": key}


@pytest.fixture
def room(api):
    """A player in a voice channel whose members can be changed."""
    player = MusicPlayer()
    player.voice_client = FakeVoiceClient()
    player.voice_client.channel = types.SimpleNamespace(members=[])
    state.music_players[GUILD_ID] = player
    return player


def start(player, track):
    player.now_playing = track
    player.started_at = time.monotonic()
    player.mark_started(0.0, same_track=False)


async def settle_tasks():
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)
    for task in list(state.background_tasks):
        await task


# --- Signature and calls -----------------------------------------------------


def test_signature_is_md5_of_sorted_params_and_secret():
    params = {"method": "auth.getSession", "api_key": "abc", "token": "tok"}
    expected = "api_keyabcmethodauth.getSessiontokentok" + "sec"
    assert lastfm.sign(params, "sec") == hashlib.md5(expected.encode()).hexdigest()


def test_signature_ignores_format_and_callback_and_handles_unicode():
    base = {"artist": "Sigur Rós", "track": "Hoppípolla"}
    plain = lastfm.sign(base, "s")
    assert lastfm.sign({**base, "format": "json", "callback": "x"}, "s") == plain
    text = "artistSigur Róstrack" + "Hoppípolla" + "s"
    assert plain == hashlib.md5(text.encode("utf-8")).hexdigest()


async def test_call_signs_and_asks_for_json(api):
    api.answers["auth.getToken"] = {"token": "t"}
    assert await lastfm.call("auth.getToken") == {"token": "t"}
    body = api.calls[0]
    assert body["format"] == "json" and body["api_key"] == "key123"
    unsigned = {k: v for k, v in body.items() if k not in ("api_sig", "format")}
    assert body["api_sig"] == lastfm.sign(unsigned, "secret456")


async def test_api_errors_raise_with_their_code(api):
    api.answers["auth.getToken"] = {"error": 10, "message": "Invalid API key"}
    with pytest.raises(lastfm.LastfmError) as caught:
        await lastfm.call("auth.getToken")
    assert caught.value.code == 10


async def test_server_errors_and_junk_are_unavailable(api, monkeypatch):
    async def down(body):
        return 503, None

    monkeypatch.setattr(lastfm, "_request", down)
    with pytest.raises(lastfm.LastfmUnavailable):
        await lastfm.call("track.scrobble")


def test_disabled_without_both_credentials(monkeypatch):
    monkeypatch.setattr(config, "LASTFM_API_KEY", "k")
    monkeypatch.setattr(config, "LASTFM_API_SECRET", "")
    assert not lastfm.enabled()
    monkeypatch.setattr(config, "LASTFM_API_SECRET", "s")
    assert lastfm.enabled()


# --- Linking and storage -----------------------------------------------------


async def test_link_flow_stores_the_session_key_privately(api):
    api.answers["auth.getToken"] = {"token": "tok1"}
    kind, url = await lastfm.start_link(7)
    assert kind == "url"
    assert url == "https://www.last.fm/api/auth/?api_key=key123&token=tok1"

    # Not authorised yet: running link again gives the same link back
    api.answers["auth.getSession"] = {"error": 14, "message": "not authorised"}
    assert await lastfm.start_link(7) == ("url", url)
    assert not os.path.exists(lastfm.FILE)

    api.answers["auth.getSession"] = {"session": {"name": "rj", "key": "SK-secret"}}
    assert await lastfm.start_link(7) == ("linked", "rj")
    assert api.calls[-1]["token"] == "tok1"
    assert os.stat(lastfm.FILE).st_mode & 0o777 == 0o600
    assert state.lastfm_pending_auth == {}

    state.lastfm_links.clear()
    lastfm.load_links()
    assert state.lastfm_links == {7: {"name": "rj", "key": "SK-secret"}}


async def test_expired_token_gets_a_fresh_one(api):
    api.answers["auth.getToken"] = {"token": "tok1"}
    await lastfm.start_link(7)
    api.answers["auth.getToken"] = {"token": "tok2"}
    api.answers["auth.getSession"] = {"error": 15, "message": "expired"}
    kind, url = await lastfm.start_link(7)
    assert kind == "url" and url.endswith("token=tok2")


async def test_done_without_a_link_in_progress(api):
    with pytest.raises(LookupError):
        await lastfm.complete_link(7)


def test_unlink_removes_the_key_and_queued_scrobbles(api):
    link(7)
    link(8)
    lastfm.save_links()
    state.lastfm_queue[7] = [{"artist": "a", "track": "b", "timestamp": 1}]
    assert lastfm.unlink(7) is True
    assert lastfm.unlink(7) is False
    assert 7 not in state.lastfm_queue
    with open(lastfm.FILE, encoding="utf-8") as f:
        assert list(json.load(f)) == ["8"]


def test_bad_entries_are_dropped_on_load(data_dir):
    data_dir.mkdir(exist_ok=True)
    with open(lastfm.FILE, "w", encoding="utf-8") as f:
        json.dump({"5": {"name": "ok", "key": "k"}, "x": {}, "6": {"key": ""}}, f)
    lastfm.load_links()
    assert state.lastfm_links == {5: {"name": "ok", "key": "k"}}


# --- What is scrobbled -------------------------------------------------------


def test_thresholds():
    assert lastfm.should_scrobble(200, 100)  # half
    assert not lastfm.should_scrobble(200, 99)
    assert lastfm.should_scrobble(1000, 240)  # 4 minutes
    assert not lastfm.should_scrobble(1000, 239)
    assert not lastfm.should_scrobble(29, 29)  # too short
    assert lastfm.should_scrobble(30, 15)
    assert not lastfm.should_scrobble(200, None)
    assert lastfm.should_scrobble(None, 240) and not lastfm.should_scrobble(None, 100)


def test_youtube_titles_are_parsed_and_spotify_metadata_wins(media_dir):
    yt = make_track(
        media_dir, "a", title="Daft Punk - Get Lucky (Official Video)", duration=369
    )
    assert lastfm.track_info(yt) == {
        "artist": "Daft Punk",
        "track": "Get Lucky",
        "duration": 369,
    }
    sp = make_track(
        media_dir,
        "b",
        title="whatever (lyrics)",
        artist="Muse",
        lyrics_title="Uprising",
        lyrics_duration=304,
        album="The Resistance",
    )
    assert lastfm.track_info(sp) == {
        "artist": "Muse",
        "track": "Uprising",
        "album": "The Resistance",
        "duration": 304,
    }
    assert lastfm.track_info(None) is None


async def test_now_playing_goes_to_linked_listeners_only(room, api, media_dir):
    link(1, key="sk1")
    link(2, key="sk2")  # linked but not in the channel
    room.voice_client.channel.members = [member(1), member(3), member(99, bot=True)]
    track = make_track(media_dir, "t", title="Artist - Song", duration=200)
    start(room, track)
    await settle_tasks()
    assert api.methods() == ["track.updateNowPlaying"]
    call = api.calls[0]
    assert (call["artist"], call["track"], call["sk"]) == ("Artist", "Song", "sk1")
    assert call["duration"] == 200
    assert room.lastfm_listeners == {1}


async def test_scrobble_for_those_present_at_start_and_end(room, api, media_dir):
    for uid in (1, 2, 3):
        link(uid, key=f"sk{uid}")
    room.voice_client.channel.members = [member(1), member(2)]
    track = make_track(media_dir, "t", title="Artist - Song", duration=200)
    start(room, track)
    await settle_tasks()
    # 2 leaves, 3 joins late; the track was played for 150 s
    room.voice_client.channel.members = [member(1), member(3)]
    room.play_accum = 150.0
    retire_play(room, GUILD_ID, None)
    await settle_tasks()
    scrobbles = [c for c in api.calls if c["method"] == "track.scrobble"]
    assert len(scrobbles) == 1
    assert scrobbles[0]["sk"] == "sk1"
    assert scrobbles[0]["artist[0]"] == "Artist" and scrobbles[0]["track[0]"] == "Song"
    assert scrobbles[0]["timestamp[0]"] == int(room.play_wall)
    assert state.lastfm_queue == {}  # sent and cleared


async def test_short_or_skipped_tracks_are_not_scrobbled(room, api, media_dir):
    link(1)
    room.voice_client.channel.members = [member(1)]
    short = make_track(media_dir, "s", title="A - B", duration=20)
    start(room, short)
    room.play_accum = 20.0
    retire_play(room, GUILD_ID, None)
    skipped = make_track(media_dir, "k", title="A - B", duration=200)
    start(room, skipped)
    room.play_accum = 30.0
    retire_play(room, GUILD_ID, "next")
    await settle_tasks()
    assert "track.scrobble" not in api.methods()


async def test_nothing_happens_when_disabled(room, monkeypatch, media_dir):
    link(1)
    room.voice_client.channel.members = [member(1)]
    called = []

    async def request(body):
        called.append(body)
        return 200, {}

    monkeypatch.setattr(lastfm, "_request", request)
    monkeypatch.setattr(config, "LASTFM_API_KEY", "")
    track = make_track(media_dir, "t", title="A - B", duration=200)
    start(room, track)
    room.play_accum = 200.0
    retire_play(room, GUILD_ID, None)
    await settle_tasks()
    assert called == [] and state.lastfm_queue == {}


# --- The queue ---------------------------------------------------------------


def queue_scrobbles(user_id, count):
    state.lastfm_queue[user_id] = [
        {"artist": f"A{i}", "track": f"T{i}", "timestamp": 1000 + i, "album": "Al"}
        for i in range(count)
    ]


async def test_batches_of_fifty(api):
    link(1, key="sk1")
    queue_scrobbles(1, 120)
    assert await lastfm.flush() is True
    sent = [c for c in api.calls if c["method"] == "track.scrobble"]
    assert len(sent) == 3
    assert "artist[49]" in sent[0] and "artist[50]" not in sent[0]
    assert "artist[19]" in sent[2] and "artist[20]" not in sent[2]
    assert sent[0]["artist[0]"] == "A0" and sent[1]["artist[0]"] == "A50"
    assert sent[0]["album[0]"] == "Al" and sent[0]["timestamp[1]"] == 1001
    assert state.lastfm_queue == {}


async def test_network_failure_keeps_scrobbles_for_a_retry(api):
    link(1)
    queue_scrobbles(1, 3)
    api.answers["track.scrobble"] = lastfm.LastfmUnavailable("down")
    assert await lastfm.flush() is False
    assert len(state.lastfm_queue[1]) == 3
    api.answers["track.scrobble"] = {"scrobbles": {"@attr": {"accepted": 3}}}
    assert await lastfm.flush() is True
    assert state.lastfm_queue == {}


async def test_temporary_api_errors_are_retried_permanent_ones_dropped(api):
    link(1)
    queue_scrobbles(1, 2)
    api.answers["track.scrobble"] = {"error": 29, "message": "rate limit"}
    assert await lastfm.flush() is False
    assert len(state.lastfm_queue[1]) == 2
    api.answers["track.scrobble"] = {"error": 6, "message": "invalid params"}
    assert await lastfm.flush() is True
    assert state.lastfm_queue == {}


async def test_dead_session_key_unlinks_the_user(api):
    link(1)
    lastfm.save_links()
    queue_scrobbles(1, 2)
    api.answers["track.scrobble"] = {"error": 9, "message": "invalid session"}
    await lastfm.flush()
    assert state.lastfm_links == {} and state.lastfm_queue == {}


async def test_flush_task_retries_with_a_backoff(api, monkeypatch):
    link(1)
    queue_scrobbles(1, 1)
    delays = []

    async def no_wait(seconds):
        delays.append(seconds)

    monkeypatch.setattr(lastfm, "_sleep", no_wait)
    answers = [lastfm.LastfmUnavailable("x")] * 2 + [{}]
    api.answers["track.scrobble"] = lambda body: answers.pop(0)
    await lastfm._flush_loop()
    assert delays == [30.0, 60.0]
    assert state.lastfm_queue == {}


def test_queue_is_capped(room, api, monkeypatch, media_dir):
    link(1)
    room.voice_client.channel.members = [member(1)]
    monkeypatch.setattr(config, "LASTFM_MAX_PENDING", 3)
    track = make_track(media_dir, "t", title="A - B", duration=100)
    for _ in range(5):
        start(room, track)
        room.play_accum = 100.0
        retire_play(room, GUILD_ID, None)
    assert len(state.lastfm_queue[1]) == 3


# --- Commands ----------------------------------------------------------------


@pytest.fixture
def slash(channel):
    author = FakeAuthor()
    author.id = 7
    return make_slash_ctx(GUILD_ID, channel, author)


async def test_commands_say_so_when_disabled(slash, channel):
    await commands.lastfm_link.callback(slash)
    assert slash.replies == [
        ("❌ Last.fm scrobbling is not enabled on this bot.", True)
    ]


async def test_slash_link_replies_privately_with_a_done_button(api, slash):
    api.answers["auth.getToken"] = {"token": "tok1"}
    await commands.lastfm_link.callback(slash)
    assert slash.interaction.deferred == [True]
    ((text, ephemeral),) = slash.replies
    assert ephemeral and "token=tok1" in text


async def test_prefix_link_goes_to_direct_messages(api, channel):
    author = FakeAuthor()
    author.id = 7
    ctx = make_ctx(GUILD_ID, channel, author)
    api.answers["auth.getToken"] = {"token": "tok1"}
    await commands.lastfm_link.callback(ctx)
    assert "token=tok1" in author.dms[0]
    assert "token=tok1" not in " ".join(channel.log)


async def test_prefix_link_with_closed_dms_fails_without_the_link(api, channel):
    author = FakeAuthor(dms_open=False)
    author.id = 7
    ctx = make_ctx(GUILD_ID, channel, author)
    api.answers["auth.getToken"] = {"token": "tok1"}
    await commands.lastfm_link.callback(ctx)
    assert "direct message" in channel.log[-1]
    assert "tok1" not in " ".join(channel.log)


async def test_done_button_completes_the_link(api):
    api.answers["auth.getToken"] = {"token": "tok1"}
    await lastfm.start_link(7)
    api.answers["auth.getSession"] = {"error": 14, "message": "no"}
    assert "not been authorised" in await commands._lastfm_finish(7, "en")
    api.answers["auth.getSession"] = {"session": {"name": "rj", "key": "k"}}
    assert "**rj**" in await commands._lastfm_finish(7, "en")
    assert lastfm.link_of(7)["name"] == "rj"


async def test_status_and_unlink(api, slash):
    await commands.lastfm_status.callback(slash)
    assert "No Last.fm account" in slash.replies[-1][0]
    link(7, name="rj")
    queue_scrobbles(7, 2)
    await commands.lastfm_status.callback(slash)
    assert "**rj**" in slash.replies[-1][0] and "2 scrobbles" in slash.replies[-1][0]
    assert "sk" not in slash.replies[-1][0]
    await commands.lastfm_unlink.callback(slash)
    assert "unlinked" in slash.replies[-1][0]
    assert lastfm.link_of(7) is None
