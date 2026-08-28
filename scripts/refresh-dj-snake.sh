#!/bin/bash
# Rafraîchit le bot pour récupérer la dernière version de yt-dlp.
#
# Pourquoi : YouTube casse les extracteurs yt-dlp toutes les quelques semaines,
# ce qui produit un « HTTP Error 403: Forbidden » au téléchargement. L'image
# Docker fige yt-dlp au build ; l'entrypoint le met à jour à chaque démarrage,
# donc un simple restart suffit — inutile de reconstruire l'image.
#
# Appelé par cron. Voir la crontab de l'utilisateur.

set -euo pipefail

COMPOSE_DIR="/opt/dj-snake"
LOG="/opt/dj-snake/logs/refresh.log"

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "=== $(date -Is) : redémarrage de dj-snake ==="

cd "$COMPOSE_DIR"
docker compose restart dj-snake

# Laisser l'entrypoint faire sa mise à jour avant de relever la version.
sleep 30

version=$(docker exec dj-snake python -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo "indisponible")
echo "yt-dlp: $version"

if docker ps --filter name=dj-snake --filter status=running --format '{{.Names}}' | grep -q dj-snake; then
    echo "OK : le conteneur tourne."
else
    echo "ERREUR : le conteneur ne tourne pas après le redémarrage." >&2
    exit 1
fi
