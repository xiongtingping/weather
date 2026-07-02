#!/bin/bash
# 数据库定期维护脚本
# 建议每周运行一次

set -euo pipefail

DB_PATH="/Users/xiong/weather/data/archive.db"
LOG_PATH="/Users/xiong/weather/data/logs/db_maintenance.log"
ROOT="/Users/xiong/weather"
. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$ROOT")" || {
  echo "missing project python runtime" | tee -a "$LOG_PATH"
  exit 1
}

echo "[$(date)] 开始数据库维护..." | tee -a "$LOG_PATH"

ARGS=(--db "$DB_PATH")
if [ "${PM_ARCHIVE_DB_CHENGDU_TICK_RETENTION:-0}" = "1" ]; then
  ARGS+=(--chengdu-tick-retention)
  echo "启用成都-only raw tick治理：保留成都活跃日期、最近 ${PM_DB_CHENGDU_RECENT_HOURS:-48}h、每key最新tick；非成都raw rollup后删除。" | tee -a "$LOG_PATH"
fi
if [ "${PM_ARCHIVE_DB_GOVERNANCE_APPLY:-0}" = "1" ]; then
  ARGS+=(--apply --checkpoint)
  if [ "${PM_ARCHIVE_DB_GOVERNANCE_COMPACT:-0}" = "1" ]; then
    DB_BYTES=$(stat -f%z "$DB_PATH" 2>/dev/null || echo 0)
    FREE_KB=$(df -Pk "$(dirname "$DB_PATH")" 2>/dev/null | awk 'NR==2 {print $4}')
    FREE_BYTES=$(( ${FREE_KB:-0} * 1024 ))
    REQUIRED_BYTES=$(( DB_BYTES + DB_BYTES / 4 ))
    if [ "$DB_BYTES" -gt 0 ] && [ "$FREE_BYTES" -ge "$REQUIRED_BYTES" ]; then
      ARGS+=(--compact)
    else
      echo "skip_compact_insufficient_free_space db_bytes=${DB_BYTES} free_bytes=${FREE_BYTES} required_bytes=${REQUIRED_BYTES}" | tee -a "$LOG_PATH"
    fi
  fi
else
  echo "默认 dry-run；真实删除需设置 PM_ARCHIVE_DB_GOVERNANCE_APPLY=1" | tee -a "$LOG_PATH"
fi

"$PY" "$ROOT/scripts/archive_db_governance.py" "${ARGS[@]}" | tee -a "$LOG_PATH"
"$PY" "$ROOT/scripts/archive_db_governance.py" --db "$DB_PATH" --ensure-schema | tee -a "$LOG_PATH"
sqlite3 "$DB_PATH" "PRAGMA optimize;" | tee -a "$LOG_PATH"

SIZE=$(du -h "$DB_PATH" | cut -f1)
PAGES=$(sqlite3 "$DB_PATH" "PRAGMA page_count;")
FREE_PAGES=$(sqlite3 "$DB_PATH" "PRAGMA freelist_count;")
echo "维护完成 - 文件大小: $SIZE, pages: $PAGES, free_pages: $FREE_PAGES" | tee -a "$LOG_PATH"
