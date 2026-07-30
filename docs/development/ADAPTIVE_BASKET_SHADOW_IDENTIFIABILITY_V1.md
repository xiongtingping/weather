---
status: superseded
authority: L2
owner: basket-target
effective_from: 2026-07-23
---

# Adaptive Basket Shadow 可识别性合同 v1

状态：`SHADOW_ONLY`  
生产授权：`false`  
订单提交：`0`  
适用里程碑：`M4-15 DONE`、`M8-11 PARTIAL`

## 1. 核心结论

公开成交只能证明已成交后的可见行为，不能直接识别源账户的私有挂单、队列位置、精确分配权重或紧迫阈值；因此本合同把参数写成预注册候选族，并要求完整的历史决策点证据通过三层硬门后才允许回放、排名或晋级。

当前 229 个成熟中国八站事件中，可执行候选回放事件为 0；正确结果是 `BLOCKED_EVIDENCE`，不是 `CAPITAL_FIRST` 与 `SHARE_FIRST` 表现相同。

## 2. 范围与非目标

本合同负责：

- 固定训练与 holdout 的互斥时间边界；
- 生成内容寻址的候选参数网格；
- 定义一个历史决策点必须具备的 strict-as-of 证据向量；
- 分离 allocation、execution 和 source replication 三种可识别性；
- 对缺字段、未来泄漏、伪多档深度和私有订单缺口 fail closed。

本合同不负责：

- 推断源账户私有模型或未成交挂单；
- 用公开成交反推精确阈值；
- 把 topbook 或 top-N 聚合深度伪装成逐价位深度；
- 下单、访问 gateway、写生产数据库或修改生产 activation。

## 3. 候选族而非账户点估计

候选集合定义为：

```text
C = A × W × T

A = {CAPITAL_FIRST, SHARE_FIRST}
W = {observed_baseline, time_dominant, gap_dominant,
     fill_dominant, liquidity_dominant}
T = {0.55, 0.67, 0.80}
|C| = 2 × 5 × 3 = 30
```

每个候选 ID 由 allocator、五因子权重和 Taker 紧迫阈值的规范化内容计算 SHA-256；相同输入必须生成相同 ID，不允许以运行时间、样本结果或人工标签参与寻址。

五因子紧迫度只选择执行表达，不改写概率、中心桶或方向：

```text
U = w_time·u_time + w_gap·u_gap + w_fill·u_fill
  + w_spread·u_spread + w_depth·u_depth

urgent = 1[U >= taker_urgency_threshold]
Σw = 1, 0 <= w_i <= 1, 0 < threshold < 1
```

`observed_baseline` 只是候选族名称，不表示已从源账户识别出真实权重。

## 4. 互斥时间切分

```text
TRAIN   : event_date <= 2026-07-13
HOLDOUT : 2026-07-14 <= event_date <= 2026-07-20
EXCLUDED: 其余日期
```

必须满足 `train_end < holdout_start <= holdout_end`；训练、调参或候选筛选不得读取 holdout，2026-07-21 及以后的未成熟事件不得进入当前晋级结论。

## 5. DecisionPointEvidence 数据合同

Schema：`adaptive_basket_decision_point_evidence_v2`

| 证据组 | 必需字段 | 语义 |
|---|---|---|
| 决策身份 | `evidence_id`、`event_id`、`decision_at` | 唯一历史决策点；时间必须带时区 |
| 阶段概率 | `probability_stage`、完整桶概率、posterior version/SHA/observed_at | 只接受已物化 `P0-M`、`P0-F`、`P0-F^r` 或 `P1`；中心与方向均由本阶段 `p_s(B)` 计算 |
| 盘口 | quote version/observed_at/source、完整桶覆盖 | 来源必须是 `OFFICIAL_CLOB_WS` |
| 逐档深度 | `quote_levels_complete` | 必须有逐价位 asks；聚合 top-N 不合格 |
| 库存 | inventory version/observed_at | 只计决策点前已确认成交 |
| 订单状态 | order-state version/observed_at、maker queue、dated order family | 识别等待、撤单、迟到成交和补仓时点 |
| Lineage | `lineage_complete` | 所有输入版本可追溯且属于同一代际 |
| 源账户私有状态 | `source_pending_orders_observable` | 单独判断是否能声称复刻源账户 |

统一 strict-as-of 规则：

```text
valid(x) = 1[x.version exists]
         × 1[x.observed_at exists]
         × 1[x.observed_at <= decision_at]
```

任何 `observed_at > decision_at` 都是未来泄漏；等于决策时刻允许通过。

## 6. 三层可识别门

### 6.1 Allocation eligibility

```text
G_alloc = G_stage_probability × G_quote_topbook
        × G_inventory × G_lineage
```

它回答：在不模拟挂单队列和逐档成交的前提下，能否诚实比较支持集、方向账本和资金/份额 allocator。

### 6.2 Execution eligibility

```text
G_exec = G_alloc × G_multilevel_depth × G_order_state
       × G_maker_queue × G_dated_order_family
```

它回答：能否重放 Maker 等待、撤单、部分成交、补仓以及有价格上限的多档 FOK；只有 topbook 时必须失败。

### 6.3 Source replication identifiability

```text
G_source = G_exec × G_source_private_pending_observable
```

它回答：能否声称复刻源账户本身；即使本系统未来完整保存自己的 Shadow order-state，也不能因此倒推出源账户历史私有 pending/cancelled/unknown。

三层关系严格为：

```text
G_source <= G_exec <= G_alloc
```

通过较低层不代表通过更高层；源账户私有状态缺失不得阻断本系统未来 Shadow 执行回放，但必须阻断“已复刻源账户”的结论。

## 7. 关键失败码

| 门 | 失败码 | 含义 |
|---|---|---|
| Allocation | `materialized_stage_probability_missing` | 决策点前没有有效 P0-M/P0-F/P1 阶段概率 |
| Allocation | `posterior_bucket_distribution_incomplete` | 概率桶不完整或未归一化 |
| Allocation | `posterior_not_strict_asof` | 后验晚于决策点或缺时间 |
| Allocation | `official_clob_ws_bucket_coverage_incomplete` | 所需桶盘口未全覆盖 |
| Allocation | `inventory_not_strict_asof` | 库存不是决策点前确认状态 |
| Execution | `official_clob_ws_multilevel_depth_missing` | 缺逐价位 asks |
| Execution | `order_state_not_strict_asof` | 订单状态缺失或晚于决策点 |
| Execution | `maker_queue_state_unknown` | Maker 队列不可回放 |
| Execution | `dated_order_family_detail_missing` | 订单族缺日期与状态明细 |
| Source | `source_private_pending_orders_unobservable` | 源账户私有挂单不可见 |

失败码必须保留原始层级；不得把 execution 缺口降级成 allocation 通过后的警告。

## 8. 当前历史审计结果

| 指标 | 结果 |
|---|---:|
| 成熟中国八站事件 | 229 |
| TRAIN / HOLDOUT | 173 / 56 |
| 建仓窗口内有效 P0-M | 0 |
| 建仓窗口内有效 P1 | 1 |
| 独立天气方向输入 | 不允许；方向由当前阶段概率确定 |
| 官方 WS 完整桶 topbook | 0 |
| 官方 WS 逐价位多档深度 | 0 |
| confirmed filled inventory 可重建 | 229 |
| 权威历史 pending/order-state | 0 |
| allocation replay eligible | 0 |
| execution replay eligible | 0 |
| source replication identifiable | 0 |

唯一局部 P1 事件是成都 2026-07-17：建仓窗口内有 40 个 P1 checkpoint、11 个概率桶，但代表决策点仅 6 个 outcome 在 5 秒新鲜窗内，且官方 WS 完整桶覆盖与权威订单状态均缺失，因此仍不能进入候选比较。

30 个候选统一返回 `NOT_EVALUATED_MISSING_EVIDENCE`，`promotion_permitted=false`；这些结果没有收益、排名或胜者字段。

## 9. 晋级合同

```text
promotion_permitted =
    1[train 中有完整可执行向量]
  × 1[holdout 中有完整可执行向量]
  × 1[train/holdout 互斥]
  × 1[候选在比较前预注册]
  × 1[预定义安全、行为和收益门全部通过]
```

若任一乘子为 0：

- M4-15 可以因候选族、数据合同和 fail-closed 门已经实现而标记 `DONE`；
- M8-11 必须保持 `PARTIAL`；
- 不得输出候选排序、收益差、最优 allocator 或 live 建议。

## 10. 前向 ShadowEvidenceCapture 要求

下一阶段只能建立与生产执行隔离的前向证据捕获：

1. 在同一 Shadow 决策点原子冻结 P0-M/P0-F/P1 当前阶段概率、官方 CLOB WS 全桶逐档深度、confirmed inventory、Shadow 自有 order-state 与七字段版本向量；方向由冻结的 `p_s(B)` 确定，不接收独立天气方向字段。
2. 同时保存 `decision_at`、接收时间、来源序列、内容哈希和 freshness 判断。
3. Shadow 自有 pending/cancelled/unknown 可用于本系统 execution replay；源账户私有 pending 永久保留不可识别标记。
4. 缺任一字段只产生阻断回执，不生成补值、不提交订单、不写生产数据库。
5. 完整证据积累出互斥 train/holdout 之前，M8-11 不得晋级。

## 11. 可重复验收

```bash
python3 -m pytest -q tests/test_adaptive_basket_shadow_identifiability.py
python3 scripts/adaptive_basket_shadow_acceptance.py
python3 data/reports/polymarket_wallet_0x8fbd7cf5_20260721/audit_adaptive_basket_historical_evidence.py
python3 data/reports/polymarket_wallet_0x8fbd7cf5_20260721/analysis/execute_notebook_stdlib.py data/reports/polymarket_wallet_0x8fbd7cf5_20260721/analysis/adaptive_basket_identifiability_oos_v1.ipynb
```

最低验收：30 个内容寻址候选、互斥时间切分、所有单字段失败 fail closed、future leakage 拒绝、topbook-only 拒绝执行、低→高→低回绕不保留隐藏通过态、100 次重建 ID 一致、`orders_submitted=0`。
