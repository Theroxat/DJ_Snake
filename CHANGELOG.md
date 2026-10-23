# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `GET /healthz`: 200 with status, Discord latency, guild and player counts, uptime and version when the bot is connected, 503 with the reason otherwise.
- Docker `HEALTHCHECK` on `/healthz` (60 s start period), so `docker compose ps` shows whether the bot is healthy.
- Optional `GET /metrics` in the Prometheus text format (players, queued tracks, downloads, viewers, Discord latency, tracks played), off unless `METRICS=1`.
- Search by text: `!play daft punk one more time` (any argument that is not a link) and the clip page's add box search YouTube and queue the best match, scored like Spotify tracks. When nothing scores high enough the bot says so.
- Loop and shuffle. `!loop [off|track|queue]` (no argument cycles) replays the current track or sends finished tracks back to the end of the queue; `!shuffle` shuffles the upcoming queue once. The clip page has matching shuffle and loop buttons, the control API gets `loop` and `shuffle` actions, `/api/{sid}/state` exposes `loop`, and the status panel shows the loop mode.
- Per-server volume: `!volume` shows it, `!volume <0-150>` sets it (percent, 100 = the normalised level). It applies to the playing track without a restart, glides over a few frames to avoid clicks, is soft-limited above 100%, works with crossfade and is saved in `DATA_DIR/volume.json`.
- Clip page: a volume button with a slider popover next to Sync, a `volume` control action and a `volume` field in `/api/{sid}/state`. The status panel shows the volume only when it is not 100%.
- Autoplay ("radio"): `!autoplay [on|off]`, a switch on the clip page and the `AUTOPLAY` default. When the queue runs out, a track related to the last one (YouTube's `RD<id>` mix) is prepared during playback and plays next; recent tracks, live streams, tracks over 10 minutes and non-music titles are skipped, and the bot lingers idle when nothing is found. Loop modes take precedence. Autoplay tracks are marked in the status panel and on the page.
- Saved playlists per server: `!playlist save|add|load|show|delete <name>` and `!playlist list`, kept in `playlists.db` under `DATA_DIR` (links and titles only, up to 50 playlists of 200 tracks). The clip page can save the queue and load a playlist (`GET /api/{sid}/playlists`, `save_playlist` and `load_playlist` control actions).
- SoundCloud and Bandcamp links: `!play` and the clip page's add action accept
  tracks and sets/albums (`soundcloud.com`, `*.bandcamp.com`). Playlists respect
  `MAX_PLAYLIST_SIZE`; the page shows the artwork for tracks without video
  (`now_playing.audio_only` in the state), and the "Downloading" list names the
  platform.
- Deezer and Apple Music links (tracks, albums, playlists), read without
  credentials (Deezer's public API, Apple's iTunes lookup API and playlist page)
  and matched on YouTube like Spotify tracks (new `djsnake/catalog.py`).

### Changed

- The "link not accepted" message lists every supported source.
- Synced lyrics on the clip page: a Lyrics button shows the words over the clip, the sung line highlighted and scrolled in time, or plain lyrics when no timing exists. Lyrics come from [LRCLIB](https://lrclib.net) (free, no key), are looked up in the background when a track is queued and cached in `DATA_DIR/lyrics/`. `LYRICS=0` turns them off.

## [1.3.0] - 2026-10-22

### Added

- Theme preset `cobalt`: a flat ultramarine page with blush-pink and butter-yellow accents, wide Unbounded headings, borderless rounded panels and springy controls (`!theme cobalt`, `WEB_THEME`, base for custom).

## [1.2.1] - 2026-10-22

Version 1.2.0 was never published as a Docker image or GitHub Release (its
release build failed); use 1.2.1 instead.

### Fixed

- The arm64 Docker image failed to build on Python 3.15: `davey` has no
  prebuilt aarch64 wheel there, so it is now compiled from source in a separate
  builder stage, keeping the compiler out of the final image.

## [1.2.0] - 2026-10-22

Upgrading: the bgutil PO token plugin and the `pot-provider` image were bumped
together to 2.0.2, so pull the new image too (`docker compose pull && docker
compose up -d`).

### Added

- Listening stats: a chart button in the clip page header opens the server's
  stats for the last week, month or all time: top tracks, artists, most skipped
  track, hours played, tracks, sessions, peak listeners and a day-by-hour
  heatmap in the viewer's timezone. Plays are recorded in `DATA_DIR/stats.db`
  (SQLite) without ever blocking playback. New route
  `GET /api/{sid}/stats?range=week|month|all`.
- Live viewer count ("👁 N") in the clip page header; the state JSON gains
  `viewers`.
- "Downloading" section on the clip page, under "Up next", listing every track
  asked for but not queued yet (links, playlist and Spotify entries). The state
  JSON gains `downloading` and `downloading_total`, and the status panel shows
  the count.
- Discreet "Now playing" row above "Up next" on the clip page.
- Idle linger: when the queue runs out the bot stays in voice for
  `IDLE_DISCONNECT_SECONDS` (default 300) before leaving, so a track added
  meanwhile plays at once. It leaves after 30 s if nobody is in the channel,
  and `!stop` still leaves at once.
- Theme preset `space` (`!theme space`, `WEB_THEME` default, base for custom).
- User documentation on Read the Docs (https://dj-snake.readthedocs.io), built
  with Sphinx, MyST and Furo; CI builds it with warnings as errors.
- The live demo opens on a simulated Discord channel driving the real clip page.

### Changed

- Clip page redesign shared by every theme: video, title, seek bar and controls
  form one player block, with a soft glow from the artwork; the Minimal theme
  uses Geist.
- The web page and errors are now in English. The `nuit`, `vinyle` and `perso`
  themes are now `night`, `vinyl` and `custom`; saved themes are migrated
  automatically.
- Docker image now runs on Python 3.15, CI tests Python 3.10 to 3.15, and the
  yt-dlp floor is 2026.10.21.
- Dependencies bumped: discord.py 2.7.1, python-dotenv 1.2.4, aiohttp 3.14.5,
  PyNaCl 1.6.2, davey 0.1.7, audioop-lts 0.2.2, bgutil-ytdlp-pot-provider and
  `pot-provider` image 2.0.2, dev tools and GitHub Actions.

### Fixed

- The clip page could stay stuck on a stale track: polls now time out and a
  watchdog makes the page always converge to the playing track.
- Stats: listener peak recorded after the write, flush at shutdown, short-track
  skip rule.

### Removed

- "DJ of the week" and top DJs from the stats panel (the requester is still
  recorded but never shown).

## [1.1.0] - 2026-10-17

### Added

- Spotify links: `!play` and the clip page's add action accept Spotify
  playlists, albums and tracks (`open.spotify.com` links and `spotify:` URIs).
  The track list is read from the public embed page (no configuration, 100
  tracks max) or from the Web API when `SPOTIFY_CLIENT_ID` and
  `SPOTIFY_CLIENT_SECRET` are set. Each track is searched on YouTube and the
  best match (duration, title and artist, official video first) is queued;
  tracks without a match are skipped and counted. Nothing is downloaded from
  Spotify.
- GitHub link in the clip page header.

### Changed

- Long track titles are clamped to two lines, and queue titles are more
  readable.
- Refreshed demo and showcase (README, project page, social preview), which
  now highlight Spotify links.

## [1.0.0] - 2026-10-17

### Added

- Discord music bot: `!play` downloads audio and video from YouTube with
  yt-dlp and plays it in a voice channel, with an independent queue per server.
- Playlists: whole playlists load in the background (up to `MAX_PLAYLIST_SIZE`),
  and radio/mix links ask whether to play one track or the full list.
- Playback commands: `!list`, `!next`, `!pause`, `!resume`, `!seek`, `!stop`
  and `!reset`.
- Clip live page: every voice session gets a link to a synced web page that
  shows the muted video of the current track, the queue and a progress bar.
- Web controls: skip, previous, pause, resume, seek, jump to a queued track,
  reorder, remove and add YouTube links from the page.
- Automix: off-topic segments are cut with SponsorBlock, loudness is
  normalised, silence at the start and end is trimmed, and tracks fade in and
  out.
- Optional crossfade between tracks (`!crossfade on|off` or from the page).
- Live status message per session, edited in place instead of spamming the
  channel.
- Selectable clip page themes, with a per-server default and a theme editor.
- Docker Compose deployment with a PO token provider sidecar to get past
  YouTube's "Sign in to confirm you're not a bot" check.
- yt-dlp self-update on every container start, plus a weekly refresh script
  for long-running hosts.
- Retries with backoff for transient download failures.
- Sessions expire after `SESSION_IDLE_HOURS` without activity and clean up
  their files.
- Automated test suite (pytest) run in CI, with the code split into the
  `djsnake/` package.
- Prebuilt multi-architecture (amd64, arm64) Docker images on GitHub Container
  Registry.

### Fixed

- HTTP 403 errors on YouTube downloads.
- Queued tracks being wiped on routine gateway reconnects.
- Python 3.13+ compatibility.

[Unreleased]: https://github.com/Theroxat/DJ_Snake/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/Theroxat/DJ_Snake/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/Theroxat/DJ_Snake/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/Theroxat/DJ_Snake/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Theroxat/DJ_Snake/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Theroxat/DJ_Snake/releases/tag/v1.0.0
