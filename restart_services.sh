#!/bin/bash
# 重启 Weather 守护进程（用户级别）

echo "=========================================="
echo "重启 Weather 守护进程"
echo "=========================================="
echo ""

SERVICES=(
    "com.weather.model_scan"
    "com.weather.auto_trade_worker"
    "com.weather.trade_engine"
    "com.weather.market_refresh_1m"
    "com.weather.auto_trade_entry_1m"
)

for service in "${SERVICES[@]}"; do
    echo "重启: $service"
    if launchctl kickstart -k "gui/$(id -u)/$service" 2>/dev/null; then
        echo "  ✓ $service 已重启"
    else
        echo "  ⚠ $service 重启失败"
    fi
done

echo ""
echo "=========================================="
echo "重启完成！"
echo "=========================================="
echo ""
echo "查看服务状态："
echo "  launchctl list | grep weather"
echo ""
echo "查看日志："
echo "  tail -f /Users/xiong/weather/weather/data/model_scan.log"
echo "  tail -f /Users/xiong/weather/weather/data/logs/trade_engine.log"
