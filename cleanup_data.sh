#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"

echo "=== 数据清理脚本 ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 检查数据库大小
echo "【清理前】"
du -sh data/archive.db
echo ""

# 1. 备份数据库
BACKUP_FILE="data/archive.db.backup.$(date +%Y%m%d_%H%M%S)"
echo "正在备份数据库到: $BACKUP_FILE"
cp data/archive.db "$BACKUP_FILE"
echo "✅ 备份完成"
echo ""

# 2. 检查是否有进程在使用数据库
echo "检查数据库使用情况..."
if lsof data/archive.db 2>/dev/null | grep -v "COMMAND" | head -5; then
  echo "⚠️  警告：有进程正在使用数据库"
  echo "建议先停止所有服务再清理"
  read -p "是否继续？(y/N) " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "已取消清理"
    exit 1
  fi
fi
echo ""

# 3. 清理历史数据
echo "【清理历史数据】"
sqlite3 data/archive.db << 'EOF'
-- 统计清理前的数据量
SELECT '清理前统计:' as info;
SELECT 'event_runs: ' || COUNT(*) || ' rows' FROM event_runs;
SELECT 'event_fact_history: ' || COUNT(*) || ' rows' FROM event_fact_history;
SELECT 'auto_trade_decisions: ' || COUNT(*) || ' rows' FROM auto_trade_decisions;
SELECT 'market_snapshots: ' || COUNT(*) || ' rows' FROM market_snapshots;
SELECT 'event_market_ticks: ' || COUNT(*) || ' rows' FROM event_market_ticks;
SELECT 'model_snapshots: ' || COUNT(*) || ' rows' FROM model_snapshots;

-- 清理30天前的 event_runs
DELETE FROM event_runs
WHERE created_at < datetime('now', '-30 days');

-- 清理30天前的 event_fact_history
DELETE FROM event_fact_history
WHERE fact_as_of < datetime('now', '-30 days');

-- 清理30天前的 auto_trade_decisions
DELETE FROM auto_trade_decisions
WHERE created_at < datetime('now', '-30 days');

-- 清理30天前的 market_snapshots
DELETE FROM market_snapshots
WHERE created_at < datetime('now', '-30 days');

-- 清理7天前的 event_market_ticks
DELETE FROM event_market_ticks
WHERE created_at < datetime('now', '-7 days');

-- 清理30天前的 model_snapshots
DELETE FROM model_snapshots
WHERE created_at < datetime('now', '-30 days');

-- 统计清理后的数据量
SELECT '清理后统计:' as info;
SELECT 'event_runs: ' || COUNT(*) || ' rows' FROM event_runs;
SELECT 'event_fact_history: ' || COUNT(*) || ' rows' FROM event_fact_history;
SELECT 'auto_trade_decisions: ' || COUNT(*) || ' rows' FROM auto_trade_decisions;
SELECT 'market_snapshots: ' || COUNT(*) || ' rows' FROM market_snapshots;
SELECT 'event_market_ticks: ' || COUNT(*) || ' rows' FROM event_market_ticks;
SELECT 'model_snapshots: ' || COUNT(*) || ' rows' FROM model_snapshots;
EOF

echo "✅ 历史数据清理完成"
echo ""

# 4. 压缩数据库
echo "【压缩数据库】"
echo "正在执行 VACUUM（可能需要几分钟）..."
sqlite3 data/archive.db "VACUUM;"
echo "✅ VACUUM 完成"
echo ""

# 5. 优化数据库
echo "【优化数据库】"
echo "正在执行 ANALYZE..."
sqlite3 data/archive.db "ANALYZE;"
echo "✅ ANALYZE 完成"
echo ""

# 6. 清理日志文件
echo "【清理日志文件】"
LOG_COUNT=$(find data -name "*.log" -mtime +7 2>/dev/null | wc -l)
if [ "$LOG_COUNT" -gt 0 ]; then
  echo "删除 $LOG_COUNT 个7天前的日志文件..."
  find data -name "*.log" -mtime +7 -delete 2>/dev/null || true
  echo "✅ 日志清理完成"
else
  echo "没有需要清理的日志文件"
fi
echo ""

# 7. 清理缓存文件
echo "【清理缓存文件】"
if [ -f data/weather_events_cache.json ]; then
  rm -f data/weather_events_cache.json
  echo "✅ 删除 weather_events_cache.json"
fi
if [ -f data/weather_events_cache.good.json ]; then
  rm -f data/weather_events_cache.good.json
  echo "✅ 删除 weather_events_cache.good.json"
fi
echo ""

# 8. 显示清理结果
echo "【清理后】"
du -sh data/archive.db
echo ""

echo "【备份文件】"
ls -lh "$BACKUP_FILE"
echo ""

echo "=== 清理完成 ==="
echo "如果系统运行正常，可以删除备份文件："
echo "  rm $BACKUP_FILE"
