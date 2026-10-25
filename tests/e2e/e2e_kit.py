"""Helpers for the end-to-end tests: test media made by ffmpeg, a voice
client that really pulls PCM frames, and signal measurements."""

import array
import asyncio
import audioop  # Standard library up to 3.12; audioop-lts on 3.13+
import os
import shutil
import subprocess
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from djsnake.config import FRAMES_PER_SECOND, PCM_FRAME_SIZE
from djsnake.media import analyse_media

GUILD_ID = 1

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)

# Test media, all made at run time (nothing binary is committed). File time,
# seconds: CHIRP has 1.5 s of silence, an 18 s sweep whose pitch encodes the
# position (300 Hz + 40 Hz per second of tone), then 2 s of silence. TONE is a
# steady, quiet 440 Hz (about 20 dB below the chirp) with no silence.
CHIRP_LEAD, CHIRP_TONE, CHIRP_TAIL = 1.5, 18.0, 2.0
CHIRP_LENGTH = CHIRP_LEAD + CHIRP_TONE + CHIRP_TAIL
TONE_LENGTH = 18.0
CHIRP_TITLE = "E2E Artist - Chirp"
TONE_TITLE = "E2E Artist - Tone"


def chirp_frequency(file_time):
    """The chirp's pitch at a file time inside its tone."""
    return 300.0 + 40.0 * (file_time - CHIRP_LEAD)


def chirp_time(frequency):
    """The file time at which the chirp has this pitch."""
    return CHIRP_LEAD + (frequency - 300.0) / 40.0


def _make(path, video, audio, seconds):
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"{video}=size=320x180:rate=10:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=exprs='{audio}':s=48000:d={seconds}",
            "-ac",
            "2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            "10",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-shortest",
            path,
        ],
        check=True,
        timeout=120,
    )


def make_media(directory):
    """Write chirp.mp4 and tone.mp4 (H.264 + AAC, like a real download) and
    return {name: (path, analysis)}."""
    lead, tone_end = CHIRP_LEAD, CHIRP_LEAD + CHIRP_TONE
    t = f"(t-{lead})"
    chirp = f"if(between(t,{lead},{tone_end})," f"0.5*sin(2*PI*(300*{t}+20*{t}*{t})),0)"

    def build(name, video, audio, seconds):
        path = os.path.join(directory, f"{name}.mp4")
        _make(path, video, audio, seconds)
        return name, (path, analyse_media(path))

    jobs = (
        ("chirp", "testsrc2", chirp, CHIRP_LENGTH),
        ("tone", "testsrc", "0.05*sin(2*PI*440*t)", TONE_LENGTH),
    )
    with ThreadPoolExecutor(len(jobs)) as pool:
        return dict(pool.map(lambda job: build(*job), jobs))


class MediaKit:
    """The session's generated media; `track()` copies a file into the test's
    media directory (playback may delete it) and returns a queued-track dict
    shaped like media.download_media()'s."""

    def __init__(self, made):
        self.made = made
        self._count = 0

    def track(self, name, directory, title=None):
        source, analysis = self.made[name]
        self._count += 1
        path = os.path.join(directory, f"{name}{self._count}.mp4")
        shutil.copyfile(source, path)
        end = analysis["end"] if analysis["end"] is not None else analysis["duration"]
        return {
            "id": os.urandom(4).hex(),
            "filepath": path,
            "title": title or (CHIRP_TITLE if name == "chirp" else TONE_TITLE),
            "duration": analysis["duration"],
            "start": analysis["start"],
            "end": end,
            "loudnorm": analysis["loudnorm"],
            "thumbnail": None,
            "audio_only": False,
            "url": f"https://www.youtube.com/watch?v=e2e{name}",
            "video_id": None,
            "channel": "E2E Artist",
        }


HOLD_LIMIT = 5.0  # Seconds a PacedVoiceClient.hold may block reading


@dataclass
class Frame:
    gen: int  # The mixer's generation after the read: which track it belongs to
    overlap: bool  # Two tracks were mixed (crossfade)
    data: bytes
    at: float  # time.monotonic() of the read


class PacedVoiceClient:
    """discord.VoiceClient stand-in that really plays: like discord.py's
    AudioPlayer, a thread reads the source 20 ms frame by frame (here at
    `speed` times real time) and calls `after` once the source runs dry or
    stop() ends it. Every frame read is kept in `frames`."""

    def __init__(self, speed=1.0):
        self.speed = speed
        self.connected = True
        self.channel = types.SimpleNamespace(name="General")
        self.frames = []
        self.errors = []
        self._run = None  # The current playback: its stop and pause flags
        self._threads = []
        # Optional hold(source) -> bool: while true, reading waits (at most
        # HOLD_LIMIT seconds), as if the event loop answered instantly. Lets a
        # test read faster than real time without starving the loop's work.
        self.hold = None

    def play(self, source, after):
        if self._run is not None and not self._run.done.is_set():
            raise RuntimeError("Already playing audio.")
        run = types.SimpleNamespace(
            source=source,
            after=after,
            stopped=threading.Event(),
            resumed=threading.Event(),
            done=threading.Event(),
        )
        run.resumed.set()
        self._run = run
        thread = threading.Thread(target=self._play, args=(run,), daemon=True)
        self._threads.append(thread)
        thread.start()

    def _play(self, run):
        interval = 1.0 / FRAMES_PER_SECOND / self.speed
        due = time.monotonic()
        held_since = None
        try:
            while not run.stopped.is_set():
                if not run.resumed.is_set():
                    run.resumed.wait(0.05)
                    due = time.monotonic()
                    continue
                if self.hold is not None and self.hold(run.source):
                    held_since = held_since or time.monotonic()
                    if time.monotonic() - held_since < HOLD_LIMIT:
                        run.stopped.wait(0.005)
                        due = time.monotonic()
                        continue
                else:
                    held_since = None
                data = run.source.read()
                if not data:
                    break
                self.frames.append(
                    Frame(
                        getattr(run.source, "gen", 0),
                        getattr(run.source, "outgoing", None) is not None,
                        data,
                        time.monotonic(),
                    )
                )
                due += interval
                delay = due - time.monotonic()
                if delay > 0:
                    run.stopped.wait(delay)
        except Exception as e:  # Reported by the test, as discord.py would log it
            self.errors.append(e)
        finally:
            run.done.set()
            try:
                run.source.cleanup()
            finally:
                if run.after is not None:
                    run.after(None)

    def is_playing(self):
        run = self._run
        return bool(run and not run.done.is_set() and run.resumed.is_set())

    def is_paused(self):
        run = self._run
        return bool(run and not run.done.is_set() and not run.resumed.is_set())

    def is_connected(self):
        return self.connected

    def pause(self):
        if self._run:
            self._run.resumed.clear()

    def resume(self):
        if self._run:
            self._run.resumed.set()

    def stop(self):
        run, self._run = self._run, None
        if run is not None:
            run.stopped.set()
            run.resumed.set()

    async def disconnect(self, force=False):
        self.stop()
        self.connected = False

    def shutdown(self, timeout=5.0):
        """Test teardown: stop, and wait for the audio threads to end."""
        self.connected = False
        self.stop()
        for thread in self._threads:
            thread.join(timeout)

    def track_frames(self, gen):
        return [f for f in self.frames if f.gen == gen]


async def wait_until(predicate, timeout=20.0, interval=0.02, what="condition"):
    """Poll `predicate` until it is true; fail after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out waiting for {what}")
        await asyncio.sleep(interval)


def rms(frames):
    """RMS level of PCM frames, 0..32767."""
    if not frames:
        return 0
    return audioop.rms(b"".join(f.data for f in frames), 2)


def frequency(frames):
    """Dominant pitch of a pure tone in PCM frames (left channel), from its
    zero crossings."""
    samples = array.array("h")
    samples.frombytes(b"".join(f.data for f in frames))
    left = samples[0::2]
    crossings = sum(1 for a, b in zip(left, left[1:]) if (a < 0) != (b < 0))
    seconds = len(left) / 48000
    return crossings / 2 / seconds


def all_full_frames(frames):
    return all(len(f.data) == PCM_FRAME_SIZE for f in frames)
