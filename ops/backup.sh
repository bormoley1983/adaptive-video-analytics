#!/usr/bin/env bash
# AVA ops PostgreSQL backup and restore drill (B-024-S2).
#
# Two modes:
#   backup        Logical pg_dump -Fc of the ops postgres to ops/backups/.
#                 Keeps the last N dumps (AVA_BACKUP_KEEP, default 3).
#   restore-drill Drop and recreate database ava, restore from a dump,
#                 re-apply migrations, and verify row-level consistency.
#
# DSN inputs (environment):
#   AVA_PG_HOST      default 127.0.0.1
#   AVA_PG_PORT      default 5432
#   AVA_PG_USER      default ava
#   AVA_PG_PASSWORD  required
#   AVA_PG_DB        default ava
#
# Output paths are git-ignored (ops/backups/, results/). The script never
# prints the password.

set -euo pipefail

HOST="${AVA_PG_HOST:-127.0.0.1}"
PORT="${AVA_PG_PORT:-5432}"
USER_NAME="${AVA_PG_USER:-ava}"
DB="${AVA_PG_DB:-ava}"
PASSWORD="${AVA_PG_PASSWORD:?AVA_PG_PASSWORD is required}"
BACKUP_DIR="${AVA_BACKUP_DIR:-$(cd "$(dirname "$0")" && pwd)/backups}"
KEEP="${AVA_BACKUP_KEEP:-3}"

export PGPASSWORD="$PASSWORD"
unset PGPASSFILE 2>/dev/null || true

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { log "ERROR: $1 not found on PATH"; exit 1; }
}

wait_ready() {
  # Poll pg_isready until the server accepts connections.
  local deadline=$((SECONDS + 60))
  until psql -h "$HOST" -p "$PORT" -U "$USER_NAME" -d postgres -c "SELECT 1" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      log "ERROR: postgres not ready at $HOST:$PORT after 60s"
      exit 1
    fi
    sleep 1
  done
}

cmd_backup() {
  require_cmd pg_dump
  mkdir -p "$BACKUP_DIR"
  local stamp file
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  file="$BACKUP_DIR/ava-$stamp.dump"
  log "backup: dumping database $DB to $file"
  pg_dump -h "$HOST" -p "$PORT" -U "$USER_NAME" -d "$DB" -Fc -f "$file"
  log "backup: wrote $file ($(wc -c <"$file") bytes)"

  # Keep-last-N retention: delete oldest dumps beyond KEEP.
  local count
  count="$(ls -1t "$BACKUP_DIR"/ava-*.dump 2>/dev/null | wc -l)"
  if (( count > KEEP )); then
    local excess=$((count - KEEP))
    ls -1t "$BACKUP_DIR"/ava-*.dump | tail -n "$excess" | while read -r old; do
      log "backup: retention — removing $old"
      rm -- "$old"
    done
  fi
  log "backup: done (keeping last $KEEP dumps)"
}

cmd_restore_drill() {
  local dump_file="${1:?usage: backup.sh restore-drill <dump-file>}"
  [[ -f "$dump_file" ]] || { log "ERROR: dump file not found: $dump_file"; exit 1; }
  require_cmd psql
  require_cmd pg_restore

  local rto_start rto_end
  rto_start="$(date -u +%s.%N)"
  log "restore-drill: starting (dump=$dump_file)"

  wait_ready
  log "restore-drill: dropping and recreating database $DB"
  psql -h "$HOST" -p "$PORT" -U "$USER_NAME" -d postgres -v ON_ERROR_STOP=1 <<SQL
DROP DATABASE IF EXISTS $DB WITH (FORCE);
CREATE DATABASE $DB OWNER $USER_NAME;
SQL

  log "restore-drill: restoring dump into $DB"
  pg_restore -h "$HOST" -p "$PORT" -U "$USER_NAME" -d "$DB" --no-owner "$dump_file"

  if [[ -n "${AVA_ALEMBIC_INI:-}" ]]; then
    log "restore-drill: applying migrations (alembic upgrade head)"
    export ALEMBIC_DATABASE_URL="postgresql+psycopg://$USER_NAME:$PASSWORD@$HOST:$PORT/$DB"
    export AVA_PG_DSN="postgresql://$USER_NAME:$PASSWORD@$HOST:$PORT/$DB"
    require_cmd alembic
    alembic -c "$AVA_ALEMBIC_INI" upgrade head
  fi

  wait_ready
  rto_end="$(date -u +%s.%N)"
  local rto_s
  rto_s="$(awk -v a="$rto_start" -v b="$rto_end" 'BEGIN { printf "%.2f", b - a }')"
  log "restore-drill: RTO ${rto_s}s (drop+recreate+restore+migrations+ready)"

  # Consistency verification.
  local events_rows outbox_rows orphan_outbox evidence_events evidence_missing
  read -r events_rows < <(psql -h "$HOST" -p "$PORT" -U "$USER_NAME" -d "$DB" -Atc "SELECT count(*) FROM events")
  read -r outbox_rows < <(psql -h "$HOST" -p "$PORT" -U "$USER_NAME" -d "$DB" -Atc "SELECT count(*) FROM outbox")
  orphan_outbox="$(psql -h "$HOST" -p "$PORT" -U "$USER_NAME" -d "$DB" -Atc \
    "SELECT count(*) FROM outbox o LEFT JOIN events e ON e.event_id = o.event_id WHERE e.event_id IS NULL")"
  evidence_events="$(psql -h "$HOST" -p "$PORT" -U "$USER_NAME" -d "$DB" -Atc \
    "SELECT count(*) FROM events WHERE evidence_path IS NOT NULL")"
  evidence_missing="$(psql -h "$HOST" -p "$PORT" -U "$USER_NAME" -d "$DB" -Atc \
    "SELECT count(*) FROM events WHERE evidence_kind IS NOT NULL AND evidence_path IS NULL")"

  log "consistency: events=$events_rows outbox=$outbox_rows orphan_outbox=$orphan_outbox evidence_events=$evidence_events evidence_missing=$evidence_missing"
  if [[ "$orphan_outbox" != "0" ]]; then
    log "ERROR: $orphan_outbox outbox rows reference missing events"
    exit 1
  fi
  if [[ "$evidence_missing" != "0" ]]; then
    log "ERROR: $evidence_missing events have evidence_kind but no evidence_path"
    exit 1
  fi

  # Record the RTO data point as an evidence artifact.
  local evidence_dir="${AVA_TEST_EVIDENCE_DIR:-results/infra}"
  mkdir -p "$evidence_dir"
  local rto_file="$evidence_dir/rto-$(date -u +%Y%m%dT%H%M%SZ).json"
  cat > "$rto_file" <<JSON
{"rto_seconds": $rto_s, "dump": "$dump_file", "events_rows": $events_rows, "outbox_rows": $outbox_rows, "evidence_events": $evidence_events}
JSON
  log "restore-drill: RTO evidence written to $rto_file"
  log "restore-drill: done"
}

case "${1:-}" in
  backup) cmd_backup ;;
  restore-drill) shift; cmd_restore_drill "$@" ;;
  *)
    echo "usage: $0 backup | restore-drill <dump-file>" >&2
    exit 2
    ;;
esac
