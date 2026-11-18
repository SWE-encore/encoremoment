#!/usr/bin/env bash
# Generate a test clip with a visible timestamp and a beep every second.
# Usage: tools/make_test_video.sh [output.mp4] [seconds]
# H.264/AAC in MP4 by default; use a .webm name for VP8/Vorbis.
set -euo pipefail
out="${1:-samples/test-movie.mp4}"
secs="${2:-30}"
mkdir -p "$(dirname "$out")"
case "$out" in
  *.webm) codecs=(-c:v libvpx -b:v 1M -c:a libvorbis) ;;
  *)      codecs=(-c:v libx264 -preset veryfast -pix_fmt yuv420p -c:a aac -movflags +faststart) ;;
esac
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=${secs}" \
  -f lavfi -i "sine=frequency=880:beep_factor=4:sample_rate=48000:duration=${secs}" \
  -vf "drawtext=text='%{pts\:hms}':fontsize=96:fontcolor=white:box=1:boxcolor=black@0.6:x=(w-tw)/2:y=h-th-40" \
  "${codecs[@]}" -shortest "$out"
echo "$out"
