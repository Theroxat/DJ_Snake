# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Discord music bot (discord.py) that downloads audio from YouTube with yt-dlp
and plays it in voice channels, with an independent queue per server. All the
bot logic lives in a single file, `bot.py`. It is deployed with Docker Compose
alongside a PO token provider sidecar.

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
fallback), `COMMAND_PREFIX` (`!`), `MAX_PLAYLIST_SIZE` (30), `AUDIO_FORMAT`
(`bestaudio/best`), `TEMP_DIR`, `POT_BASE_URL`, and `YTDLP_AUTO_UPDATE`
(entrypoint only). `config.json.example` is not read by the code.

## Architecture (`bot.py`)

- **Per-guild state**: `music_players` maps guild ID to a `MusicPlayer` holding
  the voice client, the queue of `{filepath, title}` items, and the flags
  `is_playing`, `should_stop` and `loading`.
- **Download before play**: tracks are fully downloaded to `TEMP_DIR` before
  they enter the queue. The downloaded file is deleted once the next track
  starts. yt-dlp is blocking, so `download_audio()` and `get_playlist_info()`
  always run through `loop.run_in_executor`.
- **Playback chain**: `play_next()` pops the queue and starts `FFmpegPCMAudio`.
  The `after` callback runs on a non-asyncio thread and schedules the next
  `play_next()` with `asyncio.run_coroutine_threadsafe(..., bot.loop)`.
- **Playlists**: `!play` first tries `get_playlist_info()`, a flat extraction
  with no download. If the URL contains both `v=` and a playlist (a radio or
  mix link), `PlaylistChoiceView` asks with buttons whether to play one track
  or the whole list; on timeout it plays the single track. Playlist tracks
  download one at a time while earlier ones play. `player.loading` stops
  `play_next()` from disconnecting when the queue briefly runs empty during
  that loading. `!stop` sets `should_stop`, which every download step checks.
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

Because the challenge is intermittent, `download_audio()` also retries
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
- Black formatting. Max line length is 127, following flake8 in CI.
