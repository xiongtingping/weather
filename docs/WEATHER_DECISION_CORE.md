---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-16
supersedes: [legacy-weather-decision-core, target-realization-v4-decision-core]
implementation_symbols:
  - docs/contracts/TARGET_REALIZATION_V6.md
  - config/trading/target_realization_v6_production_activation.json
verification_gates:
  - python3 scripts/weather_decision_core_audit.py
  - python3 scripts/production_dynamic_three_leg_strategy_contract_audit.py
last_verified_commit: working-tree-2026-07-16-target-realization-v6-generation-2-current
---

# Weather Decision Core

当前交易环权威是 ADR-0006 / Target Realization v6 generation 2；ADR-0005/v5 已 superseded 且不在运行读取链中。

## 唯一概率输入

决策只消费 production v6 已提交的 `posterior_ssot_fact / RealizationState`。它不得读取 raw METAR/SPECI、预测源、温度轨迹、动量、云雨或 UI 重建天气判断；bridge 尚未提供本次 market-bucket projection 时，只阻断交易，不阻断物理 X。

## 唯一入场口径

当前内容寻址 strategy artifact 只允许：

```text
main_yes            当前最高后验桶，YES，≤20 USDC，四态与风险×时段退出
second_yes          当前次高后验桶，YES，峰值前最后30分钟新增，<5 USDC
one_no_slot         trajectory NO≤100 或 exact hard-exclusion NO≤200，互斥
```

三腿随每次 posterior commit 动态重选。缺腿允许，伪造候选、同桶 YES/NO 对冲、多个 NO 桶、current-report NO 和 next-bucket NO 均禁止；所有合格新增 BUY 在资本不足时使用同一 `capital_scale_factor`。

## 唯一退出口径

真实卖单只允许两个互斥原因族，执行器必须精确验证 family/code，不得按前缀放行：

```text
target_realization_main_yes_upside_risk_stage_1..4
target_realization_main_yes_invalidated
market_alpha_take_profit_main_yes_999
market_alpha_take_profit_second_yes_999
market_alpha_take_profit_trajectory_no_999
market_alpha_take_profit_hard_exclusion_no_999
```

所有实际 BUY fill 默认建立 0.999 GTC coverage。主 YES staged/invalidation SELL 必须先释放并 reconcile 对应 coverage，再按冻结 fresh executable bid 提交；其他角色只使用其精确 0.999 code。v5 的 0.90、EV/Edge/LCB、repricing、stop-loss、trail-stop 和前缀通配均已退役，禁止兼容分支或 fallback。

## 决策与执行原子性

`posterior outbox → Transaction C` 在一个 `BEGIN IMMEDIATE` 内提交 claim、risk reservation、transition、`auto_trade_decisions`、intent 和 outbox completion。执行阻断原因唯一来源是 `auto_trade_decisions`；HOLD 不创建 intent。

resident 只消费 durable intent，且 reconcile-first。未知 POST 结果只按原 idempotency/order identity 查询，禁止重发。直接 Bayes BUY 必须在 T1+3000ms 前真正开始 exchange POST；超时永久禁止提交。

## 市场证据边界

决策 snapshot 与 POST 前 execution guard 只读取官方 CLOB WS 的本地 hash 原子快照，以及持续预取的 collateral/allowance。市场证据最大年龄 1000ms，余额证据最大年龄 3000ms。事件时禁止外部 REST fallback；本地证据缺失、过期或 token/side/slug/date 不一致即 fail closed。

## 永久禁止

EV、Edge、LCB、exec_edge、signal_edge、fair value、买入价格上限、dynamic repricing、stop-loss 和 trail-stop 只能用于展示、诊断或历史复盘，不得影响入场、仓位、退出、订单价格或是否下单。

生产策略读取顺序固定为：`SYSTEM_CHARTER → ADR-0005 → TARGET_REALIZATION_V5 → posterior SSOT → strategy/cutover activation`。任一身份不闭合时只允许 fail closed，不允许旧策略 fallback。
