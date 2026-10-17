#!/usr/bin/env bash
# Rebuilds the demo footage in media/ from the original films (CC-BY, see the
# credits in docs/README.md). Output: clipN.webm (640x360 VP9, no audio, ~25 s,
# metadata stripped) and thumbN.jpg (real YouTube thumbnail, 320x180).
#
# Usage: make-media.sh [SRC_DIR]
#   SRC_DIR holds rv_<id>.mp4 (video, <= 720p) and rv_<id>.jpg (YouTube thumbnail).
#   Fetch them with yt-dlp, e.g. per id:
#     yt-dlp -f "bv*[height<=720]" --write-thumbnail --convert-thumbnails jpg \
#            -o "SRC_DIR/rv_<id>.%(ext)s" https://www.youtube.com/watch?v=<id>
# Uses the local ffmpeg, else the project's Docker image (dj-snake:latest).
set -euo pipefail
SRC=$(cd "${1:-.}" && pwd)
OUT="$(cd "$(dirname "$0")" && pwd)/media"
mkdir -p "$OUT"
if command -v ffmpeg >/dev/null; then
  FF=(ffmpeg)
else
  FF=(docker run --rm -v "$SRC:/src:ro" -v "$OUT:/out" --entrypoint ffmpeg dj-snake:latest)
  SRC=/src; OUT=/out
fi
# n|youtube id|start (s)|length (s)|thumbnail has baked-in letterbox bars (1/0)
TRACKS=(
  "1|SkVqJ1SGeL0|76|25|0"   # Caminandes 3: Llamigos
  "2|Y-rmzh0PI3c|528|25|0"  # Cosmos Laundromat
  "3|aqz-KE-bpKQ|188|25|0"  # Big Buck Bunny
  "4|R6MlUcmOul8|345|25|1"  # Tears of Steel
  "5|eRsGyueVLvQ|306|25|0"  # Sintel
)
for t in "${TRACKS[@]}"; do
  IFS='|' read -r n id ss len bars <<<"$t"
  TC=""; [ "$bars" = 1 ] && TC="crop=iw:ih*0.76:0:ih*0.12,"
  # crop to 16:9 (scope films have black bars baked in), then scale
  VF="crop='min(iw,ih*16/9)':'min(ih,iw*9/16)',scale=640:360,fps=24,format=yuv420p"
  "${FF[@]}" -y -v error -ss "$ss" -t "$len" -i "$SRC/rv_$id.mp4" -vf "$VF" \
    -c:v libvpx-vp9 -b:v 0 -crf 41 -row-mt 1 -g 96 -an \
    -map_metadata -1 -fflags +bitexact -flags:v +bitexact "$OUT/clip$n.webm"
  "${FF[@]}" -y -v error -i "$SRC/rv_$id.jpg" \
    -vf "${TC}crop='min(iw,ih*16/9)':'min(ih,iw*9/16)',scale=320:180" -q:v 5 \
    -map_metadata -1 "$OUT/thumb$n.jpg"
done
ls -l "$(cd "$(dirname "$0")" && pwd)/media"
