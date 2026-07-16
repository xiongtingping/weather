# Polymarket Auto Buyer（本地）

## 目标
- 监控 Polymarket 天气市场并通过 CLOB API 下限价买单（默认 dry-run 只打印不下单）。

## 工程原则（必须遵守）
- 根因优先、零技术债务：先全链路分析再给长期方案与最小改动。
- SSOT：同屏展示必须来自**单一只读快照**（建议 `/api/ui_snapshot`）。
- SSOT 写入：`facts.sqlite.*_meta_json` 只补齐不覆盖（merge-fill），禁止弱来源覆盖强来源。
- 禁止 monitor 写入 bot 信号文件：`MONITOR_SIGNAL_LOG` 不能指向 `POLY_SIGNAL_PATH`。
- 监控进程统一由 launchd 托管：禁止手工后台运行 `scripts/start_monitor_*.sh`。
- 监控守护域仅允许 `system/com.weather.monitor.daemon`；`system/com.polymarket.monitor.daemon` 归 `/Users/xiong/poly` 使用，禁止覆盖。
- 回应使用中文并称呼“熊先生”。
- 默认聚焦熊先生当前明确要求；不复述历史问题作为背景噪声。
- 主线任务默认自动推进到可验证闭环，不在普通中间确认点停住。
- 默认直接实施并验证，不以 review-only/仅建议作为交付；明确退役的旧代码直接删除，不保留兼容分叉。
- 生产概率链只允许当前唯一主模型；禁止旧模型 fallback，主模型不可用时必须 fail closed。
- 外置盘仅用于非关键、无用数据的异步冷归档；禁止作为生产数据库备份、回滚、切换、readiness 或服务启停依赖，任何外置盘异常不得影响 AMSC/METAR/SPECI、SSOT 或交易。
- 需要熊先生执行交互 sudo 时，必须同步给出可复制的完整指令。
- 每次阶段性收尾时自动说明“下一个主线任务”。
- 若熊先生已否定某个根因假设，不得再次作为当前根因。
- 若本文件需要新增/修订：保持精简（≤100 行）。

## SSOT 与 UI 口径（硬规则）
- UI/前端只读事实层（`data/archive.db`），不得前端复算第二口径。
- UI 默认按桌面看板体验设计与验收；除非明确要求，不考虑手机端适配。
- 执行阻断原因唯一来源：`auto_trade_decisions`。
- 退出未触发必须写 legacy 执行账本表 `edge_trades.exit_skip_reason/exit_skip_detail/exit_skip_at`；`edge_trades` 只代表持仓/订单状态存储，不代表交易策略口径。
- 条件口径面板由 `/api/auto-trade-criteria` 生成，前端仅渲染。
- 不适用/缺失必须提供明确 `*_na_reason`。
- 架构与策略说明使用本系统术语；不得引入 `trust_weight` 等外部抽象。`settlement_actual` 仅指天气市场已解析结果。

## Bayes 二元结算口径（当前）
- 交易策略只保留 Bayes 二元结算/目标桶实现语义。
- 入场只允许 posterior settlement、SSOT target realization、hard-source direct SSOT target 这类 Bayes 目标候选触发。
- 退出只允许 L1 合同闭集中的 `target_realization` 或独立 `market_alpha_take_profit` family/code 触发，禁止仅按前缀放行；后者仅适用于入场时已冻结目标价、绑定批准策略 artifact 的 `top_yes_wave`，不得改名伪装成天气目标失效。
- 旧 `edge_min`、`hold_edge_realizable`、`lcb_*`、`stop_loss`、`trail_stop`、`weather_repricing*` 不得触发真实卖单；Edge/LCB/exec_edge/signal_edge/旧收益退出/repricing 字段仅保留为上游供数、展示、诊断、兼容或复盘，不参与入场、仓位、退出或下单硬门槛。

## 时区与统计口径
- 日口径固定 ET（`America/New_York`）。

## 常用运行（本地）
- 运行 bot（强制 dry-run）：`python3 -m bot.main --once` / `python3 -m bot.main`
- 显式 Live：`python3 -m bot.main --once --live --confirm-live I_UNDERSTAND_THIS_PLACES_REAL_ORDERS`
- 重启 bot 交易引擎：`bash scripts/weather_service_control.sh restart bot`
- 启动/重载监控守护：`sudo bash scripts/install_monitor_launchdaemon.sh && sudo launchctl kickstart -k system/com.weather.monitor.daemon`
- SSOT 对齐检查（只读）：`python3 scripts/check_ssot_alignment.py`
- 决策主线审计（只读）：`python3 scripts/weather_decision_core_audit.py`

## 关键词触发：订单分析
- 触发关键词：`订单分析` / `复盘调优` / `策略体检` / `失败特征` / `共同特征` / `参数建议`
- 默认范围：近 3 天（ET），按市场×策略版本分层
- 输出：成功/失败共同特征、参数建议、影响评估（信号量/胜率/赔率）
- 落地顺序：先只读分析报告 → 熊先生确认 → 再执行参数写入与重启
