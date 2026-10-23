# Configuration

DJ Snake is configured through environment variables. With Docker Compose, put them in a `.env` file next to `docker-compose.yml` (copy `.env.example`); Compose passes the file to the container. Without Docker, the bot loads `.env` itself. Variables are read once at startup, so restart the bot after changing them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | none (required) | Bot token. A `token` file is a fallback. |
| `COMMAND_PREFIX` | `!` | Prefix for text commands. |
| `MAX_PLAYLIST_SIZE` | `30` | Maximum tracks loaded from a playlist (YouTube, Spotify, SoundCloud, Bandcamp, Deezer or Apple Music). |
| `MEDIA_FORMAT` | max 720p H.264 + m4a | yt-dlp format selector. The default is `bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best`. |
| `TEMP_DIR` | a fresh temporary directory | Where downloaded tracks are stored. Files are deleted once played. |
| `POT_BASE_URL` | `http://pot-provider:4416` | URL of the PO token provider. The default matches Docker Compose. |
| `WEB_PORT` | `47823` | Clip page port, inside the container and on the host. |
| `WEB_BIND` | `127.0.0.1` | Host IP the web port is published on (Compose only). Use a LAN IP or `0.0.0.0` for a reverse proxy or tunnel. |
| `PUBLIC_BASE_URL` | empty (`http://localhost:WEB_PORT`) | Public address used in the clip link the bot posts, for example `https://dj.example.com`. |
| `SESSION_IDLE_HOURS` | `6` | Hours without activity before a clip page link expires. |
| `IDLE_DISCONNECT_SECONDS` | `300` | Seconds the bot stays in voice once the queue runs out, before leaving (30 s instead when no human is left in the channel). |
| `AUTOPLAY` | `0` | Default of the per-session autoplay setting (`1`, `true`, `on` or `yes` to start with it on). Change it per session with `!autoplay` or the clip page. |
| `DATA_DIR` | `/app/data` | Persistent data: per-server themes (`themes.json`), volumes (`volume.json`), logos and listening stats (`stats.db`). Compose mounts `./data` there. |
| `WEB_THEME` | `arcade` | Default clip page theme for servers that never ran `!theme`: `arcade`, `minimal`, `night`, `vinyl`, `club`, `terminal`, `space` or `cobalt`. |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | empty | Optional Spotify Web API credentials, for playlists over 100 tracks. Both are needed. See [Spotify](spotify.md). |
| `YTDLP_AUTO_UPDATE` | `1` | Upgrade yt-dlp on container start (`0` to skip). Used by the container entrypoint only. |
| `METRICS` | `0` | Set to `1` to serve Prometheus metrics on `GET /metrics` (same port as the clip page). Off, the route answers 404. See [Troubleshooting](troubleshooting.md#health-check-and-metrics). |
| `DJ_SNAKE_TAG` | `latest` | Image tag to pull (Compose only), for example `1.0.0`. |

```{note}
An invalid `WEB_THEME` is ignored with a warning in the logs, and `arcade` is used instead.
```

## Notes

- **`PUBLIC_BASE_URL` matters.** It is only used to build the link the bot posts in Discord. If it is empty, the link points to `http://localhost:47823`, which only works on the machine running the bot.
- **`MEDIA_FORMAT`** replaces the old `AUDIO_FORMAT` variable, which is no longer read. The clip page needs a video stream, so choose a format that includes one.
- **`MAX_PLAYLIST_SIZE`** is also the limit for Spotify lists, on top of Spotify's own 100-track limit without credentials.
- **Never commit `.env`**, a `token` file or any real token.
