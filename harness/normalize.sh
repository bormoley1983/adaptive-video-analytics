#!/usr/bin/env bash
# AVA RTSP harness: one-time video normalization (E-3-T2 / B-019).
#
# Transcodes the four source clips to a uniform H.264 720p @ 10 FPS format
# under videos/normalized/. Publishers then loop these files with -c copy
# (no re-encode), keeping generator CPU near zero (ADR-2: no GPU, no
# generator bottleneck).
#
# Usage: ./harness/normalize.sh
#   Run once. Idempotent — skips files that already exist.

set -euo pipefail

VIDEOS_DIR="$(cd "$(dirname "$0")/../videos" && pwd)"
NORM_DIR="${VIDEOS_DIR}/normalized"
FPS=10
RES=1280x720
BITRATE=2M
GOP=30

# camera_id : source_file
declare -a SOURCES=(
  "cam-01:crosswalk-day-pedestrians.mp4"
  "cam-02:intersection-signal-day-vehicles.mp4"
  "cam-03:intersection-night.mp4"
  "cam-04:highway-fog-low-visibility.mp4"
)

mkdir -p "$NORM_DIR"

for entry in "${SOURCES[@]}"; do
  cam_id="${entry%%:*}"
  src_file="${entry#*:}"
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
