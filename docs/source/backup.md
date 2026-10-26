# Backup and restore

Everything DJ Snake remembers lives in `DATA_DIR` (`./data` with Compose): themes and logos, languages, volumes, DJ roles, saved playlists and listening stats. Back it up from time to time, and before an upgrade.

A backup is one `.tar.gz` holding a `manifest.json` (format version, date, and the size and SHA-256 of every file) and the files themselves. SQLite databases (`playlists.db`, `stats.db`) are copied with SQLite's own backup API, so you can back up while the bot is running and still get a consistent copy.

The lyrics cache (`lyrics/`) is left out by default, since the bot rebuilds it. The `backups/` folder and earlier `.pre-restore-*` copies are never included.

```{warning}
A backup holds everything in `DATA_DIR`, secrets included (for example the Last.fm session keys of members who linked their account). Archives are written with `0600` permissions; store and share them like a password. Keep `cookies.txt` **out of** `./data`: mount it separately and read-only as described in [Keeping YouTube working](youtube.md), or it ends up in every backup.
```

## Create a backup

```bash
docker compose exec dj-snake python -m djsnake.backup create
```

The archive lands in `DATA_DIR/backups/` (`./data/backups/` on the host) as `djsnake-backup-<date>.tar.gz`. Options:

- `--out FILE` writes it somewhere else (a path inside the container).
- `--include-cache` also saves the lyrics cache.

Because Compose mounts `./data` into the container, the file is already on the host: copy it out with `cp ./data/backups/djsnake-backup-*.tar.gz /safe/place/`. Without the mount, use `docker compose cp dj-snake:/app/data/backups/<file> .`.

Outside Docker, run `python -m djsnake.backup create` from the checkout, with the same `DATA_DIR` as the bot.

## Automatic backups

Set these in `.env`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BACKUP_INTERVAL_HOURS` | `0` | Hours between automatic backups. `0` turns them off. |
| `BACKUP_KEEP` | `7` | How many automatic backups to keep; older ones are deleted. |

Automatic backups are written to `DATA_DIR/backups/` as `djsnake-auto-<date>.tar.gz` by a background task, off the bot's event loop. The first one runs a minute after start if the newest one is older than the interval. Rotation only touches `djsnake-auto-*` files, never your manual backups. They sit on the same disk as the data, so copy them elsewhere too.

## Check and list

```bash
docker compose exec dj-snake python -m djsnake.backup verify /app/data/backups/<file>
docker compose exec dj-snake python -m djsnake.backup list
```

`verify` reads the whole archive and checks every checksum.

## Restore

Restoring replaces the data, so the bot must be stopped: `restore` refuses to run while the bot is up (the bot holds a lock on `DATA_DIR/.bot.lock`), and holds that lock itself until it is done, so a bot started meanwhile cannot take it. It also refuses an archive whose files do not match the sizes in its manifest, and checks that the disk has room for it first.

```bash
docker compose stop dj-snake
docker compose run --rm --no-deps dj-snake python -m djsnake.backup restore /app/data/backups/<file>
docker compose up -d dj-snake
```

To restore a file from elsewhere, copy it into `./data/backups/` first. The restore:

1. verifies the archive and extracts it next to the data (nothing is touched if a checksum fails);
2. moves the current data to `DATA_DIR/.pre-restore-<date>/`, so you can go back;
3. moves the restored files into place, undoing everything if that fails.

The lyrics cache is kept unless the backup was made with `--include-cache`. Delete the `.pre-restore-*` folder once you are happy with the result.
