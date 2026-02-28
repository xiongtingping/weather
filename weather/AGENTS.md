# Polymarket Auto Buyer（本地）

## 目标
- 监控 Polymarket 天气市场并通过 CLOB API 下限价买单（默认 dry-run 只打印不下单）。

## 工程原则（必须遵守）
- 根因优先、零技术债务：先全链路分析再给长期方案与最小改动。
- SSOT：同屏展示必须来自**单一只读快照**（建议 `/api/ui_snapshot`）。
- SSOT 写入：`facts.sqlite.*_meta_json` 只补齐不覆盖（merge-fill），禁止弱来源覆盖强来源。
- 禁止 monitor 写入 bot 信号文件：`MONITOR_SIGNAL_LOG` 不能指向 `POLY_SIGNAL_PATH`。
- 监控进程统一由 launchd 托管：禁止手工后台运行 `scripts/start_monitor_*.sh`。
- 监控守护域仅允许 `system/com.polymarket.monitor.daemon`。
- 回应使用中文并称呼“熊先生”。
- 默认聚焦熊先生当前明确要求；不复述历史问题作为背景噪声。
- 若熊先生已否定某个根因假设，不得再次作为当前根因。
- 禁止未经确认触发历史回填/大范围写入；必须先说明影响面并征求确认。
- 若本文件需要新增/修订：保持精简（≤100 行）。

## SSOT 与 UI 口径（硬规则）
- UI/前端只读事实层（`data/archive.db`），不得前端复算第二口径。
- 执行阻断原因唯一来源：`auto_trade_decisions`。
- 退出未触发必须写 `edge_trades.exit_skip_reason/exit_skip_detail/exit_skip_at`。
- 条件口径面板由 `/api/auto-trade-criteria` 生成，前端仅渲染。
- 不适用/缺失必须提供明确 `*_na_reason`。

## Edge 口径（重构后）
- **入场硬门槛：仅看 `edge_realizable`**（扣费后可兑现 edge）。
- **退出硬门槛：仅看 `hold_edge_realizable` + 风险约束**。
- `edge_realizable_score` 仅用于排序/展示，不参与入场阻断。
- `exec_edge_entry/edge_score/signal_edge` 仅诊断与追踪，不参与硬门槛。
- 当 `PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE=1` 时，策略内 `*_require_exec_edge` 不生效。
- 当 `PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE=1` 时，`q/ask/lag/spread_max/volume/depth/overprice/no_by_yes/skip_top` 等门槛降级为软门槛（不阻断，仅展示/惩罚）。

## 时区与统计口径
- 日口径固定 ET（`America/New_York`）。

## 常用运行（本地）
- 运行 bot：`python3 -m bot.main --once` / `python3 -m bot.main`
- 重启 bot 守护：`sudo launchctl kickstart -k system/com.polymarket.bot.daemon`
- 启动/重载监控守护：`sudo bash scripts/install_monitor_launchdaemon.sh && sudo launchctl kickstart -k system/com.polymarket.monitor.daemon`
- SSOT 对齐检查（只读）：`python3 scripts/check_ssot_alignment.py`

## 关键词触发：订单分析
- 触发关键词：`订单分析` / `复盘调优` / `策略体检` / `失败特征` / `共同特征` / `参数建议`
- 默认范围：近 3 天（ET），按市场×策略版本分层
- 输出：成功/失败共同特征、参数建议、影响评估（信号量/胜率/赔率）
- 落地顺序：先只读分析报告 → 熊先生确认 → 再执行参数写入与重启
