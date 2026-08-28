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
# Set YTDLP_AUTO_UPDATE=0 to skip this (offline runs, CI smoke tests).

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

exec "$@"
