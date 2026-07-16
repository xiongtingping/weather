---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-15
supersedes: [SSOT_FACTS.md, ssot_facts_design.md]
implementation_symbols:
  - production_reported_x_public_ssot_writer.py
  - reported_x_successor_checkpoint_reader.py
  - ui/server.py:ui_snapshot
  - auto_trade_decision_writer.py
  - sequential_bayes_public_ssot_shadow_writer.py:commit_public_ssot_shadow_transaction_b
  - sequential_bayes_public_ssot_shadow_writer.py:commit_public_physical_ssot_shadow_transaction
  - scripts/probability_ssot_mainline_audit.py
verification_gates:
  - python3 scripts/probability_ssot_mainline_audit.py --summary
  - python3 scripts/check_ssot_alignment.py
  - python3 scripts/production_chain_uniqueness_audit.py --no-db --summary
last_verified_commit: working-tree-2026-07-15
---

# SSOT 合同

- 事实数据库唯一为 `data/archive.db`。
- 概率事实唯一为 `posterior_ssot_fact`。
- `X→S→B bridge` 未闭合时，合法 posterior 仍必须写 `posterior_ssot_fact(state=physical_only_market_projection_na)`；其中物理 `p(X)` 可用，`p(S)`、`p(B)` 与 RealizationState 必须为空并携带明确 NA reason，且不得创建交易 outbox。禁止因 bridge 缺失省略 public posterior 或建立第二个 X-only latest pointer。
- resident 热路径的概率预热闭集只有冻结的 prior、weak forecast snapshot 与已批准 strong-LR artifact；`market_context` 属于独立的 `X→S→B`/决策预热。市场缺失或过期不得阻断 canonical AMSC 事件、强观测增量更新、checkpoint 或物理 `p(X)` 的公开 SSOT 提交。
- 当前事实指针唯一为 `event_fact_latest`，历史追加为 `event_fact_history`。
- canonical fact writer 唯一为 `weather_fact_snapshot_writer.py`。
- 对 reported-X v5 current latest，`event_fact_latest` 必须绑定同一 `posterior_checkpoint_id/posterior_run_id`、activation contract SHA、runtime manifest SHA、replay generation、canonical event 与 StrongObservationUpdate 身份；history append 与 latest pointer 必须在同一 Transaction B 提交。复制旧 posterior 后重指针、旧 v3.1 materialization attestation 或 UI/辅助源事实一律不是 current SSOT，禁止进入决策或时延审计。
- 该约束的 runtime scope 仅为当前最高 event date；历史未证明指针保留为审计违规，不能阻断当前日 AMSC live，也不能被历史状态“修好”后伪装为当前日通过。
- AMSC 是实时强观测的唯一 canonical producer；AWC 只可作为 reference METAR 采集与诊断，禁止写 `event_fact_history/latest`、重算 posterior、创建 decision/job 或触发订单。
- WU/TWC、TAF 和官方数值模式均为弱预测源；它们只能按 as-of revision 进入唯一联合弱似然，不能以“刷新事实”的方式覆盖 AMSC posterior。
- `*_meta_json` 只 merge-fill，不允许弱源覆盖强源。
- 执行阻断唯一事实为 `auto_trade_decisions`。
- 可执行退出决定必须在 `auto_trade_decisions` 中分别持久化 `exit_reason_family/exit_reason_code`，并按 L1 合同的精确闭集校验；禁止仅凭前缀放行。`target_realization_*` 与 `market_alpha_take_profit_*` 不得互相伪装。
- market-alpha 必须引用当前 `posterior_run_id/realization_state_id/strategy_artifact_sha256`、`bucket/token_id/position_lot_version`、`market_snapshot_id/quote_id/quote_expires_at`、`requested_sell_size/logical_exit_intent_id/expected_order_id`；任一身份缺失或 posterior/lot/quote 变化即 fail closed/reconcile。旧 `wave_cycle_id/frozen_wave_target_price/market_alpha_cycles` 不再是 SSOT 合同。
- UI 通过 pinned SQLite read transaction 组装 `/api/ui_snapshot`；前端只渲染。
- UI 对 reported-X 必须同时读取当前 checkpoint 的 `consumed_event_ids_json` 与同一 target/day 已接受的 `canonical_observation_events`。若存在未消费事件，旧 checkpoint 概率只能标记为 `frozen_stale_checkpoint`，必须展示待处理事件数、有效时与报文温度；不得称为当前概率，也不得由前端用新报文重算。常规 METAR 可显示“待推进 X 硬下界”，SPECI 只能显示“待进入 strong soft evidence”。该 freshness 必须保留在 full snapshot、fast summary、cold-cache metadata fallback 与顶层“SSOT 概率主线”四条展示路径中；任何压缩或缓存回退都不得把冻结后验重新标成 ready/current。
- AMSC 文件 pending 队列不是第二事实源。新 activation generation 全量回放后，只有当计划的 `event_date + valid_time + source_observation_id` 精确命中 canonical event，且该 canonical event 已出现在该 generation 最新 committed checkpoint 的 `consumed_event_ids_json` 中，才可把冗余计划归档为 `consumed`。未消费计划必须保留；缺少 `source_observation_id` 的兼容计划禁止退化为仅按时间匹配。归档过程只读 `archive.db`，不得写 posterior、事实或订单。
- `amsc_scheduled_regular_metar_reported_daily_max_integer_x` 与 `physical_high_exact_grid` 都属于物理 X 展示语义；精确 X 格点不得被渲染成 `or below/or higher` 市场边缘桶。市场边缘桶只允许来自已闭合 `X→S→B` bridge 的 committed `market_bucket_probabilities`。
- Live 执行只从数据库 SSOT 读取，不允许 UI HTTP、snapshot file 或前端派生旁路。
- 缺失字段必须有明确 `*_na_reason`；不得用默认值伪装 available。
