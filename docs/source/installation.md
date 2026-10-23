# Installation

DJ Snake is meant to be self-hosted. The recommended setup is Docker Compose, which runs the bot together with a PO token provider sidecar that keeps YouTube downloads working.

## 1. Create the Discord bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application and add a bot.
2. Under **Bot**, enable the **Message Content Intent**. It is the only privileged intent needed; without it the bot ignores your commands.
3. Copy the bot token. You will put it in `.env` as `DISCORD_TOKEN`. Never share or commit it.
4. Invite the bot to your server with the `bot` and `applications.commands` scopes (the latter makes the `/` commands appear) and these permissions: **View Channels, Send Messages, Read Message History, Add Reactions, Connect, Speak**.

## 2. Docker Compose quick start

Using the prebuilt image (recommended):

```bash
git clone https://github.com/Theroxat/DJ_Snake.git
cd DJ_Snake
cp .env.example .env        # set DISCORD_TOKEN
docker compose pull && docker compose up -d
docker compose logs -f dj-snake
```

The prebuilt image is `ghcr.io/theroxat/dj_snake:latest` (amd64 and arm64). To pin a version, set `DJ_SNAKE_TAG` in `.env`, for example `DJ_SNAKE_TAG=1.0.0`.

Then join a voice channel and send:

```
!play https://www.youtube.com/watch?v=...
```

The bot joins, posts the [clip page](clip-page.md) link and starts playing.

### Building from source

To run your local changes, build the image from your checkout instead of pulling it:

```bash
docker compose up -d --build
```

The compose file keeps a `build: .` entry for exactly this, and tags the result with the same image name.

### What Compose starts

- **dj-snake**: the bot and the clip web server. It reads your `.env`, mounts `./data` for persistent data (themes, logos, listening stats), and publishes the web port on `WEB_BIND` (default `127.0.0.1`).
- **pot-provider**: a [bgutil](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) Proof-of-Origin token provider. It is not exposed on the host; only the bot reaches it over the Compose network. See [Keeping YouTube working](youtube.md).

```{note}
The container runs as UID 1000, which must be able to write the `./data` directory, otherwise themes and stats cannot be saved.
```

## Running without Docker

You need Python 3.10 or newer, `ffmpeg` and `deno` on your `PATH`, and a reachable PO token provider (for example the `pot-provider` container with its port published). Point `POT_BASE_URL` at it, or expect intermittent "Sign in to confirm you're not a bot" errors, because the default `http://pot-provider:4416` only resolves inside Compose.

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set DISCORD_TOKEN and POT_BASE_URL
python3 bot.py
```

On Python 3.13 and newer, `audioop-lts` is also needed for voice (it is listed in `requirements.txt`).

## Making the clip page reachable

By default the clip page is only reachable from the machine running the bot. See [Exposing the page](clip-page.md#exposing-the-page) to put it behind a reverse proxy or a tunnel.

## Updating

```bash
cd DJ_Snake
git pull
docker compose pull && docker compose up -d
```

See the [changelog](changelog.md) for what changed between versions.
