"""CrossfadeMixer: the audio source handed to the voice client."""

import array
import audioop  # Standard library up to 3.12; audioop-lts on 3.13+
import math
import threading

import discord

from .config import (
    FRAMES_PER_SECOND,
    PCM_FRAME_SIZE,
    VOLUME_MAX,
    VOLUME_RAMP_FRAMES,
)

# Where the soft limiter starts, as a fraction of full scale. Below it the
# signal is only scaled; above it peaks are squeezed towards (never past) full
# scale instead of clipping hard.
LIMIT_KNEE = 0.9
GAIN_SLICES = 8  # A frame is ramped in this many steps, so a change is smooth
# Largest gain change per frame: the whole range in VOLUME_RAMP_FRAMES frames.
MAX_GAIN_STEP = (VOLUME_MAX / 100.0) / VOLUME_RAMP_FRAMES


def scale(chunk, gain):
    """16-bit PCM `chunk` times `gain`, soft-limited above LIMIT_KNEE."""
    full = 32767
    if audioop.max(chunk, 2) * gain <= LIMIT_KNEE * full:
        return audioop.mul(chunk, 2, gain)
    samples = array.array("h", chunk)
    knee, room = LIMIT_KNEE, 1.0 - LIMIT_KNEE
    for i, x in enumerate(samples):
        v = abs(x) * gain / full
        if v > knee:
            v = knee + room * math.tanh((v - knee) / room)
        v = min(v, 1.0) * full
        samples[i] = int(v) if x >= 0 else -int(v)
    return samples.tobytes()


def apply_gain(chunk, start, end):
    """`chunk` with the gain gliding linearly from `start` to `end`."""
    if start == end:
        return scale(chunk, end)
    size = max(4, len(chunk) // GAIN_SLICES // 4 * 4)
    out = []
    for i, pos in enumerate(range(0, len(chunk), size)):
        gain = start + (end - start) * min(1.0, (i + 1) * size / len(chunk))
        out.append(scale(chunk[pos : pos + size], gain))
    return b"".join(out)


class CrossfadeMixer(discord.AudioSource):
    """The source handed to the voice client: plays one track's FFmpeg source
    and, once a crossfade is attached, mixes the tail of the outgoing track
    under the head of the next one.

    The FFmpeg filters do the fades, so mixing is a saturating sample-wise
    add, done only while two sources overlap. When the current track reaches
    its fade-out, `on_tail(mixer, gen)` is called once (on the audio thread)
    so the event loop can `attach()` the next track. A chain of crossfaded
    tracks is a single voice-client playback: the `after` callback fires once,
    when the last one ends, and reads `gen` to know which track that was.
    """

    def __init__(
        self, source, gen, frames=None, fade_frames=0, on_tail=None, volume=1.0
    ):
        self._lock = threading.Lock()
        self.source = source
        self.outgoing = None  # Previous track, draining under `source`
        self.gen = gen
        self._frames_left = frames  # Frames until `source` ends (None: unknown)
        self._fade_frames = fade_frames  # Length of its fade-out, in frames
        self._on_tail = on_tail
        self._tail_signalled = False
        self._pending = None  # First frame of an attached source, already read
        self.finished = False
        # Output gain stage (the server's volume). `_gain` glides to `_target`.
        self._gain = self._target = float(volume)

    @property
    def _current_error(self):
        # Read by discord.py's AudioPlayer when the source ends.
        return getattr(self.source, "_current_error", None)

    def is_opus(self):
        return False

    def remaining_seconds(self):
        if self._frames_left is None:
            return None
        return self._frames_left / FRAMES_PER_SECOND

    @property
    def volume(self):
        """The gain the output is heading to (1.0 = the normalised level)."""
        return self._target

    def set_volume(self, gain):
        """Change the gain without restarting: it glides over a few frames."""
        self._target = max(0.0, float(gain))

    def rearm(self):
        """Signal the tail again on the next frame (the crossfade was put off,
        e.g. by a pause)."""
        self._tail_signalled = False

    def attach(self, source, first_frame, gen, frames, fade_frames):
        """Start mixing `source` (whose first frame was already read) over the
        current one. False if playback has ended or a crossfade is running."""
        with self._lock:
            if self.finished or self.outgoing is not None:
                return False
            self.outgoing, self.source = self.source, source
            self._pending = first_frame
            self.gen = gen
            self._frames_left = frames
            self._fade_frames = fade_frames
            self._tail_signalled = False
            return True

    def read(self):
        data = self._read_mixed()
        if not data or (self._gain == self._target == 1.0):
            return data
        start = self._gain
        step = max(-MAX_GAIN_STEP, min(MAX_GAIN_STEP, self._target - start))
        self._gain = (
            self._target
            if abs(self._target - start) <= MAX_GAIN_STEP
            else (start + step)
        )
        return apply_gain(data, start, self._gain)

    def _read_mixed(self):
        with self._lock:
            if self.finished:
                return b""
            source, outgoing, data = self.source, self.outgoing, self._pending
            self._pending = None
        if data is None:
            data = source.read()
        if outgoing is not None:
            old = outgoing.read()
            if len(old) == PCM_FRAME_SIZE and len(data) == PCM_FRAME_SIZE:
                data = audioop.add(data, old, 2)  # Clips instead of wrapping
            elif not data:
                data = old
            if not old:  # The outgoing track is over: back to a single source
                with self._lock:
                    if self.outgoing is outgoing:
                        self.outgoing = None
                outgoing.cleanup()
        if not data:
            with self._lock:
                # A source attached just as `source` ended plays on.
                attached = self.source is not source
                if not attached:
                    self.finished = True
            return self._read_mixed() if attached else b""
        if self._frames_left is not None:
            self._frames_left -= 1
            if (
                not self._tail_signalled
                and self._on_tail is not None
                and self._fade_frames > 0
                and self._frames_left <= self._fade_frames
            ):
                self._tail_signalled = True
                try:
                    self._on_tail(self, self.gen)
                except Exception as e:
                    print(f"Crossfade signal failed: {e}")
        return data

    def cleanup(self):
        with self._lock:
            self.finished = True
            sources = [self.source, self.outgoing]
            self.outgoing = None
        for source in sources:
            if source is not None and hasattr(source, "cleanup"):
                try:
                    source.cleanup()
                except Exception as e:
                    print(f"Audio source cleanup failed: {e}")
