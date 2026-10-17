"""CrossfadeMixer: the audio source handed to the voice client."""

import audioop  # Standard library up to 3.12; audioop-lts on 3.13+
import threading

import discord

from .config import FRAMES_PER_SECOND, PCM_FRAME_SIZE


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

    def __init__(self, source, gen, frames=None, fade_frames=0, on_tail=None):
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
            return self.read() if attached else b""
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
