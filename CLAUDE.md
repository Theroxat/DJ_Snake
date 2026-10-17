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

CI (`.github/workflows/ci.yml`) runs these checks on Python 3.10 to 3.12, and a
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
`discord.FFmpegPCMAudio`, and `tests/conftest.py` sets `TEMP_DIR`, `DATA_DIR`
and `WEB_THEME` before `djsnake` is imported and empties the shared state
between tests. The web API runs through aiohttp's `TestClient`. Tests that need
the real `ffmpeg` binary are marked `ffmpeg` and skipped without it. Patch
`media.download_media`, `media.get_playlist_info`, `state.clock` or
`sessions.PANEL_MIN_INTERVAL` on their module: callers look them up there.
Playback in Discord is still tested by hand, e.g. `!play <youtube url>`.

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
`python3 docs/demo/build.py` and commit the regenerated `docs/demo/index.html`.

## Configuration

Read from the environment (`.env`, loaded via python-dotenv, and passed to the
container by `env_file`): `DISCORD_TOKEN` (required; a `token` file is the
fallback), `COMMAND_PREFIX` (`!`), `MAX_PLAYLIST_SIZE` (30), `MEDIA_FORMAT`
(max 720p H.264 + m4a, see `DEFAULT_MEDIA_FORMAT`; it replaces `AUDIO_FORMAT`,
which is no longer read), `TEMP_DIR`, `POT_BASE_URL`, `WEB_PORT` (47823),
`WEB_BIND` (host IP the web port is published on, compose only, default
`127.0.0.1`; set a LAN IP or `0.0.0.0` for a reverse proxy or tunnel),
`PUBLIC_BASE_URL` (e.g. `https://dj.example.com`; empty falls back to
`http://localhost:WEB_PORT`), `SESSION_IDLE_HOURS` (6), `DATA_DIR`
(`/app/data`, mounted from `./data` by Compose; the container's UID 1000 must
be able to write it), `WEB_THEME` (`arcade`, the theme of guilds that never
chose one), and `YTDLP_AUTO_UPDATE` (entrypoint only).

## Architecture (`djsnake/`)

Module map, lowest layer first (each imports only the ones above it):

- `config.py`: environment variables and constants, read once at import
  (`WEB_INDEX` resolves `web/index.html` from the repository root).
- `state.py`: shared mutable state: the `bot` instance, `music_players`,
  `sessions`, `guild_themes`, `edit_tokens`, `background_tasks` and `clock()`.
  Always use it as `state.music_players` (never `from .state import ...`), so
  every module and test sees the same objects.
- `player.py`: `MusicPlayer`, `discard_tracks()`, `file_in_use()`,
  `push_history()`.
- `sessions.py`: session expiry (`touch()`, `session_player()`,
  `session_cleanup_loop()`, `open_session()`) and the status panel
  (`refresh_panel()`, `set_action()`, `ack()`, `short()`).
- `themes.py`: theme storage, logos and `!theme edit` tokens.
- `media.py`: YouTube URL parsing, yt-dlp (`download_media()`,
  `get_playlist_info()`), the SponsorBlock cut, `analyse_media()`,
  `playback_options()`, `crossfade_eligible()`.
- `mixer.py`: `CrossfadeMixer`.
- `playback.py`: voice connection, `download_and_queue_song()`,
  `queue_playlist()`, `play_next()` / `_advance()` / `_start_crossfade()`,
  `open_track_source()`.
- `controls.py`: `ControlError`, the `control_*` functions, `WebContext`.
- `web.py`: the aiohttp routes and `start_web_server()`.
- `commands.py`: the Discord commands, `before_invoke`, `on_command_error`.
- `app.py`: `on_ready`, `on_voice_state_update`, `setup_hook`, `main()`;
  importing it registers the commands. `bot.py` calls `main()`.

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
- **Controls** (`control_next/previous/pause/resume/remove/move/add/seek/jump/crossfade`) are shared
  by Discord commands and the web page and raise `ControlError` (HTTP status,
  French web message, English Discord message). Previous restarts the track
  after 5 s of play or with empty history. Add accepts only YouTube hosts and
  rebuilds a canonical URL (`parse_youtube_url()`, no SSRF via yt-dlp); `v=`
  means one track, otherwise `list=` loads up to `MAX_PLAYLIST_SIZE` in the
  background with a `WebContext` (no author, so it can never join voice).
  Web actions are announced in the session's text channel.
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
- **Gateway reconnects are routine**: discord.py drops and resumes the gateway
  session roughly every couple of hours. Don't do cleanup in `on_disconnect`.
  An earlier handler deleted `TEMP_DIR` there and broke long playlists in the
  middle of playback. Per-track cleanup in `play_next()` and `!stop` is the only
  place files are removed. `on_voice_state_update` resets the player's voice
  state when the bot is kicked from a channel.
- **Clip live sessions**: a session (`secrets.token_urlsafe(16)`, `sessions`
  maps sid to guild) belongs to the guild and survives leaving voice (queue
  end, `!stop`, kick, `!reset`). A fresh join reuses the unexpired sid, or
  makes one, and posts `{PUBLIC_BASE_URL}/{sid}`; that channel becomes
  `player.text_channel` for notices. It expires after `SESSION_IDLE_HOURS`
  without activity (playback or being in voice, a command via
  `bot.before_invoke`, a web control): lazily in `session_player()` plus
  `session_cleanup_loop()` every 10 min. Expiry deletes the player's files.
- **Web server**: aiohttp, started in `setup_hook` on the bot's loop, bound to
  `0.0.0.0:WEB_PORT`. Routes, the contract `web/index.html` relies on:
  `GET /` landing text; `GET /{sid}` serves `web/index.html`;
  `GET /api/{sid}/state` returns `{active, in_voice, guild, channel, loading,
  paused, can_previous, crossfade, crossfading, server_time, now_playing: {id,
  title, duration, thumbnail, url, video_url, elapsed, start, end} | null, queue: [{id, title, duration,
  thumbnail, url}], theme: {skin, base, bg, text, accent, name, logo_url}}`
  (404 `{"active": false}` when the sid is unknown or
  expired; out of voice, `now_playing` is null and `queue` empty; `elapsed`
  freezes while paused). `POST /api/{sid}/control` takes `{action:
  next|previous|pause|resume|remove|move|add|seek|jump|crossfade, id?, index?,
  url?, position?, enabled?}` (body <= 4 KB; crossfade takes a boolean
  `enabled`; seek restarts the track at `position` seconds (file time),
  clamped, staying paused if it was, unannounced; jump plays a queued `id` now)
  and returns 200 `{"ok": true}` (202 for add, which downloads in the
  background) or `{"ok": false, "error": "<French>"}` with 400 (bad input,
  non-YouTube URL), 404 (unknown sid or track id) or 409 (not in voice, nothing
  playing). `GET /media/{sid}/{id}` streams the playing, a queued or a history
  track's mp4 with Range support. Files are found by track id, never by a path
  from the request. Theme routes: `POST /api/{sid}/theme`,
  `POST|DELETE /api/{sid}/theme/logo`, `GET /theme-logo/{sid}` (see Themes).
- **Themes**: the page is fully token-driven: each preset is a `[data-skin]`
  block of CSS custom properties on `<html>` (Arcade is the original look);
  `perso` takes `--c-bg/--c-text/--c-accent` and derives the rest with
  `color-mix`, inheriting fonts and shapes from the preset in `data-base`;
  `data-shape` (the skin, or the base for perso) scopes a few Arcade-only
  details. Per-guild defaults live in `guild_themes`, persisted to
  `{DATA_DIR}/themes.json` (`{guild id: {skin, base, bg, text, accent, name,
  logo: {ext, v} | null}}`, written atomically with a temp file and
  `os.replace`, cleaned field by field on load); a logo is
  `{DATA_DIR}/logos/{guild id}.{ext}`. `theme_for()` fills in `WEB_THEME` and
  the perso defaults. `!theme edit` issues an edit token
  (`secrets.token_urlsafe(24)`, in memory in `edit_tokens` with the guild id,
  30 min) and DMs `{PUBLIC_BASE_URL}/{sid}?edit=<token>`, opening a session
  (and its panel) without joining voice if the guild has none. Theme routes
  take it in `X-Edit-Token`, compared with `secrets.compare_digest` and
  bound to the session's guild: `POST /api/{sid}/theme` (`{skin, base, bg,
  text, accent, name}`, JSON <= 4 KB, colors `#rrggbb`, name <= 32 chars) and
  `POST|DELETE /api/{sid}/theme/logo` (raw body <= 512 KB, PNG/JPEG/WebP/GIF
  by magic bytes, never SVG) answer `{"ok": true, "theme": ...}` or a French
  error (400 bad input, 403 bad or expired token, 404 unknown session, 500
  write failed). `GET /theme-logo/{sid}` serves the logo with `nosniff`;
  `logo_url` carries `?v=` for cache busting. The page strips the token from
  the URL at load and keeps it in memory only; a viewer's own preset choice is
  in localStorage, and the custom theme is admin-only.
- **Status panel**: each web session has one message in its text channel
  (`player.panel_message`), edited in place instead of announcing events:
  clip link, now playing / paused / idle, up next, loading progress and the
  last action. Every state change calls `refresh_panel(player)`, which is
  coalesced to one edit per `PANEL_MIN_INTERVAL` (1.5 s, last state wins). A
  deleted panel is re-posted; a rejoin posts a fresh one and deletes the old.
  Use `set_action()` for the last-action line. Commands acknowledge success
  with a reaction (`ack()`); only errors, `!list`, `!reset`, usage help and
  the playlist prompt are text replies.
- **Voice**: `safe_voice_connect()` cleans up any stale voice client, adds
  delays, and retries. It exists to avoid Discord close code 4006 (invalid
  session). `join_voice_channel()` wraps it. `!reset` forces a reconnect.
- **Commands**: `!play`, `!list`, `!next`, `!pause`, `!resume`, `!seek`
  (position relative to the track's automix `start`, as the page shows it),
  `!crossfade on|off`, `!stop`, `!reset`, `!theme [preset|reset|edit]`
  (Manage Server permission via `commands.has_guild_permissions`).
- Voice needs `PyNaCl`, `davey` (DAVE encryption, required by discord.py 2.7+)
  and, on Python 3.13+, `audioop-lts`. The Docker image uses Python 3.12. CI
  tests on 3.10 to 3.12.

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
   It logs to `logs/refresh.log`. Its comments and log lines are in French.

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

## Conventions

- User-facing messages, code comments and docs are in English, except the
  host-side refresh script, which is in French.
- Black formatting with its default 88-column limit (there is no
  `pyproject.toml`), so CI's `black --check` reformats anything longer. The 127
  in CI's flake8 call is a non-blocking warning pass.
