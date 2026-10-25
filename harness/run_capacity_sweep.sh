#!/usr/bin/env bash
# E-3-T5 capacity sweep driver.
#
# Runs all 16 sweep points sequentially, smallest-first (4 -> 16 -> 25 -> 50),
# both detector kinds (conventional, prompted) and both detector modes
# (shared, per_camera). Each point runs at its full committed duration_s via the
# containerized run helper. Stops escalating after the first execution failure.
# RUN_OK means the process completed and wrote metrics; acceptance is evaluated
# separately from the recorded FPS, latency, queue, drop and error values.
#
# GPU discipline: every point pins to GPU 0 (CUDA_VISIBLE_DEVICES=0 inside the
# helper). Before each point we verify GPU 0 is not unexpectedly busy and that
# GPU 1 (shared llama-server) is left untouched.
#
# Output: a per-point status table at results/e3-capacity-sweep/sweep-status.tsv
# plus each point's own artifacts under results/<point>/.
#
# Usage: ./harness/run_capacity_sweep.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GPU_INDEX="${AVA_GPU:-0}"
BUSY_MEM_THRESHOLD_MIB=4000   # GPU 0 memory above this (beyond baseline) => busy
STATUS_DIR="$ROOT/results/e3-capacity-sweep"
STATUS_FILE="$STATUS_DIR/sweep-status.tsv"
mkdir -p "$STATUS_DIR"

# Smallest-first ordering: cameras ascending, then kind, then mode.
POINTS=(
  "configs/run-e3-cap-4-conv-shared.yaml"
  "configs/run-e3-cap-4-conv-per_camera.yaml"
  "configs/run-e3-cap-4-prom-shared.yaml"
  "configs/run-e3-cap-4-prom-per_camera.yaml"
  "configs/run-e3-cap-16-conv-shared.yaml"
  "configs/run-e3-cap-16-conv-per_camera.yaml"
  "configs/run-e3-cap-16-prom-shared.yaml"
  "configs/run-e3-cap-16-prom-per_camera.yaml"
  "configs/run-e3-cap-25-conv-shared.yaml"
  "configs/run-e3-cap-25-conv-per_camera.yaml"
  "configs/run-e3-cap-25-prom-shared.yaml"
  "configs/run-e3-cap-25-prom-per_camera.yaml"
  "configs/run-e3-cap-50-conv-shared.yaml"
  "configs/run-e3-cap-50-conv-per_camera.yaml"
  "configs/run-e3-cap-50-prom-shared.yaml"
  "configs/run-e3-cap-50-prom-per_camera.yaml"
)

# START_INDEX (1-based) lets a resumed sweep skip already-completed points.
# Default 1 = run all. When >1, append to the existing status file instead of
# overwriting it, so prior run-result rows are preserved.
START_INDEX="${START_INDEX:-1}"
HEADER='point\tcameras\tkind\tmode\trun_status\twall_seconds\ttotal_events\tgpu0_mem_mib_start\tgpu_util_avg_pct\tgpu_util_peak_pct\tcpu_load1m_avg\tram_avail_avg_mib\tnote'
if [[ "$START_INDEX" -le 1 ]]; then
  printf "$HEADER\n" > "$STATUS_FILE"
else
  # Preserve prior rows only if the file already carries the extended header.
  if [[ -f "$STATUS_FILE" ]] && grep -q 'gpu_util_avg_pct' "$STATUS_FILE"; then
    :
  else
    printf "$HEADER\n" > "$STATUS_FILE"
  fi
fi

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$STATUS_DIR/sweep.log"; }

gpu0_mem_mib() {
  nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "$GPU_INDEX" 2>/dev/null | tr -d ' '
}

log "=== E-3-T5 capacity sweep start (GPU $GPU_INDEX) ==="
log "status file: $STATUS_FILE"

idx=0
total=${#POINTS[@]}
for cfg in "${POINTS[@]}"; do
  idx=$((idx + 1))
  if [[ "$idx" -lt "$START_INDEX" ]]; then
    continue
  fi
  label="$(basename "$cfg" .yaml)"
  # Parse cameras/kind/mode from the config for the status row.
  read -r ncam kind mode <<< "$(python3 -c "
import yaml
c = yaml.safe_load(open('$cfg'))
cams = c.get('live', {}).get('cameras', [])
kind = 'conv' if c.get('detector', {}).get('model') and 'yoloe' not in str(c.get('detector', {})) else 'prom'
# More robust: detect prompted by presence of a text prompt / yoloe model.
det = c.get('detector', {})
is_prom = ('yoloe' in str(det.get('model','')).lower()) or bool(det.get('prompt'))
kind = 'prom' if is_prom else 'conv'
mode = c.get('live', {}).get('detector_mode', 'shared')
print(len(cams), kind, mode)
")"

  mem_start="$(gpu0_mem_mib)"
  log "[$idx/$total] $label : cameras=$ncam kind=$kind mode=$mode gpu0_mem=${mem_start}MiB"

  # GPU guard: if GPU 0 is already busy beyond threshold, abort the sweep.
  if [[ "${mem_start:-0}" -gt "$BUSY_MEM_THRESHOLD_MIB" ]]; then
    log "[$idx/$total] ABORT: GPU $GPU_INDEX busy (${mem_start}MiB > ${BUSY_MEM_THRESHOLD_MIB}MiB). Stopping sweep."
    printf '%s\t%s\t%s\t%s\tABORTED\t-\t-\t%s\t-\t-\t-\t-\tgpu busy at start\n' \
      "$label" "$ncam" "$kind" "$mode" "$mem_start" >> "$STATUS_FILE"
    break
  fi

  # Run the point (full duration from config). Capture exit code.
  local_start="$(date '+%s')"
  if ./harness/run_live_container.sh "$cfg" > "$STATUS_DIR/$label.run.log" 2>&1; then
    rc=0
  else
    rc=$?
  fi
  local_end="$(date '+%s')"
  wall=$((local_end - local_start))

  # Extract wall_seconds from metrics.json and total_events from events.jsonl
  # (the authoritative event store; metrics.json has no top-level event count).
  out_dir_name="$(python3 -c "import yaml;print(yaml.safe_load(open('$cfg')).get('output_dir','').split('/')[-1])" 2>/dev/null)"
  point_dir="$ROOT/results/$out_dir_name"
  metrics_json="$point_dir/metrics.json"
  events_file="$point_dir/events.jsonl"
  wall_s="-"; events="-"
  if [[ -f "$metrics_json" ]]; then
    wall_s="$(python3 -c "import json;print(round(json.load(open('$metrics_json')).get('wall_seconds',-1),1))" 2>/dev/null)"
  fi
  if [[ -f "$events_file" ]]; then
    events="$(wc -l < "$events_file" | tr -d ' ')"
  fi

  # Resource averages/peaks from the point's host_samples in metrics.json.
  gpu_util_avg="-"; gpu_util_peak="-"; cpu_load_avg="-"; ram_avail_avg="-"
  if [[ -f "$metrics_json" ]]; then
    read -r gpu_util_avg gpu_util_peak cpu_load_avg ram_avail_avg <<< "$(python3 -c "
import json, statistics as st
d = json.load(open('$metrics_json'))
hs = d.get('host_samples') or []
if hs:
    print(round(st.mean(s['gpu_util_pct'] for s in hs),1),
          round(max(s['gpu_util_pct'] for s in hs),1),
          round(st.mean(s['load_1m'] for s in hs),2),
          round(st.mean(s['ram_available_mib'] for s in hs),0))
else:
    print('-','-','-','-')
" 2>/dev/null)"
  fi

  if [[ $rc -eq 0 && "$wall_s" != "-" ]]; then
    status="RUN_OK"
    note=""
  else
    status="RUN_FAILED"
    note="exit=$rc"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$label" "$ncam" "$kind" "$mode" "$status" "$wall_s" "$events" "$mem_start" \
    "$gpu_util_avg" "$gpu_util_peak" "$cpu_load_avg" "$ram_avail_avg" "$note" >> "$STATUS_FILE"
  log "[$idx/$total] $label : $status wall=${wall_s}s events=$events gpu_util=${gpu_util_avg}%/${gpu_util_peak}% cpu=${cpu_load_avg} ram=${ram_avail_avg}MiB (exit=$rc)"

  if [[ "$status" == "RUN_FAILED" ]]; then
    log "STOP-ON-FAILURE: point $label failed; not escalating further."
    break
  fi
done

log "=== sweep finished ==="
echo
echo "=== sweep status ==="
column -t -s $'\t' "$STATUS_FILE" 2>/dev/null || cat "$STATUS_FILE"
