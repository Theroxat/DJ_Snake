# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/Theroxat/DJ_Snake/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Theroxat/DJ_Snake/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Theroxat/DJ_Snake/releases/tag/v1.0.0
