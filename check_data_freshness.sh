#!/bin/bash
# 检查市场数据新鲜度

echo "=== 市场数据新鲜度检查 ==="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 检查数据库
sqlite3 data/archive.db << 'EOF'
.mode column
.headers on
SELECT
  COUNT(*) as total_events,
  SUM(CASE WHEN (julianday('now') - julianday(updated_at)) * 86400 < 300 THEN 1 ELSE 0 END) as fresh_5min,
  SUM(CASE WHEN (julianday('now') - julianday(updated_at)) * 86400 < 3600 THEN 1 ELSE 0 END) as fresh_1hour,
  ROUND(100.0 * SUM(CASE WHEN (julianday('now') - julianday(updated_at)) * 86400 < 300 THEN 1 ELSE 0 END) / COUNT(*), 1) as fresh_5min_pct,
  MAX(datetime(updated_at)) as newest_update,
  ROUND((julianday('now') - julianday(MAX(updated_at))) * 86400, 0) as newest_age_sec
FROM event_fact_latest;
EOF

echo ""
echo "=== 扫描进程状态 ==="
ps aux | grep "scan_weather_market.py" | grep -v grep | head -3

echo ""
echo "=== 最近拒绝原因（最近1小时）==="
sqlite3 data/archive.db << 'EOF'
.mode column
.headers on
SELECT
  reason_codes_json,
  COUNT(*) as count,
  ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM auto_trade_decisions WHERE created_at > datetime('now', '-1 hour')), 2) as pct
FROM auto_trade_decisions
WHERE created_at > datetime('now', '-1 hour')
  AND will_order = 0
  AND reason_codes_json IS NOT NULL
GROUP BY reason_codes_json
ORDER BY count DESC
LIMIT 10;
EOF
