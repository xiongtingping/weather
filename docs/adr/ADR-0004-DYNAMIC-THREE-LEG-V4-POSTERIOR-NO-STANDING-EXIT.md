---
status: superseded
authority: L0
owner: weather-system
effective_from: 2026-07-16
activation_state: never-activated-retired
superseded_by: ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md
supersedes:
  - legacy-dynamic-three-leg-v3-policy
implementation_symbols:
  - docs/contracts/TARGET_REALIZATION_V4.md
  - pending:target_realization_v4_runtime
  - pending:standing_exit_999_runtime
verification_gates:
  - python3 scripts/document_authority_audit.py
  - python3 scripts/target_realization_v4_document_contract_audit.py
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-target-realization-v4-contract
---

# ADR-0004：动态三腿 v4、Posterior NO 与 Standing Exit

> **已退役**：本文未形成 production runtime；当前 production 是 ADR-0005，唯一 successor 是 ADR-0006。本文不得作为 v6 fallback。

## 决策

批准 [Target Realization v4](../contracts/TARGET_REALIZATION_V4.md) 为动态三腿交易的唯一目标合同。v4 只消费 production v6 已提交的 `posterior_ssot_fact / RealizationState`，不得读取 raw METAR/SPECI、预测源、温度轨迹、温度动量或市场价格重新形成天气判断。

v4 固定三个经济槽位：

```text
主 YES：latest posterior 第一桶，最高 20 USDC
次 YES：latest posterior 第二桶，峰值前最后 30 分钟，4.99 USDC
唯一 NO：trajectory_no 最高 100 USDC
         或 hard_exclusion_no 最高 200 USDC
```

轨迹 NO 与硬排除 NO 是一个 NO 槽位的两个互斥模式，不得扩张为第四腿。完整资本边界分别为 124.99 USDC 与 224.99 USDC，唯一身份为 `dynamic_three_leg_capital_policy_v4_top20_second4_99_trajectory100_hard200`。这些金额均为上限；资本不足时同一计划的合格新增 BUY 使用统一 `capital_scale_factor` 原子缩量，硬排除 NO 不要求足额 200 USDC。

## 主 YES 状态机

主 YES 不再采用固定 0.90 波段退出。每次 posterior commit 都从同一 SSOT 读取 `P_above_top`、峰值时段与硬证书集合，并进入唯一模式：

```text
normal：当前桶有效、未硬锁定且 P_above_top<10%，保持 0.999
upside_risk：更高桶仍可达，按风险目标与时段上限的较小值累计卖出
hard_locked：当前桶有效且全部更高桶有硬证书，停止阶段卖出
invalidated：当前桶自身硬排除或角色失效，执行 target-realization
```

模式优先级为 `invalidated > hard_locked > upside_risk > normal`。风险目标在 `P_above_top` 的 `<10/10–20/20–35/35–50/>=50%` 区间分别为 `0/25/50/75/100%`；时段上限按峰值前 `>60m/30–60m/最后30m/峰后风险仍在` 分别为 `25/50/75/100%`。唯一累计卖出目标是 `min(risk_target_ratio,time_cap_ratio)`，时间本身不能触发卖出，风险也不能越过时段上限。

阈值、时段表与持仓周期水位必须冻结在内容寻址 strategy artifact 中。多条 SPECI 可以更新后验，但同一 `main_yes_position_cycle_id` 内不能重复卖出同一阶段。已阶段卖出的同桶容量不是当日永久禁买：exact `hard_locked`，或连续两条新的 canonical 强观测均满足 `P_above_top<10%` 且无 invalidation，并完成旧 SELL/coverage reconcile 后，才可创建新周期并重置水位；continuation 与行情刷新不得伪造确认。原主 YES 降为次 YES 时可在窗口外保留最多 4.99 USDC，但窗口外不得新增或补仓。

“更高桶硬不可达”和“当前桶自身硬不可达”必须严格分离：前者锁定主 YES，后者使主 YES 失效。soft unreachable、低 posterior 或有限核截断都不能生成 hard lock。

## NO 侧后验与资本

对每个二元桶独立定义：

```text
q_NO(B) = 1 - p_yes(B)
```

NO 概率不得跨桶归一化。轨迹 NO 只使用同一 committed posterior，金额单调分档为：

| `q_NO` | 目标金额 |
|---:|---:|
| `<70%` | 0 USDC |
| `70%–<80%` | 10 USDC |
| `80%–<90%` | 25 USDC |
| `90%–<95%` | 50 USDC |
| `95%–<98%` | 75 USDC |
| `>=98%` | 100 USDC |

降档采用 1 个百分点滞回。`soft_unreachable=true` 但 `q_NO<70%` 属于 SSOT 投影不一致，必须 fail closed，不能在交易层选边。

硬排除 NO 只接受绑定 exact posterior、物理 hard support、`X→S→B` bridge、bucket contract 和 authority SHA 的证书；200 USDC 只是上限，不是固定足额目标。统一资本分配器允许按 `capital_scale_factor` 缩量，不得因不足 200 单独阻断该腿，也不得用 EV、Edge、价格或收益决定比例。多个硬排除桶也只能确定一个 NO 槽位。

## 退出原因闭集

只允许两个互斥 family：

```text
target_realization
market_alpha_take_profit
```

v4 新增的 `target_realization` code：

```text
target_realization_main_yes_upside_risk_stage_1
target_realization_main_yes_upside_risk_stage_2
target_realization_main_yes_upside_risk_stage_3
target_realization_main_yes_upside_risk_stage_4
target_realization_main_yes_invalidated
```

默认 0.999 standing take-profit 的 `market_alpha_take_profit` code：

```text
market_alpha_take_profit_main_yes_999
market_alpha_take_profit_second_yes_999
market_alpha_take_profit_trajectory_no_999
market_alpha_take_profit_hard_exclusion_no_999
```

旧 `market_alpha_take_profit_top_yes_wave_completed`、0.90、0.98 NO 价格门、EV、Edge、LCB、repricing、stop-loss 与 trail-stop 全部退出 v4 production 闭集。

## Standing Exit 原子边界

每笔 BUY 的实际 fill 默认创建 0.999 GTC SELL coverage。主 YES `upside_risk/invalidated` 是唯一明确例外：执行器必须先撤销对应 0.999 coverage，确认 cancel/remaining size，再按冻结 executable bid 创建不可追价的 target-realization SELL。主 YES `hard_locked` 保留 0.999 或自然结算。

本地原子事务必须提交 posterior、decision、risk reservation、intent、position-lot version、standing coverage、`main_yes_position_cycle_id`、stage watermark 与 `capital_scale_factor`；网络 POST 不得伪装成数据库原子操作。ACK unknown、partial fill、cancel unknown 和 coverage 不完整时只允许 reconcile，禁止替代订单或重叠卖量。

## 互斥与库存

- 同桶不得同时持有或准备 YES 与 NO；
- NO 同时只能有一个 bucket exposure；
- 旧仓、off-role、未决阶段 SELL 与未成交 standing exit 全部占用组合额度；
- 主 YES 阶段卖出和角色变化必须先 SELL/reconcile，再对冲突新桶 BUY；
- 所有阻断原因只写入 `auto_trade_decisions`，UI 不重算。

## Activation 边界

本 ADR 是 accepted 开发权威，不是生产 activation。只有以下内容作为一个内容寻址 release 原子闭合后才可 live：

1. v4 L1 合同与两个 family/code 闭集；
2. RealizationState、composer、capital policy、position-lot、standing coverage schema；
3. idempotent resident、cancel/reconcile 与 partial-fill 测试；
4. EV/Edge gate-zero、同桶互斥、资本和 signal-to-order 审计；
5. accepted strategy/cutover artifact 与受管 activation。

任一项缺失时 v4 production 必须 fail closed；不得继续运行 v3、不得把 v4 文档当成订单授权，也不得回退旧 0.90/硬 NO 100 路径。
