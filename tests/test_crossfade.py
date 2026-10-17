"""Crossfade between tracks: promotion at the tail, mixing, cancellation by
controls, and the !crossfade / !seek commands."""

import types

import pytest

from djsnake import commands
from djsnake.controls import (
    control_next,
    control_pause,
    control_resume,
    control_seek,
)
from djsnake.media import playback_options
from djsnake.playback import _start_crossfade, play_next
from djsnake.sessions import render_panel
from fakes import make_track, sample, settle, titles

FPS = 50  # Frames per second


def drain(mixer, count):
    return [mixer.read() for _ in range(count)]


@pytest.fixture
def track(media_dir, ffmpeg):
    """make(name, start, end, value): an analysed track whose fake FFmpeg
    source yields samples of `value`."""

    def make(name, start, end, value=1000):
        ffmpeg.values[name[:2]] = value
        return make_track(media_dir, name, start=start, end=end, loudnorm=None)

    return make


@pytest.fixture
def xfade_player(player):
    player.crossfade = True
    return player


async def start(player, ctx, *tracks):
    for t in tracks:
        player.add_to_queue(t)
    await play_next(ctx)
    return player.mixer


async def get_state(client, player):
    return await (await client.get(f"/api/{player.sid}/state")).json()


# --- Web toggle ----------------------------------------------------------------


async def test_web_toggle_turns_crossfade_on(client, player):
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "crossfade", "enabled": True}
    )
    assert r.status == 200 and await r.json() == {"ok": True}
    assert player.crossfade
    assert (await get_state(client, player))["crossfade"] is True
    await settle(0.1)
    assert "🔀 Crossfade on" in render_panel(player)


@pytest.mark.parametrize("enabled", [None, "yes", 1])
async def test_web_toggle_requires_a_boolean(client, xfade_player, enabled):
    r = await client.post(
        f"/api/{xfade_player.sid}/control",
        json={"action": "crossfade", "enabled": enabled},
    )
    assert r.status == 400
    assert xfade_player.crossfade


# --- A chain of three crossfaded tracks ------------------------------------------


@pytest.fixture
async def chain(xfade_player, ctx, ffmpeg, track):
    """X (0-20 s) playing, Y (2-40 s) and Z (0-30 s) queued, drained to one
    frame before X's 6 s fade-out."""
    p = xfade_player
    x = track("X1", 0.0, 20.0, 1000)
    y = track("Y1", 2.0, 40.0, 2000)
    z = track("Z1", 0.0, 30.0, 31000)
    mixer = await start(p, ctx, x, y, z)
    assert ffmpeg.last["options"].endswith("afade=t=out:st=14.000:d=6.000")
    gen0 = p.play_gen
    drain(mixer, 20 * FPS - 300 - 1)
    await settle()
    return types.SimpleNamespace(mixer=mixer, x=x, y=y, z=z, gen0=gen0)


async def test_tail_promotes_the_next_track_once(chain, xfade_player, client, ffmpeg):
    p, m = xfade_player, chain.mixer
    assert p.now_playing is chain.x and p.play_gen == chain.gen0

    drain(m, 1)  # 300 frames left: the tail is signalled
    await settle()
    assert p.now_playing is chain.y
    assert titles(p.queue) == ["Z1"]
    assert p.history[-1] is chain.x
    assert p.play_gen == chain.gen0 + 1 and m.gen == p.play_gen
    assert m.outgoing is not None
    assert 2.0 <= p.elapsed() < 2.3
    assert len(p.voice_client.played) == 1  # No play()/stop()

    kw = ffmpeg.last  # Fades in over the overlap, out over 6 s
    assert kw["before_options"] == "-ss 2.000 -t 38.000"
    assert "afade=t=in:st=0:d=6.000" in kw["options"]
    assert kw["options"].endswith("afade=t=out:st=32.000:d=6.000")

    s = await get_state(client, p)
    assert s["crossfading"] is True and s["now_playing"]["title"] == "Y1"

    await _start_crossfade(p, m, chain.gen0)  # A late duplicate signal
    assert p.now_playing is chain.y and p.play_gen == chain.gen0 + 1


async def test_overlap_sums_both_tracks_then_the_next_plays_alone(
    chain, xfade_player, client, ffmpeg
):
    m = chain.mixer
    drain(m, 1)
    await settle()
    mixed = drain(m, 299)
    assert {sample(f) for f in mixed} == {3000}
    after = drain(m, 3)
    assert (await get_state(client, xfade_player))["crossfading"] is False
    assert sample(after[-1]) == 2000
    assert m.outgoing is None
    assert ffmpeg.made[-2].cleaned


async def test_chain_continues_and_ends_with_one_advance(chain, xfade_player):
    p, m = xfade_player, chain.mixer
    drain(m, 1)
    await settle()
    drain(m, 302)
    drain(m, 38 * FPS - 300 - 302)  # Y's own tail -> Z
    await settle()
    assert p.now_playing is chain.z and p.history[-1] is chain.y
    assert p.play_gen == chain.gen0 + 2
    assert len(p.voice_client.played) == 1
    clipped = drain(m, 5)  # 31000 + 2000 saturates instead of wrapping
    assert {sample(f) for f in clipped} == {32767}

    while m.read():
        pass
    p.voice_client.finish()  # discord.py calls `after` once, for the chain
    await settle()
    assert p.now_playing is None and not p.is_playing
    assert titles(p.history[-3:]) == ["X1", "Y1", "Z1"]


# --- Controls during a crossfade -----------------------------------------------


async def test_next_during_the_overlap_cancels_it(xfade_player, ctx, track):
    p = xfade_player
    a, b, c = track("A2", 0.0, 20.0), track("B2", 0.0, 20.0), track("C2", 0.0, 20.0)
    m = await start(p, ctx, a, b, c)
    drain(m, 20 * FPS - 300)
    await settle()
    assert p.now_playing is b

    await control_next(p)
    await settle()
    assert p.now_playing is c and p.queue == []
    assert titles(p.history[-2:]) == ["A2", "B2"]
    assert p.mixer is not m
    assert len(p.voice_client.played) == 2

    gen = p.play_gen  # The old mixer's late tail must not touch playback
    await _start_crossfade(p, m, m.gen)
    assert p.now_playing is c and p.play_gen == gen


async def test_seek_during_the_overlap_restarts_the_promoted_track(
    xfade_player, ctx, ffmpeg, track
):
    p = xfade_player
    c, d, e = track("C2", 0.0, 20.0), track("D2", 0.0, 20.0), track("E2", 0.0, 20.0)
    m = await start(p, ctx, c, d, e)
    drain(m, 20 * FPS - 300)
    await settle()
    assert p.now_playing is d

    await control_seek(p, 5)
    await settle()
    assert p.now_playing is d and p.queue == [e]
    assert 5 <= p.elapsed() < 5.3
    assert p.mixer is not m
    assert ffmpeg.last["before_options"] == "-ss 5.000 -t 15.000"


async def test_paused_tail_is_put_off_until_resumed(xfade_player, ctx, track):
    p = xfade_player
    d, e = track("D2", 0.0, 20.0), track("E2", 0.0, 20.0)
    m = await start(p, ctx, d, e)
    await control_pause(p)
    drain(m, 20 * FPS - 300)
    await settle()
    assert p.now_playing is d

    await control_resume(p)
    drain(m, 1)
    await settle()
    assert p.now_playing is e and p.queue == []


# --- When no crossfade happens -------------------------------------------------


async def test_short_track_keeps_the_plain_fade(xfade_player, ctx, ffmpeg, track):
    p = xfade_player
    s1, s2 = track("S1", 0.0, 12.0), track("S2", 0.0, 40.0)
    m = await start(p, ctx, s1, s2)
    assert ffmpeg.last["options"].endswith("d=4.000")
    drain(m, 12 * FPS - 200)
    await settle()
    assert p.now_playing is s1  # Shorter than 15 s: no crossfade


async def test_no_next_track_nothing_happens(xfade_player, ctx, track):
    p = xfade_player
    only = track("L1", 0.0, 30.0)
    m = await start(p, ctx, only)
    drain(m, 30 * FPS - 300)
    await settle()
    assert p.now_playing is only and p.is_playing


async def test_crossfade_off_is_a_plain_passthrough(
    client, xfade_player, ctx, ffmpeg, track
):
    p = xfade_player
    r = await client.post(
        f"/api/{p.sid}/control", json={"action": "crossfade", "enabled": False}
    )
    assert r.status == 200 and not p.crossfade

    o1, o2 = track("O1", 0.0, 30.0), track("O2", 0.0, 30.0)
    m = await start(p, ctx, o1, o2)
    assert ffmpeg.last == playback_options(o1, 0)[1]
    assert ffmpeg.last["options"].endswith("afade=t=out:st=26.000:d=4.000")

    gen = p.play_gen
    frames = drain(m, 30 * FPS)
    await settle()
    assert p.now_playing is o1 and p.play_gen == gen
    assert {sample(f) for f in frames} == {1000}
    assert m.read() == b""

    p.voice_client.finish()
    await settle()
    assert p.now_playing is o2 and p.history[-1] is o1


# --- Commands ----------------------------------------------------------------------


async def test_crossfade_command(player, ctx, channel):
    await commands.crossfade.callback(ctx, "on")
    assert player.crossfade and channel.reactions == ["✅"]

    await commands.crossfade.callback(ctx, "maybe")
    assert player.crossfade and "Usage" in channel.log[-1]

    await commands.crossfade.callback(ctx, "off")
    assert not player.crossfade


async def test_seek_is_relative_to_the_automix_start(
    client, player, ctx, ffmpeg, track
):
    await start(player, ctx, track("R1", 7.5, 200.0))
    await commands.seek.callback(ctx, "1:00")
    await settle()
    assert ffmpeg.last["before_options"].startswith("-ss 67.500 ")
    assert 67.5 <= player.elapsed() < 67.8
    assert player.last_action == "⏩ Seeked to 1:00"

    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "seek", "position": 97.5}
    )
    assert r.status == 200
    assert player.last_action == "⏩ Seeked to 1:30"
