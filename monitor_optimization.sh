#!/bin/bash
# Weather 交易系统 - 优化效果监控脚本

WEATHER_ROOT="/Users/xiong/weather/weather"
cd "$WEATHER_ROOT"

echo "=========================================="
echo "优化效果监控"
echo "=========================================="
echo ""

# 1. 网络健康度
echo "[1] 网络健康度"
echo "---"
NETWORK_ERRORS=$(grep -c "CLOB Client.*error\|Max retries exceeded" data/logs/trade_engine.log 2>/dev/null || echo 0)
NETWORK_SUCCESS=$(grep -c "sync_once_ok" data/logs/trade_engine.log 2>/dev/null || echo 0)
TOTAL=$((NETWORK_ERRORS + NETWORK_SUCCESS))
if [ $TOTAL -gt 0 ]; then
    SUCCESS_RATE=$(echo "scale=2; $NETWORK_SUCCESS * 100 / $TOTAL" | bc)
    echo "  成功率: ${SUCCESS_RATE}% ($NETWORK_SUCCESS/$TOTAL)"
else
    echo "  暂无数据"
fi
echo ""

# 2. 扫描超时率
echo "[2] 扫描超时率"
echo "---"
SCAN_TIMEOUT=$(grep -c "timeout.*kill pid" data/model_scan.log 2>/dev/null || echo 0)
SCAN_SUCCESS=$(grep -c "scan_timing.*total=" data/model_scan.log 2>/dev/null || echo 0)
SCAN_TOTAL=$((SCAN_TIMEOUT + SCAN_SUCCESS))
if [ $SCAN_TOTAL -gt 0 ]; then
    TIMEOUT_RATE=$(echo "scale=2; $SCAN_TIMEOUT * 100 / $SCAN_TOTAL" | bc)
    echo "  超时率: ${TIMEOUT_RATE}% ($SCAN_TIMEOUT/$SCAN_TOTAL)"
else
    echo "  暂无数据"
fi
echo ""

# 3. 任务队列状态
echo "[3] 任务队列状态"
echo "---"
sqlite3 data/archive.db "
SELECT
  state,
  COUNT(*) as count,
  ROUND(AVG(julianday(ended_at) - julianday(started_at)) * 86400, 1) as avg_sec
FROM auto_trade_jobs
WHERE run_date = date('now')
GROUP BY state;
" 2>/dev/null || echo "  查询失败"
echo ""

# 4. 最近错误
echo "[4] 最近错误（最近 20 条）"
echo "---"
grep -i "error\|fail\|timeout" data/logs/trade_engine.log 2>/dev/null | tail -5 || echo "  无错误"
echo ""

echo "=========================================="
echo "监控完成"
echo "=========================================="
