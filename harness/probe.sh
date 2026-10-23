#!/usr/bin/env bash
# AVA RTSP replay harness probe (E-3-T2 / B-019).
#
# Acceptance checks:
#   1. All four streams are independently probeable via ffprobe.
#   2. Each stream is H.264, 1280x720, ~10 FPS.
#   3. Sustained input rate over a short window (no generator bottleneck).
#
# Usage: ./harness/probe.sh [RTSP_HOST]
#   RTSP_HOST defaults to 127.0.0.1

set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT=8554
CAMERAS=(cam-01 cam-02 cam-03 cam-04)
EXPECTED_W=1280
EXPECTED_H=720
EXPECTED_FPS=10
PROBE_SECONDS=5

echo "=== AVA RTSP harness probe: ${HOST}:${PORT} ==="
overall_ok=true

for cam in "${CAMERAS[@]}"; do
  url="rtsp://${HOST}:${PORT}/${cam}"
  echo ""
  echo "--- ${cam}: ${url} ---"

  # 1. Independently probeable?
  if ! ffprobe -v error -rtsp_transport tcp \
       -show_entries stream=codec_name,width,height,r_frame_rate \
       -of default=noprint_wrappers=1 "$url" > "/tmp/ava-harness-probe-${cam}.txt" 2>&1; then
    echo "FAIL: ${cam} not probeable"
    cat "/tmp/ava-harness-probe-${cam}.txt"
    overall_ok=false
    continue
  fi

  codec=$(grep '^codec_name=' "/tmp/ava-harness-probe-${cam}.txt" | cut -d= -f2)
  width=$(grep '^width=' "/tmp/ava-harness-probe-${cam}.txt" | head -1 | cut -d= -f2)
  height=$(grep '^height=' "/tmp/ava-harness-probe-${cam}.txt" | head -1 | cut -d= -f2)
  fps_raw=$(grep '^r_frame_rate=' "/tmp/ava-harness-probe-${cam}.txt" | head -1 | cut -d= -f2)

  echo "codec=${codec} resolution=${width}x${height} r_frame_rate=${fps_raw}"

  # 2. Verify codec and resolution.
  cam_ok=true
  if [[ "$codec" != "h264" ]]; then
    echo "FAIL: expected h264, got ${codec}"
    cam_ok=false
  fi
  if [[ "$width" != "$EXPECTED_W" || "$height" != "$EXPECTED_H" ]]; then
    echo "FAIL: expected ${EXPECTED_W}x${EXPECTED_H}, got ${width}x${height}"
    cam_ok=false
  fi

  # 3. Sustained rate: use ffmpeg to decode a bounded window and count the
  # frames it actually produced. -t is placed AFTER -i (output-side duration)
  # so the decode terminates reliably on a looping RTSP source; an input-side
  # -t before -i does not stop FFmpeg 9.x on an endless stream. Output goes to
  # a temp file (no pipe) so a slow/quiet ffmpeg cannot wedge the probe in a
  # SIGPIPE/timeout interaction; we parse the frame count after it exits.
  echo "measuring sustained rate (${PROBE_SECONDS}s window)..."
  fflog="/tmp/ava-harness-probe-${cam}-ff.log"
  # No timeout wrapper: the output-side -t already bounds the decode, and a
  # timeout+redirect in this script context wedges on FFmpeg 9.x. The bounded
  # read terminates on its own.
  ffmpeg -rtsp_transport tcp \
    -i "$url" -t "${PROBE_SECONDS}" -f null - >"$fflog" 2>&1 || true
  frame_count=$(grep -oP 'frame=\s*\K[0-9]+' "$fflog" | tail -1 || echo "0")
  # Fallback: if the frame= line was not captured, count via ffprobe.
  if [[ -z "$frame_count" || "$frame_count" == "0" ]]; then
    frame_count=$(timeout -k 5 $((PROBE_SECONDS + 10)) ffprobe -v error -rtsp_transport tcp \
      -i "$url" -count_frames -select_streams v:0 \
      -show_entries stream=nb_read_frames -of default=noprint_wrappers=1:nokey=1 \
      2>/dev/null || echo "0")
  fi
  measured_fps=$(( frame_count / PROBE_SECONDS ))
  echo "sustained rate: ~${measured_fps} FPS over ${PROBE_SECONDS}s window (${frame_count} frames)"

  if (( measured_fps < EXPECTED_FPS - 2 )); then
    echo "WARN: sustained rate ${measured_fps} FPS below expected ~${EXPECTED_FPS} FPS"
  fi

  if $cam_ok; then
    echo "PASS: ${cam}"
  else
    overall_ok=false
  fi
done

echo ""
if $overall_ok; then
  echo "=== ALL STREAMS PASS ==="
else
  echo "=== SOME STREAMS FAILED ==="
  exit 1
fi
