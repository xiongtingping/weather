---
status: superseded
authority: L2
owner: basket-target
effective_from: 2026-07-23
---

# AdaptiveBasketPlan v5：后验唯一方向 + B13 + B14 + B15 + Maker/Taker Shadow

## 结论

`adaptive_basket_shadow` 是与现有跟单和 `basket_strategy` 生产包完全分离的候选模块：它只把已物化阶段概率、官方 CLOB WebSocket 多档盘口和库存事实转换成可重放计划，不读取数据库、不联网、不创建订单、不调用 gateway。

## 运行边界

- `shadow_only=true`、`places_orders=false`、`writes_production_db=false`、`calls_order_gateway=false` 和 `orders_submitted=0` 是固定边界，不能配置覆盖。
- 包内禁止导入生产篮子、跟单、`reported_trajectory_no_runtime`、数据库、网络或子进程模块。
- CLI 只读 JSON，拒绝数据库输入和输出；当前不是 activation、daemon、scheduler 或第四个服务。
- v5 只接受 `OFFICIAL_CLOB_WS` 多档 asks；旧 `ask_price + executable_depth_shares` 单档输入被合同拒绝，避免伪造 VWAP。
- v5 删除第二套天气方向输入：中心与方向都只由当前阶段桶概率决定，方向为上下尾偏斜符号，偏斜不足时保持中性。

## 合同层级

1. `MaterializedEvidence`：只接收已物化 P0-M/P0-F/P1、版本、SHA256 和分歧摘要，不读取原始 TAF/METAR/NWP；出现未知方向字段直接拒绝。
2. `AdaptiveBasketSnapshot v5`：绑定 posterior、quote、inventory、order-state，并要求带时区的 quote 时间和严格递增多档 asks。
3. `AdaptiveBasketPolicy v5`：锁定 allocator、角色预算、coverage、clip、手续费、滑点、最大报价年龄及五因子紧迫度权重。
4. `TargetPriceResult`：B14 内部求出后验边际价值、期望收益、保护情景、预算共同允许的全成本上限。
5. `MakerShadowCandidate/Event/Receipt`：表达 post-only 反事实与队列、撤单、迟到成交、未知 POST 的确定性回放。
6. `TakerDepthSnapshot`：把一个桶的官方 WS asks、quote version、观察时间、求值时间和 age 内容寻址。
7. `TakerShadowCandidate`：表达 `FOK BUY YES @ maximum_exchange_price` 的零订单候选，绑定 B14、深度和 posterior/snapshot/quote/inventory/order-state lineage。
8. `TakerShadowReceipt`：原子返回全成或零成交，以及 VWAP、毛成本、fee、slippage、全成本、边际档和消耗档数。
9. `UrgencyEvaluation`：物化 time/gap/fill/spread/depth 五因子分量、权重、触发向量与内容寻址 ID。
10. `ExecutionGateDecision`：物化十二因子合取硬门及七字段版本向量；任一因子失败即零候选。
11. `AdaptiveBasketPlan/ZeroOrderReplayReceipt v5`：保存状态、逐桶目标、Maker/Taker 候选与回执，并拒绝序号倒退、时间倒退和同 ID 异内容。

## B13 执行硬门

```text
ALLOW_b = EvidenceFresh * ContractMatch * BudgetOK * OrderStateKnown
        * RiskInputsComplete * RiskFloorOK * 1[delta_b > 0]
        * TriggerChanged * ClipMinimumMet * 1[a_eff <= limit_b]
        * VersionVectorMatch * CandidatePresent
```

版本向量固定为 `posterior_version / posterior_sha256 / snapshot_sha256 / quote_version / inventory_version / order_state_version / target_price_id`；任何混代都 fail closed。

## B15 触发与紧迫度

```text
U_b = 0.45*time + 0.25*gap + 0.15*(1-fill_rate)
    + 0.075*spread_quality + 0.075*depth_coverage
```

`checkpoint / quote / fill / time_gate` 任一变化只打开“允许重新求值”的触发门，不直接产生订单；盘口与紧迫度不得改写概率、中心或方向。

## Taker 计算与状态

对请求份额 `q`，按 ask 价格升序逐档取量 `x_i`：

```text
sum_i x_i = q
VWAP_ask(q) = sum_i(p_i * x_i) / q
fee(q) = fee_rate * sum_i(p_i * x_i)
a_eff(q) = VWAP_ask(q) + fee(q)/q + slippage_buffer
maximum_exchange_price = tick_floor((B14_all_in_limit - slippage_buffer)/(1+fee_rate))
```

只有同时满足以下条件才标记 `TAKER_FOK`：

- quote 来源为官方 CLOB WS，年龄不超过策略上限，candidate/depth 的市场、日期、桶和 quote version 完全一致；
- 所有被吃档价格 `<= maximum_exchange_price`，且可成交份额精确覆盖 `q`；少 `1e-9` 也视为不可全成；
- `a_eff(q) <= B14_all_in_limit`，且全成本 clip 不超过预算上限；
- 模拟结果必须是 `FILLABLE`，不得把部分可成误写成成功或自动猜测重试。

失败状态为 `FOK_NOT_FILLABLE`、`PRICE_LIMIT_BREACHED`、`STALE_QUOTE`、`INVALID_DEPTH`、`VERSION_MISMATCH`、`RECONCILE_REQUIRED`；所有失败回执 `filled_shares=0`，只有 `FILLABLE` 才等于完整请求份额。

## 天气状态机与执行表达

支持集仍由后验 coverage、熵、模型分歧、方向一致性和既有库存决定；盘口只影响 Maker/Taker 表达，不改写 `p(B)`、中心桶或方向。状态优先级为硬停止门 → stale/无触发 HOLD → FILL_GAP → EXTEND_LOW/HIGH → REPLENISH → HOLD，`STOP` 为吸收态。

- Maker 有效成本：`maker_price*(1+fee_rate)+queue_cost`。
- Taker 有效成本：`VWAP_ask(q)*(1+fee_rate)+slippage_buffer`。
- 补仓：`delta=max(target-filled-pending,0)`，只对通过 B14、深度、clip、版本和状态分支的候选保留执行标签。
- Maker 与 Taker 都只是 Shadow 假设；计划和回放不生成 order、intent 或网络调用。

## 复验

```bash
python3 -m pytest -q tests/test_adaptive_basket_shadow_*.py

python3 scripts/replay_adaptive_basket_shadow.py \
  --input tests/fixtures/adaptive_basket_shadow_replay_v5.json

python3 scripts/adaptive_basket_shadow_acceptance.py
```

当前专项覆盖十二硬门逐项失败、七字段逐项漂移、五因子单调性、四类独立触发、无触发零候选、STOP 多次回绕、100 次确定性重放、1/2/多档 VWAP、精确档位边界、深度少 `1e-9`、高价档越限、fee/slippage 后越限、空/零/重复/乱序深度、quote 过期、版本错配、Maker/Taker 天气不变性和零订单边界。

## 证据边界

- 已完成 B13、B14、B15、Maker 生命周期和官方 WS 多档 Taker FOK 的纯函数闭环，但没有接入真实 exchange acknowledgement 或生产原子账本。
- Maker 排队模型、五因子紧迫度权重和源账户行为映射仍是待 OOS 证伪的候选机制，不声称识别源账户未成交挂单或私有阈值。
- 在独立 OOS、故障矩阵、L0 宪章修订和人工授权完成前，任何 production adapter 都属于禁止项。
