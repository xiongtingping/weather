#!/bin/bash
# 监控扫描进度和数据更新

echo "=== 扫描进程监控 ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 检查扫描进程
echo "【运行中的扫描进程】"
ps aux | grep "scan_weather_market.py" | grep -v grep | while read line; do
  pid=$(echo "$line" | awk '{print $2}')
  cpu=$(echo "$line" | awk '{print $3}')
  mem=$(echo "$line" | awk '{print $4}')
  time=$(echo "$line" | awk '{print $10}')
  echo "  PID: $pid | CPU: ${cpu}% | MEM: ${mem}% | TIME: $time"
done

if ! ps aux | grep "scan_weather_market.py" | grep -v grep > /dev/null; then
  echo "  无运行中的扫描进程"
fi

echo ""
echo "【数据新鲜度】"
sqlite3 data/archive.db << 'EOF'
.mode column
SELECT
  COUNT(*) as total,
  SUM(CASE WHEN datetime(updated_at) > datetime('now', '-5 minutes') THEN 1 ELSE 0 END) as fresh_5min,
  SUM(CASE WHEN datetime(updated_at) > datetime('now', '-1 hour') THEN 1 ELSE 0 END) as fresh_1hour,
  MAX(datetime(updated_at)) as newest_update,
  ROUND((julianday('now') - julianday(MAX(updated_at))) * 24, 1) as hours_ago
FROM event_fact_latest;
EOF

echo ""
echo "【最近更新的事件】"
sqlite3 data/archive.db << 'EOF'
.mode column
SELECT
  event_id,
  datetime(updated_at) as updated_at,
  ROUND((julianday('now') - julianday(updated_at)) * 60, 1) as minutes_ago
FROM event_fact_latest
WHERE updated_at > datetime('now', '-30 minutes')
ORDER BY updated_at DESC
LIMIT 10;
EOF

echo ""
echo "【扫描日志（最近10行）】"
tail -10 data/model_scan_hourly.log 2>/dev/null || echo "  无日志"

echo ""
echo "=== 监控完成 ==="
