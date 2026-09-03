#!/bin/sh
# YouTube regularly breaks older yt-dlp extractors: a version that is a few
# weeks old starts returning "HTTP Error 403: Forbidden" on the media download.
# A Docker image freezes yt-dlp at build time, so refresh it on every start.
#
# Best effort by design: if PyPI is unreachable the bot still boots with the
# version baked into the image rather than failing to start. pip's own exit
# code is not a reliable signal here -- offline it still exits 0 -- so the
# result is reported by comparing the version before and after.
#
# Only yt-dlp is updated. bgutil-ytdlp-pot-provider stays pinned because it has
# to match the pot-provider server image (see requirements.txt).
#
# Set YTDLP_AUTO_UPDATE=0 to skip the update (offline runs, CI smoke tests).

set -e

ytdlp_version() {
    python -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo "unknown"
}

if [ "${YTDLP_AUTO_UPDATE:-1}" = "1" ]; then
    before=$(ytdlp_version)
    echo "[entrypoint] Updating yt-dlp (currently ${before})..."

    # pip's warnings stay on the log: they are the visible trace of a failed
    # update, since the exit code will not show it.
    pip install --user --quiet --no-cache-dir --upgrade yt-dlp || \
        echo "[entrypoint] WARNING: pip failed." >&2

    after=$(ytdlp_version)
    if [ "$before" != "$after" ]; then
        echo "[entrypoint] yt-dlp updated: ${before} -> ${after}"
    else
        echo "[entrypoint] yt-dlp unchanged at ${after} (already current, or PyPI unreachable)"
    fi
fi

# Report the PO token provider link. A silent failure here is expensive: the bot
# starts fine and then hits "Sign in to confirm you're not a bot" under load, so
# make the state visible in the logs at boot.
pot_url="${POT_BASE_URL:-http://pot-provider:4416}"
if python -c "
import sys, urllib.request
try:
    urllib.request.urlopen('${pot_url}/ping', timeout=5)
except Exception as e:
    sys.exit(1)
" 2>/dev/null; then
    echo "[entrypoint] PO token provider reachable at ${pot_url}"
else
    echo "[entrypoint] WARNING: PO token provider unreachable at ${pot_url}." >&2
    echo "[entrypoint] Downloads may fail with \"Sign in to confirm you're not a bot\"." >&2
fi

exec "$@"
