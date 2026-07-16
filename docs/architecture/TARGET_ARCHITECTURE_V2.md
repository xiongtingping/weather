---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-15
activation_state: production-v6-enabled-v5-retired-market-execution-independent
depends_on: [../adr/ADR-0003-PRODUCTION-SUCCESSOR-V6-PRE-EVENT-STATE-SPACE-BAYES.md]
implementation_symbols:
  - production_amsc_archive_probability_ingress.py:apply_production_amsc_archive_probability_ingress
  - scripts/watch_zuuu_speci_once.py
  - lineage_identity_runtime.py
  - source_observation_runtime.py
  - forecast_weak_likelihood_runtime.py
  - sequential_remaining_warming_bayes_runtime.py
  - sequential_bayes_execution_contract.py:build_sequential_bayes_execution_contract
  - sequential_bayes_shadow_writer.py:commit_shadow_transaction_b
  - sequential_bayes_shadow_pipeline.py:run_canonical_event_shadow_update
  - trajectory_label_sample_runtime.py:build_temperature_incremental_bayes_training_sample
  - joint_forecast_weak_likelihood_runtime.py:build_joint_forecast_weak_likelihood
  - physical_x_bucket_contract_runtime.py:build_physical_x_bucket_contract
  - physical_x_prior_record_reader_runtime.py:load_physical_x_prior_records
  - initial_posterior_checkpoint_shadow.py:build_initial_posterior_checkpoint
  - initial_posterior_checkpoint_shadow.py:persist_initial_posterior_checkpoint_shadow
  - scripts/bootstrap_initial_posterior_shadow.py
  - temperature_trajectory_feature_runtime.py:build_temperature_trajectory_feature_state
  - strong_observation_lr_artifact_contract.py:validate_strong_observation_lr_artifact
  - strong_observation_lr_evaluator.py:evaluate_strong_observation_raw_log_scores
  - strong_observation_lr_runtime_registry.py:register_strong_lr_runtime_artifact
  - strong_observation_lr_runtime_registry.py:activate_strong_lr_runtime_artifact
  - strong_observation_lr_runtime_registry.py:load_active_strong_lr_runtime_artifact
  - strong_observation_lr_runtime_registry.py:assert_active_strong_lr_artifact_binding
  - strong_observation_training_feature_runtime.py:build_strong_observation_training_feature
  - strong_observation_training_dataset_runtime.py:build_strong_observation_training_dataset
  - strong_observation_oos_evaluation_runtime.py:build_strong_observation_oos_evaluation
  - strong_observation_feature_ablation_runtime.py:build_strong_observation_feature_ablation_evidence
  - strong_observation_artifact_promotion_runtime.py:build_strong_observation_artifact_promotion_gate
  - forecast_revision_canonical_runtime.py:canonicalize_forecast_revisions
  - strict_asof_forecast_capture_runtime.py:classify_strict_asof_forecast_capture
  - amsc_taf_strict_asof_capture_runtime.py:build_amsc_taf_strict_asof_capture_proposal
  - strict_asof_forecast_source_observation_runtime.py:build_tier_a_forecast_source_observation_proposal
  - strict_asof_forecast_shadow_writer.py:persist_tier_a_forecast_source_observation_proposal
  - strict_asof_forecast_p1_ingress_writer.py:persist_tier_a_forecast_p1_proposal
  - strict_asof_forecast_p1_reader.py:load_strict_forecast_p1_source_observations
  - strict_amsc_hard_observation_reader.py:load_strict_amsc_primary_hard_observations
  - p2_canonical_amsc_forward_writer.py:apply_p2_canonical_amsc_forward_append
  - p2_canonical_amsc_forward_capture_audit.py:audit_p2_canonical_amsc_forward_capture
  - p2_canonical_shadow_mirror.py:mirror_verified_p2_canonical_events_to_isolated_shadow
  - scripts/append_strict_asof_forecast_p1_ingress.py
  - scripts/append_strict_amsc_p2_canonical.py
  - scripts/p2_canonical_amsc_forward_capture_audit.py
  - scripts/mirror_p2_canonical_to_isolated_shadow.py
  - scripts/activate_p1_schema_and_forecast_ingress.py
  - scripts/p1_forecast_ingress_staging_probe.py:build_p1_forecast_ingress_staging_probe
  - scripts/p2_canonical_amsc_staging_probe.py:build_p2_canonical_amsc_staging_probe
  - observation_fast_lane_contract.py:build_observation_fast_lane_contract
  - observation_fast_lane_shadow_orchestrator.py:run_observation_fast_lane_shadow
  - observation_fast_lane_resident_shadow_worker.py:ResidentObservationFastLaneShadowWorker
  - amsc_p10_shadow_source_adapter.py:dispatch_amsc_p10_shadow_bundle
  - amsc_specli_resident_arrival_contract.py:build_amsc_specli_resident_arrival_envelope
  - resident_amsc_specli_arrival_journal_shadow.py:ResidentAmscSpecliArrivalJournalShadow
  - resident_fast_lane_prewarm_contract.py:validate_resident_fast_lane_prewarm
  - resident_fast_lane_prewarm_contract.py:validate_resident_market_projection_prewarm
  - wunderground_daily_settlement_parser.py:parse_wunderground_daily_high
  - market_resolution_finality_runtime.py:build_market_resolution_finality_attestation
  - market_resolution_finality_parser.py:parse_market_resolution_finality
  - market_resolution_finality_shadow_writer.py:persist_market_resolution_finality_attestation_shadow
  - market_finality_shadow_capture_runtime.py:run_market_finality_shadow_capture_cycle
  - market_rule_raw_archive.py:archive_market_rule_raw_snapshot
  - market_rule_target_contract_parser.py:parse_market_rule_target_contract_extraction
  - resolution_target_contract_runtime.py:build_market_resolution_target_contract_proposal
  - resolution_target_contract_shadow_writer.py:persist_resolution_target_contract_shadow
  - resolution_final_label_shadow_registry_runtime.py:build_resolution_final_label_shadow_registry_audit
  - resolution_target_capture_cursor.py:load_resolution_target_capture_not_before
  - target_rule_shadow_capture_runtime.py:run_target_rule_shadow_capture_cycle
  - resolution_final_source_dispatcher.py:build_wunderground_final_settlement_source_proposal
  - settlement_final_source_shadow_capture_runtime.py:run_settlement_final_source_shadow_capture_cycle
  - physical_final_source_shadow_capture_runtime.py:run_physical_final_source_shadow_capture_cycle
  - resident_p10_shadow_prepared_cycle.py:freeze_resident_p10_shadow_cycle
  - resident_p10_shadow_host.py:ResidentP10ShadowHost
  - resident_p10_shadow_bundle_runner.py:run_resident_p10_shadow_bundle
  - resident_p10_shadow_service.py:ResidentP10ShadowService
  - amsc_p10_shadow_bundle_loader.py:load_amsc_p10_shadow_bundle
  - scripts/run_resident_p10_shadow_inbox.py
  - amsc_p10_shadow_source_sidecar.py:capture_amsc_p10_shadow_source_attempt
  - scripts/run_amsc_p10_shadow_source_sidecar.py
  - p10_amsc_source_sidecar_acceptance.py:audit_p10_amsc_source_sidecar
  - scripts/p10_amsc_source_sidecar_acceptance_audit.py
  - p10_shadow_runtime_schema.py:initialize_p10_shadow_runtime_schema
  - p10_shadow_preparation_readiness.py:audit_p10_shadow_preparation_readiness
  - scripts/initialize_p10_shadow_runtime_schema.py
  - scripts/p10_shadow_preparation_readiness_audit.py
  - amsc_specli_resident_shadow_ingress.py:AmscSpecliResidentShadowIngress
  - resident_posterior_commit_outbox_shadow_worker.py:ResidentPosteriorCommitOutboxShadowWorker
  - observation_fast_lane_shadow_telemetry_writer.py:append_observation_fast_lane_shadow_latency_packet
  - observation_fast_lane_latency_runtime.py:build_observation_fast_lane_latency_slo_report
  - scripts/observation_fast_lane_latency_slo_audit.py
  - scripts/observation_fast_lane_shadow_db_slo_audit.py
  - dynamic_three_leg_trading_plan_runtime.py:build_dynamic_three_leg_trading_plan
  - dynamic_three_leg_shadow_decision_writer.py:run_isolated_dynamic_three_leg_shadow_decision_cycle
  - realization_state_projection_runtime.py:build_realization_state_projection
  - sequential_bayes_public_ssot_shadow_writer.py:commit_public_ssot_shadow_transaction_b
  - sequential_bayes_public_ssot_shadow_writer.py:commit_public_physical_ssot_shadow_transaction
  - scripts/probability_ssot_mainline_audit.py
  - scripts/sequential_bayes_schema_staging.py:build_in_memory_p1_staging_proof
  - resolution_bridge_runtime.py:build_final_resolution_label
  - resolution_final_label_proposal_runtime.py:build_final_resolution_label_proposal
  - resolution_label_shadow_writer.py:persist_final_resolution_label_shadow
  - resolution_final_source_runtime.py:build_amsc_physical_final_source_observation
  - resolution_final_source_runtime.py:build_wunderground_settlement_final_source_observation
  - resolution_final_source_shadow_writer.py:persist_final_source_observation_shadow
  - wunderground_final_raw_archive.py:fetch_and_archive_wunderground_daily_raw_capture
  - resolution_authority_candidate_runtime.py:build_resolution_authority_candidate_audit
  - resolution_authority_contract_runtime.py:validate_resolution_authority_contract
  - scripts/resolution_authority_candidate_audit.py
  - scripts/resolution_authority_contract_audit.py
  - scripts/fetch_market_rule_raw.py
  - scripts/initialize_resolution_final_source_shadow.py
  - scripts/register_resolution_target_contract_shadow.py
  - scripts/resolution_final_label_shadow_registry_audit.py
  - scripts/capture_target_rule_shadow.py
  - scripts/polymarket_monitor_daemon.sh:start_resolution_final_shadow_capture
  - scripts/capture_market_finality_shadow.py
  - scripts/capture_settlement_final_source_shadow.py
  - scripts/capture_physical_final_source_shadow.py
verification_gates:
  - python3 scripts/probability_ssot_mainline_audit.py --summary
  - python3 scripts/document_authority_audit.py
  - python3 scripts/production_chain_uniqueness_audit.py --no-db --summary
  - python3 scripts/live_trade_research_gate_isolation_audit.py
  - python3 scripts/resolution_final_label_shadow_registry_audit.py
last_verified_commit: working-tree-2026-07-15
---

# 目标架构 v2（Physical-State Bayes v6 Production / Market Execution Independent）

> **现行状态说明（2026-07-16）**：本文保留早期 shadow 与 v5 组件作为演进记录；它们不再拥有 current production 身份。现行唯一概率权威是 ADR-0003 的 Physical-State Bayes v6，见 `CURRENT_ARCHITECTURE.md` 与 activation。名称含 `shadow` 或 v5 的历史段落仅能用于回放、研究或事故审计，不能恢复第二 production composer。市场 bridge 与交易 execution 继续由独立 activation 管理。

> **官方数值模式修订（L1，2026-07-12）**：Open-Meteo、`source_inline_refresh_v1`、`v31_multimodel_context_policy_v1` 和旧 cache-only cutover 已退役，本文后续出现的相关段落仅保留作历史审计，不能作为实现或验收依据。当前有效合同见 [官方数值模式弱似然切换合同](../contracts/OFFICIAL_NWP_WEAK_SOURCE_CUTOVER.md)：ECMWF、ICON、GFS、JMA 分别从官方/授权 collector 获得，generation 冻结可用集合和缺失掩码。唯一弱似然主模型为每个受训组合使用独立校准、误差与相关性参数；缺源降低信息置信度与仓位能力，不能延迟 AMSC 入库，也不因缺一个预测源必然关闭 posterior。转发/聚合、旧 artifact 或 Open-Meteo 始终 fail closed；改 transport 即改 artifact，旧 Open-Meteo GFS/JMA 系数绝不能按字段名解释官方 GRIB 特征。

> **SSOT 概率主线运行基线（L1，2026-07-14）**：样本、strict-as-of、OOS 与 X→S→B bridge 只控制候选 artifact 晋级/市场投影，不控制物理 X live。生产 P0 固定由 `production_probability_p0_runtime` 重新读取获批 NCEI climate-reference、neutral-Z zero-information baseline、唯一 joint weak operational baseline 与 production-eligible strong-LR baseline 后编译；严禁复制 shadow checkpoint。activation 绑定稳定的 `prior_model_artifact_sha256`，P0 lineage 另存每日 `prior_artifact_sha256`。顺序 Bayes Transaction B、AMSC METAR/SPECI canonical drain、X-only public SSOT 与旧概率 writer 退役已经生产启用；任何概率 artifact/runtime 语义变化必须生成 successor activation、successor P0 与完整 canonical replay，不能用运维证明热替换。

> **报文定义 X（L1，2026-07-15 固定）**：production checkpoint 必须明确分为 `latent_scheduled_slot_high_distribution=p(Z)` 与 `reported_physical_x_distribution=p(X)`：`Z` 是固定常规 METAR 槽位的连续潜在日高，`X` 是各槽整度报文温度之最大值，由冻结量化规则从 Z 投影。常规 METAR 同时提供 `latent_observed_lower_bound_c=报文温度-0.5` 与 `reported_x_lower_bound_c=当日常规报文整数最大值`；SPECI 只作 strong soft evidence，不能推进 X 硬下界。`termination_risk` 从同一 `p(X)` 派生且 `updates_posterior=false`。bridge、public SSOT、UI 与策略只消费 `reported_physical_x_distribution`；旧 `physical_high_distribution` 已退役，不保留兼容分叉。任何投影或强似然语义变化都必须生成 successor activation、P0 与全 canonical replay，禁止原地热改。

> **现行 v6 强观测（L1，2026-07-16）**：唯一 composer 为 `physical_state_bayes_v6_pre_event_predict_update`。上一 checkpoint 先以 `O_<t`、冻结的候选条件化未来强迫、持续时钟和真实 `Δt` 推进 `P(Y,Z)`，当前 canonical METAR/SPECI 随后只进入一次 joint emission；常规 METAR 再施加 continuous-Y/reported-X hard condition，SPECI 只作 soft emission。禁止当前报文进入 Predict、禁止 NWP 小时曲线成为第二 likelihood、禁止 Update 后执行 post-transition。

> **First-CB 时序证据（L1，2026-07-15 修订）**：CB 不是最高温的布尔终止点；唯一强似然从 pre-event canonical 链追踪首次显式 CB 的 event/time/temperature，并在 current event 判定 `first_cb_this_event` 或 `cb_already_seen`。早于 14:00、14:00–16:00、16:00 后分别形成强/中/轻的剩余升温约束；约束只作用于高于截至当时 observed peak 的尚未实现 X 候选，对 observed peak 本身严格为零。TCU 不冒充 CB，任何分类均不得读取日终 final X。首次 CB 的直接 log-LR 只能在 `first_cb_this_event` 消费一次；后续 `cb_already_seen` 只保留时序状态，不得在每条报文重复乘同一惩罚。2026-07-14 实况首次 `FEW033CB` 为 17:00、当时已达 38°C，因此它不允许压低已形成日高，只能轻微削弱 38°C 以上候选并支持晚间终止状态。termination risk 仍是同一 posterior 的派生边际，不二次更新。

> **状态可视化 SSOT（L1，2026-07-16）**：桌面看板只从同一 `event_fact_latest` v6 reported-X SSOT 快照读取 `reported_physical_x_distribution`、八状态边际、StrongObservationUpdate 身份、动量、物理证据、First-CB lineage 与 termination risk。后端输出整数 X 桶、top latent state、Predict/Emission lineage 与证据影响；前端只翻译和渲染，不重算概率、LR、边缘桶或第二套状态机。v6 activation/checkpoint/artifact 身份不一致必须明确报错并 fail closed。

> **弱似然峰形宪章（L1，2026-07-13）**：先逐源、分条件校正偏差及残差分布，再按 exact source set 的残差相关性和信息量形成混合。峰形由校正后距离、残差尺度、密度重叠与相关性自然产生；既不强制多峰，也不强制单峰。预校准固定 scale 只是批准的无样本 fallback residual distribution，不是最终 RMSE；禁止用无证据的宽核抹平分歧。D0 AMSC 轨迹、动量和云雨露点只进入 strong update，绝不回写当日弱权重或 bias state。

> **运行证据合同（L1，2026-07-13）**：代码、plist 或 DB 中存在 cycle 都不能单独证明部署。唯一 monitor 启动时必须原子发布 `p10_monitor_runtime_status_v1`，绑定 `runtime_generation=ssot_probability_mainline_v1`、system domain 与实际存活 PID；resident consumer 每 5 秒发布 `resident_p10_shadow_runtime_status_v1`，绑定存活 PID、event date、prepared cycle、处理计数及最后结果。受管 forward consumer 固定 `--from-now`，启动前的 inbox backlog 不得进入新 event-date target；晚启动缺失只能由显式 replay 通道恢复，文件读取时刻不能重置 T1。`scripts/ssot_probability_runtime_acceptance.py` 只读联合校验 generation/PID/heartbeat、当地日期应有 cycle、active strong runtime、默认 joint weak artifact 与结构唯一性；offline promotion blocker 单列，不能混成 runtime blocker。

> **Cutover 状态（L1，2026-07-16）**：Physical-State Bayes v6 已完成首次生产切换，终态为 `production_v6_active_v5_retired_no_fallback`。持续验收使用 `production_probability_v6_readiness.py`、`production_probability_v6_live_acceptance.py` 与 `production_chain_uniqueness_audit.py`；runtime manifest、artifact、P0、canonical replay 或前驱身份不闭合即 fail closed。v5 与更早切换工具只保留历史证据，不再决定 current production。X→S→B bridge、交易策略和 T1→T5 仍分别控制市场投影与真实下单。

> **实时 SPECI 入口（L1，2026-07-14）**：目标生产入口固定为 `immutable raw archive/first_seen/raw hash → append-only CanonicalObservationEvent → production canonical drain → Transaction B posterior SSOT`。canonical 事实先独立提交；activation、P0 或推理失败只能阻断 posterior，不能回滚 SPECI。常规 METAR 与 SPECI 同等级更新强观测，但 SPECI 永远不能填补 44 槽常规 METAR 或改写 final `X`。`termination_risk.probability_no_material_new_daily_high` 是同一强似然更新后的剩余升温边际，`updates_posterior=false`，绝不作为第二个 likelihood 再乘一次。旧 SPECI materializer 与新入口互斥，切换前默认仍关闭新入口。

> **概率 writer 退役边界（L1，2026-07-14）**：`AMSC_ZUUU_APPLY` 只控制 canonical AMSC source-fact 入库，切换前后都必须保持；它不是旧概率 writer。需要原子退役的是常规 METAR 的 `amsc_posterior_materialize` queue/enqueue 与 SPECI 对同一旧 materializer 的直调，并由 production Transaction B 唯一替换。切换审计必须分别证明事实入口存活、两条旧 posterior 路径均关闭；禁止为消除旧 writer 而关闭 AMSC 入库。

## 权威边界

本文是 ADR-0003 的 L1 目标架构合同。v6 概率生产身份以 ADR-0003、`CURRENT_ARCHITECTURE.md` 和不可变 activation 为准；本文继续约束 bridge、动态交易、无订单演练与受控维护。Probability activation 只授权 orderless posterior，永不单独授权真实订单。

研究依据、详细 Schema、对抗记录和迁移 Checklist 位于 `docs/research/WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR.md`。研究文档不能覆盖本文或当前生产合同。

## L0 目标类型

```text
X = physical daily maximum
S = resolution-source settled scalar
B = market settlement bucket/outcome
C = versioned resolution/source/unit/day/rounding/bucket contract
```

```text
p(S|e,C) = Integral p(S|X,C) p(X|e) dX
p(B|e,C) = Sum_S p(B|S,C) p(S|e,C)
```

`operations_date_et` 与 `resolution_local_date(C)` 是两个独立字段。已接受的 ZUUU shadow authority 固定在 `config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json`：`X` 为 AMSC primary `METAR` 依冻结 44 槽形成的报文日高；`S` 为 ZUUU-bound Wunderground backing historical observations 在 `Asia/Shanghai` 目标日内的 metric 温度最大值；`B` 只由冻结 market bucket contract 从 `S` 导出。v3 渲染页 HTML 因含未来预报数组而退役，禁止解析其 `calendarDayTemperatureMax` 作为历史结算值。v4 仍为 `accepted_shadow_only`，bridge=`data_accumulation/closed=false`，不单独授权 Transaction B 或订单。

### X 校正与市场结算的四条独立通道

```text
AMSC closed station day ───────────────► final physical X
         │                                       │
forecast revisions as-of ────────────────────────┼──► weak likelihood daily correction samples
AMSC trajectory / momentum / physical evidence ──┼──► strong `physical_x` daily shadow samples
market target + finality + Wunderground S ───────┴──► X/S pair → bridge → B validation
```

`final X` 的产生仅由 closed AMSC station day 和其 raw lineage 决定，绝不读取/等待 target contract、forward market、market finality、Wunderground `S` 或 `X/S bridge`。弱似然的 daily candidate 使用 `station_day:ZUUU:<event_date>` 作为训练身份；market target 只能是可选注释，不能作为 P1 forecast + X 样本的门槛。`scripts/materialize_station_day_weak_physical_training.py` 在既有 monitor 的低优先级 child 中，以固定本地 09:00/11:00/13:00/15:00/17:00 的 strict-as-of cutoffs 将 candidate append 到 research storage；某一无效预测源只 quarantine 自己，保留其余合法 source set 并显式交由 missingness mask。candidate 的 `input_lineage_sha256` 只覆盖**该 event day**的 requests、forecast-only context 和同日 final-X；别日标签增加绝不能改变它。`materialization_sha256` 则覆盖 append revision 与 `supersedes` 血缘；只有 input lineage 真变才追加 successor。research reader 只接受 predecessor path/SHA/revision 全部闭合的连续 `candidate_revision` 链，并从其最高有效版本读取，禁止按文件名猜测、覆盖历史或双计。旧 v1/v2 candidate 只作为可审计前序。弱似然和强似然的 `physical_x` 样本都以“当时可见的输入 + 最终 X”日更追加；它们只生成候选参数和 shadow 对比，不能每日改写已批准基线。`X→S→B` 则是独立的慢通道：bridge 未闭合只能阻断市场桶校验、新 artifact 的真实交易晋级和市场级 OOS，不能阻断 AMSC 入库、SSOT 刷新、已批准基线实时运行或 X 样本积累。

训练/结算的最终标签采用 `resolution_final_label_v1`：带独立 raw-object provenance 的最终 `X` 与 `S` 必须先通过冻结 `p(S|X,C)` 的正支持验证，`B` 只能由冻结 bucket contract 导出。它只允许 append 到隔离 shadow registry；legacy `settlement_actual` 不是该标签的替代品。

每个市场先要形成 L0 target contract：`Gamma event raw -> content-addressed raw snapshot -> parse_market_rule_target_contract_extraction -> resolution_target_contracts`。parser 只读取 slug、目标日、resolution source、单位、舍入和 bucket 边界；outcome、价格、深度、成交、仓位与 P&L 均不在其词汇表。极端“以下/以上”桶在 contract 中以显式单侧无界表示，绝不以任意温度上/下限替代。`target_rule_shadow_capture_runtime` 只读取生产 `weather_market_discovery` 的 slug/日期/first-seen，并以 isolated registry 初始化时冻结的 `forward_capture_not_before` 排除所有历史发现；它不能把既有缓存回填成前向证据。

四条通道必须独立：`(1) AMSC coverage_v2 -> raw-lineage reader -> physical X`；`(2) forecast revision + X -> weak-likelihood training`；`(3) AMSC trajectory/物理证据 + X -> strong-likelihood training`；`(4) target contract -> Gamma 全桶 resolved 原文 -> market finality -> Wunderground S，并将已存在的 X 与 S join 为 X/S pair -> B`。其中 X 是 ZUUU 站点日标签：只要当前版本 44 个计划 primary `METAR`（本地 00:00–03:00 整点、04:00–23:30 半点）、无计划外槽/未解决 correction 与 raw integrity 闭合，便立即独立追加，绝不等待市场出现、target contract、market finality、S 或 bridge。晚到报文不否定物理 X；它只令 `strict_asof_training_eligible=false`，隔离 strict weak/strong/OOS 样本。Gamma finality sidecar 在 `resolution_local_date + 1 day 00:00 Asia/Shanghai` 前不得发出 finality 请求；日终后的临时网络失败只进入 retryable shadow 状态，结构性原文/身份错误才 fail closed。S 强制 raw ref/hash、原文内日期绑定且单位明确的标量抽取片段和 target-bound finality attestation。`coverage_v2` 是唯一可读 coverage 目录；旧 `coverage/` 的 48 槽快照已退役，不能兼容读取。若 raw objects 早于 v2 writer 存在，只能由 `materialize_amsc_daily_coverage_v2.py` 在显式确认下从 immutable raw+signed manifest 重建内容寻址的 v2 派生物，迟到/缺失状态不得改写。final-X 默认由 `amsc_primary_raw_coverage_reader.py` 直接重验 coverage membership，不依赖旧 `archive.db` 事实投影是否完整。Wunderground capture 只可访问 authority contract 指定的按日页面 URL，原始 HTML 与去敏 transport manifest 均 content-addressed；它不读取 Weather.com API、页面 current 值或 TWC 代理。X/S pair 只积累 bridge calibration，明确不产生 B，也不关闭 bridge。四条通道均只写 isolated shadow，不写 production archive、posterior、decision 或 order。

实时 AMSC primary `METAR` 的 release-window poller 同样只有一个所有者：`system/com.weather.monitor.daemon` 在 UTC `:00/:30` 的受控启动窗口派生 `amsc_zuuu_metar_taf_poll_once.sh` 子进程。旧 `gui/*/com.weather.amsc_zuuu_metar_taf_poll` 已退役，严禁与 monitor 并行运行。新鲜报文取得并完成不可变 raw object/manifest 归档后，唯一关键路径固定为 `minimal SourceObservation append → CanonicalObservationEvent append → sequential Bayes candidate → Transaction B SSOT commit(T3)`；不得把 `event_fact_history` 的旧 AMSC source-fact 投影、通用 task queue、shadow mirror、AWC、弱源网络刷新，或 `coverage_v2` 全 archive 扫描放在它前面。raw object 与 signed poll manifest 是强观测实时入口所需的唯一 archive 前置；`coverage_v2` 只是 final-X/训练/审计派生物，必须在 T3 尝试完成后独立物化，失败不能回滚 raw/canonical/posterior。`scripts/apply_amsc_probability_fast_lane_from_plan.py` 直接消费冻结 fetch plan 与 raw archive，A0/A1/Transaction B 共用一次 filesystem writer-queue claim，但仍保持三个独立 SQLite transaction：canonical 事实可在推理失败时保留，低优先级 writer 也不能插入 A1→B 间隙。SourceObservation 与 canonical 分别短事务提交；activation/P0/推理失败只能保留 raw/source/canonical 并使 posterior fail closed。A1 必须只把本次 plan 中“当前物理日、primary AMSC、尚未存在于 `canonical_observation_event_sources`”的 `source_observation_id` 交给 strict reader/canonical writer；显式空集合只能形成零事件幂等 no-op，禁止退化为整日 raw/relation 扫描。重复 source id 必须 no-op，身份或 raw lineage 不闭合则只隔离该项并 fail closed，绝不能为追求速度跳过重验。旧 AMSC fact projection 仅在 T3 尝试完成后运行，服务兼容与诊断，不是 Bayes 输入入口或概率 SSOT。writer queue、SQLite busy 与推理各阶段必须独立计时；任何争用导致 T1→T3 超过 3000ms 都是真实 SLO 失败，不得重置 T1、补造时间或迟到下单。

> **T1→T3 热路径收敛补充（L1，2026-07-15）**：A0 与 A1 共同只消费本次 plan 中“当前物理日、primary AMSC、尚未存在于 `canonical_observation_event_sources`”的 `source_observation_id`。provider 窗口里的历史 METAR、TAF/AMD/COR 和兼容行保留在 immutable plan，交由 T3 后各自通道处理，禁止在热路径逐行 replay 18/48 条窗口记录；尚未 canonical 的重试项仍执行完整 immutable drift 与 raw lineage 重验。canonical 禁表写保护由 SQLite authorizer 在执行前拒绝，不得为诊断在每条实况上前后扫描大型事实/决策表。writer queue、SQLite busy、A0、A1、activation、P0、推理和 Transaction B 必须分别埋点。

### 可执行的物理 X shadow posterior

目标公式已有唯一实现，不再把“强观测会修正预测”留在口头层面：

```text
P0(X,Z0) = Normalize(P_prior(X) × L_F(F|X,A,M) × P_prior(Z0|X,F))
Pt(X,Zt) = Normalize(HardCondition(Pt-1(X,Zt-1)
                                   × exp(raw_log_score_t(X,Zt-1))
                                   × transition_t(Zt|Zt-1)))
```

`physical_x_bucket_contract_runtime.py` 固定版本化、市场无关的物理温度域（含两端尾部）；任何暂时市场桶、slug、价格或结算规则都不得决定其网格或截断。P0 只接受 `physical_temperature_prior_context_v1` 与对应 latent-state prior；样本不足时不得生成 uniform/reference-measure P0。样本不足只限制新目标模型启动或晋级，不反向阻断当前已激活生产模型。

2026-07-13 的物理标签读链验收结果为三个 closed station day：2026-07-10=36℃、07-11=31℃、07-12=33℃；三条均因真实迟到状态保持 non-strict。prior reader 已以 forward final-X 身份覆盖同日 H 辅助记录，最终去重仍为三天。这个样本状态只限制新经验 prior 与 strong artifact 的离线晋级，绝不成为 AMSC live、已批准基线 posterior 或交易运行时阻断；目标 P0 在 prior context 未闭合时保持未启动，不能伪造 reference measure。

原 `physical_x_shadow_posterior_runtime → joint_physical_x_bayes_composer_runtime → physical_x_shadow_posterior_writer` 属于过渡期 absolute-strong-map 实验，已于 2026-07-13 退役并从 AMSC launcher 删除。它把 `p(X|O)` 形式的绝对强分布再次乘入 prior/weak，不能满足逐 canonical event 的增量 LR 时序，也会产生第二套 latest pointer。历史 `data/shadow/physical_x_posterior.db` 只保留为不可继续追加的旧实验数据，不是 SSOT、readiness 或 SLO 证据。目标概率链只允许 `initial_posterior_checkpoint_shadow → sequential_bayes_filter_shadow_runtime → Transaction B → public posterior SSOT`。

当前生产 queue 仅是 Transaction A.1 的过渡 dispatch：`task_queue_worker` 对全局写锁的**任务认领**最多等待 250ms，锁忙则保留 durable pending task 并在下一次快速轮询重试，不能以单次 30 秒等待吞没报文窗口。这只降低排队尾延迟，绝不构成 P10 的 `T1→T5≤3s` 证据；最终热路径仍是 resident host 的“常驻推理 + 原子 posterior/outbox + 无 generic task queue”拓扑。

**当前实测差距（2026-07-14）**：最新 legacy v3.1 AMSC event 的持久化时序是 `T1=19:00:42Z → T3=19:00:56.663Z = 14,663ms`；其中 source-response→posterior-apply-start 为 `8,000ms`、task created→started 为 `2,000ms`、apply-start→T3 为 `6,663ms`。该记录携带旧 queue lineage，并因公式契约/硬截止而 fail closed；不得把它称为 3 秒合格、也不得靠放宽 deadline 或恢复旧模型下单。正确迭代是把 P10 prepared resident host/Transaction B 从 isolated shadow 按已审批 artifact 与原子 outbox 合同推进到 production；**不是**继续调大 queue、提高 worker 频率、缩短 UI 刷新或让 legacy v3.1 走 CLOB。

AMSC 的 UTC 时间段/报文类型历史检索是额外的离线 H 回补通道，不替代上述前向 final-source 状态机。它可提供保留 query 原文与结果 raw 的历史 `METAR/SPECI` 序列；完整计划槽日由 `amsc_historical_physical_x_samples` 只读生成物理 `X`、升温/降温轨迹和剩余新高辅助样本，供结构学习、特征设计与轨迹重放。事后查询不能倒填 `first_actionable_seen_at`，故 H 不可进入 canonical T1、resident P10 延迟验收，也不可单独成为 strict-as-of/OOS 合格 strong-LR artifact 的训练证据。H 的价值是样本覆盖，不是实时到达证明；其 strict-as-of/OOS 结果仅决定后续 artifact 是否晋级，绝不构成已激活 live 交易链的运行时 gate。

`data/shadow/resolution_final_labels.db` 已初始化为 isolated sentinel registry（target/finality/final-source/observed-X-S-pair/final-label 当前均可审计）。所有 writer 按 identity append-only 幂等；五个 forward-only capture stage 由唯一 `system/com.weather.monitor.daemon` 的 `start_resolution_final_shadow_capture` 以单个低优先级 child 顺序调度，不能另装 LaunchDaemon、不能写 production archive、posterior、decision 或 order。启用后仍须以审计证明连续采集、无 archive 写入、无历史回填；在积累实际 X/S pairs 并完成 bridge 校准/OOS 前，仍不可声称 final-label coverage 或训练 readiness。

X/S bridge 的数据充足性只能由 `python3 scripts/resolution_bridge_pair_coverage_audit.py` 判定。它只读 registry，逐行重验 target identity、X 与 S 的有限值、两端 source-observation/raw-ref/raw-SHA，并同时要求至少 **90 个不同 event day、12 个不同 physical X、12 个不同 settlement S**。任一条件不足、row 损坏或 lineage 不完整均为 `accumulating`；该状态既不拟合/关闭 bridge，也不写 posterior 或订单。达到阈值也只进入 `ready_for_calibration_review`，后续仍需独立严格校准与 OOS，不能自动生成 B label 或改变 live。

bridge 的定义、artifact、promotion、activation、原子 market Transaction B 与切换 checklist 以 [X→S→B Resolution Bridge 合同](../contracts/RESOLUTION_X_S_B_BRIDGE.md) 为唯一 L1 权威。实现固定为“一份离散 `p(S-X|C)` artifact + 一份 immutable target contract”；不得为每个市场复制一套天气 posterior。`resolution_bridge_calibration_runtime.py` 使用全局/季节收缩残差 PMF 与 chronological expanding prequential OOS；`posterior_market_projection_runtime.py` 只投影已提交 `p(X)`；`production_reported_x_market_transaction_writer.py` 在 bridge 获批后才可原子提交 RealizationState/public `p(B)`/outbox，并继续永久 orderless、无网络、无 CLOB。

## 七层架构

| 层 | 唯一职责 | 正式输出 | 禁止 |
|---|---|---|---|
| L0 Target Contract | 定义 X/S/B、时间、resolution | raw-bound append-only target contract | 概率、outcome/价格、交易 |
| L1 Evidence | prior、forecast revision、canonical observation | immutable evidence snapshots/events | posterior、交易信号 |
| L2 Sequential Bayes | 状态预测、raw score、posterior delta、hard condition | checkpoint candidate | 市场和策略 |
| L3 Posterior SSOT | X/S/B 边缘化、RealizationState、原子提交 | PosteriorSnapshot + outbox | 重算证据、交易 |
| L4 TradingPlan | 动态三腿候选、组合目标、资金和互斥 | auto_trade_decisions + intents | raw weather 重判 |
| L5 Execution | 风控、幂等、签名、POST、reconcile | order lifecycle facts | 修改 posterior/策略 |
| L6 Read/Audit | UI、回放、准确率和时延 | read-only snapshots/reports | 写事实、复算概率 |

### L1 ForecastRevision 严格到达边界

预测弱似然的 strict-as-of Tier A 不是“请求发出”或“body 读完”。唯一合格的 arrival evidence 是 immutable raw envelope 在 body 读取前记录的 `response_headers_observed=true`，并绑定 `requested_at`、header 到达 `response_started_at`、`response_completed_at`、raw provider body/hash、source 与 target。`response_completed_at`、provider declared time、文件 mtime、derived metrics 或 caller 传参都不能补齐该事实。缺 header evidence 的 live snapshot 只能是 Tier C；historical declared availability 最多是 Tier B；B/C 不得形成 ForecastRevision strict-training event、动态弱权重或 strong artifact 训练样本。当前 joint production 不因该 shadow 合同改变。

Tier-A forecast 的 P1 ingress 只可追加既经 `strict_asof_forecast_source_observation_proposal_v1` 验证的 `forecast_weak_source`。`strict_asof_forecast_p1_ingress_writer_v1` 在 P1 六张表已经完成 additive migration 后，才允许在调用方的显式事务中写入既有 `source_observations`；它重验 header/raw/snapshot 与 peak-valid-time 或 `daily_max_event_window` record-valid-time 绑定、只保留 append/idempotent 或 fail-closed replay drift，且不写 canonical event、posterior、fact/latest、outbox、decision 或 order。daily window 必须 `trajectory_eligible=false`，不能改名为峰值轨迹。`strict_asof_forecast_p1_reader_v1` 是唯一返回给 ForecastRevision 的读取边界：它反序列化 `observation_json` 后逐项重验与表列、payload、header/raw/时间语义的绑定；任一 drift 不向弱似然泄漏。`append_strict_asof_forecast_p1_ingress.py` 默认只读；apply 需确认短语和 writer quiescence。首次实际 activation 必须使用 `activate_p1_schema_and_forecast_ingress.py`：它先为每个待 append event-day 运行内存 `raw Tier-A→append→reader→latest-visible ForecastRevision` preflight，之后才在同一 `BEGIN IMMEDIATE` 事务完成 additive P1 DDL、exact-schema verify 与全部 Tier-A append；任何 preflight/DDL/验证/append 失败都不进入或回滚事务。2026-07-12 首次 activation 已创建六表/六索引并追加 11 条 Tier-A forecast row；post-apply exact schema、strict reader（11/0 blocked）及四条 latest-visible ForecastRevision 均通过，且 canonical/posterior/fact/latest/outbox/decision/order 均仍为零写入。某一来源缺两种合格时间语义时只 quarantine 该来源，不能猜补时间，也不能阻断其他独立合格来源。

## Bayes 事件时序

### P0：唯一初始 posterior（先验 × 弱似然）

在当天第一条 canonical 报文被强似然消费之前，唯一允许创建的概率检查点是：

```text
Posterior_0(X,Z_0+)
  = Normalize[p_prior(X|A) * L_F(F*|X,A) * p_prior(Z_0+|X,A,F*)]
```

P0 数学只有 `build_initial_posterior_checkpoint_for_mode` 一份；shadow 与 production 具有不同身份和提交边界。production 只能经 `production_probability_p0_runtime → production_initial_posterior_checkpoint → production_initial_posterior_writer`，且 prior、weak、strong 三类输入必须显式 `production_runtime_eligible=true, shadow_only=false` 并匹配 activation。P0 只接收同一温度格上的连续气候先验、`joint_forecast_weak_likelihood_v1`、初始 source-reliability state、strong-LR 的结构/identity 以及冻结 resolution contract；不接收 D0 `METAR/SPECI`、轨迹、动量、云雨、盘口、策略、订单或 v3.1 输出。bridge 未闭合仍写 P0-X，S/B 明确 NA；B 不可用绝不能阻断后续实况更新。

`scripts/bootstrap_initial_posterior_shadow.py` 不接受 caller 在输入 JSON 中携带 strong artifact；预览与 apply 都必须先按 `target_identity + decision_as_of` 从同一 isolated registry 读取 active scorer，再构造 P0，apply 时由持久化边界再次核验该 active identity。这样“预览通过、落库换模型”的双口径被关闭；registry 缺 active artifact 时明确返回 runtime blocker，不以测试 artifact、外部文件或 payload 内对象回退。

生产 P0 在一个事务内追加 `weak_forecast_evidence_snapshots(state=active)`、`posterior_checkpoints(checkpoint_kind=initial)`、`production_initial_posterior_lineage`、X-only public fact 与 `event_fact_latest`；不写 StrongObservationUpdate、RealizationState、outbox、decision、intent 或 order。lineage 同时绑定稳定 prior-model SHA、每日 prior-context SHA、weak artifact/evidence snapshot、strong artifact、初始 source-reliability pre-state、resolution 与 activation SHA。唯一性键是 `target_identity + activation_contract_sha256`：每个 activation 只能有一个 P0；同一 target 在任一时刻只能有一个由当前 activation 选中的活动 generation。新 activation 只能以 `replay_generation+1` 和 `supersedes_checkpoint_id` 追加线性 successor，不得形成并行 fork；同 activation 重试幂等，任何不同身份均 fail closed。strict-as-of/OOS 只决定后续 artifact promotion，不是 live P0 或 AMSC 入口 gate。

生产 Bayes 公式固定为 `P0=Normalize(Pprior×Lweak0×P(Z0|X))`。强观测 evaluator 输出截至当前 canonical 链的累计证据评分 `log Lstrong(E1:t|X,Z)`；composer 每次只消费条件创新 `ΔlogL_t=logLstrong(E1:t)-logLstrong(E1:t-1)`，等价于替换上一份累计强证据，禁止把重叠轨迹、云雨背景或首次 CB 在半点/整点/SPECI 上重复相乘。凌晨至 08:00 的自然夜间降温对当天午后最高温 X 直接似然保持中性，08:00–09:00 平滑启用，09:00 后才完整消费升温动量；报文事实照常入库，常规 METAR 硬下界始终立即生效，SPECI 仍只作 soft evidence。新 TAF/TAF AMD/TAF COR 同理应用 `log Lweak,new(X)-log Lweak,old(X)`。TAF 修订必须先按 target event date 隔离，再按报文自身 `issue_time_utc` 排序；晚发现的旧 issue 只归档、不覆盖已可见的新 issue，同一 issue time 才以 `COR > AMD > original` 决胜。若修订没有改变模型实际消费的预测特征，记录为 neutral revision，不伪造概率变化。每次有效修订必须在一个 `BEGIN IMMEDIATE` 中原子追加新 weak snapshot、更新、后继 checkpoint 与 public SSOT/latest；canonical replay、迟到 TAF 或旧 issue 的时间戳均不得令 head/latest 倒退。

生产跨日由 AMSC fast lane 内的 `ensure_production_daily_p0` 负责：canonical 原始事实可以先独立持久化，但在任何当日 canonical event 进入 Strong-LR 前，必须幂等确认当前 activation 的当日 P0；不存在时以当时可见的先验与唯一联合弱似然原子创建，再消费已落库观测。该步骤不读取 METAR/SPECI 作为 P0 输入，不等待市场发现、market finality 或 X→S→B bridge。public slug 只是由 ZUUU 物理日期确定性生成的稳定展示键，不构成市场存在证据。同 activation、同 target 的重复调用必须返回原 P0；并行 lineage、P0 输入不可用或 activation 漂移一律 fail closed，但 canonical 事实保留。

```text
Posterior_t_minus(X,Z_t^-)
  = Transition(Posterior_t-1(X,Z_t-1^+), delta_t)

raw_log_score_t(X,Z_t^-)
  = observation_model(o_t, X, Z_t^-, history, F*, A)

normalizer_offset_t
  = LogSumExp(log Posterior_t_minus + raw_log_score_t)

posterior_delta_t
  = raw_log_score_t - normalizer_offset_t

Posterior_t_pre_state_update
  = Normalize(Posterior_t_minus * exp(posterior_delta_t) * hard_condition)

Posterior_t(X,Z_t^+)
  = PostStateTransition(Posterior_t_pre_state_update, o_t)
```

`o_t` 只能影响 `Z_t^+`，不能修改用于自身评分的 `Z_t^-`。

strict-as-of/OOS 的职责是离线版本 promotion、训练数据质量与回放评价；它们不属于 L4/L5 的 live entry block，也不应由 P8/P10 每事件重验。P8 只校验已激活 scorer 的结构、identity 与当前 freshness，L4/L5 只消费已提交 posterior SSOT 和实时风险/执行事实。缺离线证据只能阻止新 artifact 被激活，不能阻断已激活 live 交易链。

## 原子提交

推理在事务外执行。提交前 CAS 验证 previous posterior/checkpoint、canonical sequence、resolution contract、已激活 artifact 的 identity/structure、当前 freshness 和 deadline；不得在此重验 strict-as-of 训练、OOS 或离线 approval packet。

单一 Transaction B：

```text
StrongObservationUpdate
+ PosteriorCheckpoint
+ RealizationState
+ event_fact_history PosteriorSnapshot
+ event_fact_latest pointer
+ posterior_commit_outbox
+ posterior_outbox_execution_contracts（与 outbox 一对一）
```

全部成功才 commit。`posterior_outbox_execution_contracts` 在同一 Transaction B 内固化 canonical `T1=first_actionable_seen_at`、精确的 `T1+3000ms` 订单硬截止、T3 提交时间以及提交时是否仍具执行资格；历史回放或迟到提交允许保留 posterior/outbox 证据，但必须写 `execution_eligible_at_commit=0` 与明确 `execution_na_reason`，绝不能成为 live 候选。outbox 领取必须使用 lease owner、lease expiry 和 generation CAS，并在查询和 claim CAS 两层重验该合同；样本、strict-as-of、OOS 与 bridge 积累不参与此 live gate。

运维存储边界同样不得倒挂：`archive.db` 的短切换回滚只使用本机同一 APFS 卷的瞬时 CoW 克隆，外置盘只服务可丢弃、可重建的无用数据异步冷归档。生产脚本禁止检查外置盘挂载、空间、速度或全量哈希，禁止向其复制生产数据库，禁止让任何外置盘状态进入 readiness、维护窗口、服务启停、SSOT 或交易 gate。

当前 reported-X 候选数学只有一条：`production_reported_x_event_runtime.py` 从 canonical event、direct predecessor、冻结 P0/weak/strong lineage 生成 orderless successor candidate；`reported_x_successor_checkpoint_runtime.py` 只消费当前累计 strong score 与前序累计 strong score 的条件创新。候选不写库、不读市场、不做决策、不接触执行凭据。bridge 未闭合时只产生 StrongUpdate、reported-X Checkpoint 与 X-only public SSOT，S/B/realization 明确 NA，零 outbox、decision 或 intent；任何 market/strategy 对象都不得混入该概率事务。

bridge 闭合时也不允许另起第二条概率链：同一 reported-X candidate 先由 `posterior_market_projection_runtime.py` 生成 `p(S),p(B)`，再由 `reported_x_market_realization_runtime.py` 生成 B-space RealizationState，最后由 `production_reported_x_market_transaction_writer.py` 在同一 Transaction B 提交 checkpoint、realization、public SSOT 与 outbox。writer 必须验证前驱 posterior SHA CAS、冻结 P0 context、唯一 activation lineage、realization 内容寻址、public `X` 与 checkpoint 完全相等、bridge/target SHA 及直接 `current_report_bucket` 为空。bridge 样本/OOS不足只关闭此分支，绝不关闭 physical-only Transaction B。

UI 必须保持同一语义边界：`/api/ui_snapshot` 直接从 `event_fact_latest` 的 `sequential_temperature_posterior_ssot_v3_reported_x.reported_physical_x_distribution` 投影只读整数 X 后验；不得把某个精确 X 桶改名为 `34°C or below`。后者是市场 B 的累计边缘合约，只能在闭合 bridge 生成 `market_bucket_probabilities` 后出现在市场投影区。bridge 未闭合时 X 后验照常可视化，市场 B 明确 `market_projection_na`；UI 不运行旧投影、不前端复算，也不以市场桶缺失遮蔽 X。所有 UI/trace/read endpoint 只能使用 `mode=ro` 的 `archive.db` 快照与内存展示缓存：页面请求不得抓取市场、刷新盘口、清理执行状态、启动交易任务、进入 archive writer gate 或修改任何事实/decision/intent/order 表。缓存过期或 posterior pending 时只返回标记 `stale/refreshing` 的上一已提交 checkpoint；后台展示计算也只能重读同一只读事实层，不得创造第二 posterior 或以 UI 时间重置 T1。

旧 `production_sequential_bayes_public_ssot_writer.py / production_probability_runtime_activation_v2 / sequential_temperature_posterior_ssot_v2` 段落属于已退役的 physical-high 纪元，不再是生产写入权威。当前 reported-X writer 为 `production_reported_x_public_ssot_writer.py`：它不复用影子提交器，在一个 `BEGIN IMMEDIATE` 内原子提交 StrongUpdate、reported-X Checkpoint 与 `event_fact_history/event_fact_latest` public SSOT；任一步失败全部回滚。bridge 未闭合时结构上禁止 RealizationState、outbox、decision、intent 与 order。writer 必须验证 `config/production_probability_runtime_activation.json`、P0 lineage、direct predecessor、canonical sequence、prior/weak/strong artifact 和 runtime manifest；当前累计强证据纪元还必须验证相邻 raw-score SHA 与条件创新身份。它不运行推理、不读取市场、不签名、不发 CLOB 请求，且永久声明 `places_orders=false`、`authorizes_clob=false`。

切换失败后的数据库证据采用 append-only activation generation，不执行清表或伪装幂等：新 P0 generation 必须引用被替代 checkpoint，运行时按当前 activation hash 选择唯一 lineage，并从新 P0 重新消费 canonical 序列。这样 activation 文件回滚不会把已经提交的旧 generation 伪装成当前生产血缘，后续重试也不会因“目标日已初始化”或旧 StrongUpdate 存在而跳过物理观测。

`production_reported_x_transaction_host.py` 是 reported-X writer 的唯一调用边界：它只接收已完成推理、绑定 direct predecessor 与受管 activation 的冻结候选；任何 shadow、旧 schema、旧 generation 或 lineage 漂移均拒绝。常规 METAR、半点 METAR 与 SPECI 均经 SourceObservation→CanonicalObservationEvent 进入同一 fast lane，TAF/AMD/COR 只经独立 weak-revision replacement 事务更新。启用顺序固定为：停写并证明 quiescence → 精确备份 → 切换冻结 runtime → 写 successor activation → 重建 P0 → 全 canonical replay → 零订单 acceptance → 恢复服务 → readiness。新旧概率 writer 绝不能并行。

`scripts/production_reported_x_runtime_readiness.py` 是上线后的唯一持续只读 readiness 入口：验证 activation/runtime manifest 及其本地依赖闭包、P0 lineage、latest reported-X checkpoint/SSOT、累计强证据身份、pending canonical、METAR/SPECI/TAF revision 入口和唯一 system monitor。它必须读取实际安装的 `/Library/LaunchDaemons/com.weather.monitor.daemon.plist`，证明半点 METAR、production probability update、SPECI watch 与 SPECI probability ingress 四个开关均启用，不能只凭仓库模板或源码字符串通过；同时只将已捕获的 METAR 半点、SPECI、TAF original/AMD/COR 数量作为观测证据，某类报文当天没有自然出现不得阻断 live。`scripts/production_sequential_bayes_chain_audit.py` 只对同一 reported-X activation 追加逐事件 lineage 与真实 T1→T3 证据审计，不再把 v2 activation/旧 manifest 当作当前生产口径。训练样本、strict-as-of、OOS 与 bridge 不得成为 live 阻断；身份漂移、事件缺口或 writer 不唯一必须 fail closed。

`scripts/build_production_probability_v6_candidate_activation.py` 是 v6 candidate activation 的内容寻址生成入口；首次 `scripts/cutover_physical_state_bayes_v6_production.sh` 已执行并永久关闭重复切换。后续 v6 epoch 必须绑定 prior/weak/transition/observation artifact、公式、composer/checkpoint/update/public-SSOT schema、runtime manifest 与旧 writer 退役证据，并在 writer-quiescent 窗口完成新 P0、全量 replay 和零订单验收。

`scripts/production_probability_v6_live_acceptance.py` 验证 activation、P0、全部 canonical replay、整数 `reported_physical_x_distribution`、SPECI-soft/常规-METAR-hard-bound、Predict/Emission lineage、pending=0 与 v5 fallback 禁止。`scripts/production_probability_v6_readiness.py` 验证 live head；真实报文的 `first_actionable_seen_at(T1) → SSOT committed_at(T3) ≤3000ms` 继续独立记录，隔离 clone 只能作候选性能证据，不能冒充 production 前向样本。2026-07-16 切换 replay 最大单事件 Transaction B 为 241.901ms、p95 为 163.169ms；该证据证明概率计算满足 3 秒预算余量，但不冒充 T5 下单完成。

### P10 主报文入口边界

强 LR 的拟合边界由 [强观测 LR 训练与拟合合同](../contracts/STRONG_LR_FIT_CONTRACT.md) 单独定义：`post_state_transition` 是物理路径 latent state `Z_t^-→Z_t^+`，绝不是 source reliability 的 post-state。`strong_observation_lr_fit_runtime.py` 现可从冻结 dataset 和 latent replay label 生成 shadow candidate，但不写 registry/不激活；新候选仍必须由离线 strict-as-of/OOS 晋级。运行时则只认 `strong_lr_runtime_artifacts_shadow + strong_lr_runtime_artifact_activations_shadow` 的唯一 active identity：已有批准基线可直接持续运行，样本、OOS、bridge 不参与每事件阻断；从未批准过任何可用基线的全新目标链才因 active artifact 缺失 fail closed。禁止以手工物理规则、旧系数或无信息 placeholder 伪造批准基线。

`strict_amsc_hard_observation_reader` 是 P2 离线/回放唯一的 primary AMSC 读取边界，不是实时 watcher。它从既有 `source_observations` 只读取 primary AMSC 行，并逐项核验表列、`observation_json`、不可变 raw object、签名 manifest、source/station/report-type/valid-time/raw hash 与原始 `first_seen_at` 的闭合；legacy fact-projection 行与 AWC 都不能由它重建 T1 或变成 primary strong event。读取器把 `first_transport_seen_at=first_actionable_seen_at=immutable first_seen_at` 明确注入 canonicalizer；原始首见与 as-of clock 不一致的历史行独立 quarantine，绝不以本次 `fetched_at` 修补、回填或延迟其他合格报文。AMSC raw archive metadata 一经附着，后续 re-fetch 的 `as_of_eligible_at` 必须同步复用 immutable first-seen，不能保留本轮抓取完成时间。`p2_canonical_amsc_staging_probe.py` 只在内存表证明 strict primary evidence→canonical append→retry idempotency；它不写 `archive.db`、posterior、fact、decision 或 order。

`p2_canonical_amsc_forward_writer` 固定 `target_identity=physical_weather_day_v1:{station}:{local_date}`：这是**物理站点日**，不是可变 market slug、resolution contract 或 posterior target。writer 在 shared archive writer gate 下开启自己的 `BEGIN IMMEDIATE`，重验 P1 exact schema 和 strict raw/manifest/T1 边界，只允许 append `canonical_observation_events` 与 `canonical_observation_event_sources`；任意 strong update、checkpoint、realization、fact/latest、outbox、decision、intent 或 order 表变化均回滚。生产快通道现在先以同一冻结 plan 最小提交 `source_observations`，随后立即调用该 canonical writer，再进入唯一 production drain；它不再等待 v3.1/legacy source fact。`append_strict_amsc_p2_canonical.py` 保留为显式审计/恢复入口，不是 live poller 的子进程链。shadow mirror 永远在 T3 之后，失败不得回滚生产 canonical 或 posterior。

AMSC poller 与 P10 sidecar 的触发和事实资格必须分离：唯一 monitor 在所有 UTC `:00/:30` 都触发一次 AMSC 采集，以发现 METAR、SPECI 及 TAF 原报/AMD/COR；`amsc_poll_slot_runtime` 仍以**每次实际 fetch attempt**的开始时刻判断常规 METAR 资格，并只允许 `latest_metar_at == expected_slot_at` 的计划报文进入 strong lane。本地 00:30–03:30 虽不在 44 槽 regular-METAR 时刻表内，也必须 fetch：没有新原文时写审计 no-op；出现新 TAF revision 时进入 weak forecast-revision lane；旧整点 METAR 绝不能重复成为 canonical strong event。其余计划槽返回更早或更晚 METAR 均明确 retry/留痕。因此 SQLite 锁导致的长时间 retry 不会再以 launcher 的旧槽位放行旧 METAR，半点触发也不会伪造物理观测。

`p2_canonical_shadow_mirror` 是 P2→P7 的单向 replay bridge：它在一个 source read snapshot 内先重新执行 production P2 strict proof，再读取同一 snapshot 的 canonical rows；destination 必须已有 exact P1 schema 和 `v2_shadow_runtime_manifest=isolated_shadow_only`。它只以独立 transaction append canonical event/source relation，任何 shadow checkpoint、public fact、outbox、decision 或 order 表变化均回滚。2026-07-12 的 durable `data/shadow/p2_canonical_amsc_shadow.db` 首次从 `archive.db` 只读镜像 16 event/16 relation，retry 为 0/0；04:30Z/12:30 CST forward sidecar 又自动镜像 1/1，当前为 17/17，所有 Transaction-B/public/decision 表仍为 0。AMSC launcher 只在 P2 canonical append 成功后调用 mirror；P2/mirror 的各自 result 写入采集 status，任一失败均不能回滚已提交 production fact/P2 capture。该 mirror 是真实 source input 的 isolation proof，**不是** artifact approval、Transaction B、forward SLO 或 production activation。

`amsc_p10_shadow_source_adapter` 是 AMSC acquisition bundle 到 prepared host 的唯一 shadow adapter。它只读取 `amsc_zuuu_metar_taf_observation_bundle_v1` 中已 raw-archive 绑定的 primary AMSC `METAR/SPECI`：每条必须携带 immutable `payload.raw_object_ref`、`arrival_time_quality=observed_first_seen` 与 `first_seen_at`，且逐项匹配同 bundle 的 archive receipt（manifest ref/SHA、raw SHA、object ref、T1、arrival quality）；随后才按 T1、valid time、source observation id 逐条同步交给 `ResidentP10ShadowHost`。本轮 poll 的 `fetched_at/response_completed_at`、TAF、AWC 或整个 batch 都不能制造 arrival。adapter 只压制同一 batch 中完全重复的 `source_observation_id`；跨 batch/重启的物理报文去重和 T1 续用只能由 journal 完成。某条 primary 缺 archive lineage 或与 receipt 不符会被独立拒绝，绝不可用当前轮询时间补写或拖慢其他合格主报文；host 故障则停止该 batch，防止后续报文越过 retryable 前序事件。它没有 network、queue、archive 写入、AWC wait、signer 或 CLOB，仍不是 watcher。

`amsc_specli_resident_arrival_contract` 是主报文到 resident ingress 的唯一 host-facing 信封：它只接受一个已经解析且带 `arrival_time_quality=observed_first_seen`、`first_seen_at` 的 primary AMSC `METAR/SPECI`。T1 固定等于该已持久化 `first_seen_at`；本次轮询的 `response_completed_at` 只作 retrieval lineage，永远不能重置 T1。整批 poll 结果不是到达事件，重抓旧 raw 报文也不会成为新信号。信封在交接前重新推导并逐字段比对，调用方不能篡改 source identity 或 T1。

`AmscSpecliResidentShadowIngress.process_arrival_envelope` 只消费上述信封，并先验证 `valid_time ≤ first_transport_seen_at ≤ first_actionable_seen_at ≤ decision_as_of` 与 `T1→dispatch<=3s` hard deadline。入口将已接收报文交给同进程 resident shadow worker；worker 会以其 resident clock 再次取得 canonicalization cutoff（不信任 caller 的 `decision_as_of`），canonical append 后捕获 T2，并在 T2 已越过 T1+3s 时仅保留 canonical fact、拒绝 P8。它不拥有网络轮询、不等待 AWC、不使用通用队列、不启动子进程，也不含 signer/CLOB。故它只能证明 source→shadow 的入口正确性，不能被称为常驻 watcher 或真实下单链路。

`ResidentAmscSpecliArrivalJournalShadow` 是该信封之前唯一允许持久化 T1 的 isolated-shadow boundary。它以 `source/station/report-type/valid-time/raw-sha` 锁定物理 arrival，在任何 canonical/posterior work 之前 durable commit 首见时间；同一物理报文重抓、进程重启或 later response 只能复用该 T1。成功 dispatch 与 deadline-blocked 都成为 terminal 去重状态；其他下游临时阻断可在原 T1 下重试，绝不重置时钟。它没有网络、queue、subprocess、production archive 或 CLOB 权限，仍不是已部署 watcher。

`resident_fast_lane_prewarm_contract` 禁止以 `prewarmed=True` 空声明启动 resident worker，并把依赖拆成两个闭集。概率 SSOT 只重验三类 immutable identity 与有效期：forecast snapshot、prior context、已激活 strong-LR score surface；任一失效时保留 canonical observation 并拒绝 posterior mutation。market snapshot 只由独立 `validate_resident_market_projection_prewarm` 服务 `X→S→B` 与决策：bridge 未闭合、市场缺失或 market context 过期时，P8 仍必须原子提交 StrongUpdate、Checkpoint 和 `posterior_ssot_fact(state=physical_only_market_projection_na)`，仅将 RealizationState/outbox/decision 标为 NA。signer/connection pool 完全属于 L5 执行预检，由独立 `validate_resident_execution_prewarm` 在真实 sign/submit 前验证；它不是 Bayes 输入、不是概率 prepared-cycle identity，也不得阻塞 posterior。概率依赖 SHA 是**运行血缘，不是 Bayes 证据**：它在同一事务持久化到 `strong_observation_updates.path_auxiliary_json`，并随 `event_fact_history.posterior_ssot_fact.runtime_dependency_lineage` 对外可核验；同一事务 retry 的指纹不一致即 fail closed。离线 strict-as-of/OOS/approval 仅在激活新 artifact 前校验，不在每事件热路径复验。任何 host 重新加载、同步 forecast refresh 或偷偷替换 artifact 都不能被热路径掩盖。

概率 runtime manifest 必须覆盖从生产入口可达的全部本地 Python 依赖，而不是只登记顶层 writer。当前 reported-X 切换前必须由 `production_probability_runtime_manifest_v2_reported_x_dependency_closure` 同时证明 `missing_runtime_files=[]`、`retired_legacy_files_reachable=[]`、`parse_errors=[]`；checkpoint reader、activation validator、联合弱模型、TAF 修订排序、schema/writer 等任何依赖变化都会改变 manifest SHA，并使旧 activation fail closed。旧 `production_weak_forecast_revision_writer/event_runtime` 明确排除于 successor manifest；TAF/AMD/COR 只复用无数据库权限的 `taf_revision_candidate_selection_runtime.py`，不得通过纯选择函数重新把退役 writer 拉回生产闭包。

P8 不允许将 event-specific `trajectory_feature_state` 作为 prepared-cycle 或 caller 输入冻结：它必须在本地从 `first_actionable_seen_at<=T0` 的 canonical sequence 重新派生。当前事件以 `O_<t` 的 pre-event trajectory、当前 METAR/SPECI 物理字段和 `W_t^-` 进入 `strong_observation_lr_input_v3`；`strong_observation_feature_evaluator_v2` 在不污染 pre-state 的前提下组合当前报文，生成当前步升降温速度、60/120 分钟背景动量、加速度、含当前值的已观测峰值、回落深度/持续时间、当地时刻，以及对每个候选最高温 X 单独计算的“剩余升温”。因此 raw score 是候选 X 相关的物理可达性，不再是所有候选共享一组轨迹类别。`O_<=t` 的 post-event trajectory 只随新 checkpoint 持久化。当前没有受训的 `W_t^+` source-reliability head，因此 after state 必须与 pre state完全相同，caller/prepared cycle 的不同值一律拒绝。旧 v2 仅含 pre-event 30m slope、缺当前步动量/当地时刻/已观测峰值/回落和候选剩余升温，已退役且不能加载。缺特征 bin、artifact identity/结构不符或当前概率依赖失效，均在事务前 fail closed；strict-as-of/OOS/approval packet 缺失只阻止新版本 promotion，绝不作为已激活版本的实时事件阻断。该实现仍只写 isolated shadow DB；目前没有 production activation。

生产 AMSC 链的唯一有效交接是：raw archive 与 fetch plan 先冻结；Transaction A0 只 append immutable SourceObservation，Transaction A1 只 append canonical event，随后使用 P0 已冻结的 prior/weak/strong artifact 在进程内构造候选，Transaction B 原子提交 StrongUpdate、Checkpoint 与 public posterior SSOT（bridge 闭合时同时提交 RealizationState/outbox）。该链不创建或消费 `amsc_posterior_materialize` 通用任务，也不以 legacy `amsc_source_fact_id` 作为 canonical 前置。官方弱源 generation 必须在 P0/activation 时已冻结；热路径不发起任何数值模式网络请求。缺源组合、artifact 或 activation 失效只使 posterior fail closed，不能回滚 raw/source/canonical。官方 collector、generation 及缺源组合完整约束仍以 [官方数值模式弱似然切换合同](../contracts/OFFICIAL_NWP_WEAK_SOURCE_CUTOVER.md) 为准。

过渡链仍须写 `archive.db` 时，`archive_db_write_queue` 的目录锁只允许两种恢复：持有 PID 已死亡，或目录在 `mkdir→pid/owner` 初始化断点超过一秒仍无任何可解析 owner。后者仅表示 creator 已崩溃，恢复时只删锁、不复用 payload；有效 PID 的锁仍按正常 stale policy 保留。所有 writer 的硬边界是“**锁内只做本地短事务**”：schema 初始化、查询、外部 HTTP/报文抓取、模型刷新、队列等待和任何可阻塞 I/O 必须在取得写锁之前完成，或在其后先 `commit` 并释放锁。**AWC 是 reference METAR：其抓取完全只读，绝不能写 `event_fact_history/latest`、重算 posterior 或触发订单。** 只有 AMSC canonical materializer 能在其本地短事务中原子追加事实与 posterior task。遗留 WU/TWC、PWS 和 hard-source-direct 写入器仍须迁移到各自的 source-revision/archive 表；在该迁移完成前，它们不是 P10 3 秒链的证据，更不得成为 canonical posterior producer。该恢复与边界只消除异常锁或网络持锁造成的 30 秒等待，**不是** P10 的 3 秒达标证据；目标 P10 路径仍以独立 shadow transaction 避开 shared archive writer。

`strong_observation_lr_runtime_registry` 是强 LR runtime identity 的唯一 SSOT。artifact 内容与 activation event 分表 append-only 保存；每个 `target_identity` 在任一 as-of 只由最新 activation event 选出一个 scorer。activation basis 仅允许 `approved_runtime_baseline` 或 `promoted_candidate`，且两者都必须把 `strong_lr_runtime_activation_attestation_v1` 的证明本体与 `activation_evidence_sha256` 一并冻结进 activation event，不能只留路径、字符串或外部可变文件。基线证明必须明确其 `operational_precalibration_baseline` 身份、批准状态、未冒充 trained/OOS promotion 及风险边界；候选证明必须携带内容哈希闭合的 `strong_observation_artifact_promotion_gate_v1`，并与注册 artifact 的 candidate、training manifest、OOS evaluation 和 feature ablation 四组 SHA 完全一致。同一 target 的 activation 时间必须严格递增；完全相同事件可幂等重试，同一时刻的不同事件直接冲突，读取时还要重算 predecessor、attestation 和 activation 三层哈希，禁止用 activation-id 排序偶然决定 active scorer。注册不等于激活，candidate/OOS 文件也不能由 caller 直接注入热路径。P0 持久化、prepared cycle 持久化以及 host 重新加载都必须在各自 as-of 重验 active identity、证明 SHA 与 activation event 自身哈希；切换后旧 prepared cycle 立即失效，不能与新 scorer 混跑。该 registry 目前仍属于 isolated shadow authority，不授权 production。

`resident_p10_shadow_prepared_cycle` 是 prepared-cycle 的唯一冻结与登记边界。preparer 必须显式给出外部观察到的 `prepared_at` 和身份；模块不会补时钟、抓取、刷新或训练。它把 P8 概率输入与三类概率 prewarm identity 归一化为 content-addressed `prepared_cycle_id/SHA`；只有 bridge 已验证闭合时，才额外要求 market prewarm 与 decision context。故 X-only cycle 可在完全没有市场/decision context 时合法冻结。冻结阶段不仅验证 strong-artifact v3 结构、必需特征 evaluator 和概率 prewarm binding，还必须证明该 artifact 正是同 target 在 `prepared_at` 的 active registry identity；随后要求上一 checkpoint 已存在于同一 isolated DB，才追加写入 `resident_p10_prepared_cycles_shadow`。signer/CLOB 不能进入该 cycle；它们仅在 L5 执行边界另行验证。strict-as-of/OOS lineage 与 approval 是离线 artifact promotion 合同，不进入 prepared-cycle runtime gate。该表以拒绝 UPDATE/DELETE 的 trigger 保持 append-only；prewarm dependency SHA 只是运行血缘，绝不进入 Bayes 证据或修改 posterior。host/bundle runner/service 都只接收该已持久化 ID，不能再注入调用方内存字典或任意 scorer；记录或绑定被篡改即 fail closed。这仍是 shadow 配置与可回放证据，不是 production watcher 或下单授权。

`scripts/prepare_resident_p10_shadow_cycle.py` 是该边界唯一受控操作入口：默认只根据外部提供的 `prepared_at/prepared_by/cycle_input` 冻结并预览；`--apply` 必须带精确确认语，并且只能向已初始化、带 isolation sentinel 的 `p10_runtime.db` 追加。它不会生成 P0、伪造 X→S→B、训练/激活 LR、补 forecast 或调用 signer；任何缺失仍保留为明确 blocker。

`p10_shadow_preparation_readiness` 的语义固定为“**能否创建** prepared cycle”：prepared-cycle 表缺失才是 schema blocker；`prepared_cycle_count=0` 只报告 `prepared_cycle_missing` 后置状态，绝不能成为创建自身的循环 blocker。它从 strong-LR active registry 读取 scorer，不再接受外部 artifact/promotion 文件路径；active scorer 与 previous checkpoint 是 runtime blocker，而 final label、strict-as-of、OOS、ablation 只列入 `offline_promotion_blockers`，不能改变 `ok`。host-start 另行要求已持久化的 cycle ID。

P10 runtime 的唯一数据库是 `data/shadow/p10_runtime.db`。P2 的 `p2_canonical_amsc_shadow.db` 仅是证据镜像，禁止作为 P10 host/consumer 的 DB：同一物理报文若在 P2 已按 legacy path 写入，会把 P10 direct sidecar 的独立 `T1` 降为 relation 或覆盖为较晚 T1，破坏实际延迟证据。新 P10 DB 只能由 P10 schema 初始化，然后只接收 P10 自己的 P0、prepared-cycle、arrival journal 与 direct inbox canonical event。

`ResidentP10ShadowHost` 是唯一的 prepared-cycle host：它只按 ID 从上述 append-only registry 加载一份冻结 cycle，要求上一 `PosteriorCheckpoint` 已在同一 isolated DB 持久化，并要求 prewarmed forecast/prior/strong-LR identity 与 P8 输入逐项相等；market identity 只在 bridge 闭合并进入 P9 时校验。启动时它从同 target 的最新 P1 checkpoint 重建下一 update 所需的 joint posterior 与完整 `consumed_event_ids`，而不信任过期进程内缓存。收到一条 source observation 后只生成动态 correlation/fact identity；仅 market projection 可用时才需要 outbox identity。成功的 P8 会把完整 canonical 序列推进为下一 checkpoint，重启恢复不能遗漏先前报文。它不 bootstrap prior/weak、不抓报文、不刷新 forecast、不卡通用队列、不写 production archive、更不含 signer/CLOB；它现已作为唯一 monitor 的受管 shadow child 运行，但仍不是 production AMSC watcher 或真实下单链路。

`run_resident_p10_shadow_bundle` 是当前唯一已落地的受控组合点：它必须用已持久化的 `prepared_cycle_id` 构造 prepared host，再调用上述 adapter，因而只能把 individual candidate 而非 poll batch 送入 journal→P8→P9。它要求 isolated shadow DB 与 append-only prepared-cycle registry，且显式没有 scheduler/watcher、网络 acquisition、通用 queue、per-event subprocess、production archive 或 CLOB 权限。它适合产生第一批持久化 shadow latency packets；它本身不是 launchd service，不能被当作 production deployment 或 3 秒达标证据。

`ResidentP10ShadowService` 将上述 host 与 adapter 保持在同一常驻进程：它只用 `prepared_cycle_id` 启动，随后同一 target 的 raw-archived bundle 复用已冻结 host、durable journal 和最新 P1 checkpoint，而非每批重新 bootstrap。service 只串行接收已归档 bundle；并发 bundle 直接拒绝，避免 canonical event 越序。它仍无 acquisition loop、scheduler、queue、subprocess、production archive 或 CLOB；部署状态只能由 monitor generation/PID 与 resident cycle/PID/heartbeat 证明，不能由模块存在本身推断。

`amsc_p10_shadow_bundle_loader` 是 resident host 之前唯一允许从落盘输入重建 AMSC bundle 的边界。它强制从 archive root 读取并验签 manifest，再从 manifest 的 raw-object index（而非 fact plan 自带、可篡改的摘要）重建 receipt；每条 primary `METAR/SPECI` 还需重新核验 gzip raw object、raw SHA、raw text、source/station、object ref 与 immutable `first_seen_at`。`forward_new_primary_only` 只选取本次 manifest 中 `first_seen_at == response_completed_at` 的新 primary raw object；上一轮已经见过的报文只能以 `replay_all_archived_primary` 显式回放，绝不能因文件读取时刻变成新 arrival。loader 不产生 T1、没有网络、queue、production archive 或 CLOB。

`scripts/run_resident_p10_shadow_inbox.py` 是 loader 的**隔离 shadow inbox consumer**：它在启动时只按 append-only `prepared_cycle_id` 建立一个 `ResidentP10ShadowService`，随后以 10–1000ms 的文件轮询消费原子发布的 JSON 输入；`--from-now` 会忽略启动前 backlog，避免旧历史报文被误写为 forward 样本。默认只读 preflight；`--apply` 只能写带 isolation sentinel 的 shadow DB，强制拒绝 `archive.db`，并要求精确确认语。它不网络抓取、不触碰 generic task queue、不刷新 forecast、不含 signer/CLOB。2026-07-13 已由唯一 `system/com.weather.monitor.daemon` 受管部署，并为 `2026-07-14` 绑定真实 cycle `p10pc_e562678a1a5cfaa06671cf79`；consumer 仅在当地日期进入该 target 后启动且使用 `--from-now`。因此部署 readiness 已闭合，但首条当日前向事件尚未发生，仍不能声称 3 秒证据或 production activation。

`amsc_p10_shadow_source_sidecar` 是 inbox 前唯一的 direct AMSC source 尝试：它只使用 urllib observed-header transport（**没有** curl/subprocess fallback），在 `poll_started_at → response_started_at → response_completed_at` 实测完成后先写入独立的 `data/shadow/p10_amsc_raw` append-only raw archive，再用 loader 重验 manifest/raw/T1。它绝不与旧 `data/amsc_raw_history` 按 raw hash 共享对象：若旧链先归档同一报文，共用目录会把 P10 的独立观测错误地去重为“非新 arrival”。AMSC 当前遗漏 `GlobalSign RSA OV SSL CA 2018` 中间证书；sidecar 因此以 certifi roots 加载 `config/trust/amsc_globalsign_rsa_ov_ssl_ca_2018.pem`，仍保持 `CERT_REQUIRED`/hostname verification，绝不关闭 TLS 验证。它只在本次 fetch 对应的 exact half-hour `latest_primary_metar_at` 已出现、且 P10 manifest 首次归档了 primary raw object 时，才原子发布原 bundle 到 inbox；stale slot、P10 root 内重复 raw object、无新 primary、网络异常均不会发布。sidecar 不写 `archive.db`、不 kick queue、不计算 posterior/forecast、不含 signer/CLOB。它由唯一 `system/com.weather.monitor.daemon` 在每小时 `:00/:30` 槽开始附近以受限 child 启动；独立 GUI LaunchAgent 和安装器已退役，避免平行采集器争夺 T1。2026-07-12 的历史独立前向样本在 exact `07:30Z` 到达，`T1=07:30:41.822Z`，P10 manifest `1956eafe…` 新写一条 primary raw object 并原子发布 inbox；截至 `08:30Z`，连续三条独立 primary arrival（07:30/08:00/08:30）均由 acceptance audit 重验通过。2026-07-14 cycle 与 inbox consumer 已准备，仍须等待该 target 首条真实报文闭合 source→posterior；历史 source-only 样本不等于 full watcher、3 秒或订单证据。

`p10_amsc_source_sidecar_acceptance` 是 source service 的唯一前向验收读口：它对 inbox 中每个 JSON 重新执行 loader+adapter manifest/raw/T1 校验，只在至少一个 bundle 保留真实 primary arrival candidate 时标记 `forward_evidence_ready`。零文件时状态为 `waiting_first_forward_capture` 而非失败或成功样本；坏 bundle 使审计明确 blocked。它和 P10 preparation audit 都只消费声明 `raw_archive_dir=data/shadow/p10_amsc_raw` 的 source status；旧共享 archive 状态分别只以 `unbound_latest_sidecar_status` / `unbound_source_status` 留作诊断，绝不构成 P10 receipt/T1 或 forward evidence。它不产生 journal arrival、不写 shadow DB、不创建 P10 telemetry、更不触碰订单。2026-07-12 首个 bundle 经该读口复验为 `forward_evidence_ready`，保留一条 primary METAR candidate，`first_transport_seen_at=first_actionable_seen_at=07:30:41.822Z`；这证明受控 source child 已取得独立前向证据，但不改变其余 P10 runtime blockers。

`p10_shadow_runtime_schema` 是 P10 runtime-ready **空 schema** 的唯一初始化入口。它只接受 `isolated_shadow_only` manifest，补齐 P1、P0 append-only initial-lineage、P9 decision/intent、prepared-cycle registry、physical-arrival journal 和 telemetry 表；严格拒绝 `archive.db` 和 P2 evidence DB，并不创建任何 checkpoint、strong update、posterior、fact、decision、intent、order 或 prepared cycle row。2026-07-13 独立 `data/shadow/p10_runtime.db` 已完成 schema 迁移，并由独立 preparer 追加 `2026-07-14` 的 active neutral strong-LR、P0 和 prepared cycle；schema 初始化本身仍不生成任何运行数据。缺少 active artifact/真实 prior context 会使对应 target fail closed，但不得阻断当前已激活生产模型；bridge/final label 缺失只阻止 market realization、outbox 与订单，X-only P8 checkpoint 及其 public posterior SSOT 仍必须提交。

任何 L4/L5 执行预检都必须同时保存 `T1=first_actionable_seen_at`（legacy 仅可降级为明确标记的 transport-completed time）和 `T3=posterior commit`。T2 必须在 resident canonical append 返回后捕获；T3、T4 必须分别在 Transaction B、decision-intent 原子提交函数返回后捕获。输入中的 `canonicalized_at`、`completed_at`、`claimed_at` 只可服务业务事务/因果校验，绝不能充当 P10 时延事实或伪造 SLO。`T3` 只度量 posterior→decision 段，不能重置 `T1→T5`。缺失 T1、T1 晚于 T3，或检查时已经超过 T1 起算的 3 秒硬 deadline，均禁止下单。`observation_fast_lane_contract.HARD_EXECUTION_DEADLINE_MS` 与 `sequential_bayes_execution_contract.DEFAULT_HARD_DEADLINE_MS` 都固定为 3000；运行时配置只能收紧，不能恢复 5 秒宽限。

该硬边界必须穿透到真实 CLOB gateway，而不能只停留在 posterior/decision 审计层。Bayes 新 BUY 入场载荷必须携带 `observation_to_order_hard_deadline_at`（仅在合同明确提供时才允许用同一合同的 `signal_to_order_hard_deadline_at` 兼容）；服务先完成幂等 replay/unresolved 检查，再对全新 intent 做 admission，并在签名与 durable `signed_prepared` 之后、调用交易所 `postOrder` 之前最后复核。缺失、无效或过期均以 `exchange_post_started=false` fail closed，不得把本地接收/async ack 伪装成 T5。真正调用 POST 前捕获的 `exchange_post_started_at` 是唯一 `order_submitted_at/T5`；响应完成时间不替代 T5。此门槛仅约束 direct Bayes BUY 新入场，不误伤 SELL 退出、mirror/manual 执行或既有 intent 的幂等查询。

T5 进入 SSOT 后保持首次事实不可变：`signal_to_order_timing.order_submitted_at` 只允许从空值 merge-fill，冲突重放不得覆盖；`timing_json` 必须同时保存 `exchange_post_started=true`、完全相同的 `exchange_post_started_at` 和 `order_submission_time_quality=exchange_post_started_at`，缺任一项只能标记为 unverified，dry-run、async ack、decision write、exchange response 或 already-live 检查均不能补造。`v31_posterior_execution_latency_audit_v2` 只在三项闭合时发布 gateway-attested T5，并继续把 legacy task queue 路径标为 P10 不合格。2026-07-13 对旧 v3.1 纪元的历史只读审计显示：当时最新 fact 的 T3 attestation 存在，但 source→T3 为 **2,039,176ms**，且没有对应 decision/T1 execution contract/verified T5；它只保留为失败基线，不能覆盖 2026-07-15 reported-X v5 的前向 T1→T3=2471ms 新证据，也不能把后者扩张为 T5 下单已达标。

`sell-fill` transition 只能给已通过原始 T1 gate 的已授权后续动作单独计时，不能用 transition timestamp 复活迟到报文的 BUY。

过渡的 legacy materializer 也必须把 `T3` 做成可核验事实：`weather_fact_snapshot_writer` 的 fact transaction 返回后，`posterior_materialization_commit_attestations` 立即追加一行不可更新、不可删除的 `materialized_fact_id ↔ task_queue/source_fact ↔ committed_at` 绑定；它绝不回写不可变 posterior fact。审计只接受该账本（旧历史仍可明确显示为缺证明），不能用 task completed、事实 `created_at` 或 UI rematerialization 推断 T3。该账本只补 observability，不会把 generic queue/per-event subprocess 或缺失的 T4/T5 伪装成 P10 已达标。

对 queued materialization，`AMSC source fact as_of` 是整次投影的严格上界：同 slug 的 source history、`model_member_cache` 和 WU/TWC `event_runs` 都必须 `≤ cutoff`；重试时即使数据库已出现更晚报文、模型或预测，也不得借入。projection 的本地 `parsed_at` 只是 adapter 处理时间，不是物理或 transport 事实；同一 source event 再解析只能保留首个已落库值，不能制造 immutable drift 并阻断 posterior。这个边界既防止未来信息穿透，也避免把“旧任务现在重跑”伪装成当时可见输入。

日终物理标签也必须分层：只要 ZUUU 当地日的 AMSC 原始 lineage、44 个计划槽、raw integrity 与无未解决修正闭合，即生成 final physical `X`，不等待市场、S、bridge 或 strict-as-of replay。若存在晚到报文或其他 as-of 缺口，`X` 仍是可追溯的物理真值，但必须显式标记 `strict_asof_training_eligible=false`，仅隔离该日的 strict weak/strong/OOS 样本；绝不能因为这层训练资格而阻断 X 标签、AMSC 入库、当天后验或交易 live。

预测源的显著分歧不是旧 `bimodal` 二元风险开关：唯一联合弱似然必须保留温度峰与情景质量，强似然再用 D0 报文、温度动量、轨迹、云雨与露点对每个峰做物理可达性更新。旧 `q_bayes_bimodal_*` 仅可展示/复盘，已经不能阻断策略或缩放仓位；真实不确定性只由已批准联合弱 artifact 的 information confidence 与 posterior 表达。

`ResidentPosteriorCommitOutboxShadowWorker` 只消费仍指向 `event_fact_latest`、Transaction B 提交时具执行资格、且当前时间尚未越过 `posterior_outbox_execution_contracts.observation_to_order_hard_deadline_at` 的 committed posterior outbox，并由宿主注入同一时点冻结的市场、资本和策略 artifact 输入；它不会领取被 supersede、迟到提交或已过期的 posterior。直接调用动态三腿 decision cycle 也无法绕过该边界，因为 outbox claim 会再次重验同一执行合同后才做 lease CAS。shadow fast-lane 已经在 Transaction B 后通过该 consumer 进入动态三腿 composer，禁止 orchestrator 绕过 outbox 直接调用 P9。它不是通用 queue、网络客户端、signer 或 CLOB executor，且只接受隔离 shadow DB。因此该组件只证明 `T3→T4` 的专用消费合同，不能被误写为生产 outbox worker 已上线。

动态角色选择之后必须经过 `dynamic_three_leg_transition_runtime_v1`，不能把“新三腿 BUY 列表”直接当成执行计划。该层只消费当前 desired TradingPlan 与完整冻结的 position snapshot：离开角色的 managed lot 全卖、主 YES 降为次 YES 时修剪至 4.99 USDC、继续持有的角色只补剩余额度；存在任一 SELL 时所有 BUY 清空，fill/reconcile 后必须重新读取最新 posterior 和新 position snapshot。次 YES 新买/补仓另由 `second_yes_entry_window_state` 控制，窗口固定为 `RealizationState.peak_timing_state.entry_peak_at` 前最后 30 分钟；窗口外使用 retention candidate 保留同桶已有仓位，禁止把时窗关闭伪装成目标失效。次 YES intent 的 `quote_expires_at` 必须取盘口过期时间与预测峰值时间的较早者，resident submitter 在 POST 前再次验证冻结 window binding，并在 `now >= predicted_peak_at` 时原子记为 expired，禁止窗口内生成、窗口后排队提交。unresolved intent、lot/version 或成本基础缺失一律 fail closed；非 `dynamic_three_leg_weather` 管理域仓位不得被碰触。该层不读取天气、不重算概率、不使用 EV/Edge/LCB，也不提交订单。

Transaction C 必须把 retention candidate 落为 `auto_trade_decisions` 的 `hold_no_order` 行，但不得创建 `posterior_execution_intents`；该行是执行阻断原因的唯一权威来源。旧 `hard_source_direct_poller → target_realization_v3_runtime` 在退役完成前必须消费同一个 `peak_timing_state` 和同一个 30 分钟合同，禁止成为绕过入口。看板只展示后端返回的峰值时刻与原子决策时窗，不运行浏览器时钟判断。

`peak_timing_state` 在 Transaction B 的 bridge 分叉之前生成，并与 `remaining_warming_marginal + termination_risk` 一起写入 StrongObservationUpdate 的 `path_auxiliary_json`。bridge open 时它继续进入 RealizationState；bridge closed 时 physical-only public writer 直接从同一 strong update 发布它，且不创建 market projection/outbox。生产 chain audit 必须验证 event/update 绑定、概率归一、future slot 支持和 `does_not_update_physical_x=true`，防止 physical-only 分支再次漏字段。

当 Termination Risk 的未来质量大于零但已无未过期 forecast-supported slot 时，时刻分布以 `future_peak_time_unresolved` 保存该质量；它不提供时钟、不可开启次 YES，且不得反向归一到 observed peak。构建器、切换前全日预演、cutover acceptance 与 production chain audit 共用 `peak_timing_state_runtime.validate_peak_timing_state_contract`，任何质量丢失、伪造 future slot 或 event/update 漂移均在切换前 fail closed。

物理 X 与市场桶 B 必须跨层隔离：AMSC canonical 序列中的累计最高温只形成单调的 X 下界，任何市场桶硬排除必须由同一次 posterior 经已关闭 `X→S→B` bridge 生成的 exclusion certificate 授权。不得直接用 AMSC 温度与市场桶边界做数值比较。生产 candidate 的 RealizationState 必须标记 `candidate_mode=production / shadow_only=false`，checkpoint 状态固定经过 `production_ready → Transaction B → committed`；任何 `shadow_ready` 行不得进入生产 host。

生产候选的 Transaction C 现在由三段专用边界组成。`production_dynamic_three_leg_frozen_bundle` 是唯一的纯组合点：只用同一 outbox 绑定的 RealizationState 与已冻结 market/capital/risk/position snapshots 产生 desired plan 与 sell-first transition，完全不读 UI、旧策略、天气源或网络；risk reservation 只能在 Transaction C 内创建。`ProductionPosteriorCommitOutboxWorker` 随后只读取仍指向 `event_fact_latest`、在 T1+3 秒内且 Transaction B 已标记 execution-eligible 的 posterior outbox；宿主必须在**领取 lease 前**冻结并校验上述 bundle 输入。worker 拒绝继承外层 transaction，并以一个 `BEGIN IMMEDIATE` 包住 `claim CAS → risk/decision bundle → transition/decision/intent → outbox completed → commit`；任何 claim 冲突、blocked 或异常均 rollback，绝不留下 lease、reservation 或半套 intent。领取 CAS 会再次同时验证 latest pointer、deadline、posterior/fact identity，worker 不抓天气/盘口、不重算 posterior、不接通用队列、更不含 signer/CLOB。内层 `production_dynamic_three_leg_cycle_runner` 先创建真实 risk reservation，再调用 `production_dynamic_three_leg_decision_writer` 绑定 outbox lease/generation、latest posterior/fact、transition、position lot/version、market/capital/risk snapshot、capital epoch 与真实 risk reservation，写入 transition、decision 与 `posterior_execution_intents` 并完成 outbox；任何漂移整体回滚。risk reservation 已升格为独立 SSOT 表，BUY_NEW 必须原子绑定足额、未过期且未重复使用的 reservation，SELL_OLD/HOLD 则释放预留。`resident_dynamic_three_leg_intent_submitter` 只消费该专用 intent 表，不消费 generic task queue；它要求 connection 无外层事务，并在 CLOB 调用前 durable commit `prepared→submitting`。进程在 POST 后崩溃或 ACK 不明时，`reconcile_dynamic_three_leg_intent` 只查询原 idempotency/order identity，绝不重新提交；只有 idempotency/order、token、original/matched size 边界和冻结 order price 全部闭合时，累计成交才能进入 position-lot。resident 必须先完成所有 in-flight 对账；随后只可原子退役已经不再指向 latest posterior、仍为 `prepared` 且三项 exchange-side-effect identity 全空的旧动态 transition。退役事务同时更新 intents、decisions、transition 与 reservation；已有 BUY fill 时 reservation 保持 consumed，否则 BUY reservation 进入 superseded。这样旧 plan 不会永久占据 unresolved-intent 门槛，也不能通过新 posterior 隐藏交易所不确定状态。CLOB gateway 的 `/execution-guard-snapshot` 仅返回 raw order book 与 conditional/collateral balance/allowance，并显式无授权权力；`dynamic_three_leg_clob_gateway_adapter` 以 frozen intent 独立验证 token、≤1秒 freshness、限价内足额深度、position snapshot token balance 和余额/allowance，durable risk reservation 仍由本地 SSOT 独立验证。动态三腿由此固定为 `post_only=false` 的不可重定价 marketable GTC limit：盘口在原 ask/bid limit 内可成交才 POST，恶化即等待新快照，绝不追价。默认关闭的 `resident_dynamic_three_leg_execution_loop` 使用 autocommit dedicated connection、每轮一个 intent 且 reconcile-first；其 launchd unit 已安装但 persistent disabled、未加载，六个 live flags=0，且以项目 `.venv` 替代 macOS Python。第一阶段 execution schema 与第二阶段 position/continuation/market-alpha additive schema 均已完成受控迁移并通过只读 exact-schema 验收；真实 T1→T5 与 accepted live activation 仍未验收，因此 schema ready 不能打开任何 live 开关。

旧 `remote_no` 全链生成路径已经从 `chengdu_intraday_summary / posterior_settlement_runtime / weather_strategy_runtime / hard_source_direct_poller / scan_weather_market / ui.server` 删除；UI 只读 committed dynamic plan，不再从 posterior rows、环境变量或旧资本计划重算。旧 NO 距离环、0.98 价格门和 old-bucket opposite-side BUY 均不得作为 fallback。生产 NO 只允许 `strongly_excluded_no`，由 composer 的联合物理排除进入动态三腿计划，额度固定为 100 USDC。

动态三腿执行生命周期现由 durable intent terminal state 原子收口：全部 fills 才完成 transition 并消费 BUY reservation；明确失败/过期会阻断 transition、取消同批未提交 siblings，并按“是否已有 BUY fill”决定 consumed/released。open、partial、ACK unknown 与 not-found 均保持占用，不能释放资本或创建替代单。SELL_OLD 的 risk reservation 已在 Transaction C 释放，submitter 只复核 transition binding，不再用过期的 BUY 预算阻断降低风险的卖出；reconcile 同时校验 order identity 与 token identity。

动态持仓不再借用 `edge_trades` 推断。`dynamic_three_leg_position_lots + dynamic_three_leg_position_lot_events` 是策略持仓批次 SSOT：BUY intent 在决策事务中预分配 lot identity，partial/full fill 与 intent 状态在同一事务中推进单调版本；SELL 必须绑定 exact lot/version，提交前再次核验 remaining size。当前角色来自 latest posterior，lot 的 `entry_strategy_leg` 只保存来源且不能改名。原主 YES 降为同桶次 YES时可保留 4.99 USDC 并卖出超额，market-alpha 因不再是 current top 自动休眠；原次 YES 升为主 YES时必须先退出并以 top 腿重建，禁止把 second-entry lot 提升为波段权限。多个可计入同一当前目标的 lot 聚合成本与数量，但次 YES 只在其 30 分钟窗口内允许补仓；每个 SELL 仍保持独立 lot identity、确定 execution sequence 与逐笔递减的 expected token balance。

SELL_OLD 全部 filled 时，submitter 在同一收口事务内追加 `dynamic_three_leg_continuation_events`。`production_dynamic_three_leg_continuation_worker` 随即重新读取 latest posterior、position-lot SSOT，并重新冻结 CLOB/capital/risk snapshots：posterior 未变则生成 generation≥2 的 BUY_NEW/HOLD/必要后续 SELL，posterior 已变则旧 continuation 原子 supersede。该链路解决“卖完后等待下一报文才买新”的错误停顿；continuation 不是新的天气证据，不得重算 posterior。

`production_dynamic_three_leg_input_provider` 是生产冻结输入的唯一 I/O 组合边界。它以 `/dynamic-three-leg-snapshot` 一次本地请求并行取得相关桶 order books、managed token balance/allowance 与 collateral，校验 ≤1 秒 freshness 后生成 market/capital/risk/position snapshots，再交给纯 frozen bundle。交易层不读 UI、不使用 legacy `edge_trades`、不读取 EV/Edge/LCB。`/execution-guard-snapshot` 仍在每次 POST 前对 exact intent 做第二次独立复核。

`observation_fast_lane_shadow_telemetry_writer` 在 P8/P9 返回后追加一条 immutable P10 packet，记录 resident 所捕获的 T1/T2 与 post-commit 捕获的 T3/T4。它不属于 posterior 或 intent 的原子事务：遥测写失败不能回滚已提交事实，但该次尝试也不得计入任何 P10 release 样本。`observation_fast_lane_shadow_db_slo_audit.py` 只读带 `isolated_shadow_only` 哨兵的该表；它拒绝 `archive.db`，并按 report/date 聚合 P50/P95/P99。因此手工 JSON、caller 的逻辑时间或无 telemetry record 的成功都不能构成 forward SLO 证据。

Transaction B 的原子性证据必须绑定当前实现、测试代码和报告生成器本身，而不是保留一句“已有测试”。`scripts/materialize_probability_transaction_fault_injection_report.py` 只运行 isolated/in-memory 测试，覆盖 public SSOT history 故障、physical-only history 故障、前驱 checkpoint CAS、前驱 posterior hash CAS 和 outbox 冲突整笔回滚；报告按内容寻址写入 research root，任何绑定文件变化都会使验收失效。2026-07-13 当前报告 `c7c5baea0c7635c95f527d2acf8ee0a752709ba047680eaa058e11b73a9fc73e` 的 5 个场景均通过。该证据只关闭原子事务门，不证明 forward event、replay 等价、bridge 或 3 秒延迟。

增量与全量 replay 等价也必须是 target 级真实证据。`scripts/materialize_probability_replay_equivalence_report.py` 只读取某 target 最新 durable `checkpoint_kind=incremental` 已消费的真实 canonical event、冻结 P0/cycle 和同一 approved strong artifact，从 P0 完整重放，并逐项比较 joint posterior SHA、physical-X distribution、latent state、observed-max、consumed sequence 及 forecast/prior/resolution/artifact identity。事件一对一计数也只统计 incremental checkpoint，`replay/final` 不能混入并伪造多写。报告绑定当前代码 SHA 和最新 checkpoint；新事件追加或代码变化会自动使旧报告 stale。目标尚无真实 incremental event 时禁止生成通过报告，synthetic test 永远不能关闭该门。

AMSC pending probability 文件只承担 durable retry，不是平行概率主线。每次 fresh T3 之后、重放之前，恢复器先按 active activation SHA 找到唯一 generation 与最新 committed checkpoint；仅当 pending plan 的 `event_date + valid_time + source_observation_id` 精确绑定的 canonical event 已在 checkpoint consumed sequence 中，才把该 plan 及其 receipts 原子移动到内容寻址的 `consumed/` 目录，并写不可变 receipt。canonical 尚未消费、身份不完整或 activation/generation 不唯一时一律保留并 fail closed；禁止仅凭时间或 raw 文本相似度清队列。该动作只读 `archive.db`，不产生 posterior、事实、决策或订单，因此既避免新 epoch 全量 replay 后重复恢复，也不会吞掉尚未处理的强观测。

`posterior_target_direct_latency_drill.py` 只测量 temporary DB 中 synthetic fact 的 local `signal→worker→order` 路径；其固定 `measurement_scope=synthetic_fact_signal_to_order_local_path_only`，`valid_p10_t1_to_t5_evidence=false`。默认仅 audit，不复制数据库也不写 temporary 数据；必须显式传入 `--execute-temporary-drill` 才会运行隔离的 fake-order drill。它可以发现本地执行瓶颈，但不接收真实 AMSC T1、不会产生 P10 telemetry，绝不能用于声称观测 T1→T5 在三秒内达标。

## 交易退出原因与 runtime 边界

生产 runtime 永久不使用 Edge、LCB、exec_edge、signal_edge、旧 repricing、stop-loss 或 trail-stop 作为入场、仓位、退出和下单硬门。

目标架构只允许两个互斥退出原因族：

```text
target_realization_*
  = posterior/hard-condition/settlement target change

market_alpha_take_profit_*
  = current-posterior top_yes_wave reaches approved 0.90 executable price
```

market-alpha 必须绑定当前 `posterior_run_id/realization_state_id`、`strategy_artifact_sha256`、position lot/version、fresh executable sell quote 与 deterministic logical intent，并通过 durable intent、partial-fill、ACK-loss 和 reconcile 合同。posterior 换桶后旧桶立即失去 market-alpha 资格并走 target-realization sell-first；旧 `wave_cycle_id/frozen_target/market_alpha_cycles` 不得复活。它不得修改 posterior，也不得被归因为天气目标失效。

market-alpha 的价格语义固定为：fresh bids 在 `≥0.90` 对整个 requested lot 具有足额深度，才可创建 intent；真实 SELL limit 永远是 lot 入场绑定的 `0.90`，观察到的更高 marginal bid 仅为可执行证据。`market_alpha_take_profit_cycles` 是当前独立幂等账本，不是旧泛化 `market_alpha_cycles`，也不冻结 bucket。相同 posterior 完成 wave 后只抑制该 posterior 的 top-wave 重入；新 posterior 到来后重新按最新 top 动态选择。resident 每轮必须先处理所有 `submitting/reconcile_required/gateway_accepted/partially_filled`，绝不能撤销可能已触达交易所的 intent；只有同时满足 `state=prepared`、无 `exchange_post_started_at/expected_order_id/exchange_order_id` 且其 posterior/fact 已不再是 latest SSOT 的旧 market-alpha，才可在一个事务内把 intent、decision 与 cycle 原子标记为 `cancelled_superseded/blocked`。该清理发生在新 posterior 决策之前，防止旧 alpha 既因 SSOT 过期不能提交、又作为 unresolved intent 阻塞 target-realization 的永久互锁。

执行器必须对 `exit_reason_family + exit_reason_code` 做闭集校验：`target_realization` 的 code 集由 L1 Target Realization 合同唯一拥有；`market_alpha_take_profit` 当前仅允许 `market_alpha_take_profit_top_yes_wave_completed`。未知 code、伪装前缀、缺失 family 或双 family 一律拒绝。同一评估同时满足两族时，天气 target invalidation 优先，market-alpha 记录为 superseded，禁止双 SELL。

该原因族 runtime、独立 activation、position-lot 原子更新、quote trigger、reconcile 与同 posterior continuation 已实现，两阶段 production additive schema 均为 ready；但 repo launchd、两个 activation JSON 与六个环境开关全部默认关闭，resident persistent disabled 且未加载。因此状态是 `implemented-default-off-schema-ready`，仍不能表述为 deployed/live。

## 生产晋级边界

目标架构必须依次通过：

```text
current runtime charter conformance
  = EV/Edge gate-zero + exact exit reason allowlist
-> X/S/B bridge
-> canonical evidence identity
-> event-level as-of replay
-> prior/forecast weak OOS artifacts
-> strong-observation incremental LR artifact
-> sequential Bayes shadow
-> atomic transaction fault injection
-> dynamic three-leg TradingPlan shadow
-> 3-second fast lane
-> accepted cutover ADR
```

晋级以前 current joint composer 仍是唯一 production，主模型不可用继续 fail closed，无 fallback。

其中 prior/weak 与 strong-observation 只能在 canonical identity 和 as-of replay 闭合后并行；posterior SSOT 必须等待两侧 artifact 均获批准。3 秒链路可以提前埋点，但不得早于原子 posterior/decision/intention 语义启用真实订单 fast lane。
> 生产 reported-X 切换与回滚步骤见
> `docs/runbooks/REPORTED_X_PROBABILITY_CUTOVER.md`。最高温 X 固定为常规
> AMSC METAR 报文整数日最大值；SPECI 是实时强证据，但不直接改写 X 硬下界。
