---
status: accepted
authority: L2
owner: weather-system
effective_from: 2026-07-13
---

# 动态三腿生产执行 Schema 切换 Runbook

## 目的与边界

本 Runbook 只为 production `data/archive.db` 原子增加：

- `posterior_outbox_execution_contracts`
- `dynamic_three_leg_transition_plans`
- `dynamic_three_leg_risk_reservations`
- `posterior_execution_intents`
- 对应五个查询索引

它不回填历史、不训练模型、不改 posterior、不启用 production decision writer、不启用 resident submitter，也不下单。训练/OOS/Tier-A 样本不是本迁移的前置。切换前后 `PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED`、`PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED`、`PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED` 必须保持关闭。

## 当前预检

截至 2026-07-14，生产库 schema 预检为 `already_applied`：4 表＋5 索引均存在且兼容。该维护序列保留为未来 schema 变更的唯一入口；在线 monitor、hard-source poller、UI、market refresh、market websocket 与 task worker 写者仍不得直接声明 `--writer-quiescent`，必须先进入维护窗口。

## 唯一执行序列

以下整段可复制执行。`sudo` 会交互请求本机密码；任一步失败都会尝试恢复 system monitor 与 GUI 服务。

```bash
cd /Users/xiong/weather
set -euo pipefail

resume_weather_services() {
  sudo launchctl enable system/com.weather.monitor.daemon || true
  sudo launchctl bootstrap system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
  sudo launchctl kickstart -k system/com.weather.monitor.daemon || true
  scripts/weatherctl.sh start || true
}
pause_weather_services() {
  scripts/weatherctl.sh stop
  for label in com.xiong.weather.ui com.weather.market_ws com.weather.task_queue_worker; do
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || \
      launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/${label}.plist" 2>/dev/null || true
  done
  sudo launchctl disable system/com.weather.monitor.daemon || true
  sudo launchctl bootout system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
}
trap resume_weather_services EXIT

BACKUP_DIR="/Users/xiong/weather/data/backups"
if [ ! -d "$BACKUP_DIR" ] || [ ! -w "$BACKUP_DIR" ]; then
  echo "maintenance backup directory unavailable: $BACKUP_DIR" >&2
  exit 1
fi

pause_weather_services

python3 scripts/controlled_apply_maintenance_window_packet.py \
  --db /Users/xiong/weather/data/archive.db \
  --summary --max-items-output 20

BACKUP="$BACKUP_DIR/archive.pre-dynamic-three-leg.$(date -u +%Y%m%dT%H%M%SZ).db"
sqlite3 /Users/xiong/weather/data/archive.db ".backup '$BACKUP'"

python3 scripts/controlled_apply_maintenance_window_packet.py \
  --db /Users/xiong/weather/data/archive.db \
  --summary --max-items-output 20

python3 scripts/activate_dynamic_three_leg_execution_schema.py \
  --db /Users/xiong/weather/data/archive.db \
  --backup "$BACKUP" \
  --apply \
  --writer-quiescent \
  --confirm ACTIVATE_DYNAMIC_THREE_LEG_EXECUTION_SCHEMA

python3 scripts/activate_dynamic_three_leg_execution_schema.py \
  --db /Users/xiong/weather/data/archive.db
python3 scripts/production_trade_gate_zero_audit.py
python3 scripts/production_transaction_b_no_order_drill.py \
  --backup "$BACKUP"
python3 scripts/production_probability_transaction_b_cutover_readiness.py \
  --db /Users/xiong/weather/data/archive.db --summary

resume_weather_services
trap - EXIT
```

维护窗口检查必须在备份前与 DDL 前各输出一次 `writer_quiescent=true`；任一次失败均停止，不得继续 backup/apply。迁移 apply 必须输出 `state=applied`，随后只读预检必须输出 `state=already_applied`。`production_transaction_b_no_order_drill.py` 只能接受该维护窗口 backup，明确拒绝 live `archive.db`；它必须输出 `temporary_no_order_drill_passed` 且 `auto_orders/auto_trade_decisions/edge_trades` 三项 delta 均为 0。任何失败先恢复服务，再保留 backup 与完整输出审查；禁止手工补表或跳过 exact-schema verification。

迁移前备份验证固定为 SQLite backup 成功后的文件可打开、页数、页大小、application id 与 schema identity 一致性；这在写者已静默的窗口内是迁移放行的硬条件。完整 `PRAGMA quick_check` 只可通过 `--full-integrity-check` 单独作为离线审计运行，不得把外置盘全量扫描放进生产停服窗口。

## 后续边界

第一阶段 schema 已在 2026-07-14 受控维护窗口完成：4 张表与 5 个索引均已验收，且无订单演练使用同一窗口外置备份完成。第二阶段 position-lot/continuation/market-alpha additive schema 也已完成；当前只读验收的三个 schema section 均为 `state=ready`，无缺表或缺索引。Schema 完成不等于 live 激活。resident validator、CLOB place/reconcile adapter、reconcile-first 循环、fill→position/transition/risk reservation、ACK-loss/partial-fill/restart 幂等均已实现并通过自动化测试。default-off launchd unit 已安装到用户 LaunchAgents，但保持 persistent disabled、未加载，仓库与安装 SHA 一致；launcher 使用项目 `.venv`，不使用 macOS Python。真实 T1→T5 生产证据与 accepted activation 仍未满足；未全部通过前不得开启六个 production/live 环境开关。
