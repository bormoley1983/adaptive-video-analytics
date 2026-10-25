#!/usr/bin/env bash
# AVA RTSP harness: one-time video normalization (E-3-T2 / B-019; extended for
# E-3-T5).
#
# Produces a uniform H.264 720p @ 10 FPS clip under videos/normalized/ for every
# camera in the manifest. cam-01..cam-04 are the four distinct source clips;
# cam-05..cam-50 reuse those same four sources round-robin so capacity points
# (16/25/50) can be measured without new footage. Publishers then loop these
# files with -c copy (no re-encode), keeping generator CPU near zero (ADR-2: no
# GPU, no generator bottleneck).
#
# Usage: ./harness/normalize.sh [N]
#   N is the number of cameras to normalize (default 50). Idempotent — skips
#   files that already exist.

set -euo pipefail

VIDEOS_DIR="$(cd "$(dirname "$0")/../videos" && pwd)"
NORM_DIR="${VIDEOS_DIR}/normalized"
FPS=10
RES=1280x720
BITRATE=2M
GOP=30
NUM_CAMS="${1:-50}"

# The four distinct source clips, cycled round-robin across all cameras.
declare -a SOURCE_FILES=(
  "crosswalk-day-pedestrians.mp4"
  "intersection-signal-day-vehicles.mp4"
  "intersection-night.mp4"
  "highway-fog-low-visibility.mp4"
)

mkdir -p "$NORM_DIR"

for (( n=1; n<=NUM_CAMS; n++ )); do
  cam_id=$(printf "cam-%02d" "$n")
  src_file="${SOURCE_FILES[$(( (n - 1) % ${#SOURCE_FILES[@]} ))]}"
  src_path="${VIDEOS_DIR}/${src_file}"
  dst_path="${NORM_DIR}/${cam_id}.mp4"

  if [[ ! -f "$src_path" ]]; then
    echo "SKIP: ${src_file} not found"
    continue
  fi

  if [[ -f "$dst_path" ]]; then
    echo "OK (exists): ${cam_id}.mp4"
    continue
  fi

  echo "Transcoding ${src_file} -> ${cam_id}.mp4 (${RES}@${FPS}fps, H.264, ${BITRATE})..."
  ffmpeg -y -i "$src_path" \
    -c:v libx264 -preset medium -tune zerolatency \
    -s "${RES}" -r "${FPS}" -g "${GOP}" -b:v "${BITRATE}" -pix_fmt yuv420p \
    -an -movflags +faststart \
    "$dst_path" 2> "/tmp/ava-harness-normalize-${cam_id}.log"

  if [[ $? -ne 0 ]]; then
    echo "FAIL: ${cam_id} transcode failed (see /tmp/ava-harness-normalize-${cam_id}.log)"
    exit 1
  fi

  size=$(du -h "$dst_path" | cut -f1)
  echo "DONE: ${cam_id}.mp4 (${size})"
done

echo ""
echo "=== Normalized clips ==="
ls -lh "${NORM_DIR}/"*.mp4 2>/dev/null || echo "(none)"
echo ""
echo "Publishers can now loop these with -c copy (no re-encode)."
