# Configuration

DJ Snake is configured through environment variables. With Docker Compose, put them in a `.env` file next to `docker-compose.yml` (copy `.env.example`); Compose passes the file to the container. Without Docker, the bot loads `.env` itself. Variables are read once at startup, so restart the bot after changing them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | none (required) | Bot token. A `token` file is a fallback. |
| `COMMAND_PREFIX` | `!` | Prefix for text commands. |
| `MAX_PLAYLIST_SIZE` | `30` | Maximum tracks loaded from a playlist (YouTube, Spotify, SoundCloud, Bandcamp, Deezer or Apple Music). |
| `MAX_QUEUE_SIZE` | `200` | Maximum tracks waiting in a server's queue. |
| `MAX_TRACKS_PER_USER` | `0` | Maximum tracks one member may have waiting (`0` = unlimited). The clip page counts as one member. |
| `MAX_TRACK_MINUTES` | `0` | Longest single track accepted, in minutes (`0` = unlimited). |
| `PLAY_COOLDOWN_SECONDS` | `0` | Seconds a member must wait between two `/play` calls (`0` = none). |
| `MEDIA_FORMAT` | max 720p H.264 + m4a | yt-dlp format selector. The default is `bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best`. |
| `TEMP_DIR` | a fresh temporary directory | Where downloaded tracks are stored. Files are deleted once played. |
| `POT_BASE_URL` | `http://pot-provider:4416` | URL of the PO token provider. The default matches Docker Compose. |
| `WEB_PORT` | `47823` | Clip page port, inside the container and on the host. |
| `WEB_BIND` | `127.0.0.1` | Host IP the web port is published on (Compose only). Use a LAN IP or `0.0.0.0` for a reverse proxy or tunnel. |
| `PUBLIC_BASE_URL` | empty (`http://localhost:WEB_PORT`) | Public address used in the clip link the bot posts, for example `https://dj.example.com`. |
| `SESSION_IDLE_HOURS` | `6` | Hours without activity before a clip page link expires. |
| `IDLE_DISCONNECT_SECONDS` | `300` | Seconds the bot stays in voice once the queue runs out, before leaving (30 s instead when no human is left in the channel). |
| `AUTOPLAY` | `0` | Default of the per-session autoplay setting (`1`, `true`, `on` or `yes` to start with it on). Change it per session with `!autoplay` or the clip page. |
| `VOTESKIP_PERCENT` | `50` | Default vote-to-skip threshold, 1 to 100: more than this share of the listeners must vote. Change it per server with `/voteskip threshold`. |
| `STAY_247` | `0` | Default of the per-server 24/7 mode for servers that never ran `!stay` (`1`, `true`, `on` or `yes`): the bot never leaves voice on its own. A server needs a remembered voice channel to be rejoined after a restart. |
| `SLASH_SYNC_GUILD` | empty | Server ID. When set, slash commands are published to that server only, and show up at once (for development). Empty publishes them globally, which Discord can take up to an hour to show. |
| `DATA_DIR` | `/app/data` | Persistent data: per-server themes (`themes.json`), languages (`languages.json`), volumes (`volume.json`), vote-skip thresholds (`voteskip.json`), DJ roles (`permissions.json`), queue limits (`limits.json`), 24/7 mode (`stay.json`), saved playlists (`playlists.db`), logos (`logos/`), listening stats (`stats.db`) and cached lyrics (`lyrics/`). Compose mounts `./data` there. |
| `WEB_CONTROL` | `everyone` | Who gets the clip page's control link. `everyone`: the status message shows it next to the view-only link. `dj`: it shows only the view-only link, and DJs get the control link by DM with `!weblink`. A server with a DJ role always behaves as `dj`. See [Who controls the music](clip-page.md#who-controls-the-music). |
| `WEB_THEME` | `arcade` | Default clip page theme for servers that never ran `!theme`: `arcade`, `minimal`, `night`, `vinyl`, `club`, `terminal`, `space` or `cobalt`. |
| `LYRICS` | `1` | Synced lyrics on the clip page, from [LRCLIB](https://lrclib.net). `0` turns them off: the bot sends no lookups and the page hides the Lyrics button. See [Lyrics](clip-page.md#lyrics). |
| `BOT_LANGUAGE` | `en` | Language of the bot's messages and of the clip page for servers that never ran `!language`: `en` (English), `fr` (French), `es` (Spanish) or `de` (German). |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | empty | Optional Spotify Web API credentials, for playlists over 100 tracks. Both are needed. See [Spotify](spotify.md). |
| `YTDLP_COOKIES` | empty | Path to a Netscape `cookies.txt` for age-restricted and members-only videos. Copied to a private temp file at startup. Use a throwaway Google account: see [Keeping YouTube working](youtube.md). |
| `YTDLP_AUTO_UPDATE` | `1` | Upgrade yt-dlp on container start (`0` to skip). Used by the container entrypoint only. |
| `METRICS` | `0` | Set to `1` to serve Prometheus metrics on `GET /metrics` (same port as the clip page). Off, the route answers 404. See [Troubleshooting](troubleshooting.md#health-check-and-metrics). |
| `DJ_SNAKE_TAG` | `latest` | Image tag to pull (Compose only), for example `1.0.0`. |

```{note}
An invalid `WEB_THEME` is ignored with a warning in the logs, and `arcade` is used instead. The same goes for an invalid `WEB_CONTROL` (falls back to `everyone`) and an unsupported `BOT_LANGUAGE` (falls back to `en`).
```

## Notes

- **`PUBLIC_BASE_URL` matters.** It is only used to build the link the bot posts in Discord. If it is empty, the link points to `http://localhost:47823`, which only works on the machine running the bot.
- **`MEDIA_FORMAT`** replaces the old `AUDIO_FORMAT` variable, which is no longer read. The clip page needs a video stream, so choose a format that includes one.
- **`MAX_PLAYLIST_SIZE`** is also the limit for Spotify lists, on top of Spotify's own 100-track limit without credentials.
- **Never commit `.env`**, a `token` file or any real token.
