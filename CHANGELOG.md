# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Clip page: at 320 px wide (and Cobalt at 360 px) the loop button of the transport row was clipped by the player block. The row now tightens its buttons and spacing on narrow phones; no control is hidden.
### Added

- End-to-end tests (`pytest -m e2e`, a new `e2e` CI job): the real FFmpeg playback chain on generated media (automix trim and loudness, crossfade, seek, volume) and the clip page in headless Chromium (sync, live updates, controls, view-only link, themes, lyrics, narrow screens). The contributing docs add a manual checklist for a real Discord server.
### Added

- Queue commands that used to exist only on the clip page: `!remove <position>` and `!move <from> <to>` (positions as `!list` shows them, with a queue autocomplete on the slash versions), `!previous` and `!clear` (empties the upcoming queue, the current track keeps playing). `!move`, `!previous` and `!clear` need the DJ role when one is set; `!remove` also works on a track you queued yourself.
- `!nowplaying` (alias `!np`): an embed with the linked title, requester, source, thumbnail, a progress bar with elapsed and total time, the loop, autoplay and volume state and the clip page link (the view-only one when control is limited).
- `!history [count]`: the last tracks played in the session (default 10, max 25), newest first.
### Added

- Vote to skip: `/voteskip` (`!vs`) lets members in the voice channel vote to skip the current track when they cannot use `/next`. More than `VOTESKIP_PERCENT` (default 50) of the non-deafened humans in the channel must vote; DJs and the requester skip at once, votes are recounted when people join, leave or deafen and reset with each track. One message shows the count and is edited in place, then reads "Skipped by vote". `/voteskip threshold <1-100>` (DJs) sets it per server (`DATA_DIR/voteskip.json`). The `/next` refusal suggests `/voteskip`, the status panel shows the count, and `/api/{sid}/state` has `votes: {count, needed}` shown as a small badge on the page.
### Added

- Queue limits against abuse: `MAX_QUEUE_SIZE` (200), `MAX_TRACKS_PER_USER`, `MAX_TRACK_MINUTES` and `PLAY_COOLDOWN_SECONDS` (all off at 0) are the defaults of every server, and `/limits` (Manage Server to change, saved in `DATA_DIR/limits.json`) overrides them per server. They apply to `/play`, the clip page's add, saved playlist loads and autoplay (queue size only); the page counts as one "web" member. DJs and Manage Server members skip the per-member limit and the cooldown. Playlists queue up to the limit and report what was skipped and why.
### Added

- 24/7 mode: `!stay [on|off]` (DJ to set, saved in `DATA_DIR/stay.json`) keeps the bot in its voice channel: no idle disconnect, `!stop` no longer leaves, and it rejoins the saved channel after a restart without playing anything. Dropped by Discord, it rejoins once, then turns 24/7 off (no reconnect loop). Optional `STAY_247` default, a `stay` field in `/api/{sid}/state`, "24/7" in the status panel and a header badge on the clip page.
### Added

- `/playlist import <name> <link>` creates or replaces a saved playlist from a YouTube playlist, a SoundCloud set, a Bandcamp album or a Spotify, Deezer or Apple Music playlist or album (those are matched on YouTube at import time, with unmatched tracks counted), or from an attached JSON, M3U or TXT file (up to 256 KB, only allowed sources). `/playlist export <name> [json|m3u|txt]` sends a playlist, or `queue` the current queue, as a file. Saved playlists can no longer be named `queue`.
### Added

- Clip page: Media Session support. The lock screen, headsets and media keys show the current track (title, artist from an "Artist - Title" title, server and channel, artwork, position) and, with the control link, drive play, pause, next, previous and seeking (+/- 10 s). A silent audio element started on the first tap makes browsers show the controls; view-only links get metadata only.
### Added

- Installable clip page (PWA): a per-session web app manifest (`GET /{id}/manifest.webmanifest`, named and coloured after the server's theme, with its logo as icon or default DJ Snake icons), a service worker (`/sw.js`) that caches only the page shell and fonts and shows a localised "Session offline" page when the bot is unreachable, iOS home screen tags, and an "Install app" item in the palette menu. The API, live stream, media and lyrics are never cached; view-only links stay view-only. Needs HTTPS (or localhost) to install.
### Added

- Clip page: keyboard shortcuts (play/pause, seek, next/previous, shuffle, loop, mute, volume, fullscreen, lyrics, theme) and a localised help dialog opened with `?` or the header keyboard button. View-only links keep only the non-control keys.
### Added

- Spanish and German translations (`!language es|de`): command replies, errors, `!help`, the status message and the clip page, with informal "tú" / "du", and the page language menu lists Español and Deutsch (`es-*` and `de-*` browsers are detected). Spanish and German are best effort: a key they lack falls back to English, so new strings only need English and French. `scripts/i18n-missing.py` lists what each language is missing, and `/language` suggests the available languages.

## [1.4.0] - 2026-10-23

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
- SoundCloud and Bandcamp links: `!play` and the clip page's add action accept tracks and sets/albums (`soundcloud.com`, `*.bandcamp.com`). Playlists respect `MAX_PLAYLIST_SIZE`; the page shows the artwork for tracks without video (`now_playing.audio_only` in the state), and the "Downloading" list names the platform.
- Deezer and Apple Music links (tracks, albums, playlists), read without credentials (Deezer's public API, Apple's iTunes lookup API and playlist page) and matched on YouTube like Spotify tracks (new `djsnake/catalog.py`).
- Synced lyrics on the clip page: a Lyrics button shows the words over the clip, the sung line highlighted and scrolled in time, or plain lyrics when no timing exists. Lyrics come from [LRCLIB](https://lrclib.net) (free, no key), are looked up in the background when a track is queued and cached in `DATA_DIR/lyrics/`. `LYRICS=0` turns them off.
- Slash commands: every command also works as a slash command (`/play`, `/next`, `/list`, `/seek`, `/pause`, `/resume`, `/stop`, `/reset`, `/crossfade`, `/theme`, `/loop`, `/shuffle`, `/volume`, `/autoplay`, `/playlist`, `/djrole`, `/weblink`, `/language`), with suggestions for the preset and playlist names. The `!` versions are unchanged. Re-invite the bot with the `applications.commands` scope if the commands do not show up. `SLASH_SYNC_GUILD` publishes them to one server at once, for development, and the number of synced commands is logged at startup.
- Live updates: the clip page gets each change pushed by the bot over a Server-Sent Events stream (`GET /api/{sid}/events`) instead of polling every 2 seconds, and falls back to polling when the stream cannot get through. Behind nginx, turn off `proxy_buffering` (see "Exposing the page").
- View-only clip page link: every session also has a second unguessable link that shows the same page with the controls hidden and a "View only" badge; the server refuses controls sent with it (403).
- DJ role: `!djrole <role>|off` (Manage Server, saved in `DATA_DIR/permissions.json`). When set, `!next`, `!pause`, `!resume`, `!seek`, `!crossfade`, `!loop`, `!shuffle`, `!volume`, `!autoplay`, `!stop` and `!reset` need the role or Manage Server (whoever queued the playing track may still skip it), and the status message shows only the view-only link. Changing the role gives a live session a new control link and retires the server's `!theme edit` links.
- `!weblink` DMs the control link and the view-only link (DJs only when control is limited).
- `WEB_CONTROL=everyone|dj` (default `everyone`): with `dj`, the control link is never posted publicly.
- French translation, and a framework for more languages: `!language en|fr` (Manage Server) switches a server's command replies, errors, `!help`, status message and web API error messages; the clip page follows it (or the browser's language) and has a language picker in its settings menu. `BOT_LANGUAGE` sets the default (`en`). Messages live in `djsnake/locales/{en,fr}.json`.

### Changed

- The "link not accepted" message lists every supported source.
- `!play` now rebuilds links like the clip page's add action (same host allowlist, canonical URL); links from other hosts are refused with the "link not accepted" message.
- The status message now shows the view-only link next to the control link. With the defaults, everyone can still control the music as before.

### Security

- Session ids are no longer written to the logs.

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

[Unreleased]: https://github.com/Theroxat/DJ_Snake/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/Theroxat/DJ_Snake/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/Theroxat/DJ_Snake/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/Theroxat/DJ_Snake/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/Theroxat/DJ_Snake/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/Theroxat/DJ_Snake/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Theroxat/DJ_Snake/releases/tag/v1.0.0
