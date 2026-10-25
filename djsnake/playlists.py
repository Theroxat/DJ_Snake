"""Saved playlists, per guild, in SQLite (`{DATA_DIR}/playlists.db`).

Only the YouTube link, title and duration of each track are stored (never a
media file): loading a playlist downloads its tracks again. Unlike the stats,
a failed write must reach the user, so this module does not use the stats
writer thread. Every function is blocking, opens its own short-lived
connection and is meant to run in an executor (see `run()`); WAL mode and the
busy timeout let concurrent calls queue up on SQLite's own lock.
"""

import asyncio
import os
import re
import sqlite3
import time

from .config import (
    MAX_SAVED_PLAYLISTS,
    MAX_SAVED_TRACKS,
    PLAYLISTS_DB,
    SAVED_PLAYLIST_NAME_MAX,
)

DB_PATH = PLAYLISTS_DB  # Module attribute so tests can point it elsewhere
SCHEMA_VERSION = 1
TEXT_MAX = 200  # Titles are cut to this length
URL_MAX = 200

# Letters, digits, spaces and a few marks: nothing that pings or formats.
_NAME_RE = re.compile(r"^\w[\w .'&!+()-]*$")
# `/playlist export queue` means the live queue, so no playlist may take it.
RESERVED_KEYS = frozenset({"queue"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,           -- Case-folded name: names are unique per guild
    name TEXT NOT NULL,          -- As typed
    creator_id INTEGER,          -- Discord user id; NULL: saved from the web page
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (guild_id, key)
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id INTEGER NOT NULL REFERENCES playlists (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    duration REAL,
    PRIMARY KEY (playlist_id, position)
);
"""


class PlaylistError(Exception):
    """A playlist operation that cannot be done. `code` is a ControlError
    code (see controls.py)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _connect():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        # Future versions add their migration steps here, one per version.
        conn.executescript(_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
    return conn


async def run(fn, *args, **kwargs):
    """Run one of the blocking functions below off the event loop. Raises
    PlaylistError; any other storage failure becomes `playlist_unavailable`."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))
    except PlaylistError:
        raise
    except (sqlite3.Error, OSError) as e:
        print(f"Playlist storage failed: {e}")
        raise PlaylistError("playlist_unavailable")


def clean_name(name):
    """(display name, key) of a playlist name, or PlaylistError."""
    name = " ".join(str(name or "").split())
    if not name or len(name) > SAVED_PLAYLIST_NAME_MAX or not _NAME_RE.match(name):
        raise PlaylistError("bad_playlist_name")
    if name.casefold() in RESERVED_KEYS:
        raise PlaylistError("bad_playlist_name")
    return name, name.casefold()


def clean_track(track):
    """{url, title, duration} of a queued track, or None if it has no link."""
    url = str(track.get("url") or "").strip()
    if not url or len(url) > URL_MAX:
        return None
    title = str(track.get("title") or "").strip()[:TEXT_MAX] or "?"
    duration = track.get("duration")
    try:
        duration = float(duration) if duration else None
    except (TypeError, ValueError):
        duration = None
    return {"url": url, "title": title, "duration": duration}


def _find(conn, guild_id, key):
    return conn.execute(
        "SELECT * FROM playlists WHERE guild_id = ? AND key = ?", (guild_id, key)
    ).fetchone()


def _tracks(conn, playlist_id):
    rows = conn.execute(
        "SELECT url, title, duration FROM playlist_tracks WHERE playlist_id = ?"
        " ORDER BY position",
        (playlist_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def can_edit(row, user_id, is_admin):
    """Who may replace or delete a playlist: its creator, or a manager."""
    mine = user_id is not None and row["creator_id"] == user_id
    return bool(is_admin or mine)


def check_writable(guild_id, name, creator_id=None, is_admin=False):
    """Raise PlaylistError unless `save()` could create or replace `name` for
    this caller now (the same rules, checked before slow work such as
    matching tracks). Returns the display name."""
    name, key = clean_name(name)
    conn = _connect()
    try:
        row = _find(conn, guild_id, key)
        if row is not None:
            if not can_edit(row, creator_id, is_admin):
                raise PlaylistError("playlist_exists")
        else:
            count = conn.execute(
                "SELECT COUNT(*) FROM playlists WHERE guild_id = ?", (guild_id,)
            ).fetchone()[0]
            if count >= MAX_SAVED_PLAYLISTS:
                raise PlaylistError("playlist_limit")
    finally:
        conn.close()
    return name


def _insert_tracks(conn, playlist_id, start, tracks):
    conn.executemany(
        "INSERT INTO playlist_tracks (playlist_id, position, url, title, duration)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (playlist_id, start + i, t["url"], t["title"], t["duration"])
            for i, t in enumerate(tracks)
        ],
    )


def save(guild_id, name, tracks, creator_id=None, is_admin=False):
    """Create a playlist from `tracks`, or replace one the caller may edit.
    Returns {name, count}."""
    name, key = clean_name(name)
    tracks = [t for t in map(clean_track, tracks) if t]
    if not tracks:
        raise PlaylistError("playlist_empty")
    if len(tracks) > MAX_SAVED_TRACKS:
        raise PlaylistError("playlist_full")
    now = time.time()
    conn = _connect()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _find(conn, guild_id, key)
            if row is not None:
                if not can_edit(row, creator_id, is_admin):
                    raise PlaylistError("playlist_exists")
                playlist_id = row["id"]
                conn.execute(
                    "DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
                )
                conn.execute(
                    "UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?",
                    (name, now, playlist_id),
                )
            else:
                count = conn.execute(
                    "SELECT COUNT(*) FROM playlists WHERE guild_id = ?", (guild_id,)
                ).fetchone()[0]
                if count >= MAX_SAVED_PLAYLISTS:
                    raise PlaylistError("playlist_limit")
                playlist_id = conn.execute(
                    "INSERT INTO playlists (guild_id, key, name, creator_id,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (guild_id, key, name, creator_id, now, now),
                ).lastrowid
            _insert_tracks(conn, playlist_id, 0, tracks)
    finally:
        conn.close()
    return {"name": name, "count": len(tracks)}


def add_track(guild_id, name, track):
    """Append one track to an existing playlist. Returns {name, count}."""
    _, key = clean_name(name)
    track = clean_track(track)
    if track is None:
        raise PlaylistError("nothing_playing")
    conn = _connect()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _find(conn, guild_id, key)
            if row is None:
                raise PlaylistError("playlist_not_found")
            count = conn.execute(
                "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?",
                (row["id"],),
            ).fetchone()[0]
            if count >= MAX_SAVED_TRACKS:
                raise PlaylistError("playlist_full")
            _insert_tracks(conn, row["id"], count, [track])
            conn.execute(
                "UPDATE playlists SET updated_at = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
    finally:
        conn.close()
    return {"name": row["name"], "count": count + 1}


def get(guild_id, name):
    """{name, creator_id, tracks: [{url, title, duration}]}."""
    _, key = clean_name(name)
    conn = _connect()
    try:
        row = _find(conn, guild_id, key)
        if row is None:
            raise PlaylistError("playlist_not_found")
        return {
            "name": row["name"],
            "creator_id": row["creator_id"],
            "tracks": _tracks(conn, row["id"]),
        }
    finally:
        conn.close()


def list_all(guild_id):
    """[{name, tracks, duration}] by name; `tracks` is the count."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT p.name, COUNT(t.position) AS tracks,"
            " COALESCE(SUM(t.duration), 0) AS duration"
            " FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id"
            " WHERE p.guild_id = ? GROUP BY p.id ORDER BY p.key",
            (guild_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def delete(guild_id, name, user_id=None, is_admin=False):
    """Delete a playlist the caller may edit. Returns its name."""
    _, key = clean_name(name)
    conn = _connect()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _find(conn, guild_id, key)
            if row is None:
                raise PlaylistError("playlist_not_found")
            if not can_edit(row, user_id, is_admin):
                raise PlaylistError("playlist_forbidden")
            conn.execute("DELETE FROM playlists WHERE id = ?", (row["id"],))
    finally:
        conn.close()
    return row["name"]
