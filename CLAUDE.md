# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Discord music bot (discord.py) that downloads audio from YouTube with yt-dlp
and plays it in voice channels, with an independent queue per server. All the
bot logic lives in a single file, `bot.py`. It is deployed with Docker Compose
alongside a PO token provider sidecar.

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

There is no test suite. CI (`.github/workflows/ci.yml`) runs these checks, and
a change must pass them:

```bash
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics   # hard failure
black --check --diff .                                               # hard failure
python -c "import bot"                                               # import smoke test
```

The bot is tested by hand in Discord, e.g. `!play <youtube url>`.

## Configuration

Read from the environment (`.env`, loaded via python-dotenv, and passed to the
container by `env_file`): `DISCORD_TOKEN` (required; a `token` file is the
fallback), `COMMAND_PREFIX` (`!`), `MAX_PLAYLIST_SIZE` (30), `MEDIA_FORMAT`
(max 720p H.264 + m4a, see `DEFAULT_MEDIA_FORMAT`; it replaces `AUDIO_FORMAT`,
which is no longer read), `TEMP_DIR`, `POT_BASE_URL`, `WEB_PORT` (47823),
`PUBLIC_BASE_URL` (`https://dj.example.com`), and `YTDLP_AUTO_UPDATE`
(entrypoint only). `config.json.example` is not read by the code.

## Architecture (`bot.py`)

- **Per-guild state**: `music_players` maps guild ID to a `MusicPlayer` holding
  the voice client, the queue of `{id, filepath, title, duration, thumbnail}` items, the
  flags `is_playing`, `should_stop` and `loading`, the web session `sid` and
  `started_at` (`time.monotonic()` at playback start).
- **Download before play**: tracks are fully downloaded to `TEMP_DIR` as one
  video+audio mp4 (`merge_output_format`, `+faststart`) before they enter the
  queue. The downloaded file is deleted once the next track starts. yt-dlp is
  blocking, so `download_media()` and `get_playlist_info()` always run through
  `loop.run_in_executor`.
- **Playback chain**: `play_next()` pops the queue and starts `FFmpegPCMAudio`
  (with `-vn`: Discord gets the audio only).
  The `after` callback runs on a non-asyncio thread and schedules the next
  `play_next()` with `asyncio.run_coroutine_threadsafe(..., bot.loop)`.
- **Playlists**: `!play` first tries `get_playlist_info()`, a flat extraction
  with no download. If the URL contains both `v=` and a playlist (a radio or
  mix link), `PlaylistChoiceView` asks with buttons whether to play one track
  or the whole list; on timeout it plays the single track. Playlist tracks
  download one at a time while earlier ones play. `player.loading` stops
  `play_next()` from disconnecting when the queue briefly runs empty during
  that loading. `!stop` sets `should_stop`, which every download step checks.
- **Gateway reconnects are routine**: discord.py drops and resumes the gateway
  session roughly every couple of hours. Don't do cleanup in `on_disconnect`.
  An earlier handler deleted `TEMP_DIR` there and broke long playlists in the
  middle of playback. Per-track cleanup in `play_next()` and `!stop` is the only
  place files are removed. `on_voice_state_update` resets the player's voice
  state when the bot is kicked from a channel.
- **Clip live sessions**: joining voice (`join_voice_channel()`) opens a session
  (`secrets.token_urlsafe(16)`, `sessions` maps sid to guild) and posts
  `{PUBLIC_BASE_URL}/{sid}` in the channel. Leaving voice ends it (queue end,
  `!stop`, a kick seen by `on_voice_state_update`); `player.reconnecting` keeps
  it across `!reset` and channel switches.
- **Web server**: aiohttp, started in `setup_hook` on the bot's loop, bound to
  `0.0.0.0:WEB_PORT`. Routes, the contract `web/index.html` relies on:
  `GET /` landing text; `GET /{sid}` serves `web/index.html`;
  `GET /api/{sid}/state` returns `{active, guild, channel, loading, server_time,
  now_playing: {id, title, duration, thumbnail, video_url, elapsed} | null,
  queue: [{id, title, duration, thumbnail}]}` (404 `{"active": false}` when the
  sid is unknown); `GET /media/{sid}/{id}` streams the playing or a queued
  track's mp4 with Range support. Files are found by track id, never by a path
  from the request.
- **Voice**: `safe_voice_connect()` cleans up any stale voice client, adds
  delays, and retries. It exists to avoid Discord close code 4006 (invalid
  session). `join_voice_channel()` wraps it. `!reset` forces a reconnect.
- **Commands**: `!play`, `!list`, `!next`, `!stop`, `!reset`.
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

`REMOTE_COMPONENTS = ["ejs:github"]` in `bot.py` lets yt-dlp fetch the EJS
challenge solver. Without it, signature and `n` challenge solving fail and some
formats become unavailable. It needs the `deno` runtime, which the image
installs.

### "Sign in to confirm you're not a bot"

A different failure from the 403, and intermittent: YouTube challenges traffic
from datacenter IPs and demands a Proof-of-Origin token. `yt-dlp -v` shows
`PO Token Providers: none` when nothing can answer that challenge.

The `pot-provider` service in `docker-compose.yml` mints those tokens, and
`bgutil-ytdlp-pot-provider` (pinned in `requirements.txt`) is the yt-dlp side.
**The plugin version must match the server image tag**, so bump them together.
The entrypoint therefore upgrades yt-dlp only, never the plugin.

`EXTRACTOR_ARGS` in `bot.py` points the plugin at the service. The key name is
load-bearing: `youtube:getpot_bgutil_baseurl` is deprecated and *rejected*, and
the plugin then silently falls back to its default, so the setting looks applied
but is not. The current key is `youtubepot-bgutilhttp:base_url`. To check that a
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
