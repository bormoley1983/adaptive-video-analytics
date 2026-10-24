#!/usr/bin/env bash
set -euo pipefail

export AVA_DETECTION_JSONL=${AVA_DETECTION_JSONL:-/tmp/results/detections.jsonl}
export AVA_SOURCE_WIDTH=${AVA_SOURCE_WIDTH:-1920}
export AVA_SOURCE_HEIGHT=${AVA_SOURCE_HEIGHT:-1080}
exec deepstream-app -c /tmp/t1-yolo26n-one-stream.txt
