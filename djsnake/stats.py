"""Listening statistics, per guild, in SQLite (`{DATA_DIR}/stats.db`).

Recorded: every track played (who asked for it, how long it really played,
whether it was skipped), each time the bot joins voice (a session) and the
daily peak of humans in its voice channel.

Writes go through one daemon writer thread fed by a queue, so the event loop
never waits on the disk, and they are best effort: a failure is printed and
dropped, it never reaches playback. Reads (the web route) run in an executor
on their own connection; WAL mode lets them run while the writer works.
"""

import datetime
import os
import queue
import re
import sqlite3
import threading
import time

from .config import SKIP_SECONDS, STATS_DB

DB_PATH = STATS_DB  # Module attribute so tests can point it elsewhere
SCHEMA_VERSION = 1
# Seconds back from now; None: everything.
RANGES = {"week": 7 * 86400, "month": 30 * 86400, "all": None}
# The ways of leaving a track that can count as a skip (jump is a "next").
SKIP_ACTIONS = ("next", "previous", "stop")
TEXT_MAX = 200  # Titles, artists and names are cut to this length
_QUEUE_MAX = 10000  # Records waiting for the writer; more are dropped

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    video_id TEXT,
    title TEXT NOT NULL,
    artist TEXT,
    duration REAL,
    requester TEXT,              -- Discord display name; NULL: the web page
    started_at REAL NOT NULL,    -- Unix time
    played_seconds REAL NOT NULL,
    skipped INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS plays_guild_time ON plays (guild_id, started_at);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    started_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_guild_time ON sessions (guild_id, started_at);
CREATE TABLE IF NOT EXISTS listener_peaks (
    guild_id INTEGER NOT NULL,
    day TEXT NOT NULL,           -- UTC date, YYYY-MM-DD
    listeners INTEGER NOT NULL,
    at REAL NOT NULL,            -- When that peak was first reached
    PRIMARY KEY (guild_id, day)
);
"""


def _connect(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        # Future versions add their migration steps here, one per version.
        conn.executescript(_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
    return conn


# --- Writer thread ------------------------------------------------------------

_jobs = queue.Queue(maxsize=_QUEUE_MAX)
_writer = None
_writer_lock = threading.Lock()
_writer_conn = None
_writer_path = None


def _conn_for_writer():
    global _writer_conn, _writer_path
    if _writer_conn is None or _writer_path != DB_PATH:
        if _writer_conn is not None:
            try:
                _writer_conn.close()
            except sqlite3.Error:
                pass
        _writer_conn = None
        _writer_conn = _connect(DB_PATH)
        _writer_path = DB_PATH
    return _writer_conn


def _write_loop():
    global _writer_conn
    while True:
        fn, args = _jobs.get()
        try:
            conn = _conn_for_writer()
            with conn:  # One transaction per record
                fn(conn, *args)
        except Exception as e:
            print(f"Stats write failed: {e}")
            if _writer_conn is not None:
                try:
                    _writer_conn.close()
                except Exception:
                    pass
                _writer_conn = None  # Reopen on the next record
        finally:
            _jobs.task_done()


def _submit(fn, *args):
    global _writer
    try:
        if _writer is None:
            with _writer_lock:
                if _writer is None:
                    _writer = threading.Thread(
                        target=_write_loop, name="stats-writer", daemon=True
                    )
                    _writer.start()
        _jobs.put_nowait((fn, args))
    except Exception as e:  # queue.Full, or no thread
        print(f"Stats record dropped: {e}")


def flush():
    """Wait until every queued record is written (tests, shutdown)."""
    if _writer is not None:
        _jobs.join()


# --- Recording ------------------------------------------------------------------


def _text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value[:TEXT_MAX] or None


_TOPIC_RE = re.compile(r"\s*-\s*Topic\s*$", re.IGNORECASE)
_VEVO_RE = re.compile(r"\s*VEVO\s*$")


def clean_artist(name):
    """A YouTube channel name as an artist: "Daft Punk - Topic" and
    "TaylorSwiftVEVO" become "Daft Punk" and "Taylor Swift"."""
    name = (name or "").strip()
    name = _TOPIC_RE.sub("", name)
    without = _VEVO_RE.sub("", name)
    if without != name and " " not in without:
        # VEVO channels glue the words: split them back on capitals.
        without = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", without)
    return without.strip() or None


def guess_artist(channel, title):
    """The artist of a YouTube video: its channel, cleaned, or else the
    "Artist - Title" part of its title."""
    artist = clean_artist(channel)
    if artist:
        return artist
    if title and " - " in str(title):
        return str(title).split(" - ", 1)[0].strip() or None
    return None


def artist_of(track):
    """Spotify's artist when the track came from a Spotify link."""
    return track.get("artist") or guess_artist(track.get("channel"), track.get("title"))


def _insert_play(conn, row):
    conn.execute(
        "INSERT INTO plays (guild_id, video_id, title, artist, duration, requester,"
        " started_at, played_seconds, skipped) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        row,
    )


def record_play(guild_id, track, started_at, played, action):
    """Queue the record of a track that stopped playing. `action` is how it
    was left (None: it ended by itself, or "next", "previous", "stop")."""
    try:
        played = max(0.0, float(played or 0.0))
        skipped = action in SKIP_ACTIONS and played < SKIP_SECONDS
        duration = track.get("duration")
        row = (
            int(guild_id),
            _text(track.get("video_id")),
            _text(track.get("title")) or "?",
            _text(artist_of(track)),
            float(duration) if duration else None,
            _text(track.get("requester")),
            float(started_at if started_at is not None else time.time() - played),
            round(played, 1),
            int(skipped),
        )
    except Exception as e:
        print(f"Stats record skipped: {e}")
        return
    _submit(_insert_play, row)


def _insert_session(conn, guild_id, at):
    conn.execute(
        "INSERT INTO sessions (guild_id, started_at) VALUES (?, ?)", (guild_id, at)
    )


def record_session(guild_id, at=None):
    """The bot joined a voice channel."""
    _submit(_insert_session, int(guild_id), float(at or time.time()))


# Guild id -> (UTC day, highest count written that day): spares the writer
# the voice events that cannot beat the peak.
_peaks = {}


def _day(at):
    return datetime.datetime.fromtimestamp(at, datetime.timezone.utc).strftime(
        "%Y-%m-%d"
    )


def _upsert_peak(conn, guild_id, day, count, at):
    conn.execute(
        "INSERT INTO listener_peaks (guild_id, day, listeners, at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (guild_id, day) DO UPDATE SET"
        " listeners = excluded.listeners, at = excluded.at"
        " WHERE excluded.listeners > listener_peaks.listeners",
        (guild_id, day, count, at),
    )


def record_listeners(guild_id, count, at=None):
    """Humans now in the bot's voice channel; only a new daily peak is
    written."""
    try:
        at = float(at or time.time())
        day = _day(at)
        known = _peaks.get(guild_id)
        if count <= 0 or (known and known[0] == day and known[1] >= count):
            return
        _peaks[guild_id] = (day, count)
        _submit(_upsert_peak, int(guild_id), day, int(count), at)
    except Exception as e:
        print(f"Stats listeners skipped: {e}")


def count_listeners(channel):
    """Humans (not bots) in a voice channel."""
    members = getattr(channel, "members", None) or []
    return sum(1 for m in members if not getattr(m, "bot", False))


# --- Reading --------------------------------------------------------------------


def empty_summary(range_name):
    return {
        "range": range_name,
        "top_tracks": [],
        "top_artists": [],
        "top_djs": [],
        "dj_of_week": None,
        "most_skipped": None,
        "totals": {"hours": 0.0, "tracks": 0, "sessions": 0},
        "heatmap": [0] * 168,
        "peak_listeners": None,
    }


# Hour of the week in UTC: Monday 00:00 is 0, Sunday 23:00 is 167.
_HOUR_OF_WEEK = (
    "((CAST(strftime('%w', started_at, 'unixepoch') AS INTEGER) + 6) % 7) * 24"
    " + CAST(strftime('%H', started_at, 'unixepoch') AS INTEGER)"
)


def summary(guild_id, range_name, now=None):
    """The stats of one guild over `range_name` (a RANGES key). Blocking:
    run it in an executor."""
    now = time.time() if now is None else now
    span = RANGES[range_name]
    since = now - span if span else float("-inf")
    result = empty_summary(range_name)
    if not os.path.exists(DB_PATH):
        return result
    conn = _connect(DB_PATH)
    try:
        where = "guild_id = ? AND started_at >= ?"
        args = (guild_id, since)
        result["top_tracks"] = [
            {"title": title, "artist": artist, "video_id": video_id, "plays": n}
            for title, artist, video_id, n in conn.execute(
                "SELECT title, artist, video_id, COUNT(*) AS n FROM plays"
                f" WHERE {where} AND skipped = 0"
                " GROUP BY COALESCE(video_id, title)"
                " ORDER BY n DESC, MAX(started_at) DESC LIMIT 10",
                args,
            )
        ]
        result["top_artists"] = [
            {"name": name, "plays": n}
            for name, n in conn.execute(
                "SELECT artist, COUNT(*) AS n FROM plays"
                f" WHERE {where} AND skipped = 0 AND artist IS NOT NULL"
                " GROUP BY artist ORDER BY n DESC, MAX(started_at) DESC LIMIT 10",
                args,
            )
        ]
        # A NULL requester is the web page: one entry for all its adds.
        result["top_djs"] = [
            {"name": name, "plays": n}
            for name, n in conn.execute(
                f"SELECT requester, COUNT(*) AS n FROM plays WHERE {where}"
                " GROUP BY requester ORDER BY n DESC, MAX(started_at) DESC LIMIT 5",
                args,
            )
        ]
        row = conn.execute(
            "SELECT requester, COUNT(*) AS n FROM plays WHERE guild_id = ?"
            " AND started_at >= ? AND requester IS NOT NULL"
            " GROUP BY requester ORDER BY n DESC, MAX(started_at) DESC LIMIT 1",
            (guild_id, now - RANGES["week"]),
        ).fetchone()
        if row:
            result["dj_of_week"] = {"name": row[0], "plays": row[1]}
        row = conn.execute(
            "SELECT title, artist, COUNT(*) AS n FROM plays"
            f" WHERE {where} AND skipped = 1 GROUP BY COALESCE(video_id, title)"
            " ORDER BY n DESC, MAX(started_at) DESC LIMIT 1",
            args,
        ).fetchone()
        if row:
            result["most_skipped"] = {
                "title": row[0],
                "artist": row[1],
                "skips": row[2],
            }
        seconds, tracks = conn.execute(
            "SELECT COALESCE(SUM(played_seconds), 0), COUNT(*) FROM plays"
            f" WHERE {where}",
            args,
        ).fetchone()
        (sessions,) = conn.execute(
            f"SELECT COUNT(*) FROM sessions WHERE {where}", args
        ).fetchone()
        result["totals"] = {
            "hours": round(seconds / 3600, 1),
            "tracks": tracks,
            "sessions": sessions,
        }
        heatmap = [0] * 168
        for bucket, n in conn.execute(
            f"SELECT {_HOUR_OF_WEEK} AS b, COUNT(*) FROM plays WHERE {where}"
            " GROUP BY b",
            args,
        ):
            if bucket is not None and 0 <= bucket < 168:
                heatmap[bucket] = n
        result["heatmap"] = heatmap
        row = conn.execute(
            "SELECT listeners, at FROM listener_peaks WHERE guild_id = ? AND at >= ?"
            " ORDER BY listeners DESC, at ASC LIMIT 1",
            args,
        ).fetchone()
        if row:
            result["peak_listeners"] = {"count": row[0], "at": row[1]}
    finally:
        conn.close()
    return result
