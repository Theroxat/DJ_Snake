"""Backup and restore of the persistent data (`DATA_DIR`).

Command line (run it where the data lives, e.g. `docker compose exec dj-snake`):

    python -m djsnake.backup create [--out FILE] [--include-cache]
    python -m djsnake.backup restore FILE
    python -m djsnake.backup verify FILE
    python -m djsnake.backup list

An archive is a `.tar.gz` holding `manifest.json` (version, date, file list with
sizes and SHA-256) and the files under `data/`. SQLite databases are copied
with `sqlite3.Connection.backup()`, so a running bot never yields a torn copy.
`lyrics/` (a cache) is left out unless asked for; `backups/`, `.pre-restore-*`
and the lock file are never included.

A running bot holds an exclusive `flock` on `DATA_DIR/.bot.lock` (see
`acquire_bot_lock()`); `restore` refuses while it is held. Imports only
`config`, so the command line starts without Discord.
"""

import argparse
import asyncio
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile

from . import config

try:  # POSIX only; elsewhere the running-bot check is skipped
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
PREFIX = "data/"
LOCK_NAME = ".bot.lock"
BACKUP_DIRNAME = "backups"
PRE_RESTORE_PREFIX = ".pre-restore-"
RESTORE_TMP_PREFIX = ".restore-tmp-"
CACHE_DIRS = ("lyrics",)
AUTO_PREFIX = "djsnake-auto-"
MANUAL_PREFIX = "djsnake-backup-"
SQLITE_MAGIC = b"SQLite format 3\x00"
SKIP_SUFFIXES = ("-wal", "-shm", "-journal", ".tmp")

_lock_fd = None


class BackupError(Exception):
    """A backup or restore failed; the message is shown to the operator."""


# --- helpers ---------------------------------------------------------------


def _data_dir():
    return config.DATA_DIR


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _excluded_top(name, include_cache):
    """True for top-level entries that never belong in a backup."""
    if name in (BACKUP_DIRNAME, LOCK_NAME):
        return True
    if name.startswith((PRE_RESTORE_PREFIX, RESTORE_TMP_PREFIX, ".backup-stage-")):
        return True
    return name in CACHE_DIRS and not include_cache


def _walk(root, include_cache):
    """Relative paths (posix) of the regular files to back up, sorted."""
    found = []
    for top in sorted(os.listdir(root)):
        if _excluded_top(top, include_cache):
            continue
        full = os.path.join(root, top)
        if os.path.islink(full):
            continue
        if os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [
                    d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
                ]
                for name in filenames:
                    p = os.path.join(dirpath, name)
                    if os.path.islink(p) or name.endswith(SKIP_SUFFIXES):
                        continue
                    found.append(os.path.relpath(p, root).replace(os.sep, "/"))
        elif os.path.isfile(full) and not top.endswith(SKIP_SUFFIXES):
            found.append(top)
    return sorted(found)


def _copy_sqlite(src, dst):
    """Consistent copy of a live database (works with WAL and concurrent writers)."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _safe_member(name):
    """The relative path inside data/ for a tar member name, or None if unsafe."""
    if not name.startswith(PREFIX):
        return None
    rel = name[len(PREFIX) :]
    parts = rel.split("/")
    if not rel or rel.startswith("/") or "\\" in rel or "" in parts:
        return None
    if any(p in (".", "..") for p in parts):
        return None
    return rel


# --- create ----------------------------------------------------------------


def create_backup(out=None, include_cache=False, data_dir=None):
    """Write a backup archive and return its path."""
    data_dir = data_dir or _data_dir()
    if not os.path.isdir(data_dir):
        raise BackupError(f"Data directory not found: {data_dir}")
    if out is None:
        out = os.path.join(
            data_dir, BACKUP_DIRNAME, f"{MANUAL_PREFIX}{_stamp()}.tar.gz"
        )
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".backup-stage-", dir=os.path.dirname(out))
    tmp_out = out + ".tmp"
    try:
        entries = []
        for rel in _walk(data_dir, include_cache):
            if os.path.abspath(os.path.join(data_dir, rel)) == out:
                continue
            src = os.path.join(data_dir, rel)
            path = src
            if _is_sqlite(src):
                path = os.path.join(stage, rel.replace("/", "__"))
                try:
                    _copy_sqlite(src, path)
                except sqlite3.Error as e:
                    raise BackupError(f"Could not copy database {rel}: {e}")
            elif not os.path.exists(src):
                continue  # vanished while the bot runs (e.g. a temp file)
            try:
                size = os.path.getsize(path)
                digest = _sha256(path)
            except OSError:
                continue
            entries.append({"path": rel, "size": size, "sha256": digest, "_src": path})
        manifest = {
            "version": FORMAT_VERSION,
            "app_version": config.APP_VERSION,
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "include_cache": bool(include_cache),
            "files": [{k: v for k, v in e.items() if k != "_src"} for e in entries],
        }
        blob = json.dumps(manifest, indent=2).encode("utf-8")
        with tarfile.open(tmp_out, "w:gz") as tar:
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(blob)
            info.mtime = int(datetime.datetime.now().timestamp())
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(blob))
            for e in entries:
                info = tar.gettarinfo(e["_src"], arcname=PREFIX + e["path"])
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(e["_src"], "rb") as f:
                    tar.addfile(info, f)
        os.chmod(tmp_out, 0o600)
        os.replace(tmp_out, out)
    except BaseException:
        if os.path.exists(tmp_out):
            os.unlink(tmp_out)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return out


# --- verify ----------------------------------------------------------------


def read_manifest(archive):
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember(MANIFEST)
            if not member.isfile() or member.size > 8 << 20:
                raise BackupError("Invalid manifest")
            manifest = json.load(tar.extractfile(member))
    except BackupError:
        raise
    except (OSError, tarfile.TarError, KeyError, ValueError, EOFError) as e:
        raise BackupError(f"Not a valid DJ Snake backup ({e})")
    if not isinstance(manifest, dict) or manifest.get("version") != FORMAT_VERSION:
        raise BackupError("Unsupported backup version")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BackupError("Invalid manifest")
    seen = set()
    for f in files:
        ok = (
            isinstance(f, dict)
            and isinstance(f.get("path"), str)
            and _safe_member(PREFIX + f["path"]) is not None
            and isinstance(f.get("size"), int)
            and isinstance(f.get("sha256"), str)
            and f["path"] not in seen
        )
        if not ok:
            raise BackupError("Invalid manifest entry")
        seen.add(f["path"])
    return manifest


def verify_backup(archive):
    """Check the manifest and every checksum. Returns the manifest."""
    return _read_archive(archive, None)


# Free space kept beyond the archive's total size when restoring.
RESTORE_SPACE_MARGIN = 16 << 20


def _read_archive(archive, dest):
    """Stream the archive, checking each file against the manifest, optionally
    extracting into dest. Raises BackupError on any mismatch. A member whose
    size differs from the manifest is refused before anything is written, and
    no more than the manifest's size is ever read from it."""
    manifest = read_manifest(archive)
    expected = {f["path"]: f for f in manifest["files"]}
    if dest is not None:
        total = sum(max(0, f["size"]) for f in manifest["files"])
        try:
            free = shutil.disk_usage(dest).free
        except OSError:
            free = None
        if free is not None and free < total + RESTORE_SPACE_MARGIN:
            raise BackupError(
                f"Not enough free space to restore: {_human(total)} needed, "
                f"{_human(free)} free."
            )
    done = set()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if member.name == MANIFEST:
                    continue
                rel = _safe_member(member.name)
                if rel is None or not member.isfile():
                    raise BackupError(f"Unexpected entry in archive: {member.name}")
                entry = expected.get(rel)
                if entry is None or rel in done:
                    raise BackupError(f"File not in manifest: {rel}")
                if member.size != entry["size"]:
                    raise BackupError(f"Size mismatch: {rel}")
                h = hashlib.sha256()
                size = 0
                out = None
                if dest is not None:
                    target = os.path.join(dest, *rel.split("/"))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    out = open(target, "wb")
                try:
                    src = tar.extractfile(member)
                    left = entry["size"]
                    while left > 0:  # Never more than the manifest says
                        chunk = src.read(min(1 << 20, left))
                        if not chunk:
                            break
                        h.update(chunk)
                        size += len(chunk)
                        left -= len(chunk)
                        if out:
                            out.write(chunk)
                finally:
                    if out:
                        out.close()
                if size != entry["size"] or h.hexdigest() != entry["sha256"]:
                    raise BackupError(f"Checksum mismatch: {rel}")
                done.add(rel)
    except (OSError, tarfile.TarError, EOFError) as e:
        raise BackupError(f"Archive unreadable: {e}")
    missing = set(expected) - done
    if missing:
        raise BackupError(f"Files missing from archive: {sorted(missing)[0]}")
    return manifest


# --- restore ---------------------------------------------------------------


def acquire_bot_lock(data_dir=None):
    """Hold the 'bot is running' lock for the life of the process. Returns False
    when another process already holds it (or locking is unavailable)."""
    global _lock_fd
    if fcntl is None:
        return False
    data_dir = data_dir or _data_dir()
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd = os.open(os.path.join(data_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _lock_fd = fd
    return True


def bot_running(data_dir=None):
    """True when a bot process holds the lock."""
    if fcntl is None:
        return False
    data_dir = data_dir or _data_dir()
    path = os.path.join(data_dir, LOCK_NAME)
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def _take_restore_lock(data_dir):
    """Hold the bot lock for the whole restore, so a bot cannot start (or be
    running) meanwhile. Returns the fd; raises BackupError when it is held or
    locking is unavailable."""
    if fcntl is None:
        raise BackupError("Restore needs file locking (POSIX), not available here.")
    os.makedirs(data_dir, exist_ok=True)
    try:
        fd = os.open(os.path.join(data_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as e:
        raise BackupError(f"Could not open the data lock: {e.strerror or e}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise BackupError(
            "The bot is running: stop it first (docker compose stop dj-snake)."
        )
    return fd


def restore_backup(archive, data_dir=None):
    """Replace the data with the archive's. Returns the directory holding the
    previous data. Holds the bot lock throughout: refused while a bot runs."""
    data_dir = data_dir or _data_dir()
    fd = _take_restore_lock(data_dir)
    try:
        return _restore_locked(archive, data_dir)
    finally:
        os.close(fd)  # Releases the lock


def _restore_locked(archive, data_dir):
    stage = os.path.join(data_dir, f"{RESTORE_TMP_PREFIX}{_stamp()}")
    os.makedirs(stage)
    pre = os.path.join(data_dir, f"{PRE_RESTORE_PREFIX}{_stamp()}")
    moved = []
    installed = []
    try:
        manifest = _read_archive(archive, stage)  # extract + verify
        include_cache = bool(manifest.get("include_cache"))
        incoming = sorted(os.listdir(stage))
        os.makedirs(pre)
        for name in sorted(os.listdir(data_dir)):
            if _excluded_top(name, include_cache):
                continue
            os.rename(os.path.join(data_dir, name), os.path.join(pre, name))
            moved.append(name)
        for name in incoming:
            os.rename(os.path.join(stage, name), os.path.join(data_dir, name))
            installed.append(name)
    except BaseException:
        # Roll back: drop what was installed, put the old data back.
        for name in installed:
            _remove(os.path.join(data_dir, name))
        for name in moved:
            os.rename(os.path.join(pre, name), os.path.join(data_dir, name))
        if os.path.isdir(pre) and not os.listdir(pre):
            os.rmdir(pre)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return pre


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.unlink(path)


# --- automatic backups -----------------------------------------------------


def backup_dir(data_dir=None):
    return os.path.join(data_dir or _data_dir(), BACKUP_DIRNAME)


def list_backups(data_dir=None):
    """Archives in DATA_DIR/backups, oldest first: [(path, size, mtime)]."""
    d = backup_dir(data_dir)
    if not os.path.isdir(d):
        return []
    rows = []
    for name in os.listdir(d):
        if name.endswith(".tar.gz") and name.startswith((AUTO_PREFIX, MANUAL_PREFIX)):
            p = os.path.join(d, name)
            st = os.stat(p)
            rows.append((p, st.st_size, st.st_mtime))
    return sorted(rows, key=lambda r: (r[2], r[0]))


def rotate(keep, data_dir=None):
    """Delete the oldest automatic backups beyond `keep`. Returns the paths."""
    autos = [
        r
        for r in list_backups(data_dir)
        if os.path.basename(r[0]).startswith(AUTO_PREFIX)
    ]
    removed = []
    for path, _, _ in autos[: max(0, len(autos) - max(1, keep))]:
        try:
            os.unlink(path)
            removed.append(path)
        except OSError:
            pass
    return removed


def create_auto(keep, data_dir=None):
    """One automatic backup, then rotation. Blocking: run it in an executor."""
    data_dir = data_dir or _data_dir()
    out = os.path.join(backup_dir(data_dir), f"{AUTO_PREFIX}{_stamp()}.tar.gz")
    path = create_backup(out, data_dir=data_dir)
    rotate(keep, data_dir)
    return path


def seconds_until_due(interval_seconds, data_dir=None, now=None):
    """0 when the newest automatic backup is older than the interval (or none)."""
    import time

    now = time.time() if now is None else now
    autos = [
        r
        for r in list_backups(data_dir)
        if os.path.basename(r[0]).startswith(AUTO_PREFIX)
    ]
    if not autos:
        return 0.0
    return max(0.0, autos[-1][2] + interval_seconds - now)


async def backup_loop(interval_hours=None, keep=None, first_delay=60.0, data_dir=None):
    """Background task: a backup every `interval_hours`, off the event loop.
    Does nothing when the interval is 0."""
    interval_hours = (
        config.BACKUP_INTERVAL_HOURS if interval_hours is None else interval_hours
    )
    keep = config.BACKUP_KEEP if keep is None else keep
    if interval_hours <= 0:
        return
    interval = interval_hours * 3600
    loop = asyncio.get_running_loop()
    await asyncio.sleep(max(first_delay, seconds_until_due(interval, data_dir)))
    while True:
        try:
            path = await loop.run_in_executor(None, create_auto, keep, data_dir)
            print(f"Automatic backup written: {path}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"⚠️  Automatic backup failed: {e}")
        await asyncio.sleep(interval)


# --- command line ----------------------------------------------------------


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m djsnake.backup", description="Back up or restore DATA_DIR."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create", help="write a .tar.gz of the persistent data")
    p.add_argument("--out", help="archive path (default: DATA_DIR/backups/...)")
    p.add_argument(
        "--include-cache", action="store_true", help="also save the lyrics cache"
    )
    p = sub.add_parser("restore", help="restore an archive (the bot must be stopped)")
    p.add_argument("file")
    p = sub.add_parser("verify", help="check an archive's checksums")
    p.add_argument("file")
    sub.add_parser("list", help="list the archives in DATA_DIR/backups")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "create":
            path = create_backup(args.out, args.include_cache)
            print(f"Backup written: {path} ({_human(os.path.getsize(path))})")
        elif args.cmd == "verify":
            m = verify_backup(args.file)
            print(f"OK: {len(m['files'])} files, created {m['created']}")
        elif args.cmd == "list":
            for path, size, mtime in list_backups():
                when = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                print(f"{when}  {_human(size):>9}  {path}")
        elif args.cmd == "restore":
            pre = restore_backup(args.file)
            print(f"Restored. Previous data kept in {pre}")
    except BackupError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
