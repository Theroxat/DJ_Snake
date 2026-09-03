# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Discord music bot built with discord.py that downloads and plays music from YouTube. The bot manages per-server music queues and handles voice channel connections. **The bot has been updated to fix Discord error 4006 issues with a completely rewritten voice connection system.**

## Key Commands for Development

### Setup Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate
```

### Installing Dependencies
```bash
pip install -r requirements.txt
```

### Running the Bot
```bash
python3 bot.py
```

### Testing Functionality
```bash
# Test with a Discord command in Discord
# !play https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

## Architecture

The bot uses a per-guild (server) music player architecture:

- **MusicPlayer Class** (bot.py:15-46): Manages music queue, playback state, and file cleanup for each Discord server
- **music_players Dictionary** (bot.py:48): Stores MusicPlayer instances indexed by guild ID
- **Temporary File Management**: Uses Python's tempfile module to create a temporary directory for downloaded audio files

### Core Components

1. **Voice Connection System** (bot.py:50-124):
   - `safe_voice_connect()`: New robust connection function that prevents Discord error 4006
   - Implements delays, cleanup, and retry logic to avoid session invalidation
   - `join_voice_channel()`: Simplified wrapper that uses the safe connection method

2. **Download System** (bot.py:126-210): 
   - Uses yt-dlp with simplified but robust configuration
   - Handles YouTube downloads with proper error handling
   - Returns filepath and title for queue management

3. **Playlist Support** (bot.py:49-125):
   - `get_playlist_info()`: Extracts playlist entries without downloading
   - Handles both playlist URLs and video URLs with playlist context
   - Limits playlists to 30 tracks by default to prevent excessive resource usage

4. **Queue Management**:
   - Each server has independent queue managed by its MusicPlayer instance
   - Automatic cleanup of temporary files after playback (bot.py:270-290)
   - Support for playlist downloads with progress indicators (bot.py:385-430)

5. **Playback System** (bot.py:270-340):
   - `play_next()`: Handles sequential playback from queue
   - Automatic file cleanup after each track
   - Error recovery and reconnection handling

## Important Implementation Details

- Bot token is read from a separate 'token' file (bot.py:504-508)
- FFmpeg is required for audio processing and must be installed on the system
- The bot uses discord.py intents for message content and voice states (bot.py:7-12)
- Temporary audio files are stored in a system temp directory and cleaned up after playback
- Voice state monitoring for automatic cleanup (bot.py:355-363)

## Discord Bot Commands

- `!play <URL>`: Download and play music/playlist from YouTube
- `!list`: Display current queue (paginated for long queues)
- `!next`: Skip to next track
- `!stop`: Stop playback and clear queue
- `!reset`: Force reset voice connection for troubleshooting

All bot messages, error messages, and user interactions are now in English.

## Dependencies

- Python 3.8+
- FFmpeg (system dependency)
- discord.py>=2.5.2
- yt-dlp>=2026.8.19 (older versions get HTTP 403 from YouTube)
- python-dotenv==1.0.0 (Environment variables)
- PyNaCl==1.5.0 (Discord voice support)
- PyOpus==0.9 (Optional: Audio performance enhancement)

## Keeping YouTube Downloads Working

YouTube breaks older yt-dlp extractors every few weeks. The symptom is always
the same: `HTTP Error 403: Forbidden` on the media download, while extraction
itself still succeeds. A Docker image freezes yt-dlp at build time, so the bot
has two defenses:

1. **`docker-entrypoint.sh`** upgrades yt-dlp on every container start. It is
   best effort: if PyPI is unreachable the bot boots with the bundled version.
   Set `YTDLP_AUTO_UPDATE=0` to skip it. Note that pip exits 0 even offline, so
   the script reports the result by comparing versions before and after.
2. **`scripts/refresh-dj-snake.sh`**, run weekly from cron, restarts the
   container so that upgrade actually happens on a long-running deployment.

`REMOTE_COMPONENTS = ["ejs:github"]` in `bot.py` lets yt-dlp fetch the EJS
challenge solver. Without it, signature and `n` challenge solving fail and some
formats become unavailable. It needs the `deno` runtime, installed in the image.

### "Sign in to confirm you're not a bot"

A different failure from the 403, and intermittent: YouTube challenges traffic
from datacenter IPs and demands a Proof-of-Origin token. `yt-dlp -v` shows
`PO Token Providers: none` when nothing can answer that challenge.

The `pot-provider` service in `docker-compose.yml` mints those tokens, and
`bgutil-ytdlp-pot-provider` (pinned in `requirements.txt`) is the yt-dlp side.
**The plugin version must match the server image tag** -- bump them together.

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

## Error Handling Strategies

- **Discord Error 4006 Prevention**: Complete rewrite of voice connection system
- **Safe Voice Connection**: `safe_voice_connect()` function with proper cleanup and delays
- **Retry Logic**: Multiple connection attempts with increasing delays
- **Session Management**: Proper disconnection and cleanup to prevent session conflicts
- **Graceful Degradation**: Automatic fallback and error recovery for downloads and playback

## File Structure

- `bot.py`: Main bot implementation with all core functionality
- `requirements.txt`: Python dependencies
- `Dockerfile` / `docker-compose.yml`: Container build and deployment
- `docker-entrypoint.sh`: Refreshes yt-dlp at container start (see below)
- `scripts/refresh-dj-snake.sh`: Host-side weekly restart, run from cron
- `pot-provider` service: PO token minting sidecar (no host port; internal only)
- `token`: Bot token file (not in git)
- `README.md`: English documentation for users
- `.env.example`: Environment configuration template
- `INSTALLATION.md`: Detailed setup instructions

All code comments, user messages, and documentation are in English.

## Recent Architecture Changes

The voice connection system has been completely rewritten to prevent Discord error 4006:

1. **New `safe_voice_connect()` function**: Handles connection with proper cleanup and delays
2. **Simplified command structure**: Removed complex error handling that was causing session conflicts
3. **Automatic cleanup**: Proper disconnection and session management
4. **Enhanced diagnostics**: Better error reporting and troubleshooting tools
5. **Direct execution**: Simplified deployment without Docker complexity

The bot now uses a much more reliable connection pattern that works best when run directly on the system.