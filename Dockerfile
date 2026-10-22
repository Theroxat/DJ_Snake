FROM python:3.15-slim

# System dependencies:
#  - ffmpeg   : audio decoding/transcoding for playback
#  - libopus0 : Opus codec, required by discord.py to encode voice
#  - ca-certificates : HTTPS to YouTube/Discord
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libopus0 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: JavaScript runtime that recent yt-dlp versions need to reliably extract
# YouTube streams (yt-dlp auto-detects it on PATH).
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY bot.py .
COPY djsnake/ djsnake/
COPY web/ web/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Run as an unprivileged user. UID 1000 matches the usual first host user, so
# the bind-mounted ./data directory (themes, logos) is writable by the bot.
RUN useradd --create-home --uid 1000 botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app
USER botuser

# Unbuffered output so logs show up immediately in `docker compose logs`.
ENV PYTHONUNBUFFERED=1

# The entrypoint refreshes yt-dlp before launching (see docker-entrypoint.sh);
# `pip install --user` writes here, which is why botuser owns its home.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "bot.py"]
