"""Per-server volume: the gain stage, persistence, !volume and the web control."""

import array
import json

import pytest

from djsnake import commands, state, volume
from djsnake.config import PCM_FRAME_SIZE, VOLUME_RAMP_FRAMES
from djsnake.controls import ControlError, control_volume
from djsnake.mixer import CrossfadeMixer
from djsnake.playback import play_next
from djsnake.sessions import render_panel
from fakes import CountedSource, FakeSource, make_track, sample

# --- Gain math -----------------------------------------------------------------


def samples(frame):
    values = array.array("h")
    values.frombytes(frame)
    return values


def test_percent_to_gain_is_linear_around_the_normalised_level():
    assert volume.percent_to_gain(100) == 1.0
    assert volume.percent_to_gain(50) == 0.5
    assert volume.percent_to_gain(0) == 0.0
    assert volume.percent_to_gain(150) == 1.5


def test_unity_gain_leaves_frames_untouched():
    source = FakeSource("a", 1234)
    mixer = CrossfadeMixer(source, gen=1)
    assert mixer.read() == source.frame


def test_gain_scales_every_sample():
    mixer = CrossfadeMixer(FakeSource("a", 10000), gen=1, volume=0.5)
    frame = mixer.read()
    assert len(frame) == PCM_FRAME_SIZE
    assert set(samples(frame)) == {5000}


def test_zero_volume_is_silent():
    mixer = CrossfadeMixer(FakeSource("a", 10000), gen=1, volume=0.0)
    assert set(samples(mixer.read())) == {0}


def test_a_change_glides_over_a_few_frames_without_jumps():
    mixer = CrossfadeMixer(FakeSource("a", 10000), gen=1)
    mixer.read()
    mixer.set_volume(0.5)
    firsts, last_of_prev = [], 10000
    for _ in range(VOLUME_RAMP_FRAMES + 2):
        values = samples(mixer.read())
        firsts.append(values[0])
        steps = [abs(b - a) for a, b in zip(values, values[1:])]
        assert max(steps) < 1000  # No click inside a frame
        assert abs(values[0] - last_of_prev) < 1000  # Nor between frames
        last_of_prev = values[-1]
    assert firsts[0] > 5000  # Not there yet after the first frame
    assert firsts == sorted(firsts, reverse=True)  # Monotonic descent
    assert firsts[-1] == 5000 and mixer.volume == 0.5
    assert sample(mixer.read()) == 5000


def test_boost_is_soft_limited_never_wrapping_or_hard_clipping():
    mixer = CrossfadeMixer(FakeSource("a", 30000), gen=1, volume=1.5)
    values = samples(mixer.read())
    assert set(values) == {values[0]}
    assert 29490 < values[0] < 32767  # Compressed, below full scale
    quiet = CrossfadeMixer(FakeSource("a", 10000), gen=1, volume=1.5)
    assert set(samples(quiet.read())) == {15000}  # Below the knee: plain gain


def test_negative_peaks_are_limited_symmetrically():
    mixer = CrossfadeMixer(FakeSource("a", -30000), gen=1, volume=1.5)
    value = samples(mixer.read())[0]
    assert -32767 <= value < -29490


def test_gain_follows_a_crossfade_chain():
    old = FakeSource("old", 10000, before_options="-t 0.1")  # 5 frames
    new = FakeSource("new", 4000, before_options="-t 0.2")
    mixer = CrossfadeMixer(old, gen=1, frames=5, volume=0.5)
    assert mixer.attach(new, new.read(), 2, 9, 0)
    frames = []
    while True:
        frame = mixer.read()
        if not frame:
            break
        frames.append(frame)
    assert [sample(f) for f in frames[:5]] == [7000] * 5  # (10000 + 4000) / 2
    assert [sample(f) for f in frames[5:]] == [2000] * 5
    assert mixer.gen == 2


def test_gain_does_not_disturb_the_tail_signal_or_the_end():
    calls = []
    mixer = CrossfadeMixer(
        CountedSource(6),
        gen=1,
        frames=6,
        fade_frames=2,
        on_tail=lambda m, g: calls.append(g),
        volume=0.25,
    )
    frames = []
    while True:
        frame = mixer.read()
        if not frame:
            break
        frames.append(frame)
    assert len(frames) == 6 and calls == [1] and mixer.finished


# --- Parsing and persistence -------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [("80", 80), ("0", 0), ("150", 150), (" 75% ", 75), ("151", None), ("-5", None)]
    + [("abc", None), ("", None), ("1.5", None), ("٣", None)],
)
def test_parse_volume(text, expected):
    assert volume.parse_volume(text) == expected


def test_volume_is_saved_and_reloaded(player, data_dir):
    control_volume(player, 1, 70)
    control_volume(player, 2, 120)
    assert json.loads((data_dir / "volume.json").read_text()) == {"1": 70, "2": 120}
    state.guild_volumes.clear()
    assert volume.volume_for(1) == 100  # Default when unknown
    volume.load_volumes()
    assert volume.volume_for(1) == 70 and volume.volume_for(2) == 120


def test_load_drops_bad_entries_and_survives_garbage(data_dir):
    data_dir.mkdir()
    path = data_dir / "volume.json"
    path.write_text(json.dumps({"1": 90, "x": 5, "2": 999, "3": "50", "4": True}))
    volume.load_volumes()
    assert state.guild_volumes == {1: 90}
    path.write_text("not json")
    volume.load_volumes()
    assert state.guild_volumes == {}
    path.write_text("[1, 2]")
    volume.load_volumes()
    assert state.guild_volumes == {}


def test_save_failure_still_applies_and_reports(player, monkeypatch):
    def broken():
        raise OSError("read-only")

    monkeypatch.setattr("djsnake.controls.save_volumes", broken)
    with pytest.raises(ControlError) as e:
        control_volume(player, 1, 40)
    assert e.value.status == 500 and volume.volume_for(1) == 40


@pytest.mark.parametrize("bad", [-1, 151, 50.5, "50", None, True])
def test_control_rejects_bad_values(player, bad):
    with pytest.raises(ControlError) as e:
        control_volume(player, 1, bad)
    assert e.value.code == "bad_volume" and 1 not in state.guild_volumes


# --- Playback ---------------------------------------------------------------------


async def test_new_tracks_start_at_the_saved_volume(player, ctx, media_dir):
    state.guild_volumes[1] = 50
    player.add_to_queue(make_track(media_dir, "A"))
    await play_next(ctx)
    assert player.mixer.volume == 0.5
    assert set(samples(player.mixer.read())) == {500}  # Fake source: 1000


async def test_changing_volume_reaches_the_playing_mixer(player, ctx, media_dir):
    player.add_to_queue(make_track(media_dir, "A"))
    await play_next(ctx)
    mixer = player.mixer
    control_volume(player, 1, 150)
    assert player.mixer is mixer and mixer.volume == 1.5
    assert player.voice_client.played == ["A.mp4"]  # No restart


# --- Command ----------------------------------------------------------------------


async def test_volume_command_shows_and_sets(player, ctx, channel, data_dir):
    await commands.volume.callback(ctx)
    assert "**100%**" in channel.log[-1]

    await commands.volume.callback(ctx, "65")
    assert channel.reactions == ["✅"]
    assert volume.volume_for(1) == 65
    assert (data_dir / "volume.json").exists()

    await commands.volume.callback(ctx)
    assert "**65%**" in channel.log[-1]


@pytest.mark.parametrize("arg", ["loud", "151", "-1"])
async def test_volume_command_rejects_bad_input(player, ctx, channel, arg):
    await commands.volume.callback(ctx, arg)
    assert "Usage" in channel.log[-1] and 1 not in state.guild_volumes


async def test_panel_shows_volume_only_when_not_default(player):
    assert "Volume" not in render_panel(player)
    control_volume(player, 1, 80)
    assert "🔊 Volume 80%" in render_panel(player)
    control_volume(player, 1, 100)
    assert "Volume" not in render_panel(player)


# --- Web --------------------------------------------------------------------------


async def web_volume(client, player, value):
    r = await client.post(
        f"/api/{player.sid}/control", json={"action": "volume", "value": value}
    )
    return r.status, await r.json()


async def test_web_volume_sets_persists_and_shows_in_state(client, player, data_dir):
    r = await client.get(f"/api/{player.sid}/state")
    assert (await r.json())["volume"] == 100

    assert await web_volume(client, player, 85) == (200, {"ok": True})
    assert volume.volume_for(1) == 85
    assert json.loads((data_dir / "volume.json").read_text()) == {"1": 85}
    r = await client.get(f"/api/{player.sid}/state")
    assert (await r.json())["volume"] == 85
    assert "Volume set to 85%" in player.last_action


@pytest.mark.parametrize("bad", [-1, 151, 12.5, "9", None, True, [3]])
async def test_web_volume_rejects_bad_values(client, player, bad):
    status, body = await web_volume(client, player, bad)
    assert status == 400 and not body["ok"] and "0 to 150" in body["error"]
    assert 1 not in state.guild_volumes


async def test_web_volume_needs_a_value(client, player):
    r = await client.post(f"/api/{player.sid}/control", json={"action": "volume"})
    assert r.status == 400


async def test_web_volume_works_out_of_voice(client, player):
    player.voice_client.connected = False
    assert (await web_volume(client, player, 30))[0] == 200


async def test_dragging_the_slider_coalesces_panel_edits(client, player, channel):
    for value in range(60, 90, 5):
        await web_volume(client, player, value)
    assert volume.volume_for(1) == 85
    assert len(channel.edits) <= 2  # Throttled to PANEL_MIN_INTERVAL
