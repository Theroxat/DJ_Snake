# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Discord music bot (discord.py) that downloads audio from YouTube with yt-dlp
and plays it in voice channels, with an independent queue per server. The bot
logic lives in the `djsnake/` package; `bot.py` at the root is only the entry
point. It is deployed with Docker Compose alongside a PO token provider
sidecar.

## Working Style: Orchestrate, Don't Code

The main session is an orchestrator. It does not write code, edit files or do
research itself. It plans, splits the work, delegates every task to subagents
(the Agent tool), reviews what they return, and reports to the user.

- **Pick the model per task, by difficulty.** Use `haiku` for lookups, file
  searches and mechanical edits, `sonnet` for routine implementation and
  reviews, and `opus` only for hard design, tricky debugging or subtle
  concurrency (for example the playback chain or the voice reconnect logic).
- **Run independent tasks in parallel**, in a single message. Run tasks one
  after another only when one needs another's output.
- **Watch token spend.** Keep agent prompts tight and self-contained, give each
  agent only the context it needs, don't spawn agents for trivial steps, and
  don't redo an agent's work to check it unless something looks wrong.

## Commands

```bash
# Production: build and run the bot plus the pot-provider sidecar
docker compose up -d --build
docker compose logs -f dj-snake

# Local run without Docker (needs ffmpeg and deno on PATH)
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 bot.py
```

Outside Compose, `POT_BASE_URL` defaults to `http://pot-provider:4416`, which
will not resolve. Point it at a reachable provider, or expect intermittent
"Sign in to confirm you're not a bot" failures.

CI (`.github/workflows/ci.yml`) runs these checks on Python 3.10 to 3.15, and a
change must pass them:

```bash
pip install -r requirements-dev.txt                                  # pytest, pytest-asyncio, black, flake8
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics   # hard failure
black --check --diff .                                               # hard failure
python -c "import bot"                                               # import smoke test
pytest -q                                                            # test suite
```

The tests (`tests/`, settings in `pytest.ini`) need no network, Discord or
YouTube: `tests/fakes.py` fakes the voice client, channels and
`discord.FFmpegPCMAudio`, and `tests/conftest.py` sets `TEMP_DIR`, `DATA_DIR`,
`WEB_THEME` and `BOT_LANGUAGE=en` before `djsnake` is imported, points
`i18n.LANGUAGES_FILE` at the test's directory and empties the shared state
between tests. The web API runs through aiohttp's `TestClient`. Tests that need
the real `ffmpeg` binary are marked `ffmpeg` and skipped without it. Patch
`media.download_media`, `media.get_playlist_info`, `state.clock` or
`sessions.PANEL_MIN_INTERVAL` on their module: callers look them up there.
`conftest.py` also points `stats.DB_PATH`, the theme files and
`permissions.PERMISSIONS_FILE` at the test's own directory and
calls `stats.flush()` at teardown; call `stats.flush()` before reading the
database in a test.
End-to-end tests live in `tests/e2e/`, marked `e2e`: `pytest.ini`'s `addopts`
leaves them out of `pytest`, `pytest -m e2e` runs only them (CI job `e2e`).
`e2e_kit.py` generates the media with ffmpeg at run time (`chirp.mp4`: 1.5 s
silence, an 18 s sweep whose pitch encodes the file time, 2 s silence;
`tone.mp4`: a quiet steady 440 Hz) and has `PacedVoiceClient`, a voice client
whose thread really reads the mixer's PCM frames (at `speed` times real time)
and keeps them for measurement. `test_e2e_audio.py` drives the real
`play_next()` chain; `test_e2e_web.py` serves `build_web_app()` on a free port
and drives `web/index.html` in Chromium with Playwright (`requirements-dev.txt`,
plus `python -m playwright install chromium`); they skip without ffmpeg or the
browser. They wait on conditions (`wait_until()`, Playwright's `expect`), never
on fixed sleeps. Playback in Discord is still tested by hand: see the checklist
in `docs/source/contributing.md`.

## Release

Production still runs `docker compose up -d --build` (builds from this
checkout). Users pull the prebuilt image instead: `docker compose pull && docker
compose up -d`, with `DJ_SNAKE_TAG` (default `latest`) to pin a version such as
`1.0.0`. To release: move the `[Unreleased]` notes in `CHANGELOG.md` under a new
version, commit, then tag `vX.Y.Z` and push the tag. `.github/workflows/release.yml`
builds and pushes the multi-arch `ghcr.io/theroxat/dj_snake` image and creates
the GitHub Release. The ghcr package must be set to public once, or users cannot
pull it.

After changing `web/index.html` (or `docs/demo/shim.js`), run
`python3 docs/demo/build.py` and commit the regenerated `docs/demo/player.html`.
The demo's entry page, `docs/demo/index.html` (a mock Discord client, with
`discord.js` and the fake bot in `backend.js`), is hand-written and loads
`player.html` in an iframe.

## Configuration

Read from the environment (`.env`, loaded via python-dotenv, and passed to the
container by `env_file`): `DISCORD_TOKEN` (required; a `token` file is the
fallback), `COMMAND_PREFIX` (`!`), `MAX_PLAYLIST_SIZE` (30), `MEDIA_FORMAT`
(max 720p H.264 + m4a, see `DEFAULT_MEDIA_FORMAT`; it replaces `AUDIO_FORMAT`,
which is no longer read), `TEMP_DIR`, `POT_BASE_URL`, `WEB_PORT` (47823),
`WEB_BIND` (host IP the web port is published on, compose only, default
`127.0.0.1`; set a LAN IP or `0.0.0.0` for a reverse proxy or tunnel),
`PUBLIC_BASE_URL` (e.g. `https://dj.example.com`; empty falls back to
`http://localhost:WEB_PORT`), `SESSION_IDLE_HOURS` (6), `IDLE_DISCONNECT_SECONDS`
(300, the idle linger before leaving voice once the queue runs out), `AUTOPLAY`
(0, default of the per-session autoplay), `VOTESKIP_PERCENT` (50, default vote-to-skip threshold, 1-100), `STAY_247` (0, default of the per-guild 24/7 mode, see Stay 24/7), `DATA_DIR` (`/app/data`, mounted from
`./data` by Compose; the container's UID 1000 must be able to write it; it
holds `themes.json`, `logos/`, `languages.json`, `volume.json`,
`permissions.json`, `voteskip.json`, `limits.json`, `stay.json`, `lastfm.json`, `playlists.db`, `stats.db` and `lyrics/`),
`SLASH_SYNC_GUILD` (empty: slash commands sync globally at startup; a server ID
syncs to it only, at once, for development), `WEB_THEME` (`arcade`, the theme of
guilds that never chose one), `WEB_CONTROL` (`everyone`, or `dj`: the control
link is never posted publicly, see Permissions; anything else falls back to
`everyone`), `BOT_LANGUAGE` (`en`, the language of guilds that never ran
`!language`; `en`, `fr`, `es` or `de`), `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`
(optional, both needed to use the Spotify Web API), `METRICS` (`0`; `1` enables
`GET /metrics`), `APP_VERSION` (set by the Dockerfile build arg, `dev`
otherwise; shown by `/healthz`), `LYRICS` (`1`; `0`, `false`, `no` or `off`
turns the LRCLIB lyrics off), `MAX_QUEUE_SIZE` (200), `MAX_TRACKS_PER_USER` (0),
`MAX_TRACK_MINUTES` (0) and `PLAY_COOLDOWN_SECONDS` (0) (defaults of the per-guild
queue limits, see Queue limits; 0 = unlimited/off), `YTDLP_COOKIES` (optional Netscape cookies.txt path; `cookies.py` validates it and copies it to a 0600 temp file at startup, `cookies.options()` is merged into every yt-dlp options dict in `media.py`; `media.classify_error()` and `playback.download_error_text()` map access errors to `play.error_*` messages),
`BACKUP_INTERVAL_HOURS` (0, automatic backups off) and `BACKUP_KEEP` (7), see
Backup, `LASTFM_API_KEY` / `LASTFM_API_SECRET` (optional, both
needed to turn Last.fm scrobbling on, see Last.fm), and `YTDLP_AUTO_UPDATE` (entrypoint only).

## Architecture (`djsnake/`)

Module map, lowest layer first (each imports only the ones above it):

- `config.py`: environment variables and constants, read once at import
  (`WEB_INDEX` resolves `web/index.html` from the repository root).
- `fileio.py`: `write_atomic()` (temp file + `os.replace()`), shared by every
  module that persists JSON under `DATA_DIR`. Imports nothing from the package.
- `state.py`: shared mutable state: the `bot` instance, `music_players`,
  `sessions`, `watch_ids`, `guild_perms`, `guild_themes`, `guild_languages`,
  `guild_volumes`, `guild_stay`, `stay_retries`, `edit_tokens`, `viewers`, `streams` (open live update
  streams per sid), `counters`, `background_tasks` and `clock()`.
  Always use it as `state.music_players` (never `from .state import ...`), so
  every module and test sees the same objects.
- `i18n.py`: translations (see Translations): `t()`, `lang_for()`,
  `ctx_lang()`, `set_language()`, the catalogs in `djsnake/locales/`.
- `stats.py`: listening stats in SQLite (`record_play()`,
  `record_session()`, `record_listeners()`, `summary()`, `guess_artist()`),
  written by one background thread. Imports only `config`.
- `cookies.py`: optional YouTube cookies (`YTDLP_COOKIES`): `validate()`,
  `setup()` (copies the file to a private 0600 temp file, deleted on failure,
  on re-setup and at exit by `cleanup()`), `options()` merged into every yt-dlp
  call, `enabled()`. Never logs the contents. Imports only `config`.
- `playlists.py`: saved playlists in SQLite (`{DATA_DIR}/playlists.db`, see
  Saved playlists). Imports only `config`.
- `playlist_io.py`: playlist export (`export_bytes()`: json, m3u, txt) and
  file import (`parse_import()`, 256 KB, strict schema `djsnake.playlist` v1,
  every URL through `media.parse_media_url()`, deduped, capped). Imports
  `media` and `playlists`.
- `lyrics.py`: LRCLIB lyrics (`parse_lrc()`, `parse_title()`,
  `lookup_fields()`, `fetch()`, `prefetch()`, `get()`). Imports `config`,
  `state`, `stats.clean_artist` and `spotify`'s HTTP helpers.
- `lastfm.py`: optional Last.fm scrobbling (see Last.fm). Imports `config`,
  `state`, `lyrics.lookup_fields` and `fileio`.
- `player.py`: `MusicPlayer`, `notify_state()`, `discard_tracks()`,
  `file_in_use()`, `push_history()`, `retire_play()`, `guild_of()`.
- `themes.py`: theme storage, logos and `!theme edit` tokens.
- `volume.py`: per-server volume storage (`{DATA_DIR}/volume.json`) and
  `percent_to_gain()`.
- `limits.py`: the queue limits (see Queue limits). Imports `permissions`
  and `fileio`.
- `stay.py`: 24/7 mode storage (`{DATA_DIR}/stay.json`: `load_stay()`, `stay_on()`, `stay_channel()`, `set_stay()`, `remember_channel()`).
- `backup.py`: backup and restore of `DATA_DIR` (see Backup). Imports only `config`.
- `permissions.py`: the DJ role (`load_perms()`, `set_dj_role()`,
  `dj_role()`), `is_dj()`, `web_control_restricted()` and the Discord gate
  (`DJ_COMMANDS`, `REQUESTER_COMMANDS`, `can_run_dj_command()`).
- `voteskip.py`: vote to skip (see Vote to skip): thresholds in
  `{DATA_DIR}/voteskip.json`, `status()`, `votes_needed()`, `web_status()`.
- `sessions.py`: session expiry (`touch()`, `session_player()`,
  `session_cleanup_loop()`, `open_session()`), link ids (`resolve_link()`,
  `rotate_control_link()`) and the status panel (`refresh_panel()`,
  `set_action()`, `ack()`, `short()`).
- `media.py`: URL parsing (`parse_media_url()`, `parse_play_url()`,
  `looks_like_url()`, `source_of()`), yt-dlp (`download_media()`,
  `get_playlist_info()`), the SponsorBlock cut, `analyse_media()`,
  `playback_options()`, `crossfade_eligible()`, `search_youtube()` (flat
  `ytsearchN:`) and the autoplay helpers (`related_tracks()`,
  `choose_related()`).
- `spotify.py`: Spotify links: `check_url()`, the embed parser and the
  optional Web API (`get_tracks()`), the shared no-redirect HTTP helpers
  (`_http()`, `_opener`, `_read_capped()`), and YouTube matching
  (`score_candidate()`, `best_match()`, `find_youtube_match()`,
  `find_text_match()`).
- `catalog.py`: Deezer and Apple Music links: `check_link()` (every metadata
  link: Spotify, Deezer, Apple Music), `get_tracks()`, parsers.
- `mixer.py`: `CrossfadeMixer`.
- `playback.py`: voice connection, `download_and_queue_song()`,
  `queue_playlist()`, `queue_spotify()`, `queue_search()`, `play_next()` /
  `_advance()` / `_start_crossfade()`, `open_track_source()`.
- `controls.py`: `ControlError`, the `control_*` functions, `WebContext`.
- `web.py`: the aiohttp routes and `start_web_server()`.
- `commands.py`: the Discord commands (all hybrid, see "Adding a command"),
  `before_invoke`, `on_command_error`, `on_app_command_error`.
- `app.py`: `on_ready`, `on_voice_state_update`, `setup_hook` (starts the web
  server and `sync_commands()`), `main()`; importing it registers the
  commands. `bot.py` calls `main()`.

- **Per-guild state**: `music_players` maps guild ID to a `MusicPlayer` holding
  the voice client, the queue of `{id, filepath, title, duration, thumbnail,
  url}` items, `history` (last 5 played tracks), the flags `is_playing`,
  `should_stop` and `loading` (`loading_jobs > 0`), `epoch` (bumped by `!stop`
  / `!reset` so older download jobs give up), the web session `sid`, and
  `started_at` / `paused_at` / `paused_total` for `elapsed()`.
- **Download before play**: tracks are fully downloaded to `TEMP_DIR` as one
  video+audio mp4 (`merge_output_format`, `+faststart`) before they enter the
  queue. Files are removed by `discard_tracks()`, which skips any path still
  referenced by a player (the same video shares one path). yt-dlp is
  blocking, so `download_media()` and `get_playlist_info()` always run through
  `loop.run_in_executor`.
- **Automix** (always on, no settings): after the download,
  `_remove_offtopic()` cuts SponsorBlock `music_offtopic` segments out of the
  file (stream copy, best effort: no segments or API down keeps the file).
  `analyse_media()` then runs one audio-only ffmpeg pass (~5 s per track):
  `loudnorm` first pass plus `silencedetect`, stored on the track as
  `loudnorm`, `start` (end of leading silence, max 20 s) and `end` (start of
  trailing silence). `playback_options()` builds the FFmpeg args: input
  `-ss`/`-t` keep playback inside `start`..`end` (file time), and `-af` applies
  the `loudnorm` second pass (linear, I=-14, TP=-1.5), a fade-in and a 4 s
  fade-out timed from the seek point. `elapsed`, seeks and the state's
  `start`/`end` are all in file time, so the muted web video stays in sync; the
  page's progress bar spans `start`..`end`. Tracks without `end` play untouched.
- **Playback chain**: `play_next()` takes `player.lock` and `_advance()` retires
  the current track per `player.pending_action` (None/"next": to history, pop
  the queue; "previous": current back to the queue front, play the last history
  track; "restart": replay it), then starts `FFmpegPCMAudio` (`-vn`). The
  `after` callback runs on the audio thread and only schedules
  `play_next(ctx, gen, player)` on the loop; a callback whose generation
  (`play_gen`) or player (replaced by `!reset`) is stale is ignored, and a call
  with no generation only starts an idle player. Controls never pick the next
  track themselves: they set `pending_action` under the lock and call
  `voice_client.stop()`. The voice client always plays a `CrossfadeMixer`
  wrapping the FFmpeg source (a pass-through unless a crossfade runs).
- **Idle linger**: when `_advance()` finds the queue empty and nothing is
  loading, it does not leave voice: `schedule_idle_disconnect()` starts the
  player's single timer (`player.idle_task`, also in `state.background_tasks`;
  a new one cancels the old) for `IDLE_DISCONNECT_SECONDS`, or
  `EMPTY_CHANNEL_GRACE_SECONDS` (30) when no human is in the channel
  (`on_voice_state_update` shortens a running one when the last human
  leaves). `in_voice` stays true, so the page's add works and plays at once.
  When it fires it re-checks under the lock (same player, same `play_gen`,
  connected, queue empty, nothing playing, no stop) and disconnects as the
  queue end used to; a download still running re-arms it. Starting a track,
  `!stop` (`stop_and_clear()`), `!reset`, a kick and session expiry cancel
  it (`player.cancel_idle()`). The panel shows "💤 Queue empty — leaving in
  …". The demo backend lingers 60 s.
- **Stay 24/7** (`stay.py`, `state.guild_stay`: guild id to `{on, channel}`,
  saved in `{DATA_DIR}/stay.json`, atomic, cleaned on load; a guild with no
  record follows `config.STAY_247`): `!stay [on|off]` (a `DJ_WHEN_SETTING`
  command; no argument shows it; `on` outside voice joins the author's channel)
  keeps the bot in voice. `schedule_idle_disconnect()` returns without a timer
  when it is on (so the 30 s empty-channel rule is skipped too), `_idle_disconnect()`
  re-checks it, and `!stop` clears the queue without leaving. The channel id is
  remembered by `join_voice_channel()` and when a mod moves the bot
  (`remember_channel()`). `on_ready` runs `restore_stay()` once: for each guild
  with it on, `rejoin_stay()` joins the remembered channel through
  `safe_voice_connect()` (2 attempts), skipping a deleted channel, a missing
  Connect permission or a client already connected; nothing plays. A drop
  (kick, deleted channel, Discord ending the voice session; `reconnect=False`
  means a network blip is one too) runs `recover_stay()` from
  `on_voice_state_update`: one rejoin, then if it happens again within
  `STAY_RETRY_WINDOW` (120 s), or the channel is gone or off limits, or the
  connect fails, `give_up_stay()` turns it off and shows `action.stay_lost`.
  No reconnect loop. The web session already never expires while the bot is in
  voice (`_session_expired()`), so nothing special is needed: 24/7 keeps it
  alive. After a restart there is no session until the next `!play` (sessions
  are in memory). Autoplay is unchanged (it still needs a human in the
  channel). Panel line "🌙 24/7", state field `stay`, header badge `#stay`.
  Tests: `tests/test_stay.py`.
- **Autoplay** (`player.autoplay`, per session, default `config.AUTOPLAY`; `!autoplay
  [on|off]` or the web `autoplay` action, `control_autoplay()`): when `_advance()`
  finds the queue empty after a track that ended or was skipped and
  `autoplay_wanted()` (on, `player.loop_active()` false, no stop), it continues
  with `_take_autoplay()` instead of the idle linger. The pick is prepared while
  the last track plays (`prefetch_autoplay()`, a task in `player.autoplay_task`,
  result in `player.autoplay_next`, a downloaded track not in the queue and kept by
  `file_in_use()`/`stop_and_clear()`/expiry/`!reset`). `_take_autoplay()` runs
  under the lock and never waits for a search or download: it takes the ready
  pick, else starts a prefetch and the bot lingers idle until the prefetch
  lands and restarts the idle player. `autoplay_wanted()` also needs a human
  in the channel (`channel_empty()`): no autoplay into an empty channel.
  `_find_autoplay()` reads the `RD<video_id>` mix
  flat (`media.related_tracks()`, executor), `media.choose_related()` drops ids in
  `player.recent_ids` (last `AUTOPLAY_HISTORY` 50 started tracks, plus
  playing/queued/history), live streams, entries over `AUTOPLAY_MAX_SECONDS` (600)
  and non-music titles, then downloads up to `AUTOPLAY_TRIES` candidates (a
  download over the cap is discarded). The track gets `autoplay: True` and requester
  `Autoplay`; nothing found means the idle linger. A queued track always wins and
  the prepared pick waits. The panel shows "📻 Autoplay on" and "📻 Autoplay" on
  the current line; the state has `autoplay` and `now_playing.autoplay` (queue
  items carry `autoplay` too). A crossfade only overlaps queued tracks.
  Tests: `tests/test_autoplay.py` (patch `media.related_tracks`, `media.download_media`).
- **Crossfade** (`player.crossfade`, per session, off by default; `!crossfade
  on|off` or the web `crossfade` action): tracks then get a 6 s fade-out. When
  the mixer reaches a track's fade-out it calls `on_tail` on the audio thread,
  which schedules `_start_crossfade()`: under the lock, if the next queued
  track exists, both tracks' automix windows are >= 15 s and nothing is
  paused or pending, it opens the next track (fade-in = remaining overlap),
  reads its first frame in an executor, `attach()`es it, and promotes it
  exactly like a normal advance (history, queue, `play_gen`, `started_at`).
  The old source drains underneath, summed with `audioop.add` (saturating)
  only during the overlap. No `play()`/`stop()` happens, so the `after`
  callback fires once at the end of the chain and reads `mixer.gen`. Any
  control stops the whole mixer, which cancels the overlap. Off, the FFmpeg
  options are exactly the usual ones (1 s in, 4 s out). The state's
  `crossfading` is true while the old track drains; only then does the page
  cross-dissolve its two stacked `<video>`s (skips stay an instant swap).
- **Volume** (`state.guild_volumes`, guild id to percent 0-150, default 100,
  persisted by `volume.py` in `{DATA_DIR}/volume.json`, cleaned entry by entry on
  load, written atomically): `!volume [0-150]` and the web `volume` action both
  call `control_volume()`, which validates (`bad_volume`, 400), stores, saves
  (`volume_not_saved`, 500, if DATA_DIR is not writable; the change still applies)
  and calls `mixer.set_volume()`. The gain stage is the last step of
  `CrossfadeMixer.read()`, after the crossfade sum, so overlaps and the FFmpeg
  fades are unaffected. Gain is linear (`percent / 100`); a change glides from the
  current gain at most `VOLUME_MAX/100/VOLUME_RAMP_FRAMES` per 20 ms frame,
  interpolated in 8 slices inside each frame. `scale()` uses `audioop.mul`, and
  only when a frame's peak times the gain passes 0.9 of full scale does it run a
  tanh soft limiter, so 100% is bit-exact. New mixers start at the saved volume
  (`play_next()`). The web action panel line ("Volume set to N% via the web
  page") goes through `set_action()`, so dragging coalesces to one edit per
  `PANEL_MIN_INTERVAL`; the panel also shows "Volume N%" when not 100.
- **Controls** (`control_next/previous/pause/resume/remove/move/add/seek/jump/crossfade/loop/shuffle/volume/autoplay`,
  plus the `control_playlist_*` family) are shared by Discord commands and the
  web page and raise `ControlError` (a stable `code`, its HTTP status in
  `ControlError.STATUS` and the message `control.<code>`, `e.text(lang)`, shown
  on Discord and on the web page in the guild's language). Previous restarts
  the track after 5 s of play or with empty history. Add accepts only the
  hosts of `media.parse_media_url()` (YouTube, SoundCloud, Bandcamp; see More
  sources) and rebuilds a canonical URL (no SSRF via yt-dlp), or a Spotify,
  Deezer or Apple Music link (see Spotify links; `control_add()` is async), or
  non-link text (see Text search). For YouTube, `v=` means one track,
  otherwise `list=` loads up to `MAX_PLAYLIST_SIZE` in the background with a
  `WebContext` (no author, so it can never join voice). `!play` uses
  `media.parse_play_url()`: the same allowlist and canonical URL, except that
  a YouTube `v=` link also keeps its `list=` id for the playlist prompt; any
  other host gets the `bad_url` error. Web actions are announced in the
  session's text channel.
- **Vote to skip** (`voteskip.py`, `control_voteskip()`, `voteskip_update()`;
  `!voteskip` / `!vs` / `/voteskip`, not in `DJ_COMMANDS`): a member in the
  bot's voice channel adds their id to `player.votes`. Eligible listeners are
  the humans in the channel who are not deafened (self or server); deafened
  members do not count in the total and cannot vote. The count is the votes
  of members who are still eligible listeners, recomputed from the channel
  every time (`status()`), so leaving, joining and deafening need no
  bookkeeping: `app._recount_votes()` just calls `voteskip_update()` on
  voice state updates in the bot's channel. Needed = `votes_needed(total,
  percent)`: strictly more than `percent` of the listeners (`total * percent
  // 100 + 1`), at least 1 and at most `total`. When enough, `control_next()`
  runs. `player.reset_votes()` (from `mark_started(same_track=False)`, so every
  new track including a loop replay and a crossfade promotion, and
  `stop_and_clear()`) clears the votes and `player.vote_message`, the one
  message per track, edited in place and finished with "Skipped by vote".
  `!voteskip` by a DJ or the track's requester (`can_run_dj_command(...,
  "next")`) skips at once. `!voteskip threshold [1-100]` is one hybrid command
  with a `setting` argument (a slash group could not also be invoked bare);
  setting needs `require_dj()`, persisted like volume (`control_voteskip_
  threshold()`: `bad_threshold`, `threshold_not_saved`). Errors:
  `vote_not_in_channel`, `vote_deafened`, `vote_already`. When a DJ role is
  set, the `NotDJ` message for `next` adds `cmd.not_dj_voteskip`. The panel
  shows "Skip votes n/m" and the state has `votes: {count, needed} | null`
  (the page shows a header badge; it cannot vote: no identity, so no
  `CONTROL_ACTIONS` entry). Tests: `tests/test_voteskip.py`.
- **Loop and shuffle** (`player.loop`, per session, `off` by default; `!loop
  [off|track|queue]` or the web `loop` action, kept by `!reset`): `_advance()`
  replays `finished` when the track ended by itself under `track` (a new play
  in the stats: `retire_play()` records the pass, `mark_started(same_track=
  False)`); under `queue` a finished or skipped (`None`/"next") track is
  appended to the queue before the next is popped. A skip under `track`
  moves on. `_start_crossfade()` overlaps a `track` loop (or a one-track
  `queue` loop) with itself and, for `queue`, re-queues the old track.
  `control_shuffle()` shuffles `player.queue` under the lock (the current
  track stays; fewer than 2 queued raises `nothing_to_shuffle`). The panel
  shows "🔁 Loop: mode".
- **Playlists**: `!play` first tries `get_playlist_info()`, a flat extraction
  with no download. If the URL contains both `v=` and a playlist (a radio or
  mix link), `PlaylistChoiceView` asks with buttons whether to play one track
  or the whole list; on timeout it plays the single track. Playlist tracks
  download one at a time while earlier ones play. Loading posts one message
  (`📥 Loading N tracks…`) and edits it at the end (`✅ k/N tracks loaded (m
  unavailable)` or `⏹️ Loading stopped`); items download with `quiet=True`, so
  there is no per-track message, not even for a failure. `player.loading` stops
  `play_next()` from disconnecting when the queue briefly runs empty during
  that loading. `!stop` sets `should_stop` and bumps `epoch`, which every
  download step checks, and deletes queue, current and history files.
- **Saved playlists** (`playlists.py`, `{DATA_DIR}/playlists.db`, WAL, schema
  in `PRAGMA user_version`): tables `playlists` (guild_id, case-folded `key`
  unique per guild, display `name`, `creator_id` NULL when saved from the
  page) and `playlist_tracks` (position, url, title, duration; cascade
  delete). Only links and titles are stored; loading re-downloads through
  `queue_playlist()`, links re-checked with `parse_youtube_url()`. Unlike
  stats, a failed write must reach the user, so there is no writer thread:
  each blocking function opens its own connection and runs through
  `playlists.run()` in an executor (`BEGIN IMMEDIATE` for writes); storage
  errors become `ControlError` `playlist_unavailable`. Caps in `config.py`:
  `MAX_SAVED_PLAYLISTS` 50 per guild, `MAX_SAVED_TRACKS` 200,
  `SAVED_PLAYLIST_NAME_MAX` 32; names match `^\w[\w .'&!+()-]*$`. Controls:
  `control_playlist_save/add/get/list/delete/load` (`PlaylistError` codes are
  `ControlError` codes). Replacing or deleting needs the creator or Manage
  Server (`!playlist` passes `is_admin`); the page has no identity, so it can
  only create new names and load. Commands: `!playlist save|add|load|show|
  delete <name>`, `!playlist list`. Web: `GET /api/{sid}/playlists` ->
  `{playlists: [{name, tracks, duration}]}`, control actions `save_playlist`
  and `load_playlist` (`name`; load answers 202). Export/import: `/playlist export
  <name> [json|m3u|txt]` (`queue` is reserved and exports the live queue;
  `playlists.clean_name` refuses it) and `/playlist import <name> <link>|<file>`
  (`control_playlist_export/import_file/import_url`). Link imports of a
  YouTube/SoundCloud/Bandcamp list use `media.get_playlist_info(url, limit)`;
  Spotify/Deezer/Apple tracks are matched on YouTube at import time (progress
  message, unmatched counted), so stored links stay ordinary and loading needs
  no matching. Permissions are checked first (`playlists.check_writable`) and
  again by `save`. Tests: `tests/test_playlists.py`, `tests/test_playlist_io.py`.
- **Queue limits** (`limits.py`, `{DATA_DIR}/limits.json`, `{guild id: {field:
  int}}` of overrides only, cleaned on load, written atomically; `state.guild_limits`):
  fields `queue_size` (>= 1, whole queue), `per_user` (0 = unlimited),
  `track_minutes` (0 = unlimited) and `cooldown` (seconds, 0 = off), defaults from
  `config.MAX_QUEUE_SIZE` etc. read at call time by `limits_for()`. `/limits
  [queue|user|minutes|cooldown] [number|default]` shows an embed (anyone) or sets
  (Manage Server, checked in the command because the argument decides). Every
  entry point goes through `playback.admission()` (`limits.check_enqueue()`): in
  `_download_and_queue()` before the download (with the metadata `duration=` hint
  when the caller has one: flat playlist entries and Spotify/Deezer/Apple tracks)
  and again after it with the file's real duration (the file is discarded), and
  in `queue_playlist()` before each entry so a refused one is neither searched nor
  downloaded. Refusals say `limits.<reason>` to the channel (`refuse()`), or are
  counted in the playlist's `skips` and reported once in its final line
  (`load.skipped.<reason>`). Per member counts queued tracks by `requester_id`; a
  track without one (the page's `WebContext`) is the `"web"` bucket, which
  has the per-member limit but never a bypass or cooldown. `control_add()` refuses
  an add up front with `queue_full` / `user_full` (409). `bypasses()` is
  `is_dj(strict=True)`: DJ role or Manage Server, never everyone, and only for
  the per-member limit and the cooldown. The cooldown (`state.play_times`, memory
  only, `state.clock()`) is checked in `/play` after the request validates.
  Autoplay only checks the queue size. The bot never plays live streams (it
  downloads whole files), so there is no live exemption; an unknown duration
  passes. Tests: `tests/test_limits.py`.
- **Spotify links** (`spotify.py`): `check_url()` accepts only
  `open.spotify.com` (`play.spotify.com`) paths `[/intl-xx][/embed]/{playlist|album|track}/{id}`
  and `spotify:{kind}:{id}` URIs, with a 22-char base62 id; anything else on
  those hosts raises `bad_url`, and `spotify.link` short links are refused
  (`spotify_short_link`: following them would mean an arbitrary redirect).
  Requests are rebuilt from the id, sent by `_http()` (urllib, no redirects
  followed, `SPOTIFY_TIMEOUT`, `SPOTIFY_MAX_BYTES`) and run in an executor.
  `get_tracks()` returns `[{title, artists, duration}]` up to
  `MAX_PLAYLIST_SIZE`: from the Web API (client credentials, paginated, token
  only sent to `api.spotify.com`) when both credentials are set, else or on
  failure from the embed page `open.spotify.com/embed/{kind}/{id}`, whose
  `__NEXT_DATA__` JSON holds `props.pageProps.state.data.entity` (`trackList`
  items: `title`, `subtitle` = artists joined by ",\u00a0", `duration` in ms;
  a track has `artists`). The embed lists at most 100 tracks and cannot be
  paginated; `pageProps.status == 404` means private or missing. Errors are
  `SpotifyError(code)` with a `ControlError` code (`spotify_not_found` 404,
  `spotify_empty` 400, `spotify_unreadable` 502: the page changed or Spotify is
  unreachable). Matching: `search_youtube("{artist} - {title}")` (flat, 5
  results), then `score_candidate()`: title words (>= half required),
  artist in title or channel, duration (rejected beyond 2x or under half;
  official videos up to 90 s longer count as close), official video > official
  audio / Topic / artist channel, penalties for live, cover, remix, karaoke,
  lyrics... unless the Spotify title has the word; `MIN_SCORE` 40. The web add
  reads the list before answering (so its errors are HTTP errors), then loads
  in the background. A list goes through `queue_playlist()` with `{"spotify":
  track}` entries (searched one at a time; unmatched ones counted: `✅ k/N
  tracks loaded (m not found on YouTube)`), a single track through
  `queue_spotify()`. Queued items are ordinary YouTube tracks. Tests:
  `tests/test_spotify.py`, fixtures in `tests/fixtures/spotify/`; patch
  `spotify.get_tracks`, `spotify._http` or `media.search_youtube`.
- **Text search**: `media.looks_like_url()` (scheme, `www.` or a domain in the
  first word) separates links from text. `!play` takes `url` (first word) plus
  `rest`; text (<= `MAX_SEARCH_LENGTH` 200) goes to `queue_search()`, and the
  web `control_add()` does the same when `parse_media_url()` fails on
  non-link text (otherwise `bad_url`, unchanged). `spotify.find_text_match()`
  scores with `text_track()` (no artists: query words may be in title or
  channel, flat +20 instead of the artist signal, no duration check) against
  `MIN_SCORE`; no match posts "No YouTube match found for **text**".
- **More sources**: `media.parse_media_url()` (web add) accepts YouTube plus
  `soundcloud.com` (`/user/track[/token]` single, `/user/sets/name` playlist)
  and `*.bandcamp.com` (`/track/x`, `/album/x`), rebuilt canonically; short
  links and custom Bandcamp domains are refused. `!play` hands any other URL to
  yt-dlp as before. `media.source_of(url)` names the platform (the pending
  entry's `source`), `media.entry_url()` keeps non-YouTube flat-playlist URLs
  (ids are not YouTube ids). Non-YouTube downloads skip SponsorBlock and may be
  audio only: the track gets `audio_only` (no video stream, from yt-dlp's
  `vcodec`/the extension), exposed as `now_playing.audio_only`; the page then
  shows the artwork `<img id="artwork">` over the empty video. Deezer and Apple
  Music (`catalog.py`): `check_link(url)` returns `(kind, id, provider)`
  (Apple's id is a `(storefront, slug, id, song_id)` tuple; an album link with
  `?i=` is a track; short links raise `short_link`). Deezer reads
  `api.deezer.com` (`track/ID`, `album|playlist/ID/tracks?limit=`); Apple songs
  and albums use the iTunes Lookup API, playlists the `serialized-server-data`
  block of the playlist page (schema.org JSON-LD as fallback, no artists). All
  requests go through `spotify._http` and produce the same `{title, artists,
  duration}` dicts (plus `source`) as Spotify, matched by
  `spotify.find_youtube_match` and queued through the same `queue_playlist()` /
  `queue_spotify()` (entries stay `{"spotify": track}`; `pending_label()` reads
  `track["source"]`). Errors are `SpotifyError` with the generic codes
  `link_not_found`, `link_empty`, `link_unreadable` (also for any network
  failure or bad payload), `short_link`. Tests:
  `tests/test_sources.py`, fixtures in `tests/fixtures/catalog/`; patch
  `catalog.get_tracks` or `spotify._http`.
- **Pending downloads** (`player.pending_downloads`, the page's "Downloading"): an ordered list of `{key, title, source}` (`source`
  `youtube`, `spotify`, `deezer`, `apple`, `soundcloud` or `bandcamp`; `title` None until known, the page then shows "Reading
  link…") for every track asked for but not queued yet. `add_pending()`
  returns a key that the caller drops in a `finally` (`drop_pending()`).
  `download_and_queue_song()` lists a link itself unless given `pending_key`,
  and names it from yt-dlp's progress hook (`download_media(url, on_title=)`;
  fakes of `download_media` must accept `**kwargs`). `queue_playlist()` lists
  every entry up front (flat title, or "Artists – Title" for Spotify) and
  drops each as it is queued, fails or is not found; `queue_spotify()` lists
  a single track; `!play` and the web add list the link while
  `get_playlist_info()` reads it. `stop_and_clear()` (so `!stop`) and session
  expiry empty the list; `!reset` replaces the player. Adding or dropping an
  entry never calls `refresh_panel()` by itself.
- **Gateway reconnects are routine**: discord.py drops and resumes the gateway
  session roughly every couple of hours. Don't do cleanup in `on_disconnect`.
  An earlier handler deleted `TEMP_DIR` there and broke long playlists in the
  middle of playback. Per-track cleanup in `play_next()` and `!stop` is the only
  place files are removed. `on_voice_state_update` resets the player's voice
  state when the bot is kicked from a channel.
- **Clip live sessions**: a session (`secrets.token_urlsafe(16)`, `sessions`
  maps sid to guild) belongs to the guild and survives leaving voice (queue
  end, `!stop`, kick, `!reset`). It also has a view-only id
  (`player.watch_id`, same format, `state.watch_ids` maps it to the sid). A
  fresh join reuses the unexpired sid, or makes both, and posts the panel
  with its links (see Permissions); that channel becomes
  `player.text_channel` for notices. It expires after `SESSION_IDLE_HOURS`
  without activity (playback or being in voice, a command via
  `bot.before_invoke`, a web control): lazily in `session_player()` plus
  `session_cleanup_loop()` every 10 min. Expiry deletes the player's files
  and drops both ids. Ids are bearer secrets: never print or log them.
- **Permissions** (`permissions.py`, `web.py`): two central gates, each a
  single list to extend.
  - Web: `{sid}` in every route may be the session id (control link) or the
    view-only id. `resolve_link()` maps it to `(sid, can_control)` by
    comparing it with every live id through `secrets.compare_digest` (never a
    dict lookup on the raw id). `web_control()` refuses a view-only id with
    403 before reading the body, for every action: all of `CONTROL_ACTIONS`
    mutate, so a new web action is added to `CONTROL_ACTIONS` (and the
    dispatch) and is gated with no other change. Anything a view-only page
    may do belongs in a GET route. Theme routes also need the control id.
    URLs in the state (`video_url`, `logo_url`) are built from the id the
    request used, so a view-only page never sees the control id.
  - Discord: the global `bot.check` `dj_gate()` calls
    `can_run_dj_command()`: a command named in `DJ_COMMANDS` (next, pause,
    resume, seek, stop, reset, crossfade, loop, shuffle, previous, move, clear) needs `is_dj()`; `volume`, `autoplay` and `stay` need it only to set a value (bare, they show the setting; `DJ_WHEN_SETTING`, `require_dj()`) (Manage Server, or
    the guild's DJ role; no role set = everyone), except that
    `REQUESTER_COMMANDS` (next) is allowed to `track["requester_id"]` (set by
    `download_and_queue_song()`) on the playing track. `remove` is in
    `OWN_TRACK_COMMANDS`: it passes the gate and calls `require_dj_or_owner()`
    once the track at the given position is known (a non-DJ may remove only a
    track whose `requester_id` is theirs). A new mutating command
    is added to `DJ_COMMANDS`. Refusals raise `NotDJ`, answered by
    `on_command_error`. `!play` and `!list` stay open.
  - Links: the DJ role (`!djrole <role>|off`, Manage Server) is stored in
    `{DATA_DIR}/permissions.json` (`{guild id: {dj_role: id}}`, atomic write,
    cleaned on load). `web_control_restricted(guild)` is `WEB_CONTROL ==
    "dj"` or a DJ role set: then the panel shows only the view-only link and
    a hint to `!weblink`, which DMs both links to `is_dj(strict=True)`
    members (without a role: Manage Server only). Unrestricted, the panel
    shows the control link and the view-only link in `<>` (no second
    preview), and `!weblink` DMs anyone. Every `!djrole` change (set, replace
    or `off`) on a live session calls `rotate_control_link()`: a new sid, same
    view-only id, so the control link posted or held before stops working,
    and the guild's `!theme edit` tokens are retired (they point at the old
    sid).
- **Web server**: aiohttp, started in `setup_hook` on the bot's loop, bound to
  `0.0.0.0:WEB_PORT`. Routes, the contract `web/index.html` relies on:
  `GET /` landing text; `GET /{sid}` serves `web/index.html`;
  `GET /api/{sid}/state` returns `{active, in_voice, guild, channel, loading,
  paused, can_previous, can_control, crossfade, loop, volume, autoplay, votes, stay,
  crossfading, server_time, viewers, lang, lyrics, now_playing: {id, title,
  duration, thumbnail, url, autoplay, video_url, audio_only, elapsed, start,
  end} | null, queue: [{id, title, duration, thumbnail, url, autoplay}],
  downloading: [{key, title, source}], downloading_total, theme: {skin, base,
  bg, text, accent, name, logo_url}}`
  (404 `{"active": false}` when the sid is unknown or
  expired; out of voice, `now_playing` is null and `queue` and `downloading`
  empty; `elapsed` freezes while paused; `can_control` is false for a
  view-only id: the page sets `data-viewonly` on `<html>`, shows the "View
  only" header pill and hides the transport buttons, crossfade, add form,
  grips and remove buttons, and its `active()` guard stops every control
  call; `lang` is the guild's language; `lyrics` is the `LYRICS` setting, the
  page hides its Lyrics button when false; `downloading` is the first
  `MAX_DOWNLOADING` (50) entries of `player.pending_downloads`, see Pending
  downloads, and `downloading_total` counts them all; `viewers` counts the tabs that polled with an
  `X-Viewer` header, a random id `[A-Za-z0-9_-]{8,40}` each tab keeps in
  sessionStorage, within `VIEWER_WINDOW_SECONDS` (30): `state.viewers[sid]` is
  `{id: last seen}` in poll order, pruned from the front, capped at
  `MAX_VIEWERS_PER_SESSION`, and dropped when the session expires; plus the
  open live streams (a tab id counted once whether it streams, polls or both,
  a stream without a valid id on its own). `/state` is unchanged by live
  updates and stays the page's fallback.
  `GET /api/{sid}/events?viewer=<tab id>` streams the same body as
  Server-Sent Events, see Live updates.
  `GET /api/{sid}/stats?range=week|month|all` (default week, 400 otherwise,
  404 for an unknown sid) returns `stats.summary()` for the session's guild,
  cached `STATS_CACHE_SECONDS` (15) per guild and range (see Stats).
  `GET /api/{sid}/playlists` lists the saved playlists (see Saved playlists).
  `POST /api/{sid}/control` takes `{action:
  next|previous|pause|resume|remove|move|add|seek|jump|crossfade|loop|shuffle|volume|autoplay|save_playlist|load_playlist,
  id?, index?, url?, position?, enabled?, mode?, value?, name?}` (body <= 4 KB;
  crossfade and autoplay take a boolean `enabled`; volume an integer `value`
  0-150, valid out of voice too; seek restarts the track at `position` seconds
  (file time), clamped, staying paused if it was, unannounced; jump plays a
  queued `id` now; loop takes `mode` off|track|queue, none cycles; shuffle
  needs 2+ queued tracks; `save_playlist` and `load_playlist` take a `name`)
  and returns 200 `{"ok": true}` (202 for add and load_playlist, which
  download in the background) or `{"ok": false, "error": "<message>"}` (in
  the session's language, `_fail(request, status, key)`) with 400 (bad input,
  unsupported link), 403 (view-only id, see Permissions), 404 (unknown sid or
  track id) or 409 (not in voice, nothing playing).
  `GET /media/{sid}/{id}` streams the playing, a queued or a history
  track's mp4 with Range support. Files are found by track id, never by a path
  from the request. `GET /api/{sid}/lyrics/{id}` (same tracks, see Lyrics)
  returns `{synced: [[ms, line]...] | null, plain: str | null}`, 202
  `{"pending": true}` while the lookup still runs after `LYRICS_WAIT_SECONDS`
  (4), 503 when LRCLIB is unreachable (not cached: the page asks again later),
  or 404 (unknown sid or track, nothing found, `LYRICS=0`).
  Theme routes: `POST /api/{sid}/theme`,
  `POST|DELETE /api/{sid}/theme/logo`, `GET /theme-logo/{sid}` (see Themes).
  `GET /healthz` returns 200 `{status: "ok", discord: "connected", latency_ms,
  guilds, players, uptime_s, version}` when the bot is ready and its websocket
  answers (finite `bot.latency`), else 503 `{status: "unavailable", discord:
  "closed" | "not ready" | "websocket closed", uptime_s}`; it carries no ids,
  names or secrets. The Dockerfile `HEALTHCHECK` polls it (urllib, 60 s start
  period, 3 retries to ride out gateway resumes). `GET /metrics` (Prometheus
  text written by hand, 404 unless `METRICS` is on) exposes
  `djsnake_active_players`, `_queued_tracks`, `_downloads_in_progress`,
  `_web_viewers`, `_discord_latency_seconds` and the counter
  `djsnake_tracks_played_total` (`state.counters`, bumped by `retire_play()`).
  Tests: `tests/test_health.py`.
- **Live updates** (`web_events()`, `state_body()` builds the JSON shared with
  `/state`): one `data:` event with the full state on connect, then one per
  wake-up, coalesced to one per `SSE_MIN_INTERVAL` (0.1 s, last state wins);
  a real `event: ping` (data `{}`, which the page's `EventSource` sees, unlike a
  comment) after `SSE_HEARTBEAT_SECONDS` (10) of silence; and every
  `SSE_CHECK_SECONDS` (2) a diff of the state (clock fields excluded) that
  sends anything nobody notified (a crossfade drain ending, polling viewers).
  Headers `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`.
  The route resolves the id like `/state` (control or view-only; URLs in the
  body are built from the id used) and again on each wake-up: a view-only
  stream follows a control-link rotation, a control stream ends with
  `{"active": false}`, as does a session that ends, and then it closes; a closed transport
  ends the stream within a check period; the app's `on_shutdown`
  (`_close_streams`, run by `stop_web_server()` from `bot.close`) ends them
  all. Caps: `MAX_STREAMS_PER_SESSION` (100), `MAX_STREAMS_PER_CLIENT`
  (10, by `_client_key()`: the peer, or the last `X-Forwarded-For` hop when
  the peer is loopback or private) and `MAX_STREAMS_TOTAL` (500), 429 beyond. Each stream puts an
  `asyncio.Event` in `player.watchers` (moved to the new player after
  `!reset`). **The rule: after any change the page shows, call
  `notify_state(player)`** (on the loop; it bumps `player.state_version` and
  sets the watchers' events). It is already called by `refresh_panel()` (so
  by `set_action()` and every control and command), by
  `add_pending()`/`drop_pending()`/`name_pending()`/`stop_and_clear()`, and
  after every `POST /api/{sid}/control` and theme route (the `_notifies()`
  wrapper in `build_web_app()`), so a new control action or state field
  needs nothing more; anything missed still lands within
  `SSE_CHECK_SECONDS`. The page uses `EventSource` after its first poll and
  stops polling once a state arrives; on an error, a stream silent for `SSE_STALE_MS` (25 s) or a tab coming
  back after more than `SSE_RESUME_MS` (10 s) it closes the stream, polls
  (2 s, backing off to 10 s on failures) and retries the stream after 15 s,
  doubling to 2 min. A pushed state's delay is `Date.now()` minus the
  client-server clock offset (measured by the polls) minus `server_time`,
  clamped to 0..2 s, and added to `elapsed` as the poll's half round trip
  was. The demo shim replaces `EventSource` with a fake fed by the fake bot's
  change events. Tests: `tests/test_live_updates.py` (patch the `SSE_*`
  and `MAX_STREAMS_*` names on `web`).
- **Media Session** (`web/index.html`, `ms*` functions, no server part): the
  page sets `navigator.mediaSession` metadata (title/artist split from an
  "Artist - Title" title, album = guild + channel, thumbnail artwork in five
  sizes), `playbackState` and `setPositionState` (bar window `start`..`end`,
  refreshed on each state and local seek). Action handlers (play, pause,
  nexttrack, previoustrack, seekto, seekbackward/forward 10 s) call `send()` /
  `seekTo()` only with `canControl`; a view-only page registers none
  (`msBind(canControl)`). Because the clip `<video>` is muted, a silent looping
  `<audio>` (WAV blob) starts on the first pointer/key/touch gesture so
  browsers show the controls; `msClear()` runs when the session ends or goes
  idle. Tests: `tests/test_media_session.py` (string checks plus a Chromium run
  of the demo when playwright is installed). Support matrix in
  `docs/source/clip-page.md`.
- **PWA** (`web.py`, `web/sw.js`, `web/icons/`): `GET /{sid}/manifest.webmanifest`
  (`web_manifest()`, `application/manifest+json`, 404 for an unknown id) builds
  `id`/`start_url` and the logo icon URL from the id the request used, so a
  view-only manifest never contains the control id; name = theme name or
  "DJ Snake", `background_color` = `theme_color` = `themes.theme_bg()` (the
  custom bg, else `PRESET_BG`: a new preset needs a colour there too), icons =
  the guild logo (not resized, no image library) or the default PNGs. `GET
  /sw.js` serves `web/sw.js` with `__VERSION__` replaced by `APP_VERSION`
  (cache `djsnake-shell-<version>`, old ones deleted on activate),
  `Service-Worker-Allowed: /`, `no-cache`. The worker only intercepts page
  navigations (`/{sid}`, network first, a cached shell after 2.5 s, the
  localised offline page on failure; one copy stored under a fixed key, never
  a URL) and Google Fonts; every other request, so the API, `/events`, media,
  lyrics and logos, is never touched. `GET /icons/{name}` serves four fixed
  PNGs made by `scripts/make-pwa-icons.py` (stdlib only). The page adds the
  manifest link and registers the worker only when `window.DJDemoBackend` is
  absent (the static demo has none), keeps `<meta name="theme-color">` equal to
  `--bg`, and shows an "Install app" item in the palette menu on
  `beforeinstallprompt` (hidden when standalone or dismissed, `djsnake.installHidden`).
  Tests: `tests/test_pwa.py`.
- **Keyboard shortcuts** (`web/index.html`, one block before `poll()`): the
  `SHORTCUTS` array is the documented map and feeds the help `<dialog id="kbdlg">`
  (opened by `?` or the header keyboard button; `kb_*` keys in both `I18N`
  halves); one `keydown` listener skips fields, Ctrl/Alt/Meta, open dialogs and
  controls that own the key, matches letters by `event.key` (`event.code` only
  when the layout has no Latin letter), clicks the existing buttons so their
  guards apply, and needs `active()` for control keys (view-only keeps F, Y, T,
  `?`, Esc). Silent actions go through the throttled `toast()`. Tests:
  `tests/test_keyboard_shortcuts.py` (the browser part needs Playwright, else it
  is skipped).
- **Themes**: the page is fully token-driven: each preset is a `[data-skin]`
  block of CSS custom properties on `<html>` (Arcade is the original look;
  the presets are `THEME_PRESETS` in `config.py`: arcade, minimal, night,
  vinyl, club, terminal, space, cobalt); a new preset must also be listed in
  the page's early `localStorage` check, its `PRESETS` and both editor
  `<select>`s, and in `docs/demo/backend.js`; `custom` takes `--c-bg/--c-text/--c-accent` and derives the rest with
  `color-mix`, inheriting fonts and shapes from the preset in `data-base`;
  `data-shape` (the skin, or the base for custom) scopes the few rules a
  look needs beyond tokens (Arcade, Terminal, Space, Cobalt). Shape tokens also set the play button's radius (`--radius-pp`),
  the prev/next button border and shadow (`--ghost-border`, `--ghost-shadow`)
  and the opacity of the blurred artwork glow behind the player (`--ambient`,
  0 turns it off). Per-guild defaults live in `guild_themes`, persisted to
  `{DATA_DIR}/themes.json` (`{guild id: {skin, base, bg, text, accent, name,
  logo: {ext, v} | null}}`, written atomically with a temp file and
  `os.replace`, cleaned field by field on load); a logo is
  `{DATA_DIR}/logos/{guild id}.{ext}`. `theme_for()` fills in `WEB_THEME` and
  the custom defaults. `!theme edit` issues an edit token
  (`secrets.token_urlsafe(24)`, in memory in `edit_tokens` with the guild id,
  30 min) and DMs `{PUBLIC_BASE_URL}/{sid}?edit=<token>`, opening a session
  (and its panel) without joining voice if the guild has none. Theme routes
  take it in `X-Edit-Token`, compared with `secrets.compare_digest` and
  bound to the session's guild: `POST /api/{sid}/theme` (`{skin, base, bg,
  text, accent, name}`, JSON <= 4 KB, colors `#rrggbb`, name <= 32 chars) and
  `POST|DELETE /api/{sid}/theme/logo` (raw body <= 512 KB, PNG/JPEG/WebP/GIF
  by magic bytes, never SVG) answer `{"ok": true, "theme": ...}` or a localised
  error (400 bad input, 403 bad or expired token, 404 unknown session, 500
  write failed). `GET /theme-logo/{sid}` serves the logo with `nosniff`;
  `logo_url` carries `?v=` for cache busting. The page strips the token from
  the URL at load and keeps it in memory only; a viewer's own preset choice is
  in localStorage, and the custom theme is admin-only.
- **Stats** (`stats.py`, `{DATA_DIR}/stats.db`, WAL, schema version in
  `PRAGMA user_version`): tables `plays` (guild_id, video_id, title, artist,
  duration, requester, started_at, played_seconds, skipped), `sessions`
  (guild_id, started_at) and `listener_peaks` (guild_id, UTC day, listeners,
  at; one row per day keeps the max). Writes are queued to one daemon thread
  (`_submit()`; `flush()` waits for them) and best effort: errors are printed,
  never raised. `summary()` opens its own connection and runs in an executor.
  Hook points: `_advance()` calls `retire_play(player, guild, action)` before
  it resets `started_at` (a "restart"/"seek" instead adds the segment to
  `play_accum`: same play), `_start_crossfade()` retires the old track when it
  promotes the next, and `!stop` retires it under the lock before
  `stop_and_clear()`. Play time is `played_seconds()`: `elapsed()` minus the
  segment's start offset, plus `play_accum`, so pauses and seeks do not count.
  Skipped = left by next/jump (a "next"), previous or stop before
  `SKIP_SECONDS` (30). Requester: `download_and_queue_song()` stores
  `ctx.author.display_name` (None from a `WebContext`, shown as "page web");
  playlist and Spotify items inherit the `!play` author. Artist:
  the Spotify artist (`extra={"artist"}`) or `guess_artist(channel, title)`
  (`download_media()` stores `video_id` and `channel`; " - Topic" and "VEVO"
  are cleaned, else the "Artist - Title" split). A session is counted on each
  fresh voice join in `join_voice_channel()`, which also samples listeners;
  `on_voice_state_update` samples them when a human joins the bot's channel.
  The requester is recorded but never exposed by `summary()`.
  `summary()` returns `top_tracks` (10, skips excluded), `top_artists` (10),
  `most_skipped`, `totals {hours, tracks, sessions}`, `heatmap` (168
  UTC hour-of-week counts, Monday 00:00 = 0; the page shifts them to the
  browser's zone) and `peak_listeners {count, at}`. The page renders it all
  with `textContent`.
- **Lyrics** (`lyrics.py`, LRCLIB at `https://lrclib.net/api`, no key, a
  User-Agent naming the project): `_download_and_queue()` calls
  `lyrics.prefetch(track)` right after queueing, which starts one background
  task per cache key (the YouTube `video_id`, else `track:{id}`; tracked in
  `_tasks` and `state.background_tasks`) running `_load()` in an executor:
  the disk cache `{DATA_DIR}/lyrics/{video_id}.json` (found lyrics only,
  validated on read, written atomically), else `fetch()`. Never on the
  playback path; `get()` (the route) awaits a running task at most
  `LYRICS_WAIT_SECONDS`. Search fields come from `lookup_fields()`: a
  Spotify track's `artist`, `lyrics_title` and `lyrics_duration` (set by
  `spotify_fields()`), else `parse_title(title, channel)` ("Artist - Title"
  split on a spaced dash, bracketed noise like "(Official Video)" and
  "ft. …" dropped, first of several artists, the cleaned channel when there is
  no dash) and the file duration. `fetch()` tries `/api/get` with the
  duration (exact match), then `/api/search` by artist and title, then a free
  `q=` search, keeping the result closest in duration (synced first, within
  `MAX_DURATION_GAP` 30 s). Instrumental records and stubs (under `MIN_LINES`
  3 lines of text) count as nothing. `_http()` reuses Spotify's
  no-redirect urllib opener and capped read (`spotify._opener`,
  `_read_capped()`) with `LYRICS_TIMEOUT` (6 s) and `LYRICS_MAX_BYTES`; at
  most `MAX_CONCURRENT` (2) lookups run at once (a semaphore per loop); a 404 is "none" (cached in
  memory, LRU `CACHE_SIZE` 256), anything else raises `LyricsUnavailable`
  (logged, not cached, answered by the route as 503, retried by the next
  request). `parse_lrc()` reads
  `[mm:ss.xx]` tags (several per line), applies `[offset:±ms]` and keeps
  empty lines (instrumental breaks). Timestamps are song time and the page
  compares them with file time: SponsorBlock already cut talking intros out
  of the file and the automix `start` only moves the seek point, so no offset
  is applied. The page fetches lyrics for the playing track when its toggle
  (`djsnake.lyrics` in localStorage) is on, retries a 202 every 2.5 s, and
  drives the karaoke band from `targetPos()` (the video's clock, viewer offset
  included) in a `requestAnimationFrame` loop. Tests: `tests/test_lyrics.py`;
  `conftest.py` replaces `lyrics._http` with a function raising
  `LyricsUnavailable`, so no test reaches the network; patch `lyrics._http`
  to fake LRCLIB.
- **Backup** (`backup.py`, `python -m djsnake.backup create|restore|verify|list`):
  one `.tar.gz` with `manifest.json` (version 1, date, files with size and
  sha256) and `data/<path>`. SQLite files (detected by magic bytes) go through
  `sqlite3.Connection.backup()` from a read-only connection, so a live bot is
  fine; `-wal/-shm/-journal/.tmp` files, `backups/`, `.pre-restore-*`, the lock
  and (unless `--include-cache`) `lyrics/` are skipped. `main()` takes an
  exclusive `flock` on `DATA_DIR/.bot.lock` (`acquire_bot_lock()`); `restore`
  takes that lock itself for the whole restore (`_take_restore_lock()`,
  refused while a bot holds it), checks free space against the manifest
  total, refuses a member whose size differs from the manifest before
  extracting (and never reads past it), verifies while extracting into
  `.restore-tmp-*`, moves the current top-level entries to
  `.pre-restore-<date>/`, renames the restored ones in, and rolls back on
  error. `backup_loop()` (started in `setup_hook`) calls `create_auto()` in an
  executor every `BACKUP_INTERVAL_HOURS`, writing `backups/djsnake-auto-*`
  and keeping `BACKUP_KEEP` of them (`rotate()`; manual files are never
  rotated). New persistent files under `DATA_DIR` are picked up
  automatically. No Discord command. Archives contain secrets (e.g. Last.fm
  session keys), so they are 0600; `cookies.txt` belongs outside `DATA_DIR`.
  Tests: `tests/test_backup.py`.
- **Last.fm** (`lastfm.py`, off unless `LASTFM_API_KEY` and `LASTFM_API_SECRET`
  are both set, `enabled()`): `/lastfm link|unlink|status` (a `hybrid_group`).
  `start_link()` calls `auth.getToken` and returns the `last.fm/api/auth` URL
  (sent privately: ephemeral on `/`, DM on `!`, with a Done button);
  `complete_link()` exchanges the token with `auth.getSession` (error 14 = not
  authorised yet; `/lastfm link` again does the same). Links are
  `state.lastfm_links` (`{user id: {name, key}}`), persisted to
  `{DATA_DIR}/lastfm.json` (atomic, `chmod 0600`; keys are never logged or
  shown). Requests go through `lastfm._request()` (aiohttp, `LASTFM_TIMEOUT`),
  signed by `sign()` (md5 of the params sorted by name + secret, `format` not
  signed); tests replace `_request`. Hooks, both no-ops when disabled:
  `MusicPlayer.mark_started()` (a new play, not a seek) calls
  `track_started()`: it remembers the linked users in the voice channel
  (`player.lastfm_listeners`) and sends `track.updateNowPlaying` in a
  background task; `retire_play()` calls `track_ended()`: when the track lasts
  30 s or more and was played for half its length or 4 minutes
  (`should_scrobble()`), every user present at the start and still in the
  channel gets a scrobble in `state.lastfm_queue` (timestamp = start). Autoplay
  tracks count. Metadata is `track_info()`: `lyrics.lookup_fields()` (Spotify,
  Deezer or Apple fields, else the parsed YouTube title; an optional `album`
  field). `_flush_loop()` sends each user's queue 50 at a time with
  `track.scrobble`, retrying network errors and codes 8/11/16/29 with a
  30 s to 10 min backoff (queue capped at `LASTFM_MAX_PENDING` per user, in
  memory, one last flush at shutdown); error 9 unlinks the user; other
  refusals drop the batch. Tests: `tests/test_lastfm.py`.
- **Status panel**: each web session has one message in its text channel
  (`player.panel_message`), edited in place instead of announcing events:
  clip links (see Permissions), now playing / paused / idle, up next, loading progress and the
  last action. Every state change calls `refresh_panel(player)`, which is
  coalesced to one edit per `PANEL_MIN_INTERVAL` (1.5 s, last state wins). A
  deleted panel is re-posted; a rejoin posts a fresh one and deletes the old.
  Use `set_action()` for the last-action line. Commands acknowledge success
  with a reaction (`ack()`); only errors, `!list`, `!reset`, usage help and
  the playlist prompt are text replies.
- **Voice**: `safe_voice_connect()` cleans up any stale voice client, adds
  delays, and retries. It exists to avoid Discord close code 4006 (invalid
  session). `join_voice_channel()` wraps it. `!reset` forces a reconnect.
- **Commands** (all hybrid, `!name` and `/name`; `/play` has one `query`
  option, a link or search text): `!play` (YouTube, SoundCloud, Bandcamp,
  Spotify, Deezer or Apple Music link, or search text), `!list`, `!next`,
  `!pause`, `!resume`, `!seek` (position relative to the track's automix
  `start`, as the page shows it), `!crossfade on|off`, `!loop
  [off|track|queue]`, `!shuffle`, `!remove <position>`, `!move <from> <to>`, `!previous`,
  `!clear` (positions are 1-based, as `!list` shows them; `/remove` and `/move`
  autocomplete the queue; `control_clear()` is the one control only Discord
  uses), `!nowplaying` (`!np`, an embed built by `now_playing_embed()`; its
  clip link is `sessions.public_link()`), `!history [count]` (reads
  `player.played`, the last `HISTORY_LOG_SIZE` 25 titles kept by
  `push_history()`, cleared by `!stop`), `!volume [0-150]`, `!autoplay [on|off]`,
  `!stay [on|off]`, `!voteskip [threshold <1-100>]` (alias `!vs`), `!limits [setting] [value]`,
  `!playlist save|add|load|show|delete <name>` and `!playlist list` (a
  `hybrid_group`: `/playlist save`, ...), `!stop`, `!reset`, `!weblink` (see
  Permissions), and the Manage Server ones (`commands.has_guild_permissions`):
  `!theme [preset|reset|edit]`, `!djrole [role|off]` and `!language
  [code|reset]` (aliases `!lang`, `!langue`). `!help` is `HelpCommand`, one
  `help.cmd.<name>` line per command in `HELP_ORDER`.
- **Translations** (`i18n.py`): every user-facing text is a key in
  `djsnake/locales/en.json` and `fr.json` (flat, same keys and same
  `{placeholders}` in both; `tests/test_i18n.py` checks it). `es.json` and
  `de.json` are best effort: they may lack keys (never add keys absent from
  English; placeholders must match) and a missing key falls back to English
  per key, bot and page alike; `python3 scripts/i18n-missing.py` lists them.
  Plurals: one vs other for en, es and de, French also counts 0 as singular. `t(key, lang,
  **params)` formats it (`{prefix}` is always filled in; a `count` param picks
  `key.one` / `key.other`, French singular for 0 and 1), falling back to
  English, then to the key itself. A guild's language is
  `state.guild_languages` (`!language`, persisted to
  `{DATA_DIR}/languages.json` like themes), else `BOT_LANGUAGE`: use
  `ctx_lang(ctx)` for replies, `player_lang(player)` (sessions.py) for the
  panel, `set_action()` and `load_line`, and `request_lang(request)` /
  `_fail()` in web.py. Panel lines are rendered when set, so a language change
  shows on the next one. Log lines (`print`), command names, ControlError codes
  and API field names are never translated. The page has its own `I18N`
  dictionary (see Web server) and picks `state.lang` unless the viewer chose
  a language.
- Voice needs `PyNaCl`, `davey` (DAVE encryption, required by discord.py 2.7+)
  and, on Python 3.13+, `audioop-lts`. The Docker image uses Python 3.15. CI
  tests on 3.10 to 3.15.

## Keeping YouTube Downloads Working

YouTube breaks older yt-dlp extractors every few weeks. The symptom is always
the same: `HTTP Error 403: Forbidden` on the media download, while extraction
itself still succeeds. A Docker image freezes yt-dlp at build time, so the bot
has two defenses:

1. **`docker-entrypoint.sh`** upgrades yt-dlp on every container start. It is
   best effort: if PyPI is unreachable the bot boots with the bundled version.
   Set `YTDLP_AUTO_UPDATE=0` to skip it. Note that pip exits 0 even offline, so
   the script reports the result by comparing versions before and after.
2. **`scripts/refresh-dj-snake.sh`**, run weekly from the host's cron, restarts
   the container so that upgrade actually happens on a long-running deployment.
   It logs to `logs/refresh.log`.

`REMOTE_COMPONENTS = ["ejs:github"]` in `djsnake/config.py` lets yt-dlp fetch
the EJS challenge solver. Without it, signature and `n` challenge solving fail
and some formats become unavailable. It needs the `deno` runtime, which the
image installs.

### "Sign in to confirm you're not a bot"

A different failure from the 403, and intermittent: YouTube challenges traffic
from datacenter IPs and demands a Proof-of-Origin token. `yt-dlp -v` shows
`PO Token Providers: none` when nothing can answer that challenge.

The `pot-provider` service in `docker-compose.yml` mints those tokens, and
`bgutil-ytdlp-pot-provider` (pinned in `requirements.txt`) is the yt-dlp side.
**The plugin version must match the server image tag**, so bump them together.
The entrypoint therefore upgrades yt-dlp only, never the plugin.

`EXTRACTOR_ARGS` in `djsnake/config.py` points the plugin at the service. The
key name is load-bearing: `youtube:getpot_bgutil_baseurl` is deprecated and
*rejected*, and the plugin then silently falls back to its default, so the
setting looks applied but is not. The current key is `youtubepot-bgutilhttp:base_url`. To check that a
config change is actually read, point it at a dead port and confirm yt-dlp warns
about that port.

Because the challenge is intermittent, `download_media()` also retries
transient failures with backoff, while `PERMANENT_ERROR_MARKERS` keeps it from
wasting retries on deleted or private videos.

To diagnose a download failure, reproduce it directly:

```bash
docker exec dj-snake python -m yt_dlp --remote-components ejs:github \
    -f bestaudio/best -o /tmp/t.%(ext)s --no-playlist "<url>"
```

## Adding a command

Every command is a hybrid command: `!name` and `/name` from one function. Use
the `hybrid(name, description)` decorator in `commands.py` (it registers a
guild-only `bot.hybrid_command`), and put `@app_commands.describe(param=...)`
and, for fixed values, `@app_commands.choices(param=[...])` under it. Keep the
function plain (`async def name(ctx, arg: str = None)`); tests call
`commands.name.callback(ctx, ...)`. Reply paths must work for both contexts:

- `await defer(ctx)` first if it can take over 3 s (downloads, voice); it is a
  no-op for `!`. A slash `ctx.message` is synthetic: never use it. `ack(ctx)`
  reacts for `!` and sends or edits a private "✅" for `/`.
- Report user mistakes with `await fail(ctx, "text")` (private on `/`). Other
  replies use `ctx.send`.
- A context kept past the command (download jobs, `play_next`, timers) must be
  `ChannelContext(ctx)`: an interaction's replies expire after 15 minutes.
- Permission checks (`commands.has_guild_permissions`) work for both; their
  errors are worded in `error_message()`, shared by `on_command_error` and
  the slash tree's `on_app_command_error`.
- Add an autocomplete with `@name.autocomplete("param")`.

Slash commands are synced in `app.sync_commands()`. `tests/fakes.py` has
`make_slash_ctx()` for the interaction side.

## Conventions

- Code comments, docs, log lines and the host-side refresh script are in
  English. User-facing messages are English by default and translated (see
  Translations).
- **Languages**: en and fr are mandatory for every new string; es/de are
  best-effort, missing keys fall back to English (informal "tú"/"du";
  Spanish ¿ ¡, German „…“).
- **Adding a user-facing string**: never write the text inline. Add a key to
  `djsnake/locales/en.json` (English) and `fr.json` (French: neutral
  imperative, no "vous"/"tu", typographic apostrophe ’, U+00A0 before `:`,
  U+202F before `; ! ?`, `…`), then call `t("area.key", lang, **params)`. A
  new `ControlError` code gets its status in `ControlError.STATUS` and a
  `control.<code>` message; a new command gets a `help.cmd.<name>` line (and
  a place in `HELP_ORDER`). Page strings go in both halves of the page's `I18N`
  dictionary. Tests may assert English text: they run with `BOT_LANGUAGE=en`.
- Black formatting with its default 88-column limit (there is no
  `pyproject.toml`), so CI's `black --check` reformats anything longer. The 127
  in CI's flake8 call is a non-blocking warning pass.
