"""End-to-end audio pipeline: media made by ffmpeg, the real automix analysis,
the real play_next() chain with discord.FFmpegPCMAudio and CrossfadeMixer,
and the PCM frames a voice client would send, measured.

Run with `pytest -m e2e`; skipped when ffmpeg is not installed."""

import pytest

from djsnake.config import CROSSFADE_SECONDS
from djsnake.controls import control_crossfade, control_seek, control_volume
from djsnake.playback import play_next
from e2e_kit import (
    CHIRP_LEAD,
    CHIRP_LENGTH,
    CHIRP_TONE,
    GUILD_ID,
    TONE_LENGTH,
    all_full_frames,
    chirp_frequency,
    chirp_time,
    frequency,
    needs_ffmpeg,
    rms,
    wait_until,
)

pytestmark = [pytest.mark.e2e, needs_ffmpeg]

FPS = 50  # 20 ms frames
SPEED = 8.0  # The fake voice client reads this many times faster than real time


async def play_all(ctx, player, tracks, timeout=30.0):
    """Queue `tracks`, start playback and wait until the queue has played out."""
    for track in tracks:
        player.add_to_queue(track)
    await play_next(ctx)
    await wait_until(
        lambda: not player.is_playing and player.now_playing is None,
        timeout,
        what="the queue to play out",
    )


def test_automix_analysis_finds_silence_and_loudness(media_kit):
    _, chirp = media_kit.made["chirp"]
    _, tone = media_kit.made["tone"]
    assert chirp["duration"] == pytest.approx(CHIRP_LENGTH, abs=0.1)
    assert chirp["start"] == pytest.approx(CHIRP_LEAD, abs=0.15)  # Leading silence
    assert chirp["end"] == pytest.approx(CHIRP_LEAD + CHIRP_TONE, abs=0.2)  # Tail
    assert tone["start"] == 0.0
    assert tone["end"] == pytest.approx(TONE_LENGTH, abs=0.1)
    # loudnorm first pass: the chirp is measured about 20 LU louder
    assert chirp["loudnorm"] and tone["loudnorm"]
    assert chirp["loudnorm"]["input_i"] - tone["loudnorm"]["input_i"] > 15


async def test_queue_plays_trimmed_normalised_and_faded(
    media_kit, live_player, e2e_ctx, media_dir
):
    player = await live_player(SPEED)
    chirp = media_kit.track("chirp", media_dir)
    tone = media_kit.track("tone", media_dir)
    await play_all(e2e_ctx, player, [chirp, tone])

    vc = player.voice_client
    assert not vc.errors
    assert all_full_frames(vc.frames)
    assert not any(f.overlap for f in vc.frames)  # Crossfade is off
    a, b = vc.track_frames(1), vc.track_frames(2)
    assert len(a) == pytest.approx((chirp["end"] - chirp["start"]) * FPS, abs=10)
    assert len(b) == pytest.approx((tone["end"] - tone["start"]) * FPS, abs=10)
    assert [t["title"] for t in player.history] == [chirp["title"], tone["title"]]

    # The leading silence was skipped: 1.0-1.5 s in, the sweep is at file
    # time 2.5-3.0 s.
    expected = chirp_frequency(chirp["start"] + 1.25)
    assert frequency(a[50:75]) == pytest.approx(expected, abs=10)

    # Normalised: the sources differ by ~20 dB, the output by under 3 dB.
    loud, quiet = rms(a[300:400]), rms(b[300:400])
    assert loud > 1000 and quiet > 1000
    assert 0.7 < quiet / loud < 1.42

    # 1 s fade-in, 4 s fade-out, and nothing played from the silent tail.
    assert rms(a[:5]) < 0.3 * loud
    assert rms(a[-10:]) < 0.15 * loud
    assert rms(b[-10:]) < 0.15 * quiet


async def test_crossfade_overlaps_the_next_track(
    media_kit, live_player, e2e_ctx, media_dir
):
    player = await live_player(SPEED)
    # Reading 8x faster than real time, the 6 s overlap lasts 0.75 s: on a busy
    # machine the loop may not attach the next track in time. Hold the reads
    # from just after the fade-out starts until it is attached (at most 5 s).
    player.voice_client.hold = lambda mixer: (
        mixer.gen == 1
        and mixer.outgoing is None
        and (mixer.remaining_seconds() or 99) <= CROSSFADE_SECONDS - 0.1
    )
    control_crossfade(player, True)
    chirp = media_kit.track("chirp", media_dir)
    tone = media_kit.track("tone", media_dir)
    await play_all(e2e_ctx, player, [chirp, tone])

    vc = player.voice_client
    assert not vc.errors
    assert all_full_frames(vc.frames)
    overlap = sum(1 for f in vc.frames if f.overlap)
    # A 6 s overlap (the hold above lets 0.1 s through before the attach)
    assert (CROSSFADE_SECONDS - 0.2) * FPS <= overlap <= CROSSFADE_SECONDS * FPS + 2
    # The overlap frames are counted in both tracks: each played in full.
    chirp_frames = len(vc.track_frames(1)) + overlap
    assert chirp_frames == pytest.approx((chirp["end"] - chirp["start"]) * FPS, abs=10)
    assert len(vc.track_frames(2)) == pytest.approx(TONE_LENGTH * FPS, abs=10)
    # Overlapping frames are contiguous, right at the start of the tone.
    first = next(i for i, f in enumerate(vc.frames) if f.overlap)
    assert all(f.overlap for f in vc.frames[first : first + overlap])
    assert vc.frames[first].gen == 2
    assert [t["title"] for t in player.history] == [chirp["title"], tone["title"]]


async def test_seek_lands_on_the_requested_position(
    media_kit, live_player, e2e_ctx, media_dir
):
    player = await live_player(2.0)
    chirp = media_kit.track("chirp", media_dir)
    player.add_to_queue(chirp)
    await play_next(e2e_ctx)
    vc = player.voice_client
    await wait_until(lambda: len(vc.track_frames(1)) >= FPS, what="playback")

    applied = await control_seek(player, 12.0)
    assert applied == 12.0
    await wait_until(lambda: len(vc.track_frames(2)) >= 60, what="the seek")

    after = vc.track_frames(2)
    # 0.5-1.0 s after the seek point (past its short fade-in): file time ~12.75
    assert chirp_time(frequency(after[25:50])) == pytest.approx(12.75, abs=0.25)
    assert rms(after[:2]) < 0.5 * rms(after[25:50])  # Soft cut in
    assert player.elapsed() == pytest.approx(12.0, abs=2.0)  # Server clock follows
    assert not vc.errors and all_full_frames(vc.frames)


async def test_volume_scales_the_live_output(
    media_kit, live_player, e2e_ctx, media_dir
):
    player = await live_player(4.0)
    player.add_to_queue(media_kit.track("tone", media_dir))
    await play_next(e2e_ctx)
    vc = player.voice_client
    await wait_until(lambda: len(vc.frames) >= 150, what="playback")
    base = rms(vc.frames[100:150])  # Past the 1 s fade-in
    assert base > 1000

    for percent in (50, 150):
        mark = len(vc.frames)
        control_volume(player, GUILD_ID, percent)
        await wait_until(lambda: len(vc.frames) >= mark + 60, what="frames")
        level = rms(vc.frames[mark + 20 : mark + 60])  # Past the 100 ms glide
        assert level / base == pytest.approx(percent / 100, rel=0.05)
    assert player.mixer.volume == pytest.approx(1.5)
    assert not vc.errors and all_full_frames(vc.frames)
