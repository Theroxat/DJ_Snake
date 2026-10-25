import asyncio
import fcntl
import io
import json
import os
import sqlite3
import tarfile

import pytest

from djsnake import backup


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "data"
    (d / "logos").mkdir(parents=True)
    (d / "lyrics").mkdir()
    (d / "themes.json").write_text('{"1": {"skin": "night"}}')
    (d / "logos" / "1.png").write_bytes(b"\x89PNG")
    (d / "lyrics" / "abc.json").write_text("{}")
    return d


def make_db(path, rows=3):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return conn


def names(archive):
    with tarfile.open(archive) as tar:
        return set(tar.getnames())


def test_round_trip_with_live_sqlite(data, tmp_path):
    live = make_db(data / "playlists.db")  # stays open, WAL not checkpointed
    out = tmp_path / "out.tar.gz"
    backup.create_backup(str(out), data_dir=str(data))
    got = names(out)
    assert {"manifest.json", "data/themes.json", "data/logos/1.png"} <= got
    assert "data/playlists.db" in got
    assert not any(n.startswith("data/lyrics") for n in got)
    manifest = backup.verify_backup(str(out))
    assert manifest["version"] == 1 and manifest["files"]

    # Change everything, then restore.
    live.execute("DELETE FROM t")
    live.commit()
    live.close()
    (data / "themes.json").write_text("{}")
    (data / "extra.json").write_text("new")
    pre = backup.restore_backup(str(out), data_dir=str(data))

    assert json.loads((data / "themes.json").read_text()) == {"1": {"skin": "night"}}
    assert not (data / "extra.json").exists()
    assert (data / "lyrics" / "abc.json").exists()  # cache untouched
    conn = sqlite3.connect(data / "playlists.db")
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    conn.close()
    assert os.path.exists(os.path.join(pre, "extra.json"))
    assert os.path.basename(pre).startswith(".pre-restore-")
    assert not [n for n in os.listdir(data) if n.startswith(".restore-tmp-")]


def test_include_cache(data, tmp_path):
    out = tmp_path / "c.tar.gz"
    backup.create_backup(str(out), include_cache=True, data_dir=str(data))
    assert "data/lyrics/abc.json" in names(out)


def test_backups_dir_not_included(data):
    backup.create_backup(data_dir=str(data))
    second = backup.create_backup(data_dir=str(data))
    assert os.path.dirname(second) == str(data / "backups")
    assert not any(n.startswith("data/backups") for n in names(second))


def test_checksum_failure(data, tmp_path):
    out = tmp_path / "ok.tar.gz"
    backup.create_backup(str(out), data_dir=str(data))
    bad = tmp_path / "bad.tar.gz"
    with tarfile.open(out) as src, tarfile.open(bad, "w:gz") as dst:
        for m in src:
            f = src.extractfile(m)
            if m.name == "data/themes.json":
                content = b"tampered!"
                m.size = len(content)
                dst.addfile(m, io.BytesIO(content))
            else:
                dst.addfile(m, f)
    with pytest.raises(backup.BackupError, match="mismatch"):
        backup.verify_backup(str(bad))
    before = (data / "themes.json").read_text()
    with pytest.raises(backup.BackupError):
        backup.restore_backup(str(bad), data_dir=str(data))
    assert (data / "themes.json").read_text() == before
    assert not [n for n in os.listdir(data) if n.startswith(".restore-tmp-")]


def test_not_an_archive(tmp_path):
    f = tmp_path / "x.tar.gz"
    f.write_bytes(b"nope")
    with pytest.raises(backup.BackupError):
        backup.verify_backup(str(f))


def test_restore_refused_while_running(data, tmp_path):
    out = tmp_path / "o.tar.gz"
    backup.create_backup(str(out), data_dir=str(data))
    (data / "themes.json").write_text("{}")
    fd = os.open(data / backup.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert backup.bot_running(str(data))
        with pytest.raises(backup.BackupError, match="running"):
            backup.restore_backup(str(out), data_dir=str(data))
        assert (data / "themes.json").read_text() == "{}"  # untouched
    finally:
        os.close(fd)
    assert not backup.bot_running(str(data))
    backup.restore_backup(str(out), data_dir=str(data))
    assert "night" in (data / "themes.json").read_text()


def test_rotation_keeps_newest_autos(data):
    d = data / "backups"
    d.mkdir()
    for i in range(5):
        p = d / f"djsnake-auto-2026010{i}-000000.tar.gz"
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    manual = d / "djsnake-backup-20260101-000000.tar.gz"
    manual.write_bytes(b"x")
    os.utime(manual, (1, 1))
    removed = backup.rotate(2, data_dir=str(data))
    assert len(removed) == 3
    assert sorted(os.listdir(d)) == [
        "djsnake-auto-20260103-000000.tar.gz",
        "djsnake-auto-20260104-000000.tar.gz",
        "djsnake-backup-20260101-000000.tar.gz",
    ]


def test_create_auto_rotates(data, monkeypatch):
    counter = iter(range(10))
    monkeypatch.setattr(backup, "_stamp", lambda: f"s{next(counter)}")
    for _ in range(4):
        backup.create_auto(2, data_dir=str(data))
    assert len(os.listdir(data / "backups")) == 2


def test_backup_loop_off_returns(data):
    asyncio.run(backup.backup_loop(interval_hours=0, keep=3, data_dir=str(data)))
    assert not (data / "backups").exists()


def test_backup_loop_runs(data, monkeypatch):
    calls = []
    monkeypatch.setattr(backup, "create_auto", lambda keep, d: calls.append(keep))

    async def run():
        task = asyncio.ensure_future(
            backup.backup_loop(
                interval_hours=0.00001, keep=4, first_delay=0, data_dir=str(data)
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(run())
    assert calls and calls[0] == 4


def test_cli(data, tmp_path, monkeypatch):
    monkeypatch.setattr(backup.config, "DATA_DIR", str(data))
    out = tmp_path / "cli.tar.gz"
    assert backup.main(["create", "--out", str(out)]) == 0
    assert backup.main(["verify", str(out)]) == 0
    assert backup.main(["restore", str(tmp_path / "missing.tar.gz")]) == 1
    assert backup.main(["list"]) == 0
