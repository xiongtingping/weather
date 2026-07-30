---
status: accepted
authority: L0
owner: weather-system
effective_from: 2026-07-16
activation_state: superseded-by-adr-0006-no-fallback
supersedes:
  - ADR-0004-DYNAMIC-THREE-LEG-V4-POSTERIOR-NO-STANDING-EXIT.md
implementation_symbols:
  - docs/contracts/TARGET_REALIZATION_V5.md
  - config/trading/dynamic_three_leg_production_activation.json
  - config/trading/market_alpha_take_profit_production_activation.json
  - dynamic_three_leg_release_runtime.py
verification_gates:
  - python3 scripts/production_dynamic_three_leg_strategy_contract_audit.py
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-production-dynamic-three-leg-v5
---

# ADR-0005：已退役动态三腿 v5

## 当前生产语义

```text
主 YES：当前最高后验桶，最高 20 USDC；0.90 波段退出，不吃结算
次 YES：当前次高后验桶；峰值前最后 30 分钟入场；4.99 USDC；小额结算
联合物理排除 NO：exact hard certificate 明确排除的桶；最高 100 USDC
```

角色随每个 committed posterior 动态重选。生产永久禁止 EV、Edge、LCB、fair value、买入价格上限、repricing、stop-loss、trail-stop、remote NO、当前报文温度 NO 与下一个温度桶 NO。

决策与 POST 前 guard 只消费官方 CLOB WS 的本地 hash 原子快照及持续预取的余额/allowance；事件热路径没有外部 REST fallback。Transaction C 原子提交 decision/reservation/intent/outbox；resident reconcile-first，T1+3000ms 后禁止 exchange POST。

## Successor 边界

ADR-0006/Target Realization v6 已完成 production activation，本文及其 0.90/NO100 执行语义原子 superseded 且禁止 fallback。历史文档只解释旧成交血缘，不得被任何 resident 重新加载。
