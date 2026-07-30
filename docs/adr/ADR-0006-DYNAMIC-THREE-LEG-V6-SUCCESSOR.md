---
status: accepted
authority: L0
owner: weather-system
effective_from: 2026-07-16
activation_state: production-active-content-addressed-generation-2
successor_to:
  - ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md
implementation_symbols:
  - docs/contracts/TARGET_REALIZATION_V6.md
  - pending:target_realization_v6_runtime
  - pending:standing_exit_999_runtime
verification_gates:
  - python3 scripts/target_realization_v6_document_contract_audit.py
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-target-realization-v6-successor
---

# ADR-0006：当前生产动态三腿 v6

## 决策

批准并已激活 [Target Realization v6](../contracts/TARGET_REALIZATION_V6.md) 作为唯一 production 交易环；v5 已 superseded 且禁止 fallback：

```text
main YES     latest posterior 第一桶，≤20 USDC
second YES   latest posterior 第二桶，峰前最后30分钟新增，<5 USDC
one NO slot  trajectory NO≤100 或 hard-exclusion NO≤200
all fills    默认 standing SELL @0.999
```

交易只消费同一 committed v6 weather posterior/RealizationState。温度动量、轨迹、云雨、露点、Termination Risk 和 METAR/SPECI 必须先进入唯一 Bayes 后验，禁止策略层重算第二天气链。

## 主 YES

主 YES 只允许 `normal/upside_risk/hard_locked/invalidated` 四态。风险累计目标按 `P_above_top` 的 10/20/35/50% 边界映射为 25/50/75/100%，时段只提供 25/50/75/100% 最大上限；唯一实际目标为：

```text
min(risk_target_ratio,time_cap_ratio)
```

上方全部桶有 hard certificate 时锁定当前主 YES，不再阶段退出；当前主桶自身 hard exclude 时才失效。阶段水位只在 `main_yes_position_cycle_id` 内单调。exact hard lock，或连续两条新的 canonical 强观测均为低风险且无 invalidation，并完成旧 SELL/coverage reconcile 后，才可创建新周期再入场。

## NO 与资本

trajectory NO 只读每桶独立 `q_NO=1-p_yes`，按 70/80/90/95/98% 映射为 10/25/50/75/100 USDC。hard-exclusion NO 必须有 exact full-support certificate，200 USDC 只是绝对上限，不是固定足额目标。

原始新增请求超过冻结可分配资本时，同一 TradingPlan 的全部合格 `BUY_NEW` 使用统一 `capital_scale_factor` 原子缩量；不得按执行顺序先到先得，也不得用价格、EV、Edge 或 LCB 决定比例。trajectory 与 hard NO 永远互斥，系统仍是三腿。

## 退出与原子性

四种角色的实际 BUY fill 默认建立 0.999 GTC standing exit。主 YES staged/invalidation SELL 必须先释放对应 coverage 并 reconcile，再按冻结 executable bid 执行 target-realization；禁止双 SELL。

退出只允许 Target Realization v6 精确闭集中的 `target_realization` 或 `market_alpha_take_profit` family/code。旧 v5 的 0.90 `market_alpha_take_profit_top_yes_wave_completed`、EV/Edge/LCB、repricing、stop-loss、trail-stop 与旧 NO 生成器不得进入 successor runtime。

Transaction C 必须把角色仲裁、库存冲突、统一资本缩量、decision、reservation、intent 和 outbox completion 原子提交。`auto_trade_decisions` 仍是执行/阻断原因唯一来源；网络 POST 只由 durable intent 幂等执行。

生产执行 owner 唯一固定为 `com.weather.dynamic_three_leg_execution`。旧 `auto_trade_entry_1m`、`trade_engine`、`auto_trade_worker`、`trade_sync_loop` 与 `target_realization_worker` 必须持久 disabled 且不得 loaded；readiness 任一发现即阻断切换。

受管 pause 期间 CLOB gateway 必须继续提供 health、lookup、reconcile/cancel 能力，但 `place-order` 在任何本地幂等写、签名或 exchange POST 前读取 `live_paused.flag` 并 fail closed。strategy artifact 必须内嵌批准时的不可变权威文档快照并自校验；runtime closure 则持续绑定当前可执行源码，两类证据不得混为“活文档一改就让生产失效”。

## Current / Superseded 边界

部署状态只由内容寻址 activation 判定。当前 generation 2：

```text
CURRENT/DEPLOYED = ADR-0006 + Target Realization v6 + generation-2 artifacts
SUPERSEDED/NO-FALLBACK = ADR-0005 + Target Realization v5
```

generation 1 已在受管维护窗口完成 strategy/cutover artifacts、schema、runtime、精确 reason allowlist、全量审计、双 activation 与 pause 线性化；generation 2 随后修复重复 cutover 幂等性并再次完成 acceptance。ADR-0005/v5 保持 superseded 且禁止 fallback。任何文档、artifact、runtime 混代必须 fail closed。
