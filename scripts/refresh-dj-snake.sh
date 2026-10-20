#!/bin/bash
# Refresh the bot to pick up the latest version of yt-dlp.
#
# Why: YouTube breaks yt-dlp extractors every few weeks,
# which shows up as "HTTP Error 403: Forbidden" on download. The Docker
# image freezes yt-dlp at build time; the entrypoint upgrades it on every start,
# so a plain restart is enough, no need to rebuild the image.
#
# Run from cron (the crontab of the user who manages Docker).

set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$COMPOSE_DIR/logs/refresh.log"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "=== $(date -Is) : restarting dj-snake ==="

cd "$COMPOSE_DIR"
docker compose restart dj-snake

# Give the entrypoint time to upgrade before reading the version.
sleep 30

version=$(docker exec dj-snake python -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo "unavailable")
echo "yt-dlp: $version"

# The POT token provider: without it, YouTube answers "Sign in to confirm
# you're not a bot". It runs all the time; just check that it is healthy.
pot_state=$(docker inspect --format '{{.State.Health.Status}}' dj-snake-pot-provider 2>/dev/null || echo "missing")
echo "pot-provider: $pot_state"
if [ "$pot_state" != "healthy" ]; then
    echo "WARNING: the POT provider is not healthy, trying to start it." >&2
    docker compose up -d pot-provider
fi

if docker ps --filter name=dj-snake --filter status=running --format '{{.Names}}' | grep -q dj-snake; then
    echo "OK: the container is running."
else
    echo "ERROR: the container is not running after the restart." >&2
    exit 1
fi
