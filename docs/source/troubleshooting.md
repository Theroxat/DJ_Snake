# Troubleshooting and FAQ

## Troubleshooting

**The bot ignores commands.**
: Enable the **Message Content Intent** in the Discord Developer Portal, and check that the bot can view, read and send messages in that channel. Also check `COMMAND_PREFIX`.

**Voice error 4006, or the bot is connected but silent.**
: Send `!reset` to force a clean voice reconnect.

**`HTTP Error 403: Forbidden` when downloading.**
: yt-dlp is outdated. Restart the container (it upgrades yt-dlp on start) or run `scripts/refresh-dj-snake.sh`. See [Keeping YouTube working](youtube.md).

**"Sign in to confirm you're not a bot".**
: Check that `pot-provider` is healthy with `docker compose ps` and that `POT_BASE_URL` points at it. Outside Compose the default URL does not resolve.

**Is the bot healthy?**
: Run `docker compose ps`: `dj-snake` shows `healthy`, `starting` (the first 60 seconds) or `unhealthy`. See [Health check and metrics](#health-check-and-metrics).

**The clip link does not open.**
: Check `WEB_BIND` and `WEB_PORT`, that your reverse proxy or tunnel reaches the port, and that `PUBLIC_BASE_URL` matches the public address. See [Exposing the page](clip-page.md#exposing-the-page).

**The clip link opens `localhost`.**
: Set `PUBLIC_BASE_URL` to the address people use to reach the page.

**The link says the session is unknown.**
: It expired after `SESSION_IDLE_HOURS` without activity. Run `!play` again for a fresh one.

**`!theme edit` or `!weblink` says it cannot send a direct message.**
: Allow DMs from server members in your Discord privacy settings, then run it again.

**The page shows "View only" and its controls are gone.**
: You opened the view-only link. Ask a DJ, or run `!weblink` if you are one, for the control link.

**The clip link stopped working after `!djrole`.**
: Setting a DJ role replaces the public control link. The view-only link in the status message still works; DJs get the new control link with `!weblink`.

**Themes, logos or stats are not saved.**
: `DATA_DIR` (`./data` with Compose) must be writable by UID 1000, the user the container runs as.

**A Spotify link fails.**
: Short `spotify.link` URLs are refused; use the `open.spotify.com` address. Private playlists cannot be read. See [Spotify](spotify.md).

## Health check and metrics

The web server answers `GET /healthz` on `WEB_PORT`. It returns `200` with a small JSON body when the bot is connected to Discord, and `503` with the reason otherwise (`not ready`, `websocket closed`, `closed`). It never contains tokens, server names or ids.

The body lists each gateway shard (`shards`: id, `up` or `down`, latency). If only some shards are down, `status` and `discord` are both `degraded` and the code stays `200` (a shard briefly closed by a routine gateway resume still counts as up for 30 seconds); `503` is returned only when every shard is down. `latency_ms` is the average of the shards that are up.

```bash
curl -s http://127.0.0.1:47823/healthz
# {"status": "ok", "discord": "connected", "latency_ms": 42.0, "guilds": 3, "players": 1, "uptime_s": 5120, "version": "1.3.0"}
```

The Docker image checks it every 30 seconds (60 seconds of grace at startup, 3 failures in a row to turn `unhealthy`, so a routine Discord reconnect does not trip it). `docker compose ps` shows the result, and `docker inspect --format '{{json .State.Health}}' dj-snake` shows the last probes. An `unhealthy` container usually means a bad `DISCORD_TOKEN`, missing intents or no network to Discord: read `docker compose logs dj-snake`. `restart: unless-stopped` does not restart on `unhealthy`; restart it yourself, or use a tool that does.

Set `METRICS=1` to also serve `GET /metrics` in the Prometheus text format: `djsnake_active_players`, `djsnake_queued_tracks`, `djsnake_downloads_in_progress`, `djsnake_web_viewers`, `djsnake_discord_latency_seconds`, the per-shard `djsnake_shards`, `djsnake_shard_up{shard="N"}` and `djsnake_shard_latency_seconds{shard="N"}`, and the counter `djsnake_tracks_played_total`. The metrics carry no server data, but they are served on the same port as the clip page, so keep `WEB_BIND` on `127.0.0.1` or block `/metrics` in your reverse proxy if the port is public.

## FAQ

**Does it need a Spotify account?**
: No. Spotify links are only used to read the track list; audio always comes from YouTube.

**Does it stream audio?**
: No. Each track is fully downloaded before it enters the queue, and the file is deleted after it plays or on `!stop`.

**What is the maximum video quality?**
: 720p by default. Change it with `MEDIA_FORMAT`.

**Can several servers use the same bot?**
: Yes. Every server has its own queue, session, theme and stats.

**Can I use a different command prefix?**
: Yes, set `COMMAND_PREFIX`.

**Who can control the music from the clip page?**
: Anyone who has the link. Treat it as a secret.

**Is it legal to use?**
: The project is for personal use. Respect YouTube's Terms of Service and the copyright of the content you play; you are responsible for how you use it.

Still stuck? [Open an issue](https://github.com/Theroxat/DJ_Snake/issues) with the relevant log lines (`docker compose logs dj-snake`), and remove any token first.
