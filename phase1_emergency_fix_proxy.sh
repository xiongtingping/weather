#!/bin/bash
# Weather 交易系统 - 第一阶段紧急优化（代理模式）
# 执行时间: 5-10 分钟
# 风险等级: 低
# 需要停机: 否

set -e

WEATHER_ROOT="/Users/xiong/weather/weather"
cd "$WEATHER_ROOT"

echo "=========================================="
echo "第一阶段：紧急止血优化（代理模式）"
echo "=========================================="
echo ""

# 1. 备份当前配置
echo "[1/6] 备份当前配置..."
BACKUP_FILE=".env.backup.$(date +%Y%m%d_%H%M%S)"
if [ -f .env ]; then
    cp .env "$BACKUP_FILE"
    echo "✓ 配置已备份到: $BACKUP_FILE"
else
    echo "⚠ 警告: .env 文件不存在"
fi
echo ""

# 2. 更新环境变量
echo "[2/6] 更新环境变量..."
cat >> .env << 'EOF'

# ============================================
# 紧急优化配置 - 代理模式 (2026-04-22)
# ============================================

# === 网络优化（保持代理）===
# 禁用强制直连，使用代理
PM_CLOB_FORCE_DIRECT=0
PM_CLOB_FORCE_PROXY=0

# 启用直连优先（失败后自动切换代理）
PM_CLOB_DIRECT_FIRST=0

# 增加重试次数和超时时间
PM_CLOB_RETRY_MAX=5
PM_CLOB_TIMEOUT_MS=60000
PM_CLOB_DIRECT_RETRIES=3

# 缩短代理冷却期（从 900s 降低到 300s）
PM_CLOB_PROXY_COOLDOWN_SEC=300

# 配置代理端口（多个端口用逗号分隔）
# PM_PROXY_PORTS=7890,7891,7892

# Data API 优化
PM_CLOB_TRADES_MAX_PAGES=5
POLY_DATA_API_TIMEOUT=15
POLY_DATA_API_RETRIES=3

# 代理重试优化
PM_CLOB_RETRY_BASE_MS=1000
PM_CLOB_RETRY_MAX_MS=8000

# === 扫描优化 ===
# 增加扫描超时（从 360s 增加到 600s）
PM_SCAN_TIMEOUT_SEC=600

# 增加 Open-Meteo 超时
PM_SCAN_OPEN_METEO_TIMEOUT_SEC=20
PM_SCAN_OPEN_METEO_RETRIES=2

# 禁用全局速率限制（提升并发）
PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT=0

# 减少单次扫描事件数（降低压力）
PM_SCAN_MAX_EVENTS_PER_RUN=3
PM_SCAN_MAX_MATCHED_PER_RUN=3

# === 任务优化 ===
# 减少单任务处理事件数（从 12 降低到 6）
PM_AUTO_TRADE_EVENTS_PER_JOB_MAX=6

# 设置任务超时（5 分钟）
PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC=300

# === 策略优化（观察模式）===
# 启用 shadow 模式（记录但不阻断）
PM_EDGE_POLICY_MODE=shadow

# 降低 path 门槛（从 0.10 降低到 0.05）
PM_EDGE_POLICY_PATH_LCB_MIN=0.05

EOF

echo "✓ 环境变量已更新（代理模式）"
echo ""

# 3. 清理代理冷却状态
echo "[3/6] 清理代理冷却状态..."
if [ -f data/clob_proxy_cooldown.json ]; then
    rm -f data/clob_proxy_cooldown.json
    echo "✓ 代理冷却状态已清理"
else
    echo "✓ 无需清理（文件不存在）"
fi
echo ""

# 4. 验证代理连接
echo "[4/6] 验证代理连接..."
echo "检查系统代理设置..."
if command -v scutil &> /dev/null; then
    scutil --proxy | grep -E "HTTPEnable|HTTPProxy|HTTPPort|HTTPSEnable|HTTPSProxy|HTTPSPort"
fi
echo ""

if node scripts/polymarket_order.mjs balance > /tmp/balance_test.json 2>&1; then
    echo "✓ 代理连接正常"
    cat /tmp/balance_test.json | head -5
else
    echo "⚠ 警告: 代理连接测试失败"
    echo "错误信息:"
    cat /tmp/balance_test.json
    echo ""
    echo "建议检查："
    echo "  1. 代理服务是否运行"
    echo "  2. 代理端口是否正确"
    echo "  3. 环境变量 HTTPS_PROXY 是否设置"
    echo ""
    echo "继续执行其他优化..."
fi
echo ""

# 5. 清理僵尸任务（可选）
echo "[5/6] 清理僵尸任务..."
read -p "是否清理超过 10 分钟的运行中任务? (y/N): " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    CLEANED=$(python3 - <<'PY'
from auto_trade_job_writer import mark_running_jobs_timeout_older_than
print(mark_running_jobs_timeout_older_than("data/archive.db", minutes=10))
PY
    )
    echo "✓ 已清理 $CLEANED 个僵尸任务"
else
    echo "✓ 跳过清理"
fi
echo ""

# 6. 重启守护进程
echo "[6/6] 重启守护进程..."
echo "需要 sudo 权限..."

SERVICES=(
    "com.weather.model_scan"
    "com.weather.auto_trade_worker"
    "com.weather.trade_sync_loop"
)

for service in "${SERVICES[@]}"; do
    echo "  重启: $service"
    if sudo launchctl kickstart -k "system/$service" 2>/dev/null; then
        echo "  ✓ $service 已重启"
    else
        echo "  ⚠ $service 重启失败（可能未安装）"
    fi
done
echo ""

# 完成
echo "=========================================="
echo "第一阶段优化完成！（代理模式）"
echo "=========================================="
echo ""
echo "代理配置说明："
echo "  - 保持使用系统代理"
echo "  - 增强代理重试机制"
echo "  - 缩短代理冷却期"
echo "  - 支持多代理端口轮换"
echo ""
echo "后续步骤："
echo "1. 监控日志（5-10 分钟）："
echo "   tail -f data/model_scan.log"
echo "   tail -f data/logs/trade_engine.log"
echo ""
echo "2. 检查关键指标："
echo "   - 代理连接成功率"
echo "   - 扫描超时次数"
echo "   - 任务队列长度"
echo ""
echo "3. 如需回滚："
echo "   cp $BACKUP_FILE .env"
echo "   sudo launchctl kickstart -k system/com.weather.model_scan"
echo ""
echo "配置备份: $BACKUP_FILE"
echo "=========================================="
