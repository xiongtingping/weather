---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-16
activation_state: superseded-by-target-realization-v6-no-fallback
runtime_conformance_state: retired-historical-audit-only
supersedes:
  - TARGET_REALIZATION_V4.md
implementation_symbols:
  - strong_posterior_three_leg_strategy.py
  - dynamic_three_leg_transition_runtime.py
  - production_dynamic_three_leg_input_provider.py
  - production_posterior_commit_outbox_worker.py
  - resident_dynamic_three_leg_execution_loop.py
  - resident_dynamic_three_leg_intent_submitter.py
  - production_market_alpha_take_profit_coordinator.py
verification_gates:
  - python3 scripts/production_dynamic_three_leg_strategy_contract_audit.py
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-production-dynamic-three-leg-v5
---

# Target Realization v5：已退役历史合同

本层只消费 latest committed `posterior_ssot_fact / RealizationState`、冻结市场/资本/风险快照和 position-lot SSOT，不读取 raw weather 形成第二套判断。

| 槽位 | 资格 | 上限 | 退出 |
|---|---|---:|---|
| `top_yes_wave` | 当前最高后验可达桶 | 20 USDC | 当前 top 有效时 `market_alpha_take_profit_top_yes_wave_completed`，0.90 |
| `second_yes` | 当前次高可达桶、峰前最后30分钟 | 4.99 USDC | 小额结算；目标失效走 target realization |
| `strongly_excluded_no` | exact 联合物理硬排除证书 | 100 USDC | 目标/证书失效走 target realization |

三腿随每个 posterior 动态重选，SELL_OLD/reconcile 先于 BUY_NEW。唯一 market-alpha code 是 `market_alpha_take_profit_top_yes_wave_completed`；其他退出只允许批准的 `target_realization_*` 精确闭集。0.999、trajectory NO、hard NO 200、主 YES staged SELL 都不属于 v5。

该合同已由 ADR-0006/Target Realization v6 generation 1 原子取代，generation 2 继续保持无 fallback。本文只用于历史订单审计，不得产生 decision、intent 或真实 POST。
