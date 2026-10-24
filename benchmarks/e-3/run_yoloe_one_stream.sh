#!/usr/bin/env bash
# T1 Gate 3: YOLOE one-stream DeepStream validation run.
# Requires container 'deepstream-e3' running with mounts:
#   -v <repo>/benchmarks/e-3:/host-bench
#   -v /tmp/e3-yoloe-output:/host-out
#   -v /home/admaccess/yoloe-export:/host-models
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${AVA_OUT_DIR:-/tmp/e3-yoloe-output}"
mkdir -p "$OUT_DIR"

# Stage assets into container /tmp
docker cp "$SCRIPT_DIR/config_infer_primary_yoloe.txt" deepstream-e3:/tmp/config_infer_primary_yoloe.txt
docker cp "$SCRIPT_DIR/t1-yoloe-one-stream.txt" deepstream-e3:/tmp/t1-yoloe-one-stream.txt
docker cp "$SCRIPT_DIR/libnvdsinfer_custom_yoloe.so" deepstream-e3:/tmp/libnvdsinfer_custom_yoloe.so
docker cp /home/admaccess/yoloe-export/yoloe-26s-seg-det-only.engine deepstream-e3:/tmp/yoloe-26s-seg-det-only.engine
docker cp "$SCRIPT_DIR/../../videos/crosswalk-day-pedestrians.mp4" deepstream-e3:/tmp/crosswalk.mp4

# Clear previous output
rm -f "$OUT_DIR/detections.jsonl"

# Run pipeline with letterbox env vars for frame-space output
docker exec deepstream-e3 bash -c "
  export AVA_DETECTION_JSONL=/host-out/detections.jsonl
  export AVA_SOURCE_WIDTH=${AVA_SOURCE_WIDTH:-1920}
  export AVA_SOURCE_HEIGHT=${AVA_SOURCE_HEIGHT:-1080}
  deepstream-app -c /tmp/t1-yoloe-one-stream.txt
"

echo "YOLOE pipeline complete. Output: $OUT_DIR/detections.jsonl"
wc -l "$OUT_DIR/detections.jsonl" 2>/dev/null || echo "No output file found"
