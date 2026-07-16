#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
DB_PATH="${PM_ARCHIVE_DB_PATH:-$ROOT/data/archive.db}"
CONFIRM_PHRASE="RETENTION_APPLY_CONFIRMED"
APPLY=0
COMPACT=0
CHECKPOINT=1
CHENGDU_TICK_RETENTION="${PM_ARCHIVE_DB_CHENGDU_TICK_RETENTION:-0}"
EXACT_COUNTS=0
CONFIRMED=0
EXTRA_ARGS=()

usage() {
  cat <<EOF
Usage: ./cleanup_data.sh [--dry-run] [--apply --confirm-retention-apply] [--compact] [archive_db_governance args...]

Default mode is dry-run. Live retention deletes require one of:
  --confirm-retention-apply
  PM_CLEANUP_APPLY_CONFIRM=$CONFIRM_PHRASE

This wrapper delegates archive.db retention to scripts/archive_db_governance.py.
It does not run direct DELETE, VACUUM, or ANALYZE SQL itself.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      APPLY=0
      ;;
    --apply)
      APPLY=1
      ;;
    --compact)
      COMPACT=1
      ;;
    --no-checkpoint)
      CHECKPOINT=0
      ;;
    --chengdu-tick-retention)
      CHENGDU_TICK_RETENTION=1
      ;;
    --exact-table-counts)
      EXACT_COUNTS=1
      ;;
    --confirm-retention-apply)
      CONFIRMED=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      EXTRA_ARGS+=("$1")
      ;;
  esac
  shift
done

cd "$ROOT"
. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$ROOT")" || {
  echo "missing project python runtime"
  exit 1
}

echo "=== archive.db retention policy wrapper ==="
echo "time=$(date '+%Y-%m-%d %H:%M:%S')"
echo "db=$DB_PATH"
echo "policy_owner=scripts/archive_db_governance.py"
echo "default_mode=dry_run"
echo ""

ARGS=(--db "$DB_PATH")
if [ "$CHENGDU_TICK_RETENTION" = "1" ]; then
  ARGS+=(--chengdu-tick-retention)
fi
if [ "$EXACT_COUNTS" = "1" ]; then
  ARGS+=(--exact-table-counts)
fi

if [ "$APPLY" = "1" ]; then
  if [ "$CONFIRMED" != "1" ] && [ "${PM_CLEANUP_APPLY_CONFIRM:-}" != "$CONFIRM_PHRASE" ]; then
    echo "refuse_apply_missing_confirmation=1"
    echo "required_confirmation=$CONFIRM_PHRASE"
    echo "rerun_hint=./cleanup_data.sh --apply --confirm-retention-apply"
    exit 2
  fi
  ARGS+=(--apply)
  if [ "$CHECKPOINT" = "1" ]; then
    ARGS+=(--checkpoint)
  fi
  if [ "$COMPACT" = "1" ]; then
    ARGS+=(--compact)
  fi
  if [ "${PM_CLEANUP_CREATE_BACKUP:-1}" = "1" ]; then
    mkdir -p "$ROOT/data/backups"
    BACKUP_FILE="$ROOT/data/backups/archive.before-retention.$(date +%Y%m%dT%H%M%S).db"
    cp "$DB_PATH" "$BACKUP_FILE"
    echo "backup_created=$BACKUP_FILE"
  fi
else
  echo "dry_run_only=1"
  echo "apply_requires=--apply --confirm-retention-apply"
fi

"$PY" "$ROOT/scripts/archive_db_governance.py" "${ARGS[@]}" "${EXTRA_ARGS[@]}"
"$PY" "$ROOT/scripts/archive_db_governance.py" --db "$DB_PATH" --ensure-schema

LOG_DIR="$ROOT/data/logs"
LOG_COUNT=$(find "$LOG_DIR" -name "*.log" -mtime +7 2>/dev/null | wc -l | tr -d ' ')
if [ "${PM_CLEANUP_DELETE_LOGS:-0}" = "1" ]; then
  find "$LOG_DIR" -name "*.log" -mtime +7 -delete 2>/dev/null || true
  echo "deleted_old_logs=$LOG_COUNT"
else
  echo "old_logs_candidates=$LOG_COUNT"
  echo "delete_old_logs_requires=PM_CLEANUP_DELETE_LOGS=1"
fi

if [ "${PM_CLEANUP_DELETE_CACHE:-0}" = "1" ]; then
  rm -f "$ROOT/data/weather_events_cache.json" "$ROOT/data/weather_events_cache.good.json"
  echo "deleted_weather_event_caches=1"
else
  echo "delete_weather_event_caches_requires=PM_CLEANUP_DELETE_CACHE=1"
fi

echo "=== retention wrapper complete ==="
