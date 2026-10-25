#!/usr/bin/env bash
# Run one AVA live sweep point in a GPU container (E-3-T5).
#
# Mirrors the root docker-compose worker pattern (image ava/e1:local,
# entrypoint "ava") but for the live multi-camera path. Host networking is
# required so the worker reaches MediaMTX on host loopback (rtsp://127.0.0.1:8554)
# and so Prometheus can scrape the /metrics endpoint at host 9464 without any
# URL rewrite or port mapping.
#
# Dev flags enabled for every run:
#   - telemetry.log_level: debug        (verbose structured logs on stdout)
#   - prometheus metrics on 0.0.0.0:9464 (B-022 dashboard live gauges)
#   - attached stdout (-it) so logs stream live and are also captured to a file
#
# Usage:
#   ./harness/run_live_container.sh <config.yaml> [duration_s]
#
# Example:
#   ./harness/run_live_container.sh configs/run-e3-cap-4-conv-shared.yaml 90
#
# The duration override (optional) shortens live.duration_s for smoke runs; the
# config file itself is left untouched.

set -euo pipefail

CONFIG="${1:?usage: run_live_container.sh <config.yaml> [duration_s]}"
DURATION_S="${2:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="ava/e1:local"
GPU_INDEX="${AVA_GPU:-0}"

# Resolve the config path relative to the repo root.
case "$CONFIG" in
  /*) ABS_CONFIG="$CONFIG" ;;
  *)  ABS_CONFIG="$ROOT/$CONFIG" ;;
esac

if [[ ! -f "$ABS_CONFIG" ]]; then
  echo "config not found: $ABS_CONFIG" >&2
  exit 1
fi

# The container always reads the config from this fixed path; we bind-mount the
# host file (or a temp duration-overridden copy) onto it.
CONTAINER_CONFIG="/tmp/ava-live-config.yaml"

# Derive a stable run label from the config file name (e.g. run-e3-cap-4-conv-shared).
LABEL="$(basename "$CONFIG" .yaml)"

# The config's output_dir is the authoritative results location for this point
# (e.g. results/e3-cap-4-conv-shared). Read it so the shell-side log lands in the
# same folder as the container-written events.jsonl / metrics.json, keeping all
# artifacts for one sweep point together. Fall back to the label if absent.
CONFIG_OUTPUT_DIR="$(python3 -c "import yaml;print(yaml.safe_load(open('$ABS_CONFIG')).get('output_dir',''))" 2>/dev/null || true)"
if [[ -n "$CONFIG_OUTPUT_DIR" ]]; then
  RESULTS_DIR="$ROOT/${CONFIG_OUTPUT_DIR#./}"
else
  RESULTS_DIR="$ROOT/results/$LABEL"
fi
mkdir -p "$RESULTS_DIR"
LOG_FILE="$RESULTS_DIR/run.log"

# Host file that gets bind-mounted onto CONTAINER_CONFIG. Defaults to the real
# config; a duration override swaps in a temp copy so the committed config keeps
# its full steady-state duration.
HOST_CONFIG_FILE="$ABS_CONFIG"

if [[ -n "$DURATION_S" ]]; then
  HOST_CONFIG_FILE="$(mktemp /tmp/ava-live-XXXX.yaml)"
  python3 - "$ABS_CONFIG" "$DURATION_S" > "$HOST_CONFIG_FILE" <<'PY'
import sys, yaml
src, dur = sys.argv[1], int(sys.argv[2])
with open(src) as fh:
    cfg = yaml.safe_load(fh)
cfg.setdefault("live", {})["duration_s"] = dur
yaml.safe_dump(cfg, sys.stdout, sort_keys=False)
PY
fi

CMD=(live --config "$CONTAINER_CONFIG" --gpu "$GPU_INDEX")

echo "=== AVA live container run ==="
echo "image:      $IMAGE"
echo "config:     $ABS_CONFIG (mounted at $CONTAINER_CONFIG)"
echo "gpu:        $GPU_INDEX (CUDA_VISIBLE_DEVICES=$GPU_INDEX)"
echo "network:    host"
echo "results:    $RESULTS_DIR"
echo "log:        $LOG_FILE"
echo "duration_s: ${DURATION_S:-<from config>}"
echo "================================"

# Host networking + single-GPU reservation. stdout is streamed live and tee'd
# to a durable log for review; --rm removes the container on exit.
#
# The config's output_dir is relative (results/<label>). The image WORKDIR is
# /app, so a relative path would land in the ephemeral container FS and be lost
# with --rm. -w / makes `results/...` resolve onto the mounted /results volume,
# keeping committed configs portable while outputs persist to the host worktree.
# Run as the invoking user (not root) so files written under the mounted
# /results volume are owned by that user from the start — no post-run chown
# needed, and the next run's cleanup can remove them without sudo. The models
# mount is read-only; /tmp (config mount point) is world-writable.
#
# The prompted detector copies its MobileCLIP text-encoder asset into
# Ultralytics' WEIGHTS_DIR at load time. With -w / and no git repo, that dir
# resolves to the relative path `weights` -> /weights, which the host user
# cannot create under /. We bind-mount a per-run writable temp dir at /weights
# so the copy succeeds; it is removed after the run.
WEIGHTS_TMP="$(mktemp -d /tmp/ava-weights-XXXX)"
# Consolidated cleanup: remove the per-run weights dir and (if a duration
# override was used) the temp config copy.
trap 'rm -rf "$WEIGHTS_TMP"; [[ -n "${HOST_CONFIG_FILE:-}" && "$HOST_CONFIG_FILE" != "$ABS_CONFIG" ]] && rm -f "$HOST_CONFIG_FILE"' EXIT

docker run --rm \
  --name "ava-live-$LABEL" \
  --network host \
  --gpus "\"device=$GPU_INDEX\"" \
  -w / \
  --user "$(id -u):$(id -g)" \
  -e CUDA_VISIBLE_DEVICES="$GPU_INDEX" \
  -v "$ROOT/models:/models:ro" \
  -v "$ROOT/results:/results" \
  -v "$WEIGHTS_TMP:/weights" \
  -v "$HOST_CONFIG_FILE:$CONTAINER_CONFIG:ro" \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" "${CMD[@]}" 2>&1 | tee "$LOG_FILE"
