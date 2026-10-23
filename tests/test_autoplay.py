"""Autoplay: when the queue runs out, a related track keeps the music going.
yt-dlp is faked (related mix and download), no network."""

import types

import pytest

from djsnake import media, state
from djsnake.controls import control_autoplay
from djsnake.playback import play_next
from djsnake.player import file_in_use
from djsnake.sessions import render_panel
from fakes import make_track, settle

SEED = "AAAAAAAAAAA"


def vid(n):
    return f"vid{n:08d}"[:11]


def entry(n, **fields):
    item = {
        "id": vid(n),
        "title": f"Song {n}",
        "duration": 200,
        "channel": "Chan",
        "live_status": None,
    }
    item.update(fields)
    return item


@pytest.fixture
def yt(monkeypatch, media_dir):
    """Fake yt-dlp: `related` is the mix, `downloads` records what was fetched."""

    class Fake:
        related = []
        related_calls = []
        downloads = []
        fail_ids = set()
        durations = {}

    fake = Fake()
    fake.related_calls, fake.downloads = [], []

    def related_tracks(video_id):
        fake.related_calls.append(video_id)
        return list(fake.related)

    def download_media(url, **kwargs):
        video_id = url.split("v=")[1]
        fake.downloads.append(video_id)
        if video_id in fake.fail_ids:
            raise RuntimeError("boom")
        return make_track(
            media_dir,
            video_id,
            video_id=video_id,
            duration=fake.durations.get(video_id, 200.0),
        )

    monkeypatch.setattr(media, "related_tracks", related_tracks)
    monkeypatch.setattr(media, "download_media", download_media)
    return fake


@pytest.fixture
def api(client, player):
    async def get_state():
        r = await client.get(f"/api/{player.sid}/state")
        return r.status, await r.json()

    async def control(**body):
        r = await client.post(f"/api/{player.sid}/control", json=body)
        return r.status, await r.json()

    return types.SimpleNamespace(state=get_state, control=control)


@pytest.fixture
async def playing(player, ctx, media_dir, yt):
    """Autoplay on, one track (the seed) playing with an empty queue."""
    player.autoplay = True
    yt.related = [entry(0, id=SEED), entry(1), entry(2)]
    player.add_to_queue(make_track(media_dir, "seed", video_id=SEED))
    await play_next(ctx)
    await settle()
    return player


def test_choose_related_filters():
    candidates = [
        entry(1),
        entry(1),  # duplicate
        entry(2),  # excluded below
        entry(3, duration=601),
        entry(4, live_status="is_live"),
        entry(5, title="Lofi Podcast episode"),
        entry(6, duration=None),
        entry(7, duration=600),
    ]
    picks = media.choose_related(candidates, {vid(2)})
    assert [p["id"] for p in picks] == [vid(1), vid(6), vid(7)]


async def test_off_by_default_lingers(player, ctx, media_dir, yt):
    player.add_to_queue(make_track(media_dir, "A", video_id=SEED))
    await play_next(ctx)
    player.voice_client.finish()
    await settle()
    assert yt.related_calls == [] and not player.is_playing
    assert player.lingering()


async def test_prefetch_then_plays_without_gap(playing, yt):
    p = playing
    assert yt.related_calls == [SEED]
    # Picked and downloaded while the seed still plays, not queued.
    assert p.autoplay_next["video_id"] == vid(1)
    assert yt.downloads == [vid(1)] and not p.queue
    p.voice_client.finish()
    await settle()
    assert p.is_playing and p.now_playing["video_id"] == vid(1)
    assert p.now_playing["autoplay"] is True
    assert "📻 Autoplay" in render_panel(p)
    assert not p.lingering()
    # No second download for the switch itself; the next prefetch seeds from it.
    assert yt.downloads[:1] == [vid(1)]
    assert yt.related_calls[-1] == vid(1)


async def test_history_dedupe(playing, yt):
    p = playing
    for _ in range(2):
        p.voice_client.finish()
        await settle()
    played = [t["video_id"] for t in p.history] + [p.now_playing["video_id"]]
    assert played == [SEED, vid(1), vid(2)]
    assert SEED in p.recent_ids and vid(1) in p.recent_ids


async def test_fallback_to_idle_when_nothing_found(playing, yt):
    p = playing
    yt.related = [entry(0, id=SEED), entry(3, duration=900)]
    p.drop_autoplay_next()
    p.voice_client.finish()
    await settle()
    assert not p.is_playing and p.now_playing is None
    assert p.lingering()


async def test_fallback_when_downloads_fail(playing, yt):
    p = playing
    p.drop_autoplay_next()
    yt.fail_ids = {vid(1), vid(2)}
    p.voice_client.finish()
    await settle()
    assert p.lingering() and not p.is_playing


async def test_too_long_after_download_is_skipped(playing, yt):
    p = playing
    p.drop_autoplay_next()
    yt.related = [entry(1, duration=None), entry(2)]
    yt.durations = {vid(1): 3000.0}
    p.voice_client.finish()
    await settle()
    assert p.now_playing["video_id"] == vid(2)
    assert not file_in_use(p.now_playing["filepath"].replace(vid(2), vid(1)))


async def test_loop_takes_precedence(playing, yt):
    p = playing
    p.loop = "track"
    p.drop_autoplay_next()
    yt.related_calls.clear()
    p.voice_client.finish()
    await settle()
    assert yt.related_calls == [] and p.lingering()


async def test_queued_track_wins_over_prepared_pick(playing, media_dir, yt):
    p = playing
    assert p.autoplay_next is not None
    p.add_to_queue(make_track(media_dir, "mine", video_id="mine0000000"))
    p.voice_client.finish()
    await settle()
    assert p.now_playing["title"] == "mine"
    assert p.autoplay_next is not None  # Kept for the next time the queue ends
    assert file_in_use(p.autoplay_next["filepath"])


async def test_turning_off_drops_prepared_track(playing):
    p = playing
    path = p.autoplay_next["filepath"]
    control_autoplay(p, False)
    assert p.autoplay_next is None and not p.autoplay
    import os

    assert not os.path.exists(path)
    p.voice_client.finish()
    await settle()
    assert p.lingering()


async def test_turning_on_mid_track_prefetches(player, ctx, media_dir, yt):
    yt.related = [entry(1)]
    player.add_to_queue(make_track(media_dir, "seed", video_id=SEED))
    await play_next(ctx)
    assert yt.related_calls == []
    control_autoplay(player, True)
    await settle()
    assert player.autoplay_next["video_id"] == vid(1)


async def test_recent_ids_capped(player):
    for n in range(80):
        player.note_played({"video_id": vid(n)})
    assert len(player.recent_ids) == 50 and player.recent_ids[-1] == vid(79)


async def test_panel_shows_autoplay_on(player):
    assert "Autoplay" not in render_panel(player)
    player.autoplay = True
    assert "📻 Autoplay on" in render_panel(player)


async def test_control_api(api, player, playing):
    status, s = await api.state()
    assert status == 200 and s["autoplay"] is True
    assert s["now_playing"]["autoplay"] is False
    status, body = await api.control(action="autoplay", enabled=False)
    assert status == 200 and body == {"ok": True}
    assert player.autoplay is False
    status, s = await api.state()
    assert s["autoplay"] is False
    status, body = await api.control(action="autoplay", enabled=True)
    assert status == 200 and player.autoplay is True
    status, body = await api.control(action="autoplay", enabled="yes")
    assert status == 400 and body["ok"] is False
    status, body = await api.control(action="autoplay")
    assert status == 400
    await settle()
    status, s = await api.state()
    assert s["autoplay"] is True


async def test_state_marks_autoplay_track(api, playing):
    p = playing
    p.voice_client.finish()
    await settle()
    status, s = await api.state()
    assert s["now_playing"]["autoplay"] is True
