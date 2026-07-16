---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-13
market_alpha_take_profit_state: implemented-default-off-schema-ready
runtime_conformance_state: implementation-complete-activation-pending
supersedes: [THREE_EVENT_TARGET_REALIZATION_TRADING_PLAN.md, TARGET_REALIZATION_EDGE_MIGRATION_PLAN.md]
implementation_symbols:
  - target_realization_v3_runtime.py
  - strong_posterior_three_leg_strategy.py
  - dynamic_three_leg_transition_runtime.py
  - dynamic_top_yes_wave_exit_runtime.py
  - dynamic_three_leg_position_lot_runtime.py
  - production_dynamic_three_leg_input_provider.py
  - production_dynamic_three_leg_continuation_worker.py
  - production_market_alpha_take_profit_coordinator.py
  - trade_strategy_loop_audit.py
  - target_realization_execution_lock_writer.py
  - scan_weather_market.py
verification_gates:
  - python3 scripts/document_authority_audit.py
  - python3 scripts/signal_to_order_safety_contract_audit.py --summary
  - python3 scripts/production_trade_gate_zero_audit.py
  - python3 trade_strategy_loop_audit.py --summary
last_verified_commit: working-tree-2026-07-13
---

# 动态三腿交易与退出合同：Target Realization v3

`v3` 是交易策略版本，不是概率模型版本。策略只消费已提交的 `posterior_ssot_fact / RealizationState`，不得读取 raw METAR、预测源、温度轨迹或云雨文本重新判断天气。后验是唯一概率 SSOT。

## 一、唯一动态三腿入场

每一份新的 posterior 都重新选择角色，不继承旧排名：

```text
top_yes_wave
  = 当前最高 posterior 且 composer joint strong evidence 支持的可达桶
  = 最大名义金额 20 USDC
  = 吃波段，不持有到结算

second_yes
  = 当前次高 posterior 的可达桶
  = 只在 SSOT 预计最高温形成时刻前最后 30 分钟允许新买或补仓
  = 单腿累计目标名义金额严格小于 5 USDC，执行常量为 4.99 USDC
  = 小金额持有结算

strongly_excluded_no
  = composer 明确判定物理不可达的桶
  = 最大名义金额 5 USDC
```

若可达桶不足两个，允许只生成主 YES；不得把已经买 NO 的物理排除桶同时选为次 YES。某一腿缺失不允许交易层伪造候选来凑满三腿。

次 YES 的 30 分钟时窗不是前端时钟，也不是固定的 15:00/16:00 经验规则。弱预测源各自的 `forecast_peak_valid_time` 先按唯一弱模型已经批准的源信息权重形成 `forecast_peak_timing_prior`；D0 后再由同一次 strong update 的 Termination Risk、温度动量与轨迹形成 `RealizationState.peak_timing_state`。执行层只消费其 `entry_peak_at`，窗口严格为 `[entry_peak_at-30m, entry_peak_at)`。该时刻状态只决定入场时机，不得作为第二套最高温 X 概率，不得反向改写 posterior。

`peak_timing_state` 属于物理 X 主线的同事件辅助状态，不属于 `X→S→B` 市场 bridge。即使 bridge 未闭合、系统只发布 `physical_only_market_projection_na`，同一次 StrongObservationUpdate 仍必须持久化并向 public posterior SSOT 发布该状态；不得因为没有市场桶、RealizationState 或交易 outbox 而丢失预计峰值时刻。时刻计算结构异常降级为绑定当前 event/update 的显式 NA，只关闭次 YES，不阻断物理 X。

若 Termination Risk 仍保留未来创新高概率、但弱预测支持的全部未来峰值槽已经过期，剩余质量必须显式保留为 `future_peak_time_unresolved`，不得丢弃、强行归一给已观测峰，也不得伪造新的 `entry_peak_at`。该状态保持时刻边际归一，但次 YES 继续 fail closed。

`peak_timing_state` 缺失、未来时刻无权威支持、预计最高温已经形成或窗口尚未开始/已经结束时，次 YES 新买与补仓 fail closed；这只关闭次 YES，不得阻断 AMSC/SPECI 入库、strong update、主 YES 或安全 NO。窗口关闭也不是 `target_realization_second_yes_invalidated`：同一当前次高桶的已有仓位允许保留到结算；posterior 换桶仍按新角色卖旧桶，但窗口外不得买入新次高桶。

无论本次计划是否同时存在主 YES 或安全 NO 的可执行 BUY，次 YES 时窗关闭都必须在同一决策事务写入 `auto_trade_decisions` 无订单记录，包含 `second_yes_entry_window_state_id`、预计峰值、窗口起止、关闭原因及“已有同桶仓位可保留”边界；不得以候选缺席代替原因记录。UI 只读该决策与 `posterior_ssot_fact.peak_timing_state`，禁止前端根据当前时间重算时窗。

YES/NO 入场均不使用 EV、Edge、LCB、fair value 或买入价格上限作为硬门。市场报价仍必须是 fresh executable quote，并满足交易所 tick、余额、幂等与订单合同。

以下旧腿永久退役，不能作为别名恢复：

```text
safe_remote_no
trajectory_no_current_report
current_report_temperature_no
next_temperature_bucket_no
hard_zero_no（独立策略腿）
```

## 二、强物理证据接口

唯一接口是 `RealizationState.strong_physical_evidence`，schema 为 `strong_physical_evidence_projection_v1`。它必须由 Bayes composer 在 strong update 已完成后生成，并绑定：

```text
canonical_observation_event_id
strong_observation_update_id
model_artifact_sha256
realization_policy_artifact_sha256
top_bucket
top_bucket_joint_strong_support
bucket_evidence[]
bucket_exclusions[]
```

主 YES 只接受 `top_bucket_joint_strong_support=true` 且证据 top bucket 与本次 posterior top bucket 完全一致。次 YES 不要求复制主 YES 的 support flag，但必须是未被任何物理证据排除的当前次高桶。

唯一 NO 的排除证据仅有：

1. `observed_daily_high_exceeded`：primary AMSC canonical 序列形成单调不下降的当日已观测最高温 X；它只能先约束物理 X，再由**已关闭且验证通过的 `X→S→B` bridge exclusion certificate**证明对应市场桶 B 为零概率。禁止把 AMSC 的 X 数值直接与市场结算桶 B 边界比较；
2. `remaining_warming_unattainable`：联合 strong artifact 已共同消费温度、动量、轨迹、云雨、露点与来源状态，并且该桶同时满足批准政策中的低后验上限和负 log-probability-ratio 门槛。

最低 posterior、离当前温度很远或单一云雨字段都不能成为 NO 理由。交易层不得重新生成任何物理 flag。

`safe_no_observed_max_c` 必须来自截至本次 canonical event 的完整 AMSC 序列累计最高温，而不是本条报文温度。后续 METAR/SPECI 即使降温也不得使该值回退。SPECI 可以立即推进 strong update、证书投影和 posterior，但不改变日终 physical X 仍只由固定口径常规 METAR 形成的合同。

## 三、posterior 变化与换桶

动态是执行语义：每次 posterior commit 后，当前角色、目标仓位和退出资格全部重算。`trading_plan_id / posterior_run_id` 仅标识本次修订和幂等，不能冻结历史桶。

若旧持仓不再属于当前角色，执行顺序固定为：

```text
SELL_OLD
→ reconcile fill / ACK unknown / partial fill
→ refresh latest posterior SSOT
→ rebuild current TradingPlan
→ BUY_NEW
```

卖旧未完成时禁止买新。任何 CLOB POST 前必须在一个短事务中持久化 decision、order intent、position lot version 与执行锁；崩溃后从 durable intent 对账，不能重建一张语义不同的订单。

`dynamic_three_leg_transition_runtime_v1` 是唯一的目标仓位→持仓差异规划器。它同时绑定当前 `trading_plan_id / posterior_run_id / realization_state_id` 与一份完整、冻结的 `position_snapshot_id`，只管理 `management_scope=dynamic_three_leg_weather` 的仓位；镜像、手工或其他策略仓位必须忽略。输出只能是：

```text
SELL_OLD：存在离开当前角色的仓位或角色降级后的超额仓位；buy_candidates 必须为空
BUY_NEW：没有待卖仓位，只买目标名义金额与当前已投入成本的正差额
HOLD：目标仓位已满足
BLOCKED：持仓快照不完整、成本/lot version 缺失或存在 unresolved intent
```

当前角色是 latest posterior 的 `(strategy_leg, bucket, side)`；lot 上的 `entry_strategy_leg` 只保留成交来源，禁止原地改名。角色互换采用有方向的复用规则：`top_yes_wave → second_yes` 且桶/side 不变时，原 top-entry lot 可计入当前次 YES 的 4.99 USDC 目标并只卖超额，其 0.90 market-alpha 权限因该桶已非当前 top 自动休眠；`second_yes → top_yes_wave` 不得反向继承波段权限，必须先卖旧 second-entry lot，再以 top 腿建立新 lot。继续属于同一角色的多个 lot 聚合成本与数量，只在次 YES 时窗开启时补目标名义金额差额。任一 SELL 完成后必须重新读取 latest posterior 与新 position snapshot，旧 transition plan 不得直接进入 BUY。

`dynamic_three_leg_position_lots` 是新交易环唯一持仓批次 SSOT；`edge_trades` 仅保留兼容持仓/订单状态，不拥有策略腿、批次版本或换桶语义。每次 partial/full fill 必须与 intent 状态在同一 SQLite 事务内追加 lot event 并单调推进 `position_lot_version`。SELL_OLD 全部成交后，必须原子生成 `dynamic_three_leg_continuation_events`；同一 posterior 若仍为 latest，continuation 重新冻结盘口、资本、风险与持仓并生成 BUY_NEW，若 posterior 已变化则旧 continuation 只能 supersede。禁止等待下一条 METAR/SPECI 才完成卖旧→买新。

## 四、两个互斥退出原因族

### `exit_reason_family=target_realization`

仅表达 posterior、hard condition 或 settlement 改变天气/结算目标。允许 code 闭集：

```text
target_realization_yes_target_invalidated
target_realization_no_invalidated
target_realization_top_yes_invalidated
target_realization_second_yes_invalidated
target_realization_hard_zero_no_invalidated
target_realization_settled
```

不得把止盈、资本回笼、stop-loss、trail-stop、repricing 或 Edge 变化伪装成该 family。

### `exit_reason_family=market_alpha_take_profit`

当前唯一 code：

```text
market_alpha_take_profit_top_yes_wave_completed
```

它只适用于**当前 posterior 仍标记为 `top_yes_wave`**、且入场腿就是 `top_yes_wave` 的 YES lot，并且请求卖量的 fresh executable sell quote 在 `$0.90` 或以上具有足额深度。真实订单限价固定为 lot 入场时绑定策略 artifact 的 `$0.90`；观察到的 `$0.91/$0.92` 只证明当前可执行，不得改写止盈限价。主桶一旦变化，旧桶立即失去 market-alpha 资格并走 `target_realization_top_yes_invalidated`；不得继续等待旧桶到 0.90。

市场止盈 intent 必须绑定：

```text
posterior_run_id / realization_state_id
strategy_artifact_sha256
bucket / token_id / position_lot_version
market_snapshot_id / quote_id / quote_expires_at
requested_sell_size / price_for_requested_size
logical_exit_intent_id / expected_order_id
```

`logical_exit_intent_id` 由当前 posterior、持仓 lot/version、原因 family/code 和请求卖量确定生成。同一身份只允许一个有效 SELL；posterior 或 lot version 变化必须先使旧 intent terminal/reconciled，再生成新身份。

旧 `PLAN_FROZEN`、`wave_cycle_id`、`frozen_wave_target_price` 和泛化 `market_alpha_cycles` 已退役。当前独立账本只允许 `market_alpha_take_profit_cycles`，用于批次/后验/报价/intent 幂等，不得冻结桶排名。0.90 是批准策略 artifact 在 lot 入场时绑定的限价；桶身份仍随 posterior 动态变化。

## 五、执行原子性与 reconcile

必须满足：

- quote 对 requested size 可执行且未过期；mid/last/UI price 不合格；
- decision、intent、position version 与 risk reservation 同事务提交；
- signed request 在 POST 前 durable；
- ACK unknown 时只 reconcile，不重发不同 intent；
- partial fill 只处理剩余量，不能扩大卖量；
- 同一评估同时满足两族时，`target_realization` 优先，market-alpha 记为 superseded；
- 未知 family/code、仅前缀匹配或双 family 一律拒绝。

已落地、但尚未激活的生产实现由九个互不越权的边界组成：

1. `production_dynamic_three_leg_decision_writer.py` 只在 Transaction C 中原子追加 `dynamic_three_leg_transition_plans + auto_trade_decisions + posterior_execution_intents` 并完成 outbox；它不调用 CLOB。
2. `resident_dynamic_three_leg_intent_submitter.py` 使用专用、无外层事务的 SQLite connection，在任何网关调用前以 `BEGIN IMMEDIATE` 将 exact intent CAS 为 `submitting` 并 durable commit；网关结果和过期状态分别以独立短事务提交。
3. `scripts/clob_order_service.mjs:/dynamic-three-leg-snapshot` 在一次本地往返中并行返回目标桶 order books、managed token balance/allowance 与 collateral；`/execution-guard-snapshot` 负责单 intent 的最终复核。两者固定 `evidence_only=true / authorizes_order=false`，无权批准订单。Python adapter 独立校验 token/side、≤1 秒 freshness、限价内足额深度、lot balance 和 allowance；durable risk reservation 仍只能由 SQLite SSOT 证明。
4. 动态三腿使用**不可重定价的 marketable GTC limit**：BUY 的限价来自冻结 ask，SELL 的限价来自冻结 bid，`post_only=false`。当前盘口只有在该冻结 limit 内可足量成交才允许 POST；盘口恶化必须 fail closed 并等待新 market snapshot/intent，禁止追价。把 ask/bid intent 标记为 `post_only=true` 会在等价盘口下被 maker-only 拒绝，已明确退役。
5. `resident_dynamic_three_leg_execution_loop.py` 是唯一专用常驻 consumer：使用 autocommit dedicated connection，每轮只推进一个 durable intent，先处理 `submitting/reconcile_required/gateway_accepted/partially_filled`，再处理 prepared；模糊 POST 永远只按原 idempotency/order identity 对账，不重发。新 posterior 到来后，只有无 `exchange_post_started_at/expected_order_id/exchange_order_id` 的旧 `prepared` plan 才能在一个事务内把 intents、decisions、transition 与 reservation 标记为 superseded；已有 BUY fill 时 reservation 必须保持 `consumed`。任一可能已触达交易所的状态仍由 reconcile-first 独占，禁止用 posterior 更新掩盖订单不确定性。它不消费 generic task queue、不训练模型、不重算 posterior。对应 launchd unit 默认 `Disabled=true`，动态三腿与 market-alpha 共六个 production/live 开关全部为 0；部署 unit、安装 schema 均不等于 live 激活。launcher 必须使用项目受控 Python，禁止回退到 macOS `/usr/bin/python3`。
6. `production_dynamic_three_leg_input_provider.py` 只从 latest RealizationState、`market_tokens`、position-lot SSOT 与本地 CLOB raw snapshot 冻结同一决策输入；不读 UI，不使用 `edge_trades`，不消费 EV/Edge/LCB。
7. `dynamic_three_leg_position_lot_runtime.py` 将 partial/full fill 与 lot/version/cost 在同一事务内更新；任何提交前再次核验 lot/version/remaining size。成本口径固定使用 durable intent 的冻结限价；exchange `getOrder.price` 只能作为同一订单身份校验，不能在各次累计 reconcile 中改写成本价。
8. `production_dynamic_three_leg_continuation_worker.py` 消费 SELL completion 原子生成的 continuation；同 posterior 继续 BUY_NEW，新 posterior 到来则 supersede，禁止卖完后等待下一报文。
9. `production_market_alpha_take_profit_coordinator.py` 仅对当前 top 的 top-entry lot 请求 fresh `$0.90` 深度证据；天气 invalidation SELL 永远优先。

第一阶段 execution schema 入口仍为 `scripts/activate_dynamic_three_leg_execution_schema.py`。position-lot、continuation 与 market-alpha 三组 additive schema 的唯一后续入口为 `scripts/activate_dynamic_position_market_alpha_schema.py`；它默认只读，apply 同样要求固定确认词、writer-quiescent 与 15 分钟内 SQLite backup，且不改变任何 activation。

`risk_reservation_id` 不再允许是无账本字符串。`dynamic_three_leg_risk_reservations` 是其唯一 SSOT：在 Transaction C 前必须已有 `active` reservation，并绑定 trading plan、posterior、capital/risk snapshot、capital epoch、金额和过期时刻。Transaction C 对 BUY_NEW 原子 CAS 为 `bound` 并绑定 transition；SELL_OLD/HOLD 直接 `released`。reserved buy notional 小于本 transition 的 BUY intents、身份漂移、过期或重复绑定均整体回滚；resident submitter 在任何网关调用前再次核验 reservation/transition/expiry。

生命周期收口只能由 durable intent terminal state 驱动：全部 intent=`filled` 时 transition 原子变为 `completed`，BUY reservation 变为 `consumed`；明确 `failed/expired/cancelled_superseded` 时 transition 变为 `blocked`，同 transition 尚未提交的 siblings 统一取消，若尚无 BUY fill 则 reservation=`released`，已有 BUY fill 则=`consumed`。ACK unknown、open、partial、not-found 均不得提前释放或重建订单。SELL_OLD 属于降低风险路径，只要求 reservation 已按 Transaction C `released` 且绑定同一 transition；它不得因 BUY 预算的 `expires_at` 已过而被阻断。订单对账除 idempotency/order ID 外还必须核对 token、`original_size`、`0≤size_matched≤original_size` 与冻结 order price；任一不符继续 `reconcile_required`，且不得把未证明的 `size_matched` 写入 position-lot。
`reconcile_dynamic_three_leg_intent` 只按 `idempotency_key / expected_order_id / exchange_order_id` 查询已有订单。`not_found` 或身份不能证明时保持 `reconcile_required`，禁止创建替代订单；明确 filled/open/partial/cancelled 才推进对应状态。

生产候选仍双重默认关闭：activation contract 与 `PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED / PM_DYNAMIC_THREE_LEG_RESIDENT_SUBMITTER_ENABLED / PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED` 必须同时授权。默认关闭时不得写 decision/intent，也不得调用网关。

## 六、永久禁止的 runtime gate

```text
edge_min / hold_edge_realizable
LCB / exec_edge / signal_edge
weather_repricing*
stop_loss / trail_stop
旧收益退出与旧 fair-value gate
```

这些字段仅可用于展示、诊断或离线复盘，不得影响入场、仓位、退出、订单价格或是否下单。

旧 `remote_no` 的候选生成器、距离环、0.98 价格上限、posterior-settlement NO lane、poller expected-target 生成与 UI 重算实现已经物理删除。`safe_remote_no/trajectory_no/current-report NO/next-bucket NO` 也不能作为别名恢复。当前唯一 NO 是 committed dynamic plan 中的 `strongly_excluded_no`，其来源必须是 composer 联合物理排除，额度固定为 100 USDC。

资金策略身份固定为 `dynamic_three_leg_capital_policy_v2_top20_second4_99_excluded_no100`。三腿同时具备 BUY 资格时完整目标预算为 124.99 USDC；资本快照或原子 risk reservation 不足时必须 fail closed，不得按旧比例缩仓、借 EV/Edge 调仓或静默降低联合物理排除 NO 的 100 USDC 目标。某腿因物理/时窗条件缺席时，只预留本次 committed plan 实际存在腿的预算。

## 七、激活门

当前动态三腿、position-lot、同后验 continuation 与 market-alpha runtime 已实现，生产库两阶段 additive schema 均已安装并通过 exact-schema 只读验收，但六个 production/live 开关仍为 0，resident persistent disabled 且未加载，activation artifact 仍为 disabled。Schema ready 不等于 live ready。生产激活前必须证明：

1. 动态换桶 sell-first、partial fill、ACK-loss 与 crash recovery；
2. current-top 0.90 资格会随 posterior 变化立即撤销；
3. 单 posterior/单 lot/单 reason 只有一个有效 intent；
4. production runtime EV/Edge gate-zero；
5. `T1→T5 P99≤3s` 的真实 gateway-attested 证据；
6. 独立 accepted cutover ADR 绑定代码、schema、模型与策略 artifact SHA。

## 八、唯一交易环聚合审计与状态语义

当前生产交易环的只读聚合权威是：

```bash
python3 trade_strategy_loop_audit.py --summary
```

其 schema 固定为 `trade_strategy_loop_audit_v2_reported_x_dynamic_three_leg`，只组合五项现行证据：reported-X 概率主线身份、`X→S→B` bridge readiness、动态三腿 readiness、production EV/Edge gate-zero 与 signal-to-order safety。它不读取旧 AWC accepted-signal replay，不重算概率、不抓天气或盘口、不写 decision/intent/order，也不改变 activation。

状态必须按两层解释，禁止混为一个布尔值：

```text
ok
  = 当前架构和默认关闭边界安全
  = safety_blockers 为空

live_ready
  = ok
  + reported-X runtime 身份一致且无 pending canonical
  + X→S→B market projection 已批准
  + dynamic three-leg live activation 已批准
  + gateway-attested T1→T5 证据通过
```

因此 `ok=true, state=safe_fail_closed, live_ready=false` 是合法且必要的生产状态：它证明系统没有旧 EV/Edge gate、重复订单或绕过 SSOT 的可执行路径，同时明确禁止真实下单。`ok=true` 永远不得单独解释为 live 授权。

`bridge_pair_event_day/X/S support` 不足、bridge activation disabled 与市场桶 projection 缺失只能进入 `promotion_blockers`：它们阻断 B 概率、Transaction C 与真实订单，不得阻断 AMSC METAR/SPECI 入库、物理 X posterior、强似然更新或按日 X 训练。已被 activation 身份门拦住的单纯 manifest 漂移或 pending canonical 同样只允许概率主线 fail closed 并保留原事件，修复身份后必须重放，禁止 fallback 到旧 posterior。

任何其他 probability uniqueness 缺陷（旧模型 fallback、retired writer 可达、依赖闭包不完整、文档/monitor/SSOT 边界破坏），以及 runtime policy、stale-decision、duplicate-order、idempotency、未安全关闭 resident 或 unresolved exchange intent 缺陷，都必须进入 `safety_blockers`，此时状态只能是 `unsafe_blocked`。禁止通过删除 promotion blocker、降低 bridge 样本门槛或把旧 AWC replay 重新接回主线来制造 `live_ready`。
