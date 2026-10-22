# Keeping YouTube working

YouTube changes often and breaks yt-dlp extractors every few weeks. DJ Snake has several defenses so that a long-running deployment keeps working with little care.

## yt-dlp auto-update

A Docker image freezes yt-dlp at build time. To avoid stale extractors, `docker-entrypoint.sh` upgrades yt-dlp on every container start. It is best effort: if PyPI is unreachable the bot starts with the bundled version. Set `YTDLP_AUTO_UPDATE=0` to skip it.

The typical symptom of an outdated yt-dlp is `HTTP Error 403: Forbidden` while downloading, even though the video is found.

## Weekly refresh script

A container only upgrades yt-dlp when it starts. To make a long-running deployment pick up updates, run `scripts/refresh-dj-snake.sh` weekly from the cron of the user who manages Docker. It restarts the `dj-snake` container, waits for the upgrade, logs the yt-dlp version and checks the token provider. It writes to `logs/refresh.log` in the project folder.

```text
0 4 * * 1 /path/to/DJ_Snake/scripts/refresh-dj-snake.sh
```

## PO tokens ("Sign in to confirm you're not a bot")

YouTube challenges traffic from datacenter IPs and demands a Proof-of-Origin (PO) token. This is intermittent and different from the 403 error. The `pot-provider` service in `docker-compose.yml` mints those tokens, and the `bgutil-ytdlp-pot-provider` plugin (pinned in `requirements.txt`) is the yt-dlp side that asks it.

- `POT_BASE_URL` tells the bot where the provider is. The default, `http://pot-provider:4416`, matches Compose.
- **The plugin version must match the `pot-provider` image tag**, so bump `requirements.txt` and `docker-compose.yml` together. The entrypoint only ever upgrades yt-dlp, never the plugin.
- Because the challenge is intermittent, the bot also retries transient download failures with backoff, but does not waste retries on deleted or private videos.

## Challenge solving

The bot lets yt-dlp fetch the EJS challenge solver (`--remote-components ejs:github`), which needs the `deno` runtime. The Docker image includes it. Without it, signature challenges fail and some formats become unavailable. When running [without Docker](installation.md#running-without-docker), install `deno` yourself.

## Diagnosing a failure

Reproduce the download directly inside the container:

```bash
docker exec dj-snake python -m yt_dlp --remote-components ejs:github \
    -f bestaudio/best -o /tmp/t.%(ext)s --no-playlist "<url>"
```

See also [Troubleshooting](troubleshooting.md).
