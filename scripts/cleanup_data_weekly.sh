#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"
. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$ROOT")" || {
  echo "missing project python runtime"
  exit 1
}

echo "=== 定期数据治理脚本 ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 1. archive.db 治理。weekly 默认执行受控 apply：
#    - 只做保留策略 + rollup + 删除 + checkpoint
#    - 默认不 compact/VACUUM，避免低磁盘空间下放大风险
#    - 如需临时只读演练，设置 PM_ARCHIVE_DB_GOVERNANCE_DRY_RUN=1
echo "【archive.db 治理】"
ARGS=(--db "$ROOT/data/archive.db")
export PM_DB_KEEP_TICKS_DAYS="${PM_DB_KEEP_TICKS_DAYS:-2}"
export PM_DB_KEEP_DECISIONS_DAYS="${PM_DB_KEEP_DECISIONS_DAYS:-60}"
export PM_DB_KEEP_FACTS_DAYS="${PM_DB_KEEP_FACTS_DAYS:-30}"
export PM_DB_KEEP_EVENT_RUNS_DAYS="${PM_DB_KEEP_EVENT_RUNS_DAYS:-60}"
export PM_DB_KEEP_SNAPSHOTS_DAYS="${PM_DB_KEEP_SNAPSHOTS_DAYS:-30}"
export PM_DB_KEEP_OBSERVATIONS_DAYS="${PM_DB_KEEP_OBSERVATIONS_DAYS:-60}"
echo "retention：raw ticks ${PM_DB_KEEP_TICKS_DAYS}d；decisions ${PM_DB_KEEP_DECISIONS_DAYS}d；facts ${PM_DB_KEEP_FACTS_DAYS}d；snapshots ${PM_DB_KEEP_SNAPSHOTS_DAYS}d。"
if [ "${PM_ARCHIVE_DB_GOVERNANCE_DRY_RUN:-0}" = "1" ]; then
  echo "dry-run：PM_ARCHIVE_DB_GOVERNANCE_DRY_RUN=1，本次只审计不写入。"
elif [ "${PM_ARCHIVE_DB_GOVERNANCE_APPLY:-1}" = "1" ]; then
  echo "apply：执行受控保留治理与 WAL checkpoint；默认不 compact。"
  ARGS+=(--apply --checkpoint)
  if [ "${PM_ARCHIVE_DB_GOVERNANCE_COMPACT:-0}" = "1" ]; then
    ARGS+=(--compact)
  fi
else
  echo "dry-run：PM_ARCHIVE_DB_GOVERNANCE_APPLY=0，本次只审计不写入。"
fi
"$PY" scripts/archive_db_governance.py "${ARGS[@]}"
"$PY" scripts/archive_db_governance.py --db "$ROOT/data/archive.db" --ensure-schema
echo "✅ archive.db 治理完成"
echo ""

# 2. 清理日志文件
echo "【清理日志文件】"
LOG_COUNT=$(find data/logs -name "*.log" -mtime +7 2>/dev/null | wc -l)
if [ "$LOG_COUNT" -gt 0 ]; then
  find data/logs -name "*.log" -mtime +7 -delete 2>/dev/null || true
  echo "✅ 删除 $LOG_COUNT 个7天前的日志文件"
else
  echo "没有需要清理的日志文件"
fi
MAX_LOG_BYTES="${PM_CLEANUP_MAX_LOG_BYTES:-52428800}"
KEEP_LOG_BYTES="${PM_CLEANUP_KEEP_LOG_BYTES:-5242880}"
ROTATED=0
while IFS= read -r -d '' log_file; do
  size=$(stat -f%z "$log_file" 2>/dev/null || echo 0)
  if [ "$size" -le "$MAX_LOG_BYTES" ]; then
    continue
  fi
  stamp=$(date '+%Y%m%dT%H%M%S')
  rotated_path="${log_file}.${stamp}.rotated"
  tail -c "$KEEP_LOG_BYTES" "$log_file" > "${rotated_path}.tail"
  mv "$log_file" "$rotated_path"
  mv "${rotated_path}.tail" "$log_file"
  gzip -f "$rotated_path" 2>/dev/null || true
  ROTATED=$((ROTATED + 1))
  echo "rotated $(basename "$log_file"): ${size} bytes -> keep tail ${KEEP_LOG_BYTES} bytes"
done < <(find data data/logs -maxdepth 1 -type f \( -name "*.log" -o -name "*.out.log" -o -name "*.err.log" \) -print0 2>/dev/null)
if [ "$ROTATED" -eq 0 ]; then
  echo "没有超过阈值的活跃日志"
else
  echo "✅ 轮转 $ROTATED 个活跃日志"
fi
OLD_BACKUP_COUNT=$(find data/backups -maxdepth 1 -type f -name 'archive.before-chengdu-settlement-backfill.*.db' -mtime +7 2>/dev/null | wc -l)
if [ "$OLD_BACKUP_COUNT" -gt 0 ]; then
  if [ "${PM_CLEANUP_DELETE_OLD_BACKFILL_BACKUPS:-0}" = "1" ]; then
    find data/backups -maxdepth 1 -type f -name 'archive.before-chengdu-settlement-backfill.*.db' -mtime +7 -delete 2>/dev/null || true
    echo "✅ 删除 $OLD_BACKUP_COUNT 个 7 天前 settlement backfill 备份"
  else
    echo "发现 $OLD_BACKUP_COUNT 个 7 天前 settlement backfill 备份；默认不删。确认后设置 PM_CLEANUP_DELETE_OLD_BACKFILL_BACKUPS=1。"
  fi
fi
echo ""

# 3. 清理缓存文件
echo "【清理缓存文件】"
rm -f data/weather_events_cache.json data/weather_events_cache.good.json
echo "✅ 缓存文件已清理"
echo ""

# 4. 显示清理结果
echo "【清理后】"
du -sh data/archive.db
echo ""

echo "=== 清理完成 ==="
