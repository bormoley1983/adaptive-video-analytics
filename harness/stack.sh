#!/usr/bin/env bash
# AVA study-stack bring-up (E-3-T5).
#
# One idempotent, health-gated entry point for the whole local study stack:
#   system prereqs -> footage -> MediaMTX (health-gated) -> N publishers
#   (self-healing) -> verify all-N-Up -> observability (Prometheus/Loki/
#   Alloy/Grafana).
#
# It converges to a known-good state even after a reboot: if the RTSP server
# was down when publishers started, they crash-loop on DNS and exhaust their
# `on-failure` restart budget, leaving them stuck in `Exited`. This script
# starts MediaMTX first, waits for it to be healthy, then force-recreates any
# publisher that is not Up, so a single command always recovers.
#
# Usage:
#   ./harness/stack.sh up [N] [--no-ops]     bring the full stack up
#   ./harness/stack.sh down                  ordered teardown (volumes kept)
#   ./harness/stack.sh status                one-screen health table
#
#   N            number of cameras to run (default: all defined in compose).
#                Supported values follow the harness manifest/compose; the
#                script derives the camera list from compose, so any count the
#                compose defines works (4 / 16 / 25 / 50 / 100 ...).
#   --no-ops     skip the observability stack (pure capacity runs).
#
# CPU-only: this script never allocates a GPU and never touches GPU 1.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HARNESS_COMPOSE="${SCRIPT_DIR}/docker-compose.yml"
OPS_COMPOSE="${REPO_ROOT}/ops/docker-compose.yml"
MANIFEST="${SCRIPT_DIR}/manifest.yaml"
NORMALIZE="${SCRIPT_DIR}/normalize.sh"

# Bounded wait so a broken stack fails loudly instead of hanging.
WAIT_TIMEOUT_S="${AVA_STACK_WAIT_TIMEOUT_S:-120}"
POLL_INTERVAL_S=3

log()  { printf '\033[1;34m[stack]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# --- helpers -----------------------------------------------------------------

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

# All camera service names defined in the harness compose (e.g. ava-cam-01).
cam_services_all() {
  docker compose -f "${HARNESS_COMPOSE}" config --services \
    | grep '^ava-cam-' || true
}

# First N camera services, in numeric order.
cam_services_n() {
  local n="$1"
  cam_services_all | sort -t- -k3 -n | head -n "${n}"
}

# Total number of cameras defined in compose.
cam_count_total() {
  cam_services_all | wc -l | tr -d ' '
}

# Wait until a predicate returns 0, bounded by WAIT_TIMEOUT_S.
wait_until() {
  local desc="$1"; shift
  local deadline=$(( $(date +%s) + WAIT_TIMEOUT_S ))
  while :; do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      return 1
    fi
    sleep "${POLL_INTERVAL_S}"
  done
}

# MediaMTX is healthy when its container reports (healthy).
mediamtx_healthy() {
  local st
  st="$(docker inspect --format '{{.State.Health.Status}}' \
        ava-harness-ava-mediamtx-1 2>/dev/null || echo missing)"
  [[ "${st}" == "healthy" ]]
}

# A single publisher is Up (running, not restarting/exited).
cam_is_up() {
  local svc="$1" st
  st="$(docker inspect --format '{{.State.Status}}' \
        "ava-harness-${svc}-1" 2>/dev/null || echo missing)"
  [[ "${st}" == "running" ]]
}

all_cams_up() {
  local svc
  for svc in "$@"; do
    cam_is_up "${svc}" || return 1
  done
  return 0
}

# --- system prereqs ----------------------------------------------------------

check_system_prereqs() {
  log "checking system prerequisites"
  require_cmd docker
  if ! docker info >/dev/null 2>&1; then
    die "Docker daemon is not reachable. Start Docker and retry."
  fi
  ok "docker daemon reachable"

  # Pull pinned images if absent (idempotent; no-op when present).
  local img
  for img in \
    "bluenviron/mediamtx@sha256:c7540465f868a02652118bea5a0daab12a29ac0f6cb8485abd535939ecddf886" \
    "jrottenberg/ffmpeg@sha256:7e8154ec4779d335054de5e173f1ef52032abaa12ac7416cc7f0c88f4973bd14"; do
    if ! docker image inspect "${img}" >/dev/null 2>&1; then
      log "pulling missing image ${img%%@*} ..."
      docker pull "${img}" || die "failed to pull ${img}"
    fi
  done
  ok "harness images present"
}

ensure_ops_env() {
  if [[ ! -f "${REPO_ROOT}/ops/.env" ]]; then
    if [[ -f "${REPO_ROOT}/ops/.env.example" ]]; then
      cp "${REPO_ROOT}/ops/.env.example" "${REPO_ROOT}/ops/.env"
      warn "created ops/.env from .env.example (set GRAFANA_ADMIN_PASSWORD before relying on Grafana)"
    else
      warn "ops/.env and ops/.env.example both missing; ops stack may fail to start"
    fi
  fi
}

# --- footage -----------------------------------------------------------------

ensure_footage() {
  log "ensuring normalized footage exists (idempotent)"
  if [[ -x "${NORMALIZE}" ]]; then
    "${NORMALIZE}" || die "footage normalization failed"
  else
    warn "normalize.sh not found or not executable; assuming footage already present"
  fi
}

# --- harness bring-up --------------------------------------------------------

start_mediamtx() {
  log "starting MediaMTX (RTSP server) first"
  docker compose -f "${HARNESS_COMPOSE}" up -d ava-mediamtx >/dev/null
  if wait_until "MediaMTX healthy" mediamtx_healthy; then
    ok "MediaMTX is healthy"
  else
    die "MediaMTX did not become healthy within ${WAIT_TIMEOUT_S}s. Check: docker logs ava-harness-ava-mediamtx-1"
  fi
}

start_publishers() {
  local -a svcs=("$@")
  log "starting ${#svcs[@]} publishers"
  # Start them all; some may fail if the server was briefly unavailable.
  docker compose -f "${HARNESS_COMPOSE}" up -d "${svcs[@]}" >/dev/null

  # Self-heal: force-recreate any publisher that is not Up (covers the
  # restart-budget-exhausted `Exited` state left behind by a reboot).
  local -a need_recreate=()
  local svc
  for svc in "${svcs[@]}"; do
    cam_is_up "${svc}" || need_recreate+=("${svc}")
  done
  if (( ${#need_recreate[@]} > 0 )); then
    warn "force-recreating ${#need_recreate[@]} unhealthy publisher(s): ${need_recreate[*]}"
    docker compose -f "${HARNESS_COMPOSE}" up -d --force-recreate \
      "${need_recreate[@]}" >/dev/null
  fi

  if wait_until "all publishers Up" all_cams_up "${svcs[@]}"; then
    ok "all ${#svcs[@]} publishers are Up"
  else
    local down=()
    for svc in "${svcs[@]}"; do cam_is_up "${svc}" || down+=("${svc}"); done
    die "publishers not Up after ${WAIT_TIMEOUT_S}s: ${down[*]}. Check: docker logs ava-harness-${down[0]}-1"
  fi
}

# --- observability -----------------------------------------------------------

start_ops() {
  log "starting observability stack (Prometheus/Loki/Alloy/Grafana)"
  ensure_ops_env
  if [[ ! -f "${OPS_COMPOSE}" ]]; then
    warn "ops/docker-compose.yml not found; skipping observability"
    return 0
  fi
  docker compose -f "${OPS_COMPOSE}" up -d >/dev/null

  # Wait for the service that gates dashboard availability.
  ops_prom_healthy() {
    docker inspect --format '{{.State.Health.Status}}' \
      ava-ops-prometheus-1 2>/dev/null | grep -q healthy
  }
  if wait_until "Prometheus healthy" ops_prom_healthy; then
    ok "Prometheus is healthy"
  else
    warn "Prometheus not healthy within ${WAIT_TIMEOUT_S}s (dashboards may be degraded). Check: docker logs ava-ops-prometheus-1"
  fi
}

# --- subcommands -------------------------------------------------------------

cmd_up() {
  local n="" use_ops=1
  local arg
  for arg in "$@"; do
    case "${arg}" in
      --no-ops) use_ops=0 ;;
      *) n="${arg}" ;;
    esac
  done

  check_system_prereqs

  local total
  total="$(cam_count_total)"
  if [[ -z "${n}" ]]; then
    n="${total}"
  fi
  if (( n > total )); then
    die "requested ${n} cameras but compose defines only ${total}. Extend harness/docker-compose.yml + manifest.yaml first."
  fi

  log "bringing up AVA study stack: ${n} camera(s), ops=$([[ ${use_ops} == 1 ]] && echo yes || echo no)"

  ensure_footage
  start_mediamtx
  local -a svcs=()
  mapfile -t svcs < <(cam_services_n "${n}")
  start_publishers "${svcs[@]}"

  if (( use_ops == 1 )); then
    start_ops
  else
    log "skipping observability stack (--no-ops)"
  fi

  ok "stack is up. Verify with: ./harness/stack.sh status"
}

cmd_down() {
  log "tearing down AVA study stack (volumes preserved)"
  if [[ -f "${OPS_COMPOSE}" ]]; then
    docker compose -f "${OPS_COMPOSE}" down >/dev/null 2>&1 || true
  fi
  docker compose -f "${HARNESS_COMPOSE}" down >/dev/null 2>&1 || true
  ok "stack is down"
}

cmd_status() {
  echo "=== AVA study stack status ==="
  echo ""
  echo "-- Docker daemon --"
  if docker info >/dev/null 2>&1; then ok "reachable"; else warn "NOT reachable"; fi
  echo ""

  echo "-- MediaMTX --"
  local mst
  mst="$(docker inspect --format '{{.State.Status}} ({{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}})' \
        ava-harness-ava-mediamtx-1 2>/dev/null || echo missing)"
  echo "  ${mst}"
  echo ""

  echo "-- Publishers --"
  local svc up=0 downlist=()
  for svc in $(cam_services_all | sort -t- -k3 -n); do
    if cam_is_up "${svc}"; then
      (( up++ )) || true
    else
      local st
      st="$(docker inspect --format '{{.State.Status}}' "ava-harness-${svc}-1" 2>/dev/null || echo missing)"
      downlist+=("${svc}=${st}")
    fi
  done
  echo "  up: ${up} / $(cam_count_total)"
  if (( ${#downlist[@]} > 0 )); then
    echo "  not up: ${downlist[*]}"
  fi
  echo ""

  echo "-- Observability --"
  local osvc
  for osvc in prometheus loki alloy grafana; do
    local ost
    ost="$(docker inspect --format '{{.State.Status}} ({{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}})' \
          "ava-ops-${osvc}-1" 2>/dev/null || echo missing)"
    echo "  ${osvc}: ${ost}"
  done
  echo ""

  echo "-- GPU 0 (must stay free for AVA; GPU 1 is shared) --"
  if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=index,memory.used,utilization.gpu \
      --format=csv,noheader 2>/dev/null || echo "  (nvidia-smi query failed)"
  else
    echo "  (nvidia-smi not available)"
  fi
}

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

main() {
  local cmd="${1:-}"
  [[ -n "${cmd}" ]] || { usage; exit 1; }
  shift || true
  case "${cmd}" in
    up)     cmd_up "$@" ;;
    down)   cmd_down "$@" ;;
    status) cmd_status "$@" ;;
    help|-h|--help) usage ;;
    *) die "unknown command: ${cmd} (expected up|down|status)" ;;
  esac
}

main "$@"
