"""CrossfadeMixer on fake sources, plus an end-to-end pass on real FFmpeg."""

import shutil
import subprocess
import types

import pytest

from djsnake.config import PCM_FRAME_SIZE
from djsnake.media import analyse_media
from djsnake.mixer import CrossfadeMixer
from djsnake.playback import open_track_source
from fakes import CountedSource, FakeSource, sample

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


def drain(mixer):
    frames = []
    while True:
        frame = mixer.read()
        if not frame:
            return frames
        frames.append(frame)


def test_single_source_passes_through_and_ends():
    source = CountedSource(3)
    mixer = CrossfadeMixer(source, gen=1, frames=3)
    assert len(drain(mixer)) == 3
    assert mixer.finished
    assert mixer.read() == b""
    assert mixer.remaining_seconds() == 0


def test_attach_mixes_saturating_until_the_outgoing_track_drains():
    old = FakeSource("old", 30000, before_options="-t 0.1")  # 5 frames
    new = FakeSource("new", 5000, before_options="-t 0.2")  # 10 frames
    mixer = CrossfadeMixer(old, gen=1, frames=5)
    assert mixer.attach(new, new.read(), 2, 9, 0)
    assert mixer.gen == 2
    frames = drain(mixer)
    assert len(frames) == 10
    assert [sample(f) for f in frames[:5]] == [32767] * 5  # Clipped, no wrap
    assert [sample(f) for f in frames[5:]] == [5000] * 5
    assert old.cleaned and mixer.outgoing is None


def test_attach_refused_while_a_crossfade_runs_or_after_the_end():
    mixer = CrossfadeMixer(CountedSource(5), gen=1)
    assert mixer.attach(CountedSource(5), b"\0" * PCM_FRAME_SIZE, 2, 5, 0)
    assert not mixer.attach(CountedSource(5), b"\0" * PCM_FRAME_SIZE, 3, 5, 0)
    mixer.cleanup()
    assert not mixer.attach(CountedSource(5), b"\0" * PCM_FRAME_SIZE, 3, 5, 0)


def test_tail_signalled_once_when_the_fade_out_starts():
    calls = []
    mixer = CrossfadeMixer(
        CountedSource(10),
        gen=7,
        frames=10,
        fade_frames=3,
        on_tail=lambda m, gen: calls.append(gen),
    )
    for _ in range(6):
        mixer.read()
    assert calls == []
    mixer.read()  # 3 frames left
    assert calls == [7]
    drain(mixer)
    assert calls == [7]
    assert mixer.remaining_seconds() == 0


def test_rearm_signals_the_tail_again():
    calls = []
    mixer = CrossfadeMixer(
        CountedSource(10), 1, 10, 5, on_tail=lambda m, gen: calls.append(gen)
    )
    for _ in range(5):
        mixer.read()
    mixer.rearm()
    mixer.read()
    assert calls == [1, 1]


def test_source_attached_as_the_current_one_ends_plays_on():
    """Race: the next track is attached just as the current source runs dry."""
    a, b = CountedSource(0), CountedSource(3)
    mixer = CrossfadeMixer(a, 1, 10, 0)
    original_read = a.read

    def racing_read():
        mixer.attach(b, b.read(), 2, 2, 0)
        return original_read()

    a.read = racing_read
    out = [mixer.read() for _ in range(5)]
    assert sum(1 for frame in out if frame) == 2
    assert a.cleaned
    assert mixer.finished


@needs_ffmpeg
@pytest.mark.ffmpeg
@pytest.mark.parametrize("crossfade", [False, True])
def test_real_ffmpeg_tracks_analyse_and_crossfade(tmp_path, crossfade):
    paths = []
    for i, freq in enumerate((440, 660)):
        path = str(tmp_path / f"t{i}.m4a")
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"sine=f={freq}:d=20",
                "-ac",
                "2",
                path,
            ],
            check=True,
        )
        paths.append(path)

    def track(path):
        a = analyse_media(path)
        return {
            "filepath": path,
            "title": path,
            "start": a["start"],
            "end": a["end"] or a["duration"],
            "loudnorm": a["loudnorm"],
            "duration": a["duration"],
        }

    player = types.SimpleNamespace(crossfade=crossfade)
    first, second = track(paths[0]), track(paths[1])
    assert first["start"] == 0.0
    assert first["end"] == pytest.approx(20.0, abs=0.1)
    assert first["loudnorm"]

    _, source, frames, fade = open_track_source(player, first, 0.0)
    attached = {}

    def on_tail(mixer, gen):
        if not crossfade or "ok" in attached:
            return
        overlap = mixer.remaining_seconds()
        _, nxt, nxt_frames, nxt_fade = open_track_source(
            player, second, 0.0, fade_in=overlap
        )
        attached["ok"] = mixer.attach(nxt, nxt.read(), gen + 1, nxt_frames, nxt_fade)

    mixer = CrossfadeMixer(source, 1, frames, fade, on_tail)
    count = overlap = bad = 0
    while True:
        data = mixer.read()
        if not data:
            break
        bad += len(data) != PCM_FRAME_SIZE
        overlap += mixer.outgoing is not None
        count += 1
    mixer.cleanup()

    assert bad == 0
    if crossfade:
        # 20 s + 20 s with a 6 s overlap
        assert attached == {"ok": True} and mixer.gen == 2
        assert count == pytest.approx(34 * 50, abs=25)
        assert overlap == pytest.approx(6 * 50, abs=10)
    else:
        assert attached == {} and mixer.gen == 1
        assert count == pytest.approx(20 * 50, abs=10)
        assert overlap == 0
