---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-12
---

# 强观测 LR 训练与拟合合同

## 目的与当前状态

强观测的目标是学习 D0 canonical AMSC `METAR/SPECI`、温度轨迹/动量和云雨露点等物理证据对最终物理最高温 `X` 的**增量 log likelihood**，再由独立 `X→S→B` bridge 映射到市场结算桶；它不是另一份 posterior 或交易信号。

当前代码已有 runtime feature owner、严格训练样本/特征/数据集、shadow trainer、OOS 与 artifact promotion gate。`strong_observation_lr_fit_runtime.py`（CLI：`scripts/fit_strong_observation_lr_shadow.py`）只从显式冻结 JSON 输入生成 `shadow_only` candidate；当前仍没有真实 A-class final labels 和 latent replay labels，故不得配置或激活强 LR artifact，也不得把任意手工物理规则、旧 v3.1 权重或 synthetic 结果称为已训练强似然。

## 实时观测、终止风险与日终 X 的不可变边界

`AMSC METAR` 与 `AMSC SPECI` 均为同等级的实时物理强观测。两者都可以立即更新温度动量、温度轨迹、云雨/露点/风场证据、剩余升温或降温分布，以及唯一 posterior。SPECI 的作用是加速捕捉突发对流、降水、风向/压强跳变、低云恶化或快速降温等**终止风险**，不是把某个预测源晋升为事实，也不是独立的第二条 likelihood。

`termination_risk.probability_no_material_new_daily_high` 的严格定义是：从同一次强似然生成的剩余升温/降温状态混合分布中，边际计算“在本地日结束前不再形成至少 `0.5°C` 新日高”的概率。它只能读取该混合分布及其物理证据，禁止再以乘子或阈值对 posterior 二次更新；这样不会对同一报文重复计数。

CB 必须按**首次出现时刻**建模，禁止退化成 `CB=true/false` 终止开关。唯一因果口径为：pre-event canonical 序列保存首个显式 METAR cloud group `FEW/SCT/BKN/OVCnnnCB` 的 event id、valid time、当时温度和截至当时的观测峰值；当前事件若首次报告 CB，则生成 `first_cb_this_event`，以后只保留 `cb_already_seen` lineage。TCU 是独立对流发展证据，不能静默晋升为 CB。峰值窗口前首次 CB 对尚未实现的剩余升温施加更强约束；峰值窗口内为中等约束；16:00 后且已达观测峰值的首次 CB 只能轻微约束更高候选，对已实现峰值的 CB 专属 log-LR 必须为 0。它不能回看最终 X 判定“峰前/峰后”，不能逆转 observed-max hard condition，也不能作为第二个 likelihood 再乘一次。

日终物理最高温 `X` 的标签口径保持不变：只由 AMSC ZUUU 固定 44 槽**常规 METAR**温度的日最大值形成。SPECI 必须原始归档、保留 `report_type`、valid time、raw hash 与 first-seen，并作为实时 source fact 进入唯一 posterior materializer；但它不能填补、替换或创建任一常规 METAR 槽位，也不能单独重写 final `X`。SPECI source fact 成功而 frozen weak context 不可用时，source fact 仍须保留，posterior 正确 fail closed 并可审计。

实时入口必须以 AMSC response-complete 为 T1、唯一 posterior materializer 返回为 T3，写出 `amsc_speci_response_to_posterior_slo_v1`；目标为 `T3-T1 <= 3000ms`。只有 posterior 确实成功且时限满足才可标记 `met=true`；source fact 已保存但 posterior fail closed 必须标记 `posterior_blocked`，不能把一次快速失败冒充 3 秒成功。

运行状态必须通过 `scripts/weather_rebuild_readiness_audit.py` 的两条独立证据线解释：`physical_x_training_labels` 只计 AMSC physical final X 及其 `strict_asof_training_eligible`，不等待市场 finality/S/bridge；`resolution_final_labels` 只计 settlement/P10/交易晋级标签。当前如出现 “physical final X 已有但 strict=0”（例如完整站点日中存在晚到报文），它能保留物理真值，却不能进入 strict strong fit；这不是 AMSC 实况、强观测更新或现有 live 基线的阻断。

## 两个不可混淆的训练域

`strong_observation_training_dataset_v1` 的 manifest 必须显式冻结 `training_scope`。同一 artifact、feature、latent replay 与 manifest 只能属于一个域，禁止混合。

### `physical_x`：每日物理校正域

它只学习“当前实况及物理证据之后，最高温 `X` 还可能升/降多少、会落在哪个物理温度格”。它接受 A/B 级且 `physical_auxiliary_eligible=true` 的 canonical event as-of 样本，必须绑定：

- 同一 runtime owner 产生的七个 feature；
- 最终 `final_physical_high_c`；
- event-day expanding-year 影子评估 fold；
- 一个冻结的物理路径 latent replay label：`Z_t^-` 和 `Z_t^+`。

`S`、market bucket、resolution contract、market finality 与 `X/S bridge` 都不是该域的输入或日常样本门槛。它可以在 AMSC station day 关闭并生成 final `X` 后每日追加候选/影子样本；不得每日直接改写已批准 runtime 参数。

P2 canonical stream 的唯一身份是 `physical_weather_day_v1:ZUUU:<event_date>`，已明确不是市场 slug、target contract 或 resolution contract；因此 canonical event、`O_<t` trajectory、温度动量和云雨露点证据均可先于市场独立存在。`prior_context_id` 仍必须绑定当时真实、可复放的 P0 prior context（历史气候/四川盆地/站点条件及其 as-of 输入），不得为了把 B 级样本送进 trainer 而临时伪造 ID、uniform prior 或用 final X/S 回填 prior。缺 prior context 时该 cutoff 只记录 `quarantined`，不影响 final X、AMSC 入库或已批准基线。

物理 P0 的唯一候选构造器为 `physical_temperature_prior_context_runtime.py`。AMSC physical final X 仍是日常学习和未来经验 prior 替换的唯一标签权威；market settlement `S`、bucket、当前报文、预测或价格不得进入 prior。样本/OOS 不得成为 live 阻断，因此在 AMSC 历史尚不足时，运行时可使用单独获批、明确披露局限的 NOAA/NCEI ZUUU hourly daily-max **climate-reference baseline**：它只提供气候背景 P0，不得改名为 AMSC final X、不得训练 strong LR、不得充当 settlement label，并必须绑定原始 manifest/SHA/as-of。达到合格 AMSC 覆盖并晋级后，由经验 AMSC prior 整体替换，runtime 不并行 fallback。

原 `physical_x_prior_layer_runtime` 的 uniform reference measure 与 absolute-strong-map X-shadow 自动链已经退役；它不能生成 P0、prior context、checkpoint 或任何 SSOT。样本不足仍只限制新 prior/strong artifact 晋级，不能阻断当前已激活生产模型；但目标序贯主线的 P0 必须绑定真实、可回放且已批准的 prior context，绝不以 uniform reference 冒充。

latent-state P0 的经验候选构造器为 `physical_latent_state_prior_runtime.py`。它只统计 `physical_x` 域中**首条** canonical event 的事后 replay `z_pre`，先在版本化、显式且互不重叠的物理温度 bin 内形成经验 `P(Z₀|bin(X))`，再无插值地映射到该 bin 覆盖的连续温度网格；`no_prior_observation` 只是 trajectory feature 的首条缺失标志，不是 latent state。样本不足时，候选 trainer 仍必须 `insufficient_history`，不得伪造经验能力；但 live P0 可绑定单独批准的 neutral-Z operational baseline。该 baseline 在每个 X 上对 Z 均匀、严格零信息，数学上不改变 P0(X)，明确不声称训练/OOS能力，只能由获批经验 artifact 整体替换。闭合 bridge 仅决定能否投影 P0-B；bridge 未闭合时 market bucket 为 NA，不得阻断物理 P0 或后续观测更新。

`station_day_strong_physical_training_runtime.py` 是 strong `physical_x` 样本的唯一组装器：它把 P2 physical-day canonical event 与严格的 `O_<t` predecessor trajectory、full physical P0、T1 前可见的 forecast evidence、source-reliability pre-state及 final X 合并为 B-class physical sample。final X 只存在于 label，且 `strict_asof_training_eligible=true` 是 physical-X dataset 的硬前提；晚到/非 strict final X 可以积累物理真值，但不得进入 strong fit。`S`/bucket/finality/bridge 不参与该样本；首条报文的 trajectory 必须是 owner 生成的 `no_prior_observation`，不能改写成 latent state 或人为动量。组装器、feature owner、dataset、latent replay和 fit 全部只写 shadow/research，不能刷新 posterior、下单或每日替换基线。

`scripts/materialize_station_day_strong_physical_candidates.py` 是日终前置候选账本：它只把 P2 canonical event 与 physical final-X 逐日绑定，在 `data/research/strong_observation/physical_candidates/` append immutable candidate。非 strict X、缺 P2、缺 full P0、T1 forecast 或 ordered latent replay 必须写入明确 `block_reasons`/`required_future_bindings`，不能制造空训练 sample。唯一 monitor daemon 的 resolution low-priority child 在物理 X capture 后调用它；该账本不读市场 finality/S/bridge，不写 archive/posterior/order，也不阻断 AMSC live。

### `settlement`：市场结算评估与晋级域

它只接受 `ready_for_oos` 的 A-class settlement-evaluable 数据集。每一训练行还必须同时绑定：

- A-class、settlement-evaluation eligible 的 canonical event as-of 样本；
- 同一 runtime owner 产生的七个 feature；
- final physical/settlement label（仅作 supervised target）；
- event-day expanding OOS fold；
- 一个冻结的物理路径 latent replay label：`Z_t^-` 和 `Z_t^+`。

该域才可计算 market bucket NLL/Brier，才可为 artifact promotion 提供 `X→S→B` 结算级证据。bridge 的样本阈值只阻断这里的结算校验与真实下单晋级，不阻断 `physical_x` 的样本追加，也不阻断已批准基线对 AMSC 实况的实时处理。

`strong_observation_latent_replay_runtime.py` 是 sample→trainer 的唯一 label bridge。它只接受与 `training_sample_id`、target、canonical event、canonical sequence SHA 和 final-label SHA 完全一致的有序 replay row；输出的 `latent_replay_sha256` 才可传给 shadow trainer。它不生成状态、不从历史页面猜状态、不允许 market/order/source-reliability-after 字段。

`Z_t` 的允许状态空间是 `warming`、`slowing_warming`、`temporary_platform`、`temporary_cooling`、`cooling_then_rebound`、`weather_suppressed`、`terminal_cooling`、`final_locked`。任何 latent label 必须从同一 canonical event 序列的事后 replay 产生，并保留 replay artifact/hash；不能由市场价格、人工交易结果或未来报文直接写入。

## 禁止混淆的两种状态

`post_state_transition` **仅**表示物理路径状态 `Z_t^-→Z_t^+`。它不是 forecast source reliability 的更新。

`source_reliability.selected_source_state` 仅以 pre-event 状态进入 feature。强 artifact 不得接受、训练或输出调用方提供的 `source_reliability_after`；该状态的任何更新只能由 sequential composer 的受控状态机在同一 posterior transition 中决定。这样同一报文不会先改变自身评分所需的 pre-state。

## trainer 的唯一输出

每个 fit 必须输出一个带 `training_scope` 的 `strong_observation_incremental_lr_artifact_v3` shadow candidate，包含：

- temperature × `Z_t^-` 的 base raw-log-score surface；
- 固定 feature vocabulary（含 First-CB 时序）的完整 missing/other/bin handling 和 score adjustments；
- 由 replay labels 估计且逐行归一化的 `Z_t^-→Z_t^+` transition matrix；
- 训练 manifest、feature manifest、OOS、ablation 和 artifact 的重算 SHA；
- `approval_state=shadow_only`，直到 promotion gate 全部通过。

score 可以使用平滑/正则化，但不得使用 market、价格、订单、PnL、Edge/LCB、posterior delta、hard mask 或交易行为作为输入。normalizer、hard condition、posterior delta 和 SSOT 写入永远由 composer 拥有。

## 晋级与运行时边界

`physical_x` candidate 只能用于 isolated shadow replay 和物理最高温诊断；现有 market OOS / ablation / promotion gate 明确拒绝它作为真实交易晋级证据。只有 `settlement` 域补齐结算级 OOS、ablation 和 bridge 验证后，才可能晋级。

strict-as-of、OOS 和 ablation 只决定新 artifact 是否能晋级；它们不应成为已激活链路的每事件交易阻断。未晋级 candidate 只能用于 isolated shadow replay，不能写 production posterior 或订单。

晋级结论不能以文件路径或 `promotion_ref` 字符串代替。registry 激活时必须冻结 `strong_lr_runtime_activation_attestation_v1` 本体和 SHA：`promoted_candidate` 内嵌的 gate 必须具有可重算的 `promotion_gate_sha256`，并与注册 artifact 的 candidate/training/OOS/feature-ablation hashes 一致；`approved_runtime_baseline` 则必须明确披露其预校准基线身份、批准状态、非训练/OOS晋级属性与风险范围。任何身份、时间或 SHA 漂移均 fail closed，但这些检查只发生于激活和 active-artifact 加载，不把离线样本数变成每条报文的 live gate。

在没有合格训练数据时，不得伪称强观测能力已经训练；但样本/OOS也不得阻断 live。旧 `operational_physical_innovation_lr_v1` 只沿 `X` 轴评分、八个 `Z` 状态恒等传播，已列入概率纪元退役项，不能作为 v2 fallback。

唯一生产继任基线为 `strong_observation_lr_operational_state_baseline_v3`，`scoring_kind=operational_physical_state_aware_lr_v3_first_cb_once`。它对每个 `temperature × Z_t^-` cell 生成截至当前 canonical 链的累计强证据评分；composer 用前后两份评分之差形成唯一条件创新 log-LR。首次 CB 直接惩罚只在首次事件消费一次；夜间预热阶段对最终 X 的直接似然中性，08:00–09:00 平滑启用，常规 METAR 硬下界不受该相位控制。事件条件化 `P(Z_t^+ | O_t,X,Z_t^-)` 只重新分配同一 X 内状态质量。hard mask、累计证据替换、中心化、normalizer、posterior delta、termination-risk 边际和 SSOT 均只由 composer 拥有。该基线是获批预校准物理基线，不声称训练/OOS能力；后续 artifact 只能整体替换。

每条 v5 strong update 必须在 `path_auxiliary_json.strong_likelihood_update_identity` 持久化当前累计 raw-score SHA、前序累计 raw-score SHA、条件创新 SHA 和 predecessor checkpoint。验收器必须从相邻 update 的 raw score 重新计算 `ΔlogL`，并逐格证明 `posterior_delta=ΔlogL+normalizer`（hard-mask 格严格为 null）；public SSOT 只透出这些紧凑身份，不复制 6MB score surface。任一 SHA、predecessor 或逐格等式不闭合即 fail closed。

该 v3 内容寻址基线已纳入 First-CB 因果时序：`timing.first_cb_local_hour`、首次事件状态、日内相位和相对当时已观测峰值关系属于同一强观测 feature set；首时刻/温度作为 audit lineage 保存，不另建概率源。每个 raw row 必须披露 CB 对该 X 候选的剩余升温 penalty；当 `candidate.remaining_warming_c=0` 时该 penalty 恒为 0。

生产切换必须同时冻结新 artifact SHA、runtime manifest、successor P0、`replay_generation+1` 与全部 canonical replay；禁止热换 artifact、复用旧 P0 或在同一 generation 混用前后纪元。`scripts/cutover_cumulative_strong_likelihood_v5.sh` 是该 successor epoch 的唯一受管入口；它在同一停服窗口完成精确备份、源码切换、P0 重建、全量 replay、逐格增量等式、零订单与 readiness 验收，不得顺带改变任何交易 activation。失败必须恢复数据库、源码和 activation 的精确切换前状态；成功后旧 v4 公式不得恢复为 fallback。
