# Architecture

This page is a map of the code for contributors. The bot is a `discord.py` application that downloads audio and video from YouTube with yt-dlp and plays it in voice channels, with an independent queue per server. It also runs an `aiohttp` web server for the [clip page](clip-page.md), on the bot's own event loop.

## Module map

Lowest layer first; each module imports only the ones above it.

| Module | Role |
| --- | --- |
| `config.py` | Environment variables and constants, read once at import. |
| `state.py` | Shared mutable state: the `bot`, `music_players`, `sessions`, `guild_themes`, `edit_tokens`, `viewers`, `background_tasks` and `clock()`. Always used as `state.music_players`, never imported by name, so every module and test sees the same objects. |
| `stats.py` | Listening stats in SQLite, written by one background thread. |
| `player.py` | `MusicPlayer` (per-server state), cleanup of files and history helpers. |
| `sessions.py` | Session expiry and the status panel. |
| `themes.py` | Theme storage, logos and `!theme edit` tokens. |
| `volume.py` | Per-server volume storage and the percent to gain mapping. |
| `media.py` | YouTube URL parsing, yt-dlp downloads, the SponsorBlock cut, audio analysis and FFmpeg options, YouTube search. |
| `spotify.py` | Spotify link parsing, track lists, and matching to YouTube. |
| `mixer.py` | `CrossfadeMixer`, the audio source wrapping FFmpeg. |
| `playback.py` | Voice connection, download-and-queue, playlists, and the chain that advances from track to track. |
| `controls.py` | The `control_*` functions shared by Discord commands and the web page. |
| `web.py` | The aiohttp routes and the web server. |
| `commands.py` | The Discord commands and error handling. |
| `app.py` | `on_ready`, voice-state events, `setup_hook` and `main()`. `bot.py` calls `main()`. |

The web page itself is a single file, `web/index.html`, driven by the HTTP API below.

## Key design points

### Download before play

Tracks are fully downloaded to `TEMP_DIR` as one video+audio mp4 before they enter the queue. Files are removed only by per-track cleanup and `!stop`, and never while a player still references them. yt-dlp is blocking, so downloads always run in an executor.

### Automix

Always on, no settings. After the download, SponsorBlock `music_offtopic` segments are cut out (best effort). One audio-only FFmpeg pass then measures loudness and detects leading and trailing silence. Playback uses the result: seeks keep playback inside the music, loudness is normalised (I=-14, TP=-1.5), and tracks fade in and out. All times (elapsed, seeks, the page's progress bar) are in file time, which keeps the muted web video in sync.

### Playback chain

Controls never choose the next track themselves. They set a pending action under the player's lock and stop the voice client; the `after` callback then schedules the next step on the event loop, and stale callbacks (an older generation, or a player replaced by `!reset`) are ignored. This keeps skips, previous, restarts and natural track ends going through one code path.

### Controls and the web API

Next, previous, pause, resume, remove, move, add, seek, jump, crossfade and volume are shared between Discord and the page. They raise one error type carrying an HTTP status and an English message, shown in Discord and on the page. Added URLs must be YouTube hosts (rebuilt into a canonical URL) or Spotify links, which avoids server-side request forgery through yt-dlp.

The page uses these routes:

| Route | Purpose |
| --- | --- |
| `GET /{sid}` | Serves the page. |
| `GET /api/{sid}/state` | The full player state: now playing, queue, downloads, viewers, theme. `404 {"active": false}` for an unknown or expired session. |
| `GET /api/{sid}/stats?range=week\|month\|all` | Listening stats, cached for a few seconds. |
| `POST /api/{sid}/control` | Runs a control action. |
| `GET /media/{sid}/{id}` | Streams a track's mp4 with Range support. Files are found by track id, never by a path from the request. |
| `POST /api/{sid}/theme`, `POST\|DELETE /api/{sid}/theme/logo`, `GET /theme-logo/{sid}` | Theme editor routes, protected by an edit token. |

### Sessions and the status panel

A session (a random URL-safe id) belongs to a server and survives leaving voice. Each session has one status message in its text channel, edited in place and rate limited to one edit every 1.5 seconds.

### Gateway reconnects are routine

discord.py drops and resumes the gateway roughly every couple of hours. Do no cleanup in `on_disconnect`: an earlier version deleted `TEMP_DIR` there and broke long playlists.

### Voice

`safe_voice_connect()` cleans up any stale voice client, adds delays, and retries. It exists to avoid Discord close code 4006 (invalid session). Voice needs `PyNaCl`, `davey` (DAVE encryption) and, on Python 3.13+, `audioop-lts`.

### Persistent data

Everything persistent lives in `DATA_DIR`: `themes.json` (per-server themes, written atomically), `volume.json` (per-server volume), `logos/` and `stats.db` (SQLite, WAL).

## Demo site

`docs/index.html` and `docs/demo/` are a GitHub Pages site with a simulated Discord client and the real clip page running on a fake session. The demo player is generated from `web/index.html` by `docs/demo/build.py`.

## Conventions

- User-facing messages, code comments and docs are in English.
- Black formatting with its default 88-column limit.
- Tests patch `media.download_media`, `media.get_playlist_info`, `state.clock` or `sessions.PANEL_MIN_INTERVAL` on their module, because callers look them up there.

The contributor-oriented source of truth for finer details is [CLAUDE.md](https://github.com/Theroxat/DJ_Snake/blob/main/CLAUDE.md) in the repository.
