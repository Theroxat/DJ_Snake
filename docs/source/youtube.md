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

## YouTube cookies (age-restricted and members-only videos)

Optional. Without cookies the bot cannot play age-restricted, members-only or sign-in-required videos; it says so and points here. With `YTDLP_COOKIES` set to a Netscape-format `cookies.txt`, every yt-dlp call (download, playlist, search, autoplay) uses it.

> **Warning: use a throwaway Google account, never your main one.** YouTube may ban or lock accounts that are used by bots, and the cookies give full access to the account. Treat the file like a password: mount it read-only, never commit it, and keep it out of `./data` (backups archive everything there).

1. Log in to YouTube with the throwaway account in a private/incognito window, then export `youtube.com` cookies with a browser extension that writes Netscape `cookies.txt` (for example "Get cookies.txt LOCALLY"), and close the window. On a desktop with yt-dlp you can instead run `yt-dlp --cookies-from-browser firefox --cookies cookies.txt` on any URL.
2. Mount the file read-only and set the variable (see the commented lines in `docker-compose.yml`):

```yaml
    volumes:
      - ./cookies.txt:/app/cookies.txt:ro
```

```
YTDLP_COOKIES=/app/cookies.txt
```

At startup the bot checks that the file exists and looks like Netscape format (a warning is logged otherwise, never its contents), then copies it to a private temporary file (mode 0600). yt-dlp updates that copy, so your file is never modified and a read-only mount works. Cookies expire or get rotated by YouTube: when age-restricted videos fail again, export a fresh file and restart the bot.

## Diagnosing a failure

Reproduce the download directly inside the container:

```bash
docker exec dj-snake python -m yt_dlp --remote-components ejs:github \
    -f bestaudio/best -o /tmp/t.%(ext)s --no-playlist "<url>"
```

See also [Troubleshooting](troubleshooting.md).
