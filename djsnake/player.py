"""Per-guild player state and the files behind its tracks."""

import asyncio
import os
import secrets
import time

from . import state, stats
from .config import HISTORY_SIZE


class MusicPlayer:
    def __init__(self):
        self.voice_client = None
        self.queue = []
        self.history = []  # Last played tracks, oldest first (files kept)
        self.is_playing = False  # A track is loaded in the voice client
        self.should_stop = False
        # Bumped by !stop / !reset: download jobs started before it give up,
        # even if a later !play clears should_stop again.
        self.epoch = 0
        self.now_playing = None
        self.loading_jobs = 0  # Playlist / web downloads still running
        self.sid = None  # Web session id (outlives the stay in voice)
        self.last_active = state.clock()
        self.text_channel = None  # Where the session was last announced
        self.started_at = None  # time.monotonic() when now_playing started
        self.paused_at = None  # time.monotonic() when paused, else None
        self.paused_total = 0.0  # Paused seconds since started_at
        # Serializes every change to the playback state (play_next, controls).
        self.lock = asyncio.Lock()
        # What play_next does when the current track stops: None (go on),
        # "next", "previous", "restart" or "seek". Set by controls before stop().
        self.pending_action = None
        # For "seek": where to restart (seconds) and whether to stay paused.
        self.seek_position = 0.0
        self.seek_paused = False
        # Bumped on each track start; a stale `after` callback is ignored.
        self.play_gen = 0
        self.crossfade = False  # Session setting, shared by everyone
        self.mixer = None  # CrossfadeMixer handed to the voice client
        # Status panel: the one message of the session that is edited in place.
        self.panel_message = None
        self.panel_text = None  # What the message currently shows
        self.panel_task = None  # Scheduled (throttled) refresh, if any
        self.panel_dirty = False
        self.panel_last_edit = float("-inf")  # time.monotonic() of last write
        self.panel_lock = asyncio.Lock()
        self.expired = False
        self.last_action = None  # Short line: "⏭️ Skipped via the web page"
        self.load_line = None  # Playlist progress / final result
        self.load_final = False  # load_line is a result, not progress
        self.single_downloads = 0  # Single-track downloads in progress
        # Tracks asked for but not queued yet, in request order, for the web
        # page: {key, title (None: not known yet), source: youtube|spotify}.
        self.pending_downloads = []
        # Stats of now_playing: actual play time, pauses and seeks excluded.
        self.play_offset = 0.0  # File time where the current segment began
        self.play_accum = 0.0  # Seconds played before the last seek/restart
        self.play_wall = None  # time.time() when the track started
        # Idle linger: the one timer that leaves voice once the queue has
        # run out (see playback.schedule_idle_disconnect), and its delay.
        self.idle_task = None
        self.idle_delay = None

    @property
    def loading(self):
        return self.loading_jobs > 0

    def lingering(self):
        """In voice with nothing to play, waiting to leave."""
        return self.idle_task is not None and not self.idle_task.done()

    def cancel_idle(self):
        """Drop the idle linger timer, if any (it never leaves after this)."""
        task, self.idle_task, self.idle_delay = self.idle_task, None, None
        if task is not None and not task.done():
            task.cancel()

    def in_voice(self):
        return bool(self.voice_client and self.voice_client.is_connected())

    def elapsed(self):
        if self.started_at is None:
            return None
        end = self.paused_at if self.paused_at is not None else time.monotonic()
        return max(0.0, end - self.started_at - self.paused_total)

    def played_seconds(self):
        """How long now_playing has really played, across seeks."""
        elapsed = self.elapsed()
        segment = 0.0 if elapsed is None else max(0.0, elapsed - self.play_offset)
        return self.play_accum + segment

    def mark_started(self, offset, same_track):
        """Call when now_playing starts at file time `offset`. A seek or a
        restart of the same track keeps its play time (see play_accum)."""
        if not same_track:
            self.play_accum = 0.0
            self.play_wall = time.time()
        self.play_offset = offset

    def add_to_queue(self, track):
        if not self.should_stop:
            self.queue.append(track)

    def next_in_queue(self):
        if self.queue:
            return self.queue.pop(0)
        return None

    def add_pending(self, title, source="youtube"):
        """List a requested track until it is queued. Returns its key, which
        the caller must hand to drop_pending() (in a finally)."""
        key = secrets.token_hex(6)
        self.pending_downloads.append({"key": key, "title": title, "source": source})
        return key

    def drop_pending(self, key):
        self.pending_downloads = [
            item for item in self.pending_downloads if item["key"] != key
        ]

    def name_pending(self, key, title):
        """Fill in the title once yt-dlp knows it."""
        for item in self.pending_downloads:
            if item["key"] == key and title:
                item["title"] = str(title)

    def stop_and_clear(self):
        self.cancel_idle()
        self.should_stop = True
        self.epoch += 1
        self.pending_downloads = []  # Their jobs give up with the epoch
        self.play_gen += 1  # The stopped track's callback must not advance
        self.pending_action = None
        self.mixer = None
        tracks = list(self.queue) + list(self.history)
        if self.now_playing:
            tracks.append(self.now_playing)
        self.queue.clear()
        self.history.clear()
        self.now_playing = None
        if self.voice_client and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        ):
            self.voice_client.stop()
        discard_tracks(tracks)
        self.started_at = None
        self.paused_at = None
        self.paused_total = 0.0
        self.is_playing = False

    async def disconnect_voice_client(self):
        """Safely disconnect and cleanup voice client"""
        if self.voice_client:
            try:
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                if self.voice_client.is_connected():
                    await self.voice_client.disconnect()
            except Exception as e:
                print(f"Error during voice client disconnect: {e}")
            finally:
                self.voice_client = None


def file_in_use(filepath):
    """True if any player still references this file (the same video
    downloaded twice, or in two guilds, shares one path)."""
    for player in state.music_players.values():
        tracks = list(player.queue) + list(player.history)
        if player.now_playing:
            tracks.append(player.now_playing)
        if any(t.get("filepath") == filepath for t in tracks):
            return True
    return False


def discard_tracks(tracks):
    """Delete the files of tracks no player references any more."""
    for track in tracks:
        filepath = track.get("filepath") if track else None
        if not filepath or file_in_use(filepath):
            continue
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except OSError as e:
            print(f"Could not delete {filepath}: {e}")


def guild_of(player):
    """The guild id of a player (None once replaced by !reset)."""
    for guild_id, candidate in state.music_players.items():
        if candidate is player:
            return guild_id
    return None


def retire_play(player, guild_id, action):
    """Queue the stats record of now_playing, which is being left by
    `action` (None: it ended by itself; "next", "previous" or "stop").
    Call it before started_at is reset. Only queues: safe under the lock."""
    track = player.now_playing
    if not track or guild_id is None or player.started_at is None:
        return
    played = player.played_seconds()
    stats.record_play(guild_id, track, player.play_wall, played, action)
    player.play_accum = 0.0


def push_history(player, track):
    player.history.append(track)
    evicted = []
    while len(player.history) > HISTORY_SIZE:
        evicted.append(player.history.pop(0))
    discard_tracks(evicted)
