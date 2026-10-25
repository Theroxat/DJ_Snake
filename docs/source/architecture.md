# Architecture

This page is a map of the code for contributors. The bot is a `discord.py` application that downloads audio and video from YouTube with yt-dlp and plays it in voice channels, with an independent queue per server. It also runs an `aiohttp` web server for the [clip page](clip-page.md), on the bot's own event loop.

## Module map

Lowest layer first; each module imports only the ones above it.

| Module | Role |
| --- | --- |
| `config.py` | Environment variables and constants, read once at import. |
| `state.py` | Shared mutable state: the `bot`, `music_players`, `sessions`, `watch_ids`, `guild_perms`, `guild_themes`, `guild_languages`, `guild_volumes`, `edit_tokens`, `viewers`, `streams`, `counters`, `background_tasks` and `clock()`. Always used as `state.music_players`, never imported by name, so every module and test sees the same objects. |
| `i18n.py` | Translations: `t(key, lang, **params)` over the catalogs in `djsnake/locales/` (`en.json`, `fr.json`), and each server's language (`!language`, `languages.json`). |
| `stats.py` | Listening stats in SQLite, written by one background thread. |
| `playlists.py` | Saved playlists in SQLite (`playlists.db`). |
| `backup.py` | Backup and restore of `DATA_DIR` (`python -m djsnake.backup`), plus the automatic backup task. |
| `lyrics.py` | Lyrics from LRCLIB: LRC and YouTube title parsing, the lookup run in the background when a track is queued, and its memory and disk cache. |
| `player.py` | `MusicPlayer` (per-server state), cleanup of files and history helpers. |
| `themes.py` | Theme storage, logos and `!theme edit` tokens. |
| `volume.py` | Per-server volume storage and the percent to gain mapping. |
| `permissions.py` | The DJ role (`permissions.json`), `WEB_CONTROL`, and the list of Discord commands reserved for DJs. |
| `sessions.py` | Session expiry, control and view-only link ids, and the status panel. |
| `media.py` | YouTube URL parsing, yt-dlp downloads, the SponsorBlock cut, audio analysis and FFmpeg options, YouTube search. |
| `spotify.py` | Spotify link parsing, track lists, and matching to YouTube. |
| `catalog.py` | Deezer and Apple Music link parsing and track lists (no credentials); entry point for every metadata link. |
| `mixer.py` | `CrossfadeMixer`, the audio source wrapping FFmpeg. |
| `playback.py` | Voice connection, download-and-queue, playlists, and the chain that advances from track to track. |
| `controls.py` | The `control_*` functions shared by Discord commands and the web page. |
| `web.py` | The aiohttp routes and the web server. |
| `commands.py` | The Discord commands (hybrid: `!` and `/`) and error handling. |
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

Next, previous, pause, resume, remove, move, add, seek, jump, crossfade, loop, shuffle, volume, autoplay and the saved-playlist actions are shared between Discord and the page. They raise one error type carrying a code, an HTTP status and a message (`control.<code>` in the locale files) in the server's language, shown in Discord and on the page. Added URLs (the page's add box and `!play` alike, through `media.parse_play_url()`) must be YouTube, SoundCloud or Bandcamp hosts (rebuilt into a canonical URL) or Spotify, Deezer or Apple Music links, which avoids server-side request forgery through yt-dlp; other hosts get the bad-link error. Anything that does not look like a link (`media.looks_like_url()`) is a text search instead: `spotify.find_text_match()` scores the YouTube results like a Spotify track with no artist.

The page uses these routes:

| Route | Purpose |
| --- | --- |
| `GET /{sid}` | Serves the page. |
| `GET /api/{sid}/state` | The full player state: now playing, queue, downloads, viewers, theme. `404 {"active": false}` for an unknown or expired session. |
| `GET /api/{sid}/events?viewer=<tab id>` | Live updates (Server-Sent Events): the same JSON as `/state` on connect and after each change, coalesced to 10 per second, plus an `event: ping` after 10 seconds of silence. Accepts the control or the view-only id like `/state`; sends `{"active": false}` and closes when the session ends or a control id is rotated (a view-only stream follows the rotation). 429 beyond 100 streams per session, 10 per client or 500 in total. The page falls back to polling `/state` when the stream fails or goes silent. |
| `GET /api/{sid}/stats?range=week\|month\|all` | Listening stats, cached for a few seconds. |
| `GET /api/{sid}/lyrics/{id}` | Lyrics of a playing, queued or history track: `{synced: [[ms, line]...] \| null, plain}`, `202 {"pending": true}` while the lookup runs, 404 when none are found or `LYRICS=0`, 503 when LRCLIB is unreachable (the page retries later). |
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

Everything persistent lives in `DATA_DIR`: `themes.json` (per-server themes, written atomically), `languages.json` (per-server languages), `volume.json` (per-server volume), `permissions.json` (DJ roles), `playlists.db` (saved playlists), `logos/`, `stats.db` (SQLite, WAL) and `lyrics/` (lyrics found on LRCLIB, one JSON file per YouTube video id). `backup.py` archives all of it; see [Backup and restore](backup.md).

## Demo site

`docs/index.html` and `docs/demo/` are a GitHub Pages site with a simulated Discord client and the real clip page running on a fake session. The demo player is generated from `web/index.html` by `docs/demo/build.py`.

## Conventions

- User-facing messages, code comments and docs are in English.
- Black formatting with its default 88-column limit.
- Tests patch `media.download_media`, `media.get_playlist_info`, `state.clock` or `sessions.PANEL_MIN_INTERVAL` on their module, because callers look them up there.

The contributor-oriented source of truth for finer details is [CLAUDE.md](https://github.com/Theroxat/DJ_Snake/blob/main/CLAUDE.md) in the repository.
