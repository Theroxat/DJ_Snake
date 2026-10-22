"""Loop (off / track / queue) and shuffle: the controls, the !loop and
!shuffle commands, the web actions and the playback behaviour."""

import pytest

from djsnake import commands, stats
from djsnake.controls import ControlError, control_loop, control_next, control_shuffle
from djsnake.playback import _start_crossfade, play_next
from djsnake.sessions import render_panel
from conftest import GUILD_ID
from fakes import make_track, settle, titles

FPS = 50


def plays(guild_id=GUILD_ID):
    stats.flush()
    conn = stats._connect(stats.DB_PATH)
    try:
        conn.row_factory = lambda cursor, row: dict(
            zip([c[0] for c in cursor.description], row)
        )
        return conn.execute(
            "SELECT * FROM plays WHERE guild_id = ? ORDER BY id", (guild_id,)
        ).fetchall()
    finally:
        conn.close()


async def start(player, ctx, media_dir, *names, **fields):
    made = [make_track(media_dir, n, **fields) for n in names]
    for t in made:
        player.add_to_queue(t)
    await play_next(ctx)
    return made


# --- control_loop -----------------------------------------------------------------


def test_loop_cycles_and_validates(player):
    assert player.loop == "off"
    assert [control_loop(player) for _ in range(4)] == [
        "track",
        "queue",
        "off",
        "track",
    ]
    assert control_loop(player, "queue") == "queue"
    with pytest.raises(ControlError) as e:
        control_loop(player, "forever")
    assert e.value.status == 400 and player.loop == "queue"


# --- Playback: loop track ---------------------------------------------------------


async def test_loop_track_replays_when_it_ends(player, ctx, media_dir):
    a, b = await start(player, ctx, media_dir, "A", "B")
    control_loop(player, "track")
    for _ in range(3):
        player.voice_client.finish()
        await settle()
        assert player.now_playing is a
    assert titles(player.queue) == ["B"] and player.history == []
    assert player.voice_client.played == ["A.mp4"] * 4


async def test_loop_track_skip_still_moves_on(player, ctx, media_dir):
    a, b = await start(player, ctx, media_dir, "A", "B")
    control_loop(player, "track")
    await control_next(player)
    await settle()
    assert player.now_playing is b and player.history == [a]


async def test_loop_track_counts_each_pass_as_one_play(player, ctx, media_dir):
    await start(player, ctx, media_dir, "A")
    control_loop(player, "track")
    for _ in range(2):
        player.started_at -= 40
        player.voice_client.finish()
        await settle()
    rows = plays()
    assert [(r["title"], r["skipped"]) for r in rows] == [("A", 0), ("A", 0)]
    assert all(39 <= r["played_seconds"] <= 45 for r in rows)
    assert player.played_seconds() < 5  # The third pass starts fresh


async def test_loop_off_ends_the_queue(player, ctx, media_dir):
    await start(player, ctx, media_dir, "A")
    player.voice_client.finish()
    await settle()
    assert player.now_playing is None and not player.is_playing


# --- Playback: loop queue ---------------------------------------------------------


async def test_loop_queue_sends_finished_tracks_to_the_end(player, ctx, media_dir):
    await start(player, ctx, media_dir, "A", "B", "C")
    control_loop(player, "queue")
    order = []
    for _ in range(5):
        player.voice_client.finish()
        await settle()
        order.append(player.now_playing["title"])
    assert order == ["B", "C", "A", "B", "C"]
    assert sorted(titles(player.queue)) == ["A", "B"]


async def test_loop_queue_with_one_track_replays_it(player, ctx, media_dir):
    [a] = await start(player, ctx, media_dir, "A")
    control_loop(player, "queue")
    player.voice_client.finish()
    await settle()
    assert player.now_playing is a and player.queue == []


async def test_loop_queue_skip_goes_to_the_end_too(player, ctx, media_dir):
    await start(player, ctx, media_dir, "A", "B")
    control_loop(player, "queue")
    await control_next(player)
    await settle()
    assert player.now_playing["title"] == "B" and titles(player.queue) == ["A"]


async def test_loop_shows_in_the_panel_and_state(player, client):
    control_loop(player, "queue")
    assert "🔁 Loop: queue" in render_panel(player)
    s = await (await client.get(f"/api/{player.sid}/state")).json()
    assert s["loop"] == "queue"
    control_loop(player, "off")
    assert "Loop" not in render_panel(player)


# --- Crossfade + loop -------------------------------------------------------------


@pytest.fixture
def xtrack(media_dir, ffmpeg):
    def make(name, start, end, value=1000):
        ffmpeg.values[name[:2]] = value
        return make_track(media_dir, name, start=start, end=end, loudnorm=None)

    return make


async def reach_tail(player, ctx, *tracks):
    player.crossfade = True
    for t in tracks:
        player.add_to_queue(t)
    await play_next(ctx)
    mixer = player.mixer
    for _ in range(20 * FPS - 300):
        mixer.read()
    mixer.read()  # 300 frames left: the tail is signalled
    await settle()
    return mixer


async def test_crossfade_loop_track_overlaps_with_itself(player, ctx, xtrack):
    x = xtrack("X1", 0.0, 20.0)
    player.loop = "track"
    mixer = await reach_tail(player, ctx, x)
    assert player.now_playing is x and player.history == []
    assert player.mixer is mixer and mixer.outgoing is not None
    assert player.play_gen == mixer.gen
    assert len(player.voice_client.played) == 1  # No play()/stop()
    gen = player.play_gen
    await _start_crossfade(player, mixer, gen - 1)  # Stale signal: ignored
    assert player.play_gen == gen


async def test_crossfade_loop_queue_requeues_the_old_track(player, ctx, xtrack):
    x, y = xtrack("X1", 0.0, 20.0), xtrack("Y1", 0.0, 30.0)
    player.loop = "queue"
    await reach_tail(player, ctx, x, y)
    assert player.now_playing is y and titles(player.queue) == ["X1"]


async def test_crossfade_without_loop_still_ends_the_queue(player, ctx, xtrack):
    x = xtrack("X1", 0.0, 20.0)
    mixer = await reach_tail(player, ctx, x)
    assert mixer.outgoing is None and player.now_playing is x


# --- Shuffle ------------------------------------------------------------------------


async def test_shuffle_keeps_current_and_the_same_tracks(player, ctx, media_dir):
    await start(player, ctx, media_dir, *[f"T{i}" for i in range(30)])
    current = player.now_playing
    before = list(player.queue)
    await control_shuffle(player)
    assert player.now_playing is current
    assert sorted(titles(player.queue)) == sorted(titles(before))
    assert titles(player.queue) != titles(before)  # 1 in 29! of a false failure


async def test_shuffle_errors(player):
    with pytest.raises(ControlError) as e:
        await control_shuffle(player)  # Nothing queued
    assert e.value.status == 409
    player.voice_client.connected = False
    with pytest.raises(ControlError) as e:
        await control_shuffle(player)
    assert e.value.code == "not_in_voice"


# --- Discord commands -------------------------------------------------------------


async def test_loop_command(player, ctx, channel):
    await commands.loop_command.callback(ctx, None)
    assert player.loop == "track" and channel.reactions == ["✅"]
    await commands.loop_command.callback(ctx, "QUEUE")
    assert player.loop == "queue"
    await commands.loop_command.callback(ctx, "off")
    assert player.loop == "off"
    await commands.loop_command.callback(ctx, "sideways")
    assert player.loop == "off" and "Usage" in channel.log[-1]


async def test_shuffle_command(player, ctx, channel, media_dir):
    await commands.shuffle.callback(ctx)
    assert "❌" in channel.log[-1]  # Nothing to shuffle
    await start(player, ctx, media_dir, *[f"T{i}" for i in range(8)])
    await commands.shuffle.callback(ctx)
    assert "✅" in channel.reactions
    assert "Queue shuffled" in player.last_action


# --- Web API ----------------------------------------------------------------------


async def post(client, player, **body):
    r = await client.post(f"/api/{player.sid}/control", json=body)
    return r.status, await r.json()


async def test_web_loop_sets_and_cycles(client, player):
    assert await post(client, player, action="loop", mode="track") == (
        200,
        {"ok": True},
    )
    assert player.loop == "track"
    status, _ = await post(client, player, action="loop")  # Cycles
    assert status == 200 and player.loop == "queue"
    assert "Loop: queue via the web page" in player.last_action
    s = await (await client.get(f"/api/{player.sid}/state")).json()
    assert s["loop"] == "queue"


@pytest.mark.parametrize("mode", ["forever", 1, True, ""])
async def test_web_loop_rejects_bad_modes(client, player, mode):
    status, body = await post(client, player, action="loop", mode=mode)
    assert status == 400 and body["ok"] is False and player.loop == "off"


async def test_web_shuffle(client, player, ctx, media_dir):
    await start(player, ctx, media_dir, *[f"T{i}" for i in range(10)])
    status, body = await post(client, player, action="shuffle")
    assert (status, body) == (200, {"ok": True})
    assert "shuffled via the web page" in player.last_action


async def test_web_shuffle_conflicts(client, player):
    status, body = await post(client, player, action="shuffle")
    assert status == 409 and "shuffle" in body["error"]
    player.voice_client.connected = False
    status, _ = await post(client, player, action="shuffle")
    assert status == 409
