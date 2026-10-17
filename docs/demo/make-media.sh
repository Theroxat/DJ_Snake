#!/usr/bin/env bash
# Regenerates the tiny looping demo clips (VP9/WebM, no audio) with ffmpeg.
# Run only if you want different visuals; the clips are committed.
# Uses the local ffmpeg, else the project's Docker image (dj-snake:latest).
set -euo pipefail
cd "$(dirname "$0")/media"
if command -v ffmpeg >/dev/null; then
  FF=(ffmpeg)
else
  FF=(docker run --rm -v "$PWD:/w" -w /w --entrypoint ffmpeg dj-snake:latest)
fi
i=0
for pal in "0xff2e63:0x252a34:0x08d9d6:0xffb703" "0x6a11cb:0x2575fc:0x0b132b:0xf72585" \
           "0xf6d365:0xfda085:0x3a0ca3:0x1b1b2f" "0x00f5a0:0x00d9f5:0x0f2027:0x203a43" \
           "0xff512f:0xdd2476:0x1f1c2c:0x928dab"; do
  i=$((i + 1))
  IFS=: read -r a b c d <<<"$pal"
  "${FF[@]}" -y -v error -f lavfi \
    -i "gradients=s=480x270:r=15:d=60:n=4:c0=$a:c1=$b:c2=$c:c3=$d:speed=0.03:seed=$i" \
    -vf "noise=alls=6:allf=t,format=yuv420p" -c:v libvpx-vp9 -b:v 0 -crf 42 \
    -deadline realtime -cpu-used 8 -g 150 -an "clip$i.webm"
done
