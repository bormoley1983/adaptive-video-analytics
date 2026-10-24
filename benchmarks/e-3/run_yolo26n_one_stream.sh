#!/usr/bin/env bash
set -euo pipefail

export AVA_DETECTION_JSONL=${AVA_DETECTION_JSONL:-/tmp/results/detections.jsonl}
exec deepstream-app -c /tmp/t1-yolo26n-one-stream.txt
