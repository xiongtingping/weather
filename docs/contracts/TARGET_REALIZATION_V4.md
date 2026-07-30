---
status: superseded
authority: L1
owner: weather-system
effective_from: 2026-07-16
activation_state: never-activated-retired
runtime_conformance_state: retired-no-runtime-authority
superseded_by: TARGET_REALIZATION_V5.md
supersedes:
  - TARGET_REALIZATION_V3.md
implementation_symbols:
  - pending:target_realization_v4_runtime
  - pending:dynamic_three_leg_v4_composer
  - pending:standing_exit_999_runtime
verification_gates:
  - python3 scripts/document_authority_audit.py
  - python3 scripts/target_realization_v4_document_contract_audit.py
  - python3 scripts/production_trade_gate_zero_audit.py
last_verified_commit: working-tree-2026-07-16-target-realization-v4-contract
---

# 动态三腿交易与退出合同：Target Realization v4

> **已退役**：本文未形成 production runtime；当前合同是 v5，唯一 successor 是 Target Realization v6。本文不得作为兼容或 fallback。

## 0. 文档状态

本文是 accepted L1 开发与验收权威，但**不单独改变 production activation、代码、参数或订单行为**。在策略 artifact、执行 schema、审计和受管切换原子完成前，production runtime 必须 fail closed，本文不得被 runtime 单独解释为真实下单授权。

本合同相对已 superseded 的 v3 固定以下变化：

1. 恢复一个由后验低概率或软不可达支持的 `trajectory_no` 模式；
2. 轨迹 NO 最高 100 USDC，硬排除 NO 最高 200 USDC；
3. 四种持仓角色成交后均默认挂 0.999 USDC GTC 限价卖单；
4. 主 YES 增加报文驱动的升温风险阶段退出；阶段比例由风险目标与时段上限共同决定，上方所有桶硬不可达时停止阶段退出并保留剩余主 YES；
5. 退役主 YES 0.90 卖出目标和次 YES“主动持有到结算”的专属退出差异；
6. 轨迹 NO 与硬排除 NO 是同一个 NO 槽位的两种互斥模式，系统仍是三腿而不是四腿。

## 1. 目标与宪章

交易环只负责把 latest committed market-bucket posterior 转换成受资本、互斥、库存和订单合同约束的 TradingPlan。它不得重新读取 METAR/SPECI、预测源、温度动量、温度轨迹、云雨或 Termination Risk 再做一套天气判断。

固定宪章：

- 后验是概率唯一 SSOT；执行层只消费同一 committed checkpoint 的 `P(B)`、物理可达性投影、峰值时段和硬排除证书。
- 三个经济槽位固定为主 YES、次 YES、一个 NO；不允许用别名扩张为第四条 NO 腿。
- 当前主桶、次桶和 NO 候选随每次 posterior commit 动态重选，不冻结历史排名。
- 任何同桶 YES/NO 对冲、两个 NO 桶并存或超出组合额度的新增买入均 fail closed。
- 入场、仓位和退出不得使用旧 EV、Edge、LCB、fair value、repricing、stop-loss 或 trail-stop。
- 市场报价只服务订单可执行性、tick、余额、allowance、深度和幂等，不拥有天气资格或仓位大小解释权。
- UI 只读 `auto_trade_decisions`、position-lot SSOT 和 standing-exit 状态，不得重算候选、概率或金额。

## 2. 三腿与资金上限

| 经济槽位 | 策略角色/模式 | 资格 | 目标名义金额上限 |
|---|---|---|---:|
| 主 YES | `main_yes` | 当前最高 market-bucket posterior 且未被物理排除 | 20.00 USDC |
| 次 YES | `second_yes` | 当前次高 posterior、未被排除且处于预计最高温前最后 30 分钟 | 严格低于 5，执行值 4.99 USDC |
| NO | `trajectory_no` | 桶 YES 概率低或被 SSOT 标记为软不可达 | 100.00 USDC |
| NO | `hard_exclusion_no` | exact checkpoint/bridge/target 的有效硬排除证书 | 最高 200.00 USDC，可按资本比例缩量 |

`trajectory_no` 与 `hard_exclusion_no` 共享一个 NO 槽位并互斥。完整组合的最大新增目标为：

```text
主 YES + 次 YES + 轨迹 NO = 20 + 4.99 + 100 = 124.99 USDC
主 YES + 次 YES + 硬排除 NO = 20 + 4.99 + 200 = 224.99 USDC
```

唯一资本策略身份固定为：

```text
dynamic_three_leg_capital_policy_v4_top20_second4_99_trajectory100_hard200
```

全部金额都是组合上限，不是必须足额投入的固定订单，也不是 token 数量。旧仓、partial fill 和尚未卖出的 off-role 仓位都必须计入组合存量，不得只计算本轮新 intent。若本轮所有合格 `BUY_NEW` 的增量请求超过冻结的可分配资本，唯一资本分配器对同一计划内的增量请求应用统一比例：

\[
\alpha=\min\left(1,\frac{C_{available}}{\sum_i \Delta N_i^{raw}}\right),
\qquad
\Delta N_i^{final}=\alpha\Delta N_i^{raw}
\]

其中 `0<=capital_scale_factor=alpha<=1`，只缩放本轮新增买入，不强卖已有仓位。硬排除 NO 的 200 USDC 因此只是绝对上限：允许低于 200、允许按同一 `capital_scale_factor` 缩量，禁止因无法足额达到 200 而单独阻断该腿。缩量只由冻结资本/风险可用量决定，不读取价格收益、EV、Edge 或 LCB；所有腿、reservation、decision 与 intent 仍在同一原子事务提交。

## 3. YES 双桶

### 3.1 主 YES

```text
top_yes_bucket = argmax_B P_yes(B)
target_notional = min(20 USDC, portfolio_available_notional)
```

主 YES 身份随后验变化而变化。候选桶必须是 latest posterior 第一桶，且当前桶本身没有被硬排除。它不冻结桶排名，也不再绑定 0.90 卖出目标。

交易层不得直接解析 METAR/SPECI、温度动量、温度轨迹、云雨或 Termination Risk。上述证据先由强似然进入后验；主 YES controller 只读取同一 SSOT checkpoint 输出的：

```text
top_bucket / P(top_bucket)
P_above_top
remaining_warming_risk_state / rebound_risk_state
peak_timing_state.entry_peak_at
hotter_bucket_hard_exclusion_certificates
current_bucket_hard_exclusion_certificate
```

其中：

\[
P_{above}(B_{top})=\sum_{B_j>B_{top}}P(B_j)
\]

`P_above_top` 表示最终最高温进入当前主桶以上市场桶的后验概率。它由同一 market-bucket posterior 直接求和，不是执行层重新计算的第二套天气概率。

主 YES 每次 posterior commit 后必须进入下列唯一状态之一，优先级固定为 `invalidated > hard_locked > upside_risk > normal`：

| `main_yes_mode` | 定义 | 执行动作 |
|---|---|---|
| `normal` | 当前主桶有效、未 hard lock，且 `P_above_top<10%` | 不新增阶段卖出；保留默认 0.999，并可按当前持仓周期补至批准目标 |
| `upside_risk` | 至少一个更高桶未被硬排除，且 `P_above_top>=10%` | 按风险目标与预计峰值时段上限的较小值累计卖出 |
| `hard_locked` | 当前主桶有效，且其上所有市场桶均被有效硬证书排除 | 停止阶段卖出，剩余仓位保持 0.999 或自然结算 |
| `invalidated` | 当前主桶自身被硬排除，或不再属于当前 YES 角色 | 走 target-realization 退出或按次 YES 上限重分类 |

必须严格区分：

```text
更高桶硬不可达
→ 当前主 YES 被锁定，不需要继续吃波段退出

当前主 YES 自身硬不可达
→ 当前主 YES 已失效，不能以“硬锁定”为名继续持有
```

`hard_locked` 只能由覆盖当前主桶以上**全部市场桶**的 exact checkpoint/bridge/target 硬证书集合授权。低概率、软不可达或有限核截断不得伪造硬锁定。

批准的风险目标固定为：

| `P_above_top` | `risk_target_ratio` |
|---:|---:|
| `<10%` | 0% |
| `10%–<20%` | 25% |
| `20%–<35%` | 50% |
| `35%–<50%` | 75% |
| `>=50%` | 100% |

时段只定义累计卖出的最大上限，不独立产生卖出：

| `peak_phase` | `time_cap_ratio` |
|---|---:|
| 距 `entry_peak_at` 超过 60 分钟 | 25% |
| 距 `entry_peak_at` 30–60 分钟 | 50% |
| `entry_peak_at` 前最后 30 分钟 | 75% |
| 已到预计峰值、但后验仍存在继续升温或反弹风险 | 100% |

`upside_risk` 的唯一累计卖出目标为：

\[
main\_yes\_stage\_target\_ratio
=\min(risk\_target\_ratio,time\_cap\_ratio)
\]

因此“时间更晚”不能在没有后验升温风险时强制卖出，“风险很高”也不能越过当前时段上限一次性清仓。例如前一阶段已累计卖出 25%，当前目标升为 50% 时只能再卖本持仓周期原始可管理数量的 25%。

每个主 YES 持仓周期必须保存 `main_yes_position_cycle_id` 与 `sell_stage_high_watermark`；水位只在同一周期内单调，防止同一阶段被多条 METAR/SPECI 重复执行。它不是“同桶当日永久禁止再买”。已完成阶段 SELL 后，只有满足下列任一条件并完成全部旧 SELL/coverage reconcile，才允许创建新的持仓周期、重置新周期水位并按 latest posterior 重新买入：

1. exact `hard_locked` 已成立；或
2. 连续两条新的 canonical 强观测提交后均满足 `P_above_top<10%`，当前桶未失效，且期间没有新的 invalidation。

continuation、行情刷新或重复处理同一事件不得计入这两次确认。新周期必须绑定新的 `main_yes_position_cycle_id`、posterior checkpoint 与 strategy artifact。出现有效 `hard_locked` 后立即停止旧周期的后续阶段，但不追买、也不回购尚未 reconcile 的数量。

主 YES 风险阶段的 SELL 不是 0.999 take-profit：执行器必须先取消待卖数量对应的 0.999 coverage，确认撤单或剩余量后，再使用 fresh executable bid 冻结不可追价的 marketable GTC limit。它不读取 EV、Edge、LCB 或收益阈值。

若旧主桶变为当前次高桶，可在库存合同允许时最多保留 4.99 USDC；这是已有仓位的角色重分类，不是新 BUY，因此不受次 YES 入场窗口限制。超过部分走 `target_realization_main_yes_invalidated`。窗口外仍禁止对该次 YES 新买或补仓。若不再属于任何当前 YES 角色，则全部退出。SELL 未 reconcile 前禁止为新桶创建冲突 BUY。

每条 AMSC METAR 整点、半点、SPECI，以及其他能够提交新 posterior 的合法事件都重新评估主 YES；同一 `peak_phase` 内的多条 SPECI 只能更新风险，不得重复卖出同一累计阶段。`entry_peak_at` 缺失时不得伪造时段，关闭新的主 YES BUY/补仓并写 `main_yes_peak_phase_na`；当前桶自身的硬失效退出仍然有效。

### 3.2 次 YES

```text
second_yes_bucket = posterior 排名第二且物理可达的不同桶
entry_window = [entry_peak_at - 30m, entry_peak_at)
target_notional = min(4.99 USDC, portfolio_available_notional)
```

窗口由同一 SSOT 的 `peak_timing_state.entry_peak_at` 给出。窗口外禁止新买和补仓，但不得阻断主 YES、NO、观测入库或 posterior 更新。

次 YES 不再被策略主动指定为“必须持有到结算”。成交后同样挂 0.999；若该订单直到市场关闭仍未成交，才自然进入结算流程。

## 4. NO 侧后验定义

对每个二元市场桶分别定义：

\[
q_{NO}(B_i)=P(NO_i)=1-P(YES_i)=1-P(B_i)
\]

各桶的 `q_NO` 不能跨桶归一化。天气桶的 YES 事件互斥，但不同桶的 NO 事件彼此重叠；把全部 NO 概率再次归一化会制造错误仓位。

NO 候选和金额只能读取同一 committed market-bucket posterior。交易层不得从 raw temperature 或有限物理 X support 直接推导市场 NO。

## 5. 轨迹 NO

### 5.1 定义与边界

`trajectory_no` 表示：强观测已经通过唯一 Bayes 主线把温度动量、温度轨迹、云雨露点、Termination Risk 等物理证据纳入后验，使某市场桶成为低概率或软不可达候选。

“软不可达”只能来自同 checkpoint 的 `RealizationState` 投影；它是候选资格，不是绕过 posterior 的第二套概率。若 `soft_unreachable=true` 但 `q_NO<70%`，说明物理投影与市场后验不一致，本轮必须 fail closed 并记录 `trajectory_no_probability_projection_inconsistent`，不得让交易层自行选边。

允许进入候选集的最低概率条件为：

```text
P_yes(B) <= 30%
等价于 q_NO(B) >= 70%
```

软不可达用于解释和排序满足概率一致性的候选，不能把普通低概率升级为硬事实。

### 5.2 按 NO 后验单调分档

| `q_NO(B)` | 轨迹 NO 目标总仓位 |
|---:|---:|
| `< 70%` | 0 USDC |
| `70% – <80%` | 10 USDC |
| `80% – <90%` | 25 USDC |
| `90% – <95%` | 50 USDC |
| `95% – <98%` | 75 USDC |
| `>=98%` | 100 USDC |

该函数必须满足单调性：NO 后验越高，目标金额越高；NO 后验越低，目标金额越低。金额由后验决定，不按市场价格、EV 或 Edge 缩放。

为避免概率在阈值附近造成订单抖动，升档使用表中阈值；降档使用 1 个百分点滞回。例如 100 USDC 档在 `q_NO<97%` 后才降档。滞回只稳定目标档位，不能授权超过当前档位上限的新买入。

### 5.3 唯一候选选择

轨迹 NO 最多选择一个桶，顺序固定为：

1. 剔除当前主 YES、当前次 YES、已有相反 YES 持仓的桶；
2. 剔除 posterior/RealizationState/checkpoint 身份不一致的桶；
3. 按 `q_NO` 从高到低排序；
4. `q_NO` 相同则优先 `soft_unreachable=true`；
5. 再相同则按物理可达性分数从低到高、最后按稳定 bucket id 决定。

市场价格、EV、Edge、盘口收益率不得参与候选排序。

## 6. 硬排除 NO

`hard_exclusion_no` 只接受可审计、不可伪造的 market-bucket 硬排除证书。证书至少绑定：

```text
posterior_run_id / realization_state_id
canonical_observation_event_id
physical hard support
X->S->B bridge identity
market target/bucket contract
certificate authority/artifact SHA-256
```

低 posterior、软不可达、核外截断、单一云雨字段或“看起来到不了”均不是硬证书。硬证书成立时对应 `q_NO=100%`，允许同一 NO 槽位从轨迹模式升级为硬排除模式，目标上限提高到 200 USDC。200 USDC 只是上限，不是必须足额执行的固定目标；统一资本分配器可以用同一 `capital_scale_factor` 缩量，缩量后证书语义不变。

若多个桶同时具备硬证书，只能选一个：优先硬排除余量更大的桶，再按稳定 bucket id 决定；禁止因为存在多个安全桶而生成多条 NO 腿。

同桶轨迹 NO 升级为硬排除 NO 时，只补目标金额与当前已投入成本之间的正差额。不同桶出现新硬证书时，若旧 NO 桶尚未通过 0.999 卖出或结算退出，禁止建立第二个 NO 桶。

## 7. 默认 0.999 Standing Exit 与主 YES 例外

主 YES、次 YES、轨迹 NO、硬排除 NO 的每笔 BUY 成交后，均默认立即为实际已成交数量创建：

```text
side = SELL
order_type = GTC limit
limit_price = 0.999 USDC
post_only = false
```

固定规则：

- 0.999 是所有腿的默认 standing take-profit；不得保留主 YES 0.90、动态 repricing、stop-loss 或 trail-stop。
- 不要求当前 bid 已达到 0.999；这是可静置的 GTC 卖单，而不是旧“足额 bid 到价后才创建”的 marketable-only SELL。
- 若目标 token 的交易所 tick size 不支持精确 0.999，本腿 fail closed，记录 `standing_exit_999_tick_unsupported`；不得擅自改成 0.99。
- partial BUY fill 只为新增实际成交量补充卖单覆盖；不得按原始 BUY 申请量超卖。
- 一个逻辑 `standing_exit_plan` 可以有多个由 partial fill 产生的 exchange child orders，但任一 token 数量只能被一个 child order 覆盖，禁止重复卖量。
- partial SELL fill 后只保留未成交余量；ACK unknown 只按原 idempotency/order identity 对账，不创建替代单。
- 订单未在市场关闭前成交时，持仓自然进入结算；这不是新的“主动持有到结算”策略腿。
- 主 YES 的 `upside_risk` 阶段卖出和 `invalidated` 退出是明确例外：对应数量必须先从 0.999 coverage 中安全释放，再按冻结 executable bid 执行；其余数量继续保留 0.999。
- 主 YES 进入 `hard_locked` 时不得触发阶段卖出；剩余仓位继续挂 0.999，未成交则自然结算。

同一 checkpoint 的执行优先级固定为：

```text
当前主桶自身 hard invalidation
> 角色失效/reclassification
> 上方全量 hard lock
> upside-risk staged sell
> 默认 0.999 standing exit
```

0.999 standing exit 的批准 reason family/code 为：

```text
exit_reason_family = market_alpha_take_profit

market_alpha_take_profit_main_yes_999
market_alpha_take_profit_second_yes_999
market_alpha_take_profit_trajectory_no_999
market_alpha_take_profit_hard_exclusion_no_999
```

主 YES 天气风险阶段使用独立 `target_realization` 闭集：

```text
target_realization_main_yes_upside_risk_stage_1
target_realization_main_yes_upside_risk_stage_2
target_realization_main_yes_upside_risk_stage_3
target_realization_main_yes_upside_risk_stage_4
target_realization_main_yes_invalidated
```

这些 code 只有在 v4 production activation 原子生效后才可执行。不得复用旧 v3 的 `market_alpha_take_profit_top_yes_wave_completed` 来伪装新语义，也不得把阶段天气退出归类为 market-alpha 止盈。

## 8. 动态角色、降档与库存约束

“角色动态”表示每次 posterior commit 都重新计算 desired buckets、主 YES 模式和 desired notional；它不表示可以忽略已经成交、正在阶段退出或尚未按 0.999 卖出的库存。

动态角色与库存冲突按以下方式消解：

1. 后验升档时，只买 `目标总仓位 - 当前同槽位全部存量成本` 的正差额；
2. 主 YES 根据 `normal/upside_risk/hard_locked/invalidated` 保持、阶段退出、停止退出或角色失效退出；不得用统一 0.999 阻塞这些天气动作；
3. 主 YES 的 `sell_stage_high_watermark` 只在同一 `main_yes_position_cycle_id` 内单调；满足 exact hard lock 或连续两条 canonical 低风险确认后可开启新周期，禁止 continuation/行情刷新伪造确认；
4. 非主 YES 的后验降档或角色失效先停止补仓；没有批准 target-realization code 时继续挂 0.999，并标记 `off_role_inventory_awaiting_999_exit`；
5. 所有 off-role、阶段 SELL 未完成和 standing-exit 未成交库存继续占用相应 YES/NO 组合额度，禁止换桶后重复获得新预算；
6. YES 全组合新增目标上限为 24.99 USDC；旧 main/second/off-role YES 均计入该额度；
7. NO 同一时间只能有一个 bucket 经济敞口；旧 NO 未退出时，即使 posterior 选出另一个更强 NO，也只记录阻断决定，不得买入第二个 NO 桶；
8. 同桶存在 YES 仓位或未决 YES intent 时禁止 NO，反之亦然；不得用两个方向“对冲”掩盖角色冲突；
9. 原始增量请求超过可分配资本时，Transaction C 对所有合格 `BUY_NEW` 使用同一 `capital_scale_factor` 原子缩量；硬排除 NO 不要求足额 200，禁止按执行顺序先到先得。

因此 posterior 排名可以立即变化，TradingPlan 也会立即重算；但库存冲突时正确结果是 `BLOCKED/HOLD`，不是越过资金和方向边界强行换仓。

## 9. 单次决策与原子事务

每个 committed posterior 的 Transaction C 必须使用同一冻结输入生成完整组合，而不是四个策略各自下单：

```text
latest posterior + RealizationState
        + frozen market snapshot
        + complete position/standing-exit/stage snapshot
        + capital/risk snapshot
        + accepted strategy artifact
-> candidate arbitration
-> desired three-leg plan
-> inventory/conflict reconciliation
-> capital reservation
-> auto_trade_decisions + intents + outbox commit
```

固定要求：

- `auto_trade_decisions` 是执行与阻断原因唯一来源；每条缺席腿也必须写明确 `*_na_reason`。
- 任一未决 POST、ACK unknown、partial fill、position version 漂移或 standing-exit 覆盖不完整均先 reconcile，禁止重复提交。
- BUY decision、risk reservation、intent、position snapshot version 和 posterior identity 同一 SQLite 事务提交。
- Exchange BUY fill 被确认后，在同一 fill/lot 本地事务创建 durable 0.999 standing-exit intent；网络 POST 由专用 resident 幂等执行，不能假装跨网络原子。
- 主 YES 阶段 SELL 必须在同一 transition 中绑定 `main_yes_mode / P_above_top / peak_phase / sell_stage_high_watermark`；先撤销对应 0.999 coverage 并 reconcile，随后才能提交不重叠的阶段 SELL。
- 外置盘不参与数据库、订单、readiness、回滚、服务启停或本交易环任何路径。

## 10. 不使用的条件

以下字段即使存在，也只能展示或离线复盘：

```text
EV / Edge / LCB
exec_edge / signal_edge
fair value / price advantage
weather_repricing
stop_loss / trail_stop
旧 0.98 NO 买入价格门
旧 0.90 主 YES 卖出目标
```

本方案不设置策略性买入价格上限。真实 BUY 仍必须满足交易所合法价格、冻结 ask、足额余额/allowance、订单深度、幂等和风险 reservation；这些是执行合同，不是概率或收益门槛。

## 11. SSOT 最小字段

每次计划至少持久化：

```text
posterior_run_id / realization_state_id / market_projection_id
strategy_artifact_sha256
strategy_leg / no_mode / bucket / token_id / side
p_yes / q_no / soft_unreachable
hard_exclusion_certificate_id / hard_exclusion_margin
target_notional / current_slot_exposure / portfolio_exposure
amount_band / hysteresis_state
candidate_rank / exclusion_reason / conflict_reason
peak_timing_state_id / second_yes_window_state / peak_phase
main_yes_mode / p_above_top / remaining_warming_risk_state / rebound_risk_state
hotter_bucket_hard_exclusion_set_id / current_bucket_hard_exclusion_certificate_id
risk_target_ratio / time_cap_ratio / main_yes_stage_target_ratio
main_yes_position_cycle_id / reentry_confirmation_count
main_yes_cumulative_sold_ratio / sell_stage_high_watermark
raw_incremental_target_notional / capital_scale_factor / final_incremental_target_notional
standing_exit_target_price = 0.999
standing_exit_plan_id / covered_size / uncovered_size / stage_released_size
position_snapshot_id / position_lot_version
capital_snapshot_id / risk_reservation_id
decision_state / will_order / block_reason
```

概率、候选、金额档位、冲突结果和卖单覆盖必须进入同一可追溯 lineage；UI 不得通过这些字段的子集自行推断另一套状态。

## 12. 需要原子替换的现行语义

正式实施不能只修改一处参数。至少需要同步替换：

1. L0 `SYSTEM_CHARTER` 中 top YES 0.90、仅 top 可用 market-alpha 和现行退出闭集；
2. L1 `TARGET_REALIZATION_V3` 的 0.90、次 YES 结算、轨迹 NO 退役、硬 NO 100，以及主 YES 缺少阶段风险/硬锁定状态的语义；
3. `WEATHER_DECISION_CORE`、`GLOSSARY`、`CURRENT_ARCHITECTURE`、`TARGET_ARCHITECTURE_V2`；
4. 动态三腿 capital policy identity，从 20/4.99/100 改为 20/4.99/trajectory100/hard200；
5. strategy artifact、cutover artifact、activation pointer 与递归 runtime SHA；
6. standing GTC SELL、主 YES stage controller、coverage release、resident、reconcile、position-lot 和 UI 状态；
7. 文档权威审计、EV/Edge gate-zero、订单幂等、资金和信号到订单审计。

上述内容必须由一个受管切换原子发布。禁止让新 composer 配旧额度、让新 0.999 订单配旧 0.90 reason code，或保留旧策略 fallback。

## 13. 开发计划

### P1：合同与 artifact

- 接受新的 L0/L1 策略 ADR；
- 固定 NO 概率分档、1pp 滞回、三腿互斥、主 YES 风险目标×时段上限公式、持仓周期和两个 exit reason family 的闭集；
- 生成内容寻址 strategy artifact，禁止 runtime 环境变量覆盖资金和卖价。

### P2：SSOT 与 composer

- 在 RealizationState 发布 `soft_unreachable` 与 hard certificate 的独立字段；
- 实现 `q_NO=1-p_yes`、单调金额函数和唯一 NO 仲裁；
- 实现 `P_above_top`、四态 `main_yes_mode`、风险目标、峰值时段上限和 hard-lock certificate-set 投影；
- 将 off-role 存量纳入全组合额度；
- 所有阻断写入 `auto_trade_decisions`。

### P3：standing-exit 执行

- 新建 durable `standing_exit_plan/intent/coverage` 数据结构；
- BUY partial fill 后幂等补充 0.999 覆盖；
- 实现主 YES 分阶段 coverage release、撤单确认、持仓周期内累计卖出水位、canonical 再入场确认与 marketable stage SELL；
- 实现 tick 不支持、partial SELL、ACK unknown、cancel/closed market 和 settlement 对账；
- 退役旧 0.90 coordinator 和仅足额 bid 才创建 SELL 的限制。

### P4：组合原子性

- 把 YES 双桶与唯一 NO 模式放入同一个 composer/transition；
- 固定主 YES 自身失效 > 角色变化 > 上方硬锁定 > 阶段退出 > 0.999 的动作优先级；
- 同桶反向、第二 NO 桶、旧仓占额和 unresolved intent 全部 fail closed；
- risk reservation 分别验证 124.99/224.99 最大组合边界。

### P5：只读演练与受管切换

- 全 canonical replay；
- 隔离数据库零订单演练；
- 真实 schema/manifest/readiness 只读验收；
- pause 保护下原子安装合同、artifact、schema、runtime 与 activation；
- 激活后删除旧 0.90/旧 NO 路径，禁止 fallback。

## 14. 验收 Checklist

- [ ] 主 YES 始终来自 latest posterior 第一桶、当前桶自身未被硬排除，最高 20 USDC。
- [ ] `P_above_top` 只由同一 market-bucket posterior 求和，不在交易层重读天气。
- [ ] 主 YES 只处于 `normal/upside_risk/hard_locked/invalidated` 四种模式之一，且优先级固定。
- [ ] 阶段目标严格为 `min(risk_target_ratio,time_cap_ratio)`；时间不单独触发卖出，风险也不能越过时段上限。
- [ ] `P_above_top` 在 10/20/35/50% 边界映射为 25/50/75/100%，时段上限为 25/50/75/100%。
- [ ] 上方全部桶有硬证书时停止后续阶段卖出；当前主桶自身硬排除时完整失效退出。
- [ ] 阶段卖出前先释放对应 0.999 coverage，撤单/ACK unknown 未闭合时不得提交阶段 SELL。
- [ ] stage watermark 只在同一 `main_yes_position_cycle_id` 内单调；exact hard lock 或连续两条 canonical 低风险确认且 reconcile 完成后才可新建周期再入场。
- [ ] 原主 YES 降为次 YES 时可在窗口外保留最多 4.99 USDC，但窗口外不得新增或补仓。
- [ ] 次 YES 始终来自不同的第二桶，只在峰值前最后 30 分钟入场，最高 4.99 USDC。
- [ ] `q_NO` 对每个二元桶独立计算且不跨桶归一化。
- [ ] 轨迹 NO 金额在 70/80/90/95/98% 边界分别为 10/25/50/75/100 USDC。
- [ ] 轨迹 NO 金额对 `q_NO` 单调不下降，降档具有 1pp 滞回。
- [ ] `soft_unreachable` 与 posterior 不一致时 fail closed，不在交易层重算天气。
- [ ] 硬排除必须有 exact X→S→B certificate，200 USDC 仅为上限；允许按统一资本比例缩量，不要求足额、不因不足 200 单腿阻断。
- [ ] 轨迹 NO 与硬排除 NO 始终共享一个槽位，绝不同时生成两个 NO bucket。
- [ ] 当前主/次 YES 桶不得被选为 NO，同桶不同时持有 YES/NO。
- [ ] 旧仓和 off-role 仓位计入全组合额度，换桶不会累计突破 24.99/100/200；资本不足时同一计划的新增 BUY 使用统一比例而非先到先得。
- [ ] 四种角色的实际 BUY fill 均默认生成 0.999 GTC SELL；只有主 YES 阶段风险和自身失效可以安全释放对应 coverage 后走 target-realization SELL。
- [ ] tick 不支持精确 0.999 时本腿明确 fail closed，不自动改成 0.99。
- [ ] partial fill 的卖量覆盖完整且不重叠，ACK unknown 不重复 POST。
- [ ] 次 YES 未成交 0.999 时可以自然结算，但不存在“主动持有到结算”的特殊执行分叉。
- [ ] 生产 runtime 中不存在 0.90、0.98、EV、Edge、LCB、repricing、stop-loss 或 trail-stop 交易门。
- [ ] UI 完全只读 SSOT，不重算概率、金额档位、候选或阻断原因。
- [ ] 新合同、artifact、schema、runtime、activation 和审计一次性原子切换，旧策略无 fallback。

## 15. 最终目标状态

```text
latest committed posterior / RealizationState
                 |
                 v
       unique three-leg composer
          /        |        \
 top YES <=20  second YES<5  one NO slot
                              /       \
                    trajectory<=100  hard<=200
                 |
                 v
    inventory + conflict + capital reconciliation
                 |
                 v
      idempotent BUY intents and fills
                 |
                 v
      all filled quantity -> default GTC SELL @ 0.999
                 |
                 v
        main YES state controller
       /          /          |           \
  normal    upside risk  hard locked   invalidated
 keep 0.999 staged SELL  keep 0.999    role exit
       \          \          |           /
                 v
       fill/reconcile or natural settlement
```

终局不是增加一个与现有 NO 并行的新策略，而是在唯一三腿 composer 内，把 NO 槽位升级为“后验分档的轨迹模式 / 有硬证书的硬排除模式”；所有腿默认由 0.999 standing exit 收口，主 YES 再由同一 posterior 的升温风险、峰值时段和上方硬证书选择正常持有、阶段退出、硬锁定或失效退出。200 USDC hard NO 始终只是组合上限，统一资本分配器可原子缩量。
