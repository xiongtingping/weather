---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-12
supersedes:
  - Open-Meteo production multimodel transport
  - source_inline_refresh_v1
  - Open-Meteo complete-retirement wording (superseded 2026-08-02)
---

# 官方数值模式弱似然切换合同

## 定义与边界

弱似然中的数值模式预测源为已登记的全球、集合和区域模型家族。官方 native receipt 始终优先；它们都是**预测**，不是事实；AMSC/AWC METAR/SPECI 仍是强观测，不能被任一模型覆盖。TAF、WU、TWC 也都是独立预测源，不能被并入或冒充为数值模式。

生产 transport 的唯一允许路径如下：

| Provider | 允许路径 | 禁止路径 |
|---|---|---|
| ECMWF | ECMWF Open Data 或有权限的 ECMWF 交付 | Open-Meteo、Windy 转发、网页抓取 |
| ICON | DWD Open Data | Open-Meteo、Windy 转发、网页抓取 |
| GFS | NOAA/NCEP NOMADS | Open-Meteo、Windy 转发、网页抓取 |
| JMA | JMA 官方交付，或已批准、可审计的授权交付 | 未登记的第三方转发、网页抓取 |
| Open-Meteo（仅 fallback） | 同一站点×事件日的官方 native 对象缺失、失效或未及时到达时，使用带 `aggregator_republisher` lineage 的历史 strict-as-of artifact 进入 no-live P0/P0-M candidate | 伪造 native arrival、进入 P1、native arrival index、formal/live authority，或无历史 OOS activation 时启用 |

每个 provider collector 必须独立保留原始响应、请求 URL/host、request/header/body 时间、run、valid time、raw SHA、decoder ID/version 和规范化 feature。官方 native snapshot 只有在原始 URL host 命中 `config/forecast_sources/official_nwp_zuuu_v1.json` 的 allowlist、明确 `open_meteo=false` 且 `aggregator_or_republisher=false` 时才能进入 native arrival/正式 native lane。Open-Meteo fallback 必须走独立 republisher ingress，保留 native 尝试及 `native_unavailable_reason`，且没有历史 strict-as-of OOS activation 时保持 candidate/fail-closed。launchd 所用 Python 若没有系统 CA bundle，direct HTTPS collector 必须显式使用 certifi trust store，并保持 `CERT_REQUIRED` 与 hostname verification；证书错误只能使该 provider 进入缺失掩码，绝不关闭 TLS、绕过验证或把聚合商伪装成 native。

## 联合 generation 宪章

四源 collector 只写自己的 immutable `current.json`；预热器无网络权限，只读取、校验并原子组装一份 `official_nwp_multimodel_live_bundle_v1`。generation 冻结 SHA、`available_provider_ids` 和 `missing_provider_ids`：它可以是 ECMWF+ICON+GFS，也可以为空数值模式集合，让 TAF/WU/TWC 或 prior+strong 的组合被唯一主模型显式处理。缺源、过期、future、未解码、host 不符或 SHA 不符的 provider 只能进入缺失掩码，绝不借用旧 generation、替代模型或聚合商。

AMSC source fact 先独立提交；之后只能将其时点之前已存在的 official-NWP availability generation reference 写入 Transaction A.1 posterior task。热路径没有数值模式网络请求，因此弱源故障绝不能延迟或回滚 AMSC 事实。主模型若已批准该可用源组合，缺源只降低其置信度与仓位能力；只有组合未被 artifact 支持、输入血缘无效或主模型不可用时才关闭 posterior，事实归档永远不受影响。

## Open-Meteo 受限 fallback 与 Windy 规则

Open-Meteo 不得作为 native provider 或实时 P1 authority；但在 native 对象不可用时，允许作为带 `aggregator_republisher` lineage 的 no-live P0/P0-M fallback。该 fallback 必须满足：

1. 同站点×事件日先记录 native 尝试及明确不可用原因；不得跳过 native 优先顺序。
2. 使用已保存的历史闭日 strict-as-of raw/receipt/hash 与 AWC closed truth；没有可验证历史 OOS contract 时仅保留 `candidate_only`，不能激活。
3. 未来/forward 数据统一标记 `observational_candidate_not_oos`，不计入 OOS、20 train/8 holdout、TAF 24 样本门，也不阻断 `production_no_live`。
4. republisher lineage 不得进入 native arrival index、P1、formal/live authority 或订单；P1 仍只由官方 AWC METAR/SPECI 形成。

因此，fallback 是受限的 no-live 证据补位，不是把 Open-Meteo 重新命名为官方模型或把研究数据直接写入生产 artifact。

Windy 的 MultiModel 页面不能抓取。将来如取得可用于自动化交易的正式 Point Forecast 授权，Windy 只能被登记为一个独立供应商预测源，绝不是 ECMWF/ICON/GFS/JMA 的替身；因相关性高，权重只能由新的联合弱似然 artifact 学出，不能重复计权。

## D1/D0/季节准确率观测面

`scripts/build_forecast_source_accuracy_report.py` 是唯一的 D1/D0×气象季节准确率汇总器。它只读取 immutable strict-as-of `score_dataset`，以最终结算桶区间为标签，输出 `interval_mae_c`、`mean_bias_c=预测−最终结算`、RMSE、NLL、RPS、Top-1/Top-3 诊断值、快照数与**独立 event-day 数**。D1 固定为目标日开始前冻结的 `lead_hours≥23.5` 版本；D0 是目标日内、结算前的 `lead_hours<23.5` 修订。季节只由目标 event local date 按气象季推导。

同一 event day 的多条 D0 修订是不同决策时点，**不是**多天独立样本；报告必须同时显示 `sample_count` 与 `event_day_count`，且少于 30 个独立日时固定 `ranking_state=insufficient_independent_event_days`，不得用来排序、调权、训练、晋级或交易。每个源缺样本时保留明确 NA，不得以其他源、聚合商、事后分析或市场数据补齐。

日报只写 `data/research/forecast_source_skill/daily/accuracy_by_horizon_season.json`，保留 `source_data_policy`、lineage 计数及 report SHA。它经既有 `scripts/feedback_loop.py` append-only 物化为 `archive.db.feedback_snapshots(kind=forecast_source_accuracy_by_horizon_season, days=0)`；`/api/ui_snapshot` 从该只读事实快照展示，前端不得重算。该面板的 `research_only=true`、`production_weight_input=false` 是硬边界：它用于发现季节性偏差和数据覆盖，不改变当前 posterior、仓位、决策或订单。旧 Open-Meteo 转发历史若仍在 research score dataset 中，必须在 source lineage/policy 下可见，不能被解释为官方 GRIB 的生产技能。

正式替代 lane 为 `scripts/build_official_forecast_source_accuracy_report.py`：它**只**读取 station-day candidate 内已重验的 P1 Tier-A forecast revision 与 closed AMSC `physical_x_final`，不读取 legacy score、Open-Meteo、市场结算 S/B、市场价格或订单。它以 `forecast_peak_c−physical_x_final_c` 输出物理 X 的 MAE/bias/RMSE，并独立保存到 `data/research/joint_forecast_weak_likelihood/official_accuracy_by_horizon_season.json`。station-day materializer 固定同时请求 local 00:00（D1 frozen baseline）及 09/11/13/15/17（D0）cutoff；monitor 的同一低优先级 research child 在 materialize 后刷新该报告。UI 优先显示正式 lane；未有样本时必须明示“正式样本积累中”，只把 legacy lane 作为对照，绝不能静默混合或替代。

## 动态偏差校正合同（正式样本的 shadow 校正）

### 目标、定义与退役项

本合同校正的是单一预测源对物理日最高温 `X` 的**已闭合历史系统误差**，不是当天实况推断、不是另一份 posterior、不是仓位规则，也不是交易信号。对源 `s`、上下文 `c=(season,horizon[,d0_time_bin])`，定义：

```text
e(s,t) = forecast_peak(s,t) - physical_X_final(t)
b(s,c,t) = E[e(s,t) | c, historical closed days before t]
forecast_corrected(s,t) = forecast_peak(s,t) - b*(s,c,t)
```

`e<0` 表示该源偏冷，故 `b*<0` 会上调预测；`e>0` 表示偏热，故会下调预测。任何校正都只移动该源弱预测 likelihood 的中心，不得凭偏差直接提高/降低 source weight，不得把“偏差小”误读为“独立性高”。每源误差尺度与 source-set 的相关性仍由唯一联合弱似然 artifact 单独学习。

旧的等权 preview：

```text
0.25·p_GFS(X) + 0.25·p_ECMWF(X) + 0.25·p_TAF(X) + 0.25·p_ICON(X)
```

明确退役为**仅诊断**：不得作为 P0、弱似然、posterior、权重、仓位、决策或订单输入。正式路径必须先逐源校正，再按可用源集合/缺失掩码、误差分布和相关性参数联合建模。

### 证据边界与时序

1. 训练标签只能是 closed AMSC `physical_x_final`；不得用 market `S/B`、Wunderground、市场价格、订单、PnL 或实况当前值替代。
2. 预测输入只能是 P1 已重验的 Tier-A official/authorised forecast revision；Open-Meteo republisher 只能在单独的 historical strict-OOS fallback contract 中训练/校准，必须保留 republisher lineage，不能与 native 样本混称或进入 P1 authority；Windy、事后下载、Tier-B/C 与字段同名旧缓存一律不能进入官方 native 校正训练。
3. P1 中的 `source_id` 必须是唯一联合弱似然 universe 的 canonical ID（`ecmwf/icon/gfs/jma/taf/wu/twc`）。TAF 的 AMSC transport identity、WU 与 TWC 的各自 provider identity 保留在 `payload.provider_source_id`，再以 `payload.weak_source_id` 逐项绑定 canonical ID；不得将已退役的 `*_model_peak` 技术名映射为官方数值模式。
4. `b*(s,c,t)` 在 event `t` 的第一条弱预测/ P0 之前冻结；只允许在先前 event 的 `X` 闭合后更新。因此当天 METAR/SPECI、温度动量、温度轨迹、云雨、露点等不得进入偏差 state。
5. D0 新 forecast revision 只可使用冻结 bias state 进行弱源中心校正；它不能把已见强观测回灌到弱似然。当天物理证据对后验的快速修正仍只属于 strong likelihood。
6. 训练/OOS/样本不足只影响该 shadow 校正 artifact 的晋级，不得阻断 AMSC 入库、强观测更新、已批准基线 posterior 或 live 交易链。

### 分层收缩估计

禁止直接使用单格样本均值 `mean(e)` 作为校正量，也禁止把近几天的异常直接写进配置。正式 state 使用零中心、逐级收缩的经验 Bayes / partial pooling：

```text
b_source(s)                 ~ N(0, tau_global²)
b_context(s,c)              ~ N(b_source(s), tau_context²)
e_bar(s,c) | b_context      ~ N(b_context(s,c), sigma(s,c)² / n_eff(s,c))

lambda(s,c) = tau_context² / (tau_context² + sigma(s,c)² / n_eff(s,c))
b_context*(s,c) = lambda·e_bar(s,c) + (1-lambda)·b_source*(s)
```

`b_source*` 也按同一逻辑向 0 收缩。`sigma` 必须从真实残差序列以 robust scale 估计，不能从 MAE 假定正态后反推再宣称显著性；`tau_global/tau_context` 只能由冻结训练窗口和 OOS 规则估计。这样样本少的“源×季节×时段”自动回退到源全局，源全局样本仍少时自动回退到零校正。

天气型是允许的**更细一级**：`source × season × weather_regime × horizon/bin` 只有独立日数达标时，才可在 `source × season × horizon/bin` 之上细化；不足时必须回退到季节层，不能跳过季节层直接对单一天气型取均值。

### 动态更新、鲁棒性与上限

对每个已经通过层级选择的 state，按**独立 event day 的日终顺序**更新：

```text
e_robust = Winsorize(e, center = frozen median(e), radius = frozen robust_scale)
b_ewma(t) = (1-alpha)·b_ewma(t-1) + alpha·e_robust(t)
b*(t) = Clamp(shrunk(b_ewma(t)), -1.5°C, +1.5°C)
```

- 初始 `alpha` 只能从 `{0.03, 0.05}` 的小型预注册候选中，通过 walk-forward 选择；`alpha=0.05` 的事件日半衰期约 13.5 日。
- 鲁棒截尾中心/尺度、`alpha`、±1.5°C clamp、层级和 source-set policy 都是 artifact 内容，变更必须生成新 artifact SHA；截尾必须围绕冻结残差中位数，只压制离群点，禁止围绕 0 截尾后把稳定系统偏差抹掉；不得每日人工调参。
- correction clamp 是异常保护，不是目标值；若频繁触顶，必须诊断 source transport、季节分层或模型结构，而不是放宽上限。
- 同源多个 D0 快照必须按 `source × event_day × d0_time_bin` 去重；D0 time bin 固定为 `00–06 / 06–12 / 12–15 / 15–close` local，每格每事件最多保留最后一个 strict-as-of revision。快照数永远不能替代独立日数。

### 层级选择与上线门槛

| 候选层 | 最低独立 event day | 未满足时 | 可用于校正的额外条件 |
|---|---:|---|---|
| `source_global × D1` | 30 | 零校正 | walk-forward 不劣化 |
| `source × season × D1` | 30 | 回退 `source_global × D1` | 相比回退层在 OOS 有稳健改善 |
| `source × season × D0_time_bin` | 30 | 回退对应 D1 层 | 每日去重、OOS 有稳健改善 |
| 任意正式 source-set | exact artifact surface | 不借邻近组合 | 校正后误差与相关性参数都存在 |

“显著性”只能作为诊断，不能单独授权校正。禁止以假设标准差计算 t/p 值后直接上线；必须使用实际残差、按 event day 的 block bootstrap，并在严格时间顺序的 walk-forward hold-out 上同时比较 raw 与 corrected 的 MAE、NLL、RPS。候选只有在主要指标不恶化、95% 置信区间不越过预设 harm tolerance、且尾部 bucket 保护通过时，才可成为 shadow candidate；production activation 仍需单独 artifact approval。

已冻结的 OOS safety 门槛为：对同一 artifact surface 的 zero-bias raw baseline，event-day block bootstrap 的 95% 上界必须满足 `ΔMAE(corrected−raw) ≤ +0.10°C`、`ΔNLL(corrected−raw) ≤ +0.05`、`ΔRPS(corrected−raw) ≤ +0.02`。物理 `X` 的低/高尾部阈值只由**训练期** closed X 的 Q10/Q90 固定；OOS 每一侧至少 5 个独立 event day，且分别满足同一 MAE/NLL/RPS harm tolerance。`joint_weak_oos_safety_runtime.py` 是唯一实现，raw/corrected 的 event identity 或 target 不同即拒绝；尾部样本不足只阻断该候选 promotion，不得成为 AMSC、strong 或交易 live 的运行时门。

### 当前状态

现有 D1 legacy 统计只提供假设方向：近期 ECMWF 最稳、ICON 次之、GFS 存在偏冷迹象；它们不构成正式 bias artifact。正式 lane 当前必须显示 `pending_formal_samples`，直至 P1 Tier-A official revision 与 closed AMSC `physical_x_final` 自动配对。ECMWF Open Data 若没有 observed response-header arrival，仍是 Tier-C，不能进入本合同的正式训练/校正，即使其 legacy 指标较好。

### 已落地的 artifact 合同

`joint_weak_bias_calibration_runtime.py` 是唯一的离线偏差 state 训练器。它从联合弱似然训练集读取 `forecast_peak_c − physical_x_final_c`，以 `source × event_day × D0_time_bin` 保留最后一条 strict-as-of revision；不读取 D0 报文、市场或订单。它输出冻结的 `bias_correction` 到 `joint_forecast_weak_likelihood_artifact_v3`，其中记录：残差符号、预注册 `alpha` 候选、实际候选 alpha、MAD robust scale、层级 shrinkage、event-day 样本数、回退原因和 ±1.5°C clamp。`scripts/train_joint_forecast_weak_likelihood.py --bias-ewma-alpha {0.03,0.05}` 只允许产生两份 shadow candidate；walk-forward 必须比较它们后才能把胜者送入 promotion packet。

每个非空 `bias_correction` 还必须把 `training_event_date_min/max` 与 `state_asof_policy=training_event_date_max_strictly_before_scored_event_date` 写入 artifact 身份。运行时对每个 event 强制 `training_event_date_max < event_date`；同日 final X、未来日标签或日期关系不可验证时，当前 trained artifact 必须 fail closed，不能参与该日弱似然。零样本 operational baseline 没有训练日期窗口并继续输出明确零偏差；该 artifact 身份检查只约束弱模型，不阻断 AMSC 入库、strong update 或其他已批准 live 基线。

source-set 参数必须按运行时同一口径拟合：每条训练修订先计算 `e_corrected=forecast_peak−selected_hierarchical_bias−physical_X_final`，再用 `e_corrected` 估计该 exact source-set 的 `error_sigma_c`、源间相关性、inverse-variance 权重与 joint information scale。禁止一边在 live 移动中心、一边继续用未校正残差拟合组合误差；否则跨季节或 D0 时段变化会被错误计入不确定性与相关性。

`joint_forecast_weak_likelihood_oos_runtime.py` 会在同一严格时间切分、同一 OOS 事件集合上分别评估 `0.03` 与 `0.05`，按 `NLL → MAE → 较小 alpha（仅完全并列时）` 固定排序，输出两个候选 artifact SHA、覆盖集合和 `bias_alpha_selection`。`joint_forecast_weak_likelihood_promotion_runtime.py` 必须同时核验：比较完整、候选 SHA 是选中的 SHA、`candidate_is_selected=true`。非胜者可以保留为 shadow 复盘证据，但不能进入 promotion packet；这不影响实时 AMSC/strong/live 链路。

同模块的 `evaluate_joint_weak_expanding_window` 是独立的 rolling-origin/expanding-window 研究评估：每一个 origin day 只能用此前 event day 拟合，并逐折生成新的 immutable artifact SHA。它验证时间稳定性、alpha 稳健性与 raw-baseline safety，但因天然包含多个 fold artifact，`promotion_eligible=false`；不得拿它冒充单一 frozen candidate 的 promotion 身份证据，也不得因此放宽 promotion packet 的 SHA 约束。

日终自动化仅由唯一 monitor daemon 的 `resolution_final_shadow_capture` 低优先级 child 执行：先独立 closed AMSC X → station-day candidate，再执行 `run_joint_weak_bias_research_pipeline.py`。candidate 的输入身份按 station day 冻结：`input_lineage_sha256` 只含同日 strict forecast context/revision 与同日 physical X，不能因别日 final-X 追加而漂移；`materialization_sha256` 才包含 append revision 与 supersede 血缘。输入未变的重复调度必须 `skipped_existing_immutable_candidate`；输入确有变化时追加带连续 `candidate_revision` 的 successor，consumer 只取 predecessor path/SHA/revision 闭合的最高有效 revision。该 pipeline 以 `(dataset_sha256, pipeline_contract_sha256)` 联合去重；后者冻结算法合同、source catalog、样本阈值、alpha 集合、评估协议与 bias as-of policy。数据或合同任一变化时，才在 `data/research/joint_forecast_weak_likelihood/bias_pipeline/` append 新的 immutable shadow artifact/report 与 research-only `latest.json`，并产出两组 frozen alpha、frozen OOS 和 rolling-origin 研究证据；因此算法升级不会被“数据没变”错误吞掉。它不写 active pointer、archive DB、posterior 或订单；两类身份均未变化才返回 `skipped_unchanged_dataset`。任何样本不足/OOS 不通过只保留研究报告，永远不是 AMSC/strong/live 的阻断条件。

物理标签的供给链已经与旧事实投影解耦。`scripts/materialize_amsc_daily_coverage_v2.py` 默认只读预览，只有 `--apply --confirm MATERIALIZE_AMSC_DAILY_COVERAGE_V2` 才能从 immutable AMSC raw object 与 signed poll manifest 写一个 content-addressed `coverage_v2` 派生快照；它绝不读取已退役 `coverage/`，也不改 raw、`archive.db`、posterior 或订单。`amsc_primary_raw_coverage_reader.py` 是 final-X 默认读取边界：逐项重验 coverage 的 44 个成员、raw hash、站点/类型/有效时刻、首见时刻、签名 poll membership 和温度字段。故旧 `archive.db.source_observations` 某日投影不完整不能再卡住真实物理 X，且也不能通过补事实行伪造血缘。

截至 2026-07-13，isolated final-X registry 已有 2026-07-10/11/12 三个 closed physical X（36/31/33℃）。它们分别保留 19/2/1 条迟到事实，均为 `strict_asof_training_eligible=false`：可以进入 AMSC-only 物理先验记录和非严格研究覆盖，但不能进入本合同的正式 strict bias/OOS 样本。该结果验证了“每日 X 独立形成、strict 晋级证据另行积累”的边界；正式偏差 artifact 仍等待未来合格 strict event days，等待状态不阻断已批准 live 基线。

展示路径严格分离：`ui.server.materialize_joint_weak_bias_research_readiness` 只在 feedback loop 中验证 research `latest.json → immutable report → pipeline SHA → research_only boundary` 后，append 到 `archive.db.feedback_snapshots(kind=joint_weak_bias_research_readiness, days=0)`；`/api/ui_snapshot` 只从该 archive snapshot 为成都事实项附加 `joint_weak_bias_research`。UI、posterior、订单和 monitor live 路径不得直接读取 research 文件；snapshot 仅展示 readiness，不能批准 artifact、改变权重或影响交易。

运行时 `joint_forecast_weak_likelihood_runtime.py` 只能消费这个已绑定 artifact SHA 的 state，并严格执行 `forecast_corrected = forecast_peak − bias_c`。每个 source expert trace 必须带 `bias_state_id`、`bias_level`、`bias_sample_count`、`bias_fallback`；不能再从 source-set 单格 `mean(e)` 得出偏差。缺少 v3 bias state 或残差定义不一致的 artifact 直接 fail closed，绝不回退到 v2/旧静态偏移。该 fail-closed 仅适用于弱模型 artifact 身份，**不阻断** AMSC 事实入库、strong update 或 live 交易已有批准基线。

### 偏差校正实施与验收清单

- [x] 标签固定为 closed AMSC `physical_x_final`，残差符号固定为 `forecast−X`；市场 S/B、价格、订单和 D0 实况禁止进入训练特征。
- [x] 按 source→season/horizon/bin→weather regime 分层收缩；不足 30 个独立日逐级回退，最终为零校正。
- [x] 同源 D0 修订按 event day×time bin 去重；EWMA 只允许 0.03/0.05，使用实际 MAD、围绕冻结中位数截尾，并以 ±1.5℃ clamp 防异常。
- [x] exact source-set 的误差、相关性、权重和 information scale 均使用**校正后残差**重新拟合；缺源组合不借用邻近组合。
- [x] runtime 只从已批准 artifact 读取 bias state，输出完整 source trace；非空训练 artifact 强制 `training_event_date_max < scored event_date`，防止同日/未来标签泄漏。
- [x] 多源偏差校正后使用 mixture 保留合理多峰；每个峰按局部贡献绑定真实支持源，D0 curve tournament 已隔离为 research-only，不能改写生产弱权重。
- [x] 两个 alpha candidate、frozen walk-forward、raw baseline safety、rolling-origin 研究和 research-only UI readiness 已形成独立离线链；样本/OOS 不进入 live gate。
- [ ] 持续积累 P1 Tier-A official/authorised forecast revision 与独立 closed X；迟到事实只能进入非严格研究覆盖。
- [ ] 达到每个拟晋级 surface 的独立日/尾部覆盖后完成 alpha 选择、OOS safety、人工 approval 与原子 active-pointer 切换。
- [ ] 晋级后持续监控分季节/天气型的 MAE、NLL、RPS、clamp-hit rate 与 missing-source surface 覆盖；异常只生成新 shadow candidate，不在线改生产参数。

## 模型切换规则

**硬边界：样本不阻断 live。** strict-as-of capture、训练样本量、OOS、消融、X/S bridge pair 与 final-label 覆盖仅决定候选 artifact 能否晋级；它们不得被 production AMSC 入库、当前 posterior、交易决策或下单热路径读取为运行时阻断。运行时只校验当前已激活 artifact 的身份/结构与实时输入血缘/时效。`scripts/live_offline_evidence_isolation_audit.py` 必须持续证明这一隔离。

### 预校准运行基线（样本积累期）

在严格 physical-X 样本尚不足以拟合正式参数前，唯一弱似然可以使用一份**已批准、不可变、低信息量的联合基线 artifact**；它不是旧 v3.1/Open-Meteo 模型的 fallback，也不是把 shadow candidate 偷渡上线。当前默认实现为 `joint_weak_operational_baseline_v3`，由 `joint_weak_baseline_artifact_runtime.py` 定义、`scripts/materialize_joint_weak_operational_baseline.py` 以 content-addressed JSON 物化。它的 SHA、source universe、无样本 fallback residual distribution、源族相关性、family cap、信息上限、零 bias policy 与生成规则必须一起冻结。v1/v2 仅保留为不可变历史证据，不再由默认 loader 选用；已绑定旧 P0 的历史 artifact 仍按 as-of 不可变保留，但不能成为新 P0 的 fallback。

预校准基线必须把“源内残差不确定性”与“源间情景分歧”分开：逐源 fallback residual scale 只表达该源在无训练样本时的误差先验，不能承担人为保峰或抹峰；`joint_information_scale`、源间相关性折扣与 `information_confidence` 只控制重复信息和联合置信度。峰形必须由校正中心距离、残差尺度、密度重叠和相关性自然形成。禁止用 3–4℃ 的超宽单源核把明显分歧的 32.8/35.4/37.6/39℃ 情景抹成无人实际预测的中间单峰，也禁止无依据地缩窄核宽强制多峰；物理 X 校准样本闭合后，再由已批准逐源条件残差 artifact 整体替换无样本基线。

基线的 exact-surface template 对每一个实际 `(season, weather_regime, lead_bucket, A, M)` 确定性生成独立 surface identity，其中 `A` 是可用预测源集合，`M=U\\A`；这不是“借用最近组合”或 generic fallback。每一 surface 均显式记录缺源，且 source 数量/源族多样性减少时同时降低 `joint_information_scale` 与 `information_confidence`，绝不补造缺失预测。它的误差尺度保持宽、information scale 有上限、bias 固定为 0，故只在没有已批准 trained artifact 时作为保守运行基线；一旦完整训练/OOS/promotion 的 artifact 被批准，必须以新的 SHA 替换它，不能就地改参数。

live forecast revision 的唯一适配器为 `joint_weak_live_evidence_runtime.py`：官方完整日温曲线 revision 与 TAF/WU/TWC 的独立 forecast observation 均被清洗为 `forecast_weak_source` revision，只有 `event_date/available_at/forecast_peak_c/raw SHA/source revision ID` 及 forecast-declared context 可进入。D0 METAR/SPECI、温度动量、轨迹、云雨、露点、市场、订单与仓位字段全部禁止进入该适配器。Tier-A 是训练/OOS门槛；Tier-C（如当前 ECMWF 的 SDK transport）仍可作为 live 弱预测，但其状态必须留在 lineage 中，绝不能被提升为 strict 训练证据。

唯一 SSOT 概率时序固定为：

```text
P0(X,Z0) = Normalize(P_prior(X) × L_weak(F|X,A,M) × P_prior(Z0|X,F))
Pt(X,Zt) = Normalize(HardCondition(Pt-1(X,Zt-1)
                                   × exp(raw_log_score(ot|X,Zt-1,F))
                                   × transition(Zt|Zt-1,ot)))
```

弱似然只在 P0 消费一次。之后 strong artifact 只输出 event-level raw log score 与 post-state proposal；唯一 sequential composer 拥有 hard condition、normalizer、posterior delta 和 checkpoint。strong 不得改变任何 weak source weight 或当日 bias state；hard condition 只能移除物理不可能温度格，不能被 phase/置信度 temper。

弱似然只在 D0 第一条 canonical observation 前参与一次 P0：`initial_posterior_checkpoint_shadow` 将同一连续温度格上的 prior、联合 `L_F` 与 latent-state prior 归一化为初始 checkpoint。之后每条报文只能通过 `strong_observation_incremental_lr_artifact_v3` 的候选温度相关 raw log score 更新前一 checkpoint；不得重新运行弱模型、不得把 absolute strong distribution 再乘一次，也不得建立独立 X-shadow latest pointer。原 `physical_x_shadow_posterior_runtime/joint_physical_x_bayes_composer_runtime` 自动链已退役并从 AMSC launcher 删除。

旧 v3.1 artifact 在 Open-Meteo GFS/JMA feature distribution 上训练。改 transport 等于改特征分布，不能把官方 GRIB 字段按同名直接喂旧系数。正确终局是**一个**唯一弱似然主模型：输入为所有可用弱预测源、source-set/missingness mask、季节/天气型/lead；artifact 为每个已支持组合维护独立校准、误差分布与相关性参数，并输出概率与信息置信度。若既无结构有效的已批准预校准基线、也无已批准 trained artifact，runtime 必须显示 `official_nwp_artifact_not_approved` 并 fail closed；不得以旧 v3.1 系数补位。依次完成以下工作后，trained artifact 才可以替换基线：

### 唯一弱似然主模型合同

令 `U={ECMWF, ICON, GFS, JMA, TAF, WU, TWC}`，`A⊆U` 为在 `as_of` 前具有合法 raw/时间/版本血缘的可用源集合，`M=U\A` 为缺失集合。`A` 与 `M` 都是模型输入，不能由旧值、转发源或调用方默认值补齐。

```text
weak_surface = artifact[season, weather_regime, lead_bucket, A]
L_F(T | A, M, e) = temper_{information_scale×correlation_discount}(
    calibrated_correlation_aware_mixture(
    revisions[A],
    bias/error[weak_surface],
    correlation[weak_surface]
    )
)

posterior(T) ∝ prior(T) × L_F(T | A, M, e)
                         × L_strong(T | AMSC/AWC physics, e)
                         × hard_mask(T)
```

上式中的 `L_F` 已包含 `information_scale×correlation_discount` temper，进入 P0 时只能乘一次；不得再对同一 `L_F` 二次乘方。规范式固定为 `posterior(T) ∝ prior(T) × L_F(T|...) × L_strong(T|...) × hard_mask(T)`。

`artifact[..., A]` 是 exact-match surface，至少包含 `available_source_ids`、`missing_source_ids`、每源误差校准、组合相关性矩阵、联合 information scale 与 `information_confidence∈[0,1]`。例如 `ECMWF+ICON+GFS`、`GFS+TAF`、`TAF+WU+TWC` 都是不同 surface；没有匹配 surface 时不能借用“全源”或“最近组合”的参数。`A=∅` 也可由经过批准的 `empty` surface 输出中性弱似然和 0 信息置信度，允许 prior+strong posterior 正常更新。

多峰不是 KPI，也不是强制输出。唯一合法顺序是：先逐源、分 `season × weather_regime × lead/time-bin` 校正 `forecast_peak_c - final X` 的偏差；再在同一条件及 exact source set 内拟合校正后残差分布和残差相关矩阵；最后以校正后的中心距离、残差尺度、密度重叠及相关性自然形成天气情景混合。校正后相近或高度相关且密度重叠的源可以合理合并为一个峰；显著分离的合理情景必须保留。禁止人为规定多峰/单峰，禁止用无证据的 3–4℃源内核宽抹平源间分歧，也禁止用 product/log pool 或算术平均制造没有来源支持的中间峰。相关性与 `information_scale` 控制重复信息和联合置信度，不负责硬选某个源。D0 报文、温度动量、轨迹、云雨与露点完全不进入 `L_F`；它们只在 strong likelihood 中更新各峰的物理可达性。

无 closed-X 样本时，`joint_weak_operational_baseline_v3` 只是一份获批的无样本运行基线：其逐源 Gaussian scale 是显式 fallback residual distribution，不声称历史 RMSE、不能作为最终固定核宽，也不强制多峰。它用低 information confidence 表达未校准风险；一旦逐源条件偏差/残差 artifact 通过独立晋级即整体替换，runtime 不在两者间 fallback。

### 预测分歧、多峰与 D0 收敛合同

当预测源口径存在显著分歧时，系统必须保留“分歧本身”这一不确定性结构：

```text
strict-as-of 预测源分歧
→ 弱似然逐源条件偏差/残差校正后，让合理情景自然保留或合并
→ D0 AMSC 报文、动量、轨迹、云雨、露点等强观测
→ 对每个峰计算剩余升温/降温及物理可达性
→ posterior SSOT 随 canonical observation event 序贯收敛
```

1. 所有预测源始终是弱证据。实况不能把某个源“晋升为事实”，也不能产生“某模型胜出”的硬切换；AMSC canonical 报文才是实时事实源。
2. 弱层先逐源执行冻结的条件 bias correction，再拟合条件残差分布，最后以 exact source-set 的相关性和信息尺度形成混合。37.6℃ 与 32.8℃是否形成两个峰由校正后距离、残差尺度、密度重叠和相关性决定；不得机械平均成虚构的 35.2℃，也不得为了“保峰”任意缩窄核宽。
3. `forecast_multimodality_trace` 必须按每个峰的局部概率贡献列出 `supporting_source_ids` 与贡献占比；禁止把所有可用源都列为每个峰的支持者。该 trace 只解释已经形成的弱分布，不二次改概率、权重或策略。
4. D0 轨迹软仲裁只能存在于 strong observation update 的 `forecast × observation` 条件交互中：累计、时间对齐、物理一致的观测可逐步降低不可达峰；单条报文不能改写 base source weight、bias state 或弱分布。
5. 已观测日最高温构成 hard lower bound：低于它的 `X` 候选由 `hard_mask` 置零。尚未达到的高温峰只能由剩余升温/降温与云雨等联合物理证据软更新，不能仅按当前温度机械排除。
6. closed final X 仅在日终更新未来 event 的 bias、error、correlation 与组合参数；同日 final X、市场 S/B、价格、订单和 PnL 永远不能回灌当天 posterior。
7. 原 `forecast_source_curve_tournament_runtime.py` 明确降级为 research-only 诊断。其 `effective_weights` 禁止进入唯一联合弱似然、bias artifact、posterior、仓位或订单；生产 D0 收敛只允许由 strong likelihood/composer 完成。

上述边界同时解决“既不糊峰、也不造峰”和“实况快速选汰物理情景”：弱层不提前丢失合理情景，也不人为维持虚假分叉；强层不等待日终模型评分即可在每次报文后直接修正最终最高温 `X`。

ForecastRevision 在 `as_of` 前没有任何合格预测修订时必须输出**显式空快照**（空 `source_revisions`、完整缺失掩码和 `no_forecast_revisions_available`），不能先行标记 posterior blocked。下游只有在 artifact 没有获批 `empty` surface 时才 fail closed；这样“不用旧预测伪造可用”与“不能因为没有预测而阻断 AMSC strong 更新”同时成立。

后验仍是唯一 SSOT。弱模型的 `information_confidence` 只能作为可审计的风险/仓位能力输入，不能替代 posterior、不能手工改概率、更不能把缺源变成隐式 NO/YES 交易信号。AMSC strong observation 始终按 Transaction A 独立入库；缺预测源绝不阻塞它。

1. 为四源建立 direct raw/decoder/normalized forward archive；JMA 授权交付未配置时保持明确 NA。
2. 构建 strict-as-of 四源训练集与 source-family 去重；TAF/WU/TWC 作为独立源加入。
3. 训练唯一联合弱似然，并按「可用源组合 × 季节 × 天气型 × lead time」学习校准、误差、相关性与置信度，不手工恒定加权；组合例如 ECMWF+ICON+GFS、GFS+TAF、TAF+WU/TWC。
4. 完成 OOS、校准、消融、版本审批和 shadow 对比；它们是 artifact promotion gate，不是 live AMSC 事件阻断。
5. 有 P10/P8/P9 shadow 延迟证据后才向 production promotion；posterior checkpoint 仍是唯一 SSOT。

训练器的唯一入口是 `joint_forecast_weak_likelihood_training_runtime.py`（可由 `scripts/train_joint_forecast_weak_likelihood.py` 只读运行）。它只接受闭合 `physical_x_final` 标签和具备 raw SHA、header-arrival、strict Tier-A 的 forecast revision；任何 D0 报文、轨迹、云雨、市场价格、订单或旧 Open-Meteo 特征都会被 quarantine。训练输出默认是 `approval_state=shadow_only` artifact，写入也仅限 research 目录；训练脚本不读取/写入 posterior、事实库、runtime active pointer 或订单。每个 context 的 `empty` surface 是数学中性恒等式，不从事后天气数据拟合，也不能替代任一非空组合。

OOS 使用 `joint_forecast_weak_likelihood_oos_runtime.py` 按 event date walk-forward 切分。评估期的标签只能在弱似然已经输出后用于计算 MAE/NLL；评估运行时不得把 `physical_x_final` 传给弱似然。其临时批准标记仅存在于内存以复用 runtime 结构校验，不生成 active artifact、更不改变 live 链路。artifact SHA 必须由除 `artifact_sha256` 自身之外的全部不可变内容重算核对；任一字段或 approval state 变化都必须产生新 SHA，运行时拒绝不匹配 artifact。OOS、审批和 shadow 对比是 promotion 条件，明确不是 AMSC 入库、posterior 计算或交易 live 的阻断条件。

`joint_forecast_weak_likelihood_promotion_runtime.py` 只能生成 review packet：它核对 candidate SHA、strict OOS 所用 candidate 身份、样本量与指标，并列出 P10/P8/P9 shadow 延迟证据、原子 active-pointer 和 rollback reference 等上线前置条件。它不能把 `shadow_only` 改成 `approved`，不能写 runtime pointer、posterior 或订单；任何身份不匹配都只能重新训练/评估，不能改字段后沿用旧 SHA。

artifact 的训练样本 SHA 只覆盖实际进入该次拟合的 sample、source catalog 与训练政策。walk-forward 的评估期样本、同文件中被 quarantine 的行或外围全量 manifest 都不得改变 pre-cutoff candidate 身份；否则会把真正相同的训练结果错误地判成 OOS 身份漂移。

### 训练事件组装合同

训练输入不得手工拼接 JSON。唯一组装路径是 `joint_forecast_weak_training_event_runtime.py`，命令入口为 `scripts/build_joint_forecast_weak_training_events.py`：它只读三类独立证据并生成不可变 event envelope。

1. P1 的 `strict_asof_forecast_p1_reader`：只读取已重验 column/JSON binding 的 Tier-A `forecast_weak_source`；每个 revision 必须具有 raw SHA/ref、observed response-header、first-system-seen/available-at 绑定，且不晚于该 event 的 `decision_as_of`。
2. isolated `resolution_final_source_observations` registry 中 role=`physical_daily_high_final_truth` 的 AMSC 日终 X：它必须由 ZUUU 独立站点日 coverage 产生，不得等待市场 target、finality、Wunderground S 或 X/S bridge；它必须有完整排班覆盖、raw SHA/ref、`label_finality=final` 与 ZUUU 身份。其 physical-X raw SHA 与数值只作为**事后 supervised target**，绝不参与 as-of revision 选择、context 或弱似然计算。
3. `forecast_weak_context_capture_v1`：每个 cutoff 必须显式冻结 `season`、`weather_regime`、`lead_bucket`、context raw SHA 与可用时间；其 `context_source_class` 固定为 `forecast_weak_context`，只能使用 forecast-only/context evidence。D0 实况、METAR、市场、订单、仓位、PnL、Edge/LCB 字段任一进入即 quarantine。

`forecast_weak_context_capture_runtime.py` 是该第三类证据的唯一推导器，`scripts/build_forecast_weak_context_captures.py` 从显式 cutoff 请求与 P1 Tier-A forecast rows 只读生成 capture。capture 的训练身份优先是 `station_day:ZUUU:<event_date>`；有 market target 时只能作为可选附属 `target_identity`，不得反过来成为 final-X 学习的前置。`scripts/materialize_station_day_weak_physical_training.py` 在 closed AMSC X 后，按固定本地 `09:00/11:00/13:00/15:00/17:00` cutoff 物化不可变 research candidate；它由唯一 monitor daemon 的低优先级 child 调度，不新建守护进程。季节只由 event local date 推导；`lead_bucket` 只由 cutoff 相对 event local day 推导；天气型只接受 forecast payload 中已规范化的 `clear_dry/cloudy_stable/wet_precipitation/convection_or_showers/fog_low_cloud`。没有该 forecast 字段时固定写 `unknown_forecast_regime`，来源互相冲突时固定写 `mixed_forecast_regime`；不得由温度实况、云雨实况或最终标签猜天气型。capture 的 context SHA 是输入 forecast raw identity 的有序聚合，不能代替任一 source raw SHA。

上述 `weather_regime` 硬标签只服务历史训练分层与旧 artifact surface identity，不得直接作为
v6 状态转移控制。v6 live 的规范对象是 weak snapshot 内的
`forecast_background_context`：保留各来源天气情景贡献与分布向量，并与
`physical.weather_regime` 严格分名。若来源冲突，live context 保留分歧，不能以 `mixed` 抹平；
若全部未知，则 context 中性。当前 production v6 以同 snapshot 的候选条件化小时曲线及
温度/云雨/CB feature availability 作为更细物理控制；任何 regime-vector 补层必须证明只填补
缺失 feature、没有重复计数，并通过新 transition artifact 的离线消融/OOS 晋级。

同 day 的 final physical-X source 不是唯一 immutable observation 时，事件必须以 `final_physical_x_source_ambiguous` quarantine；没有 P1 Tier-A revision、revision 晚于 cutoff 或任一 binding 漂移也必须 quarantine。**弱似然只预测 X，因此不得要求 `bridge_closed=true`、最终 S 或 B 才训练 X。**否则会出现“要用 X/S 对校准 bridge，却先要求 bridge 才允许取得 X 训练样本”的循环等待。S/B 与闭合 bridge 仍是强似然的 settlement OOS、P10 和策略验收前置条件。quarantine 仅减少训练/OOS 覆盖，**绝不阻断** AMSC 实况入库、strong update 或交易 live。组装输出可直接交给 `scripts/train_joint_forecast_weak_likelihood.py --events …`，但仍须经过 walk-forward、review packet 与单独的 promotion；它不写 artifact pointer、posterior、archive 或订单。

日终 capture 不再是串行互锁：AMSC final X 按 ZUUU station day 独立执行；市场侧独立执行 `target contract → market finality → Wunderground final S`；只有之后的 observed X/S pair 才按同 target-contract/day 将两者 join。pair 由 `resolution_observed_x_s_pair_shadow.py` append 到 `resolution_observed_x_s_pairs`，明确写 `bridge_closed=false`、`market_settlement_bucket=null` 和 NA 原因。它是 X→S kernel 校准数据，**不是** final X→S→B label、posterior 或交易输入。只有完成足够 pairs 的严格校准/OOS 后，才能另行把 bridge 设为 closed，并生成 final B label。`scripts/polymarket_monitor_daemon.sh` 只能作为唯一 monitor daemon 的低优先级 child 调度这些独立 stage；不得另建守护进程。

pair 覆盖只以 `scripts/resolution_bridge_pair_coverage_audit.py` 的只读结果为准：可进入校准审查至少需要 90 个不同 event day，且 X 与 S 各至少 12 个不同有效值；每行还必须保留两端 raw SHA/ref 与 source-observation identity。它绝不把“已有足量 pair”解释成 bridge 已闭合或 B 已知。

## 官方日最高温曲线桥

官方 NWP 的单个 GRIB 点位不是“日最高温预测”。`official_nwp_snapshot_runtime.py` 对每次未来采集将 normalized point snapshot 追加到 content-addressed immutable history；`official_nwp_daily_peak_revision_runtime.py` 只在同一 provider、同一 model run、目标本地日、**完整预定 valid-time 集合**都齐备时，才将 `temperature_2m_c` 曲线转换为 `forecast_weak_source` 的 `forecast_peak_c` revision。不得插值、缺槽补值、混 run、复用旧点位或将实况温度加入曲线。

每个点位的 request/header-start/complete/raw SHA 必须原样保存。全部点位具有可观察 header-arrival 时，曲线 revision 才是 Tier A；任一点位无法观察 header（当前 ECMWF Open Data SDK 即如此）则该 revision 只能作为 Tier C live 弱预测，绝不能进入 strict-as-of 训练/OOS/动态 artifact。既有未记录 header 事实的历史快照不得回填、不得补升 Tier A。

`config/forecast_sources/zuuu_official_daily_curve_run_contract_v1.json` 是曲线采集的版本化 run contract；`official_nwp_daily_curve_plan_runtime.py` 仅按该合同、目标的 `Asia/Shanghai` resolution-local date 及显式 run 生成 valid-time 计划。它将 ECMWF 00/12Z 的 3 小时曲线、ICON/GFS 的小时曲线保留为不同 source-resolution 输入；模型分辨率不被重采样成相同曲线。若 run 晚于目标本地日开始、目标日不在该 run 的计划 lead 内或 JMA 授权交付未配置，planner 明确 blocked。实际 transport/raw arrival 仍是唯一可用性事实，合同不是“数据已经到达”的替代证明。

曲线 revision 必须携带 `raw_sha256`（由全部 component raw/snapshot identity 聚合）、`raw_sha256s`、`raw_object_refs`、`snapshot_sha256s`、`forecast_peak_valid_time` 与完整温度曲线；这使其可作为 strict-as-of 训练 event 的 forecast revision。上述字段只属 provenance，不能被误当作模型特征；反之，任意真实市场、订单、当前实况或 METAR 值出现在 forecast feature surface 时仍必须 quarantine。

曲线采集 orchestration 的唯一入口为 `scripts/run_official_nwp_daily_curve_collection.py`。默认只输出 no-network plan；`--execute` 才调用官方 adapter，且写入与 hot prewarm 根隔离的 `data/runtime/official_nwp_curve_sources_v2`。`v2_raw_payload_identity` 是当前唯一 capture generation：每个 point snapshot 既绑定 transport receipt manifest，也绑定 direct provider-body hash；旧 `official_nwp_curve_sources` 因缺少该原始 body identity 只保留审计，不能形成 strict-as-of revision 或进入 P1。它禁止对已结束的 target local date 执行补抓：事后重新下载历史 forecast 不能被伪装成当时可见的 strict-as-of 样本。该保护不阻止同日收到的后续 forecast revision；其真实 `response_completed_at` 仍决定 as-of 可见性。

完整曲线的日峰修订必须由 `official_nwp_daily_peak_revision_runtime.py` 从同一 provider、同一 run、合同指定的全部 valid-time immutable point snapshot 构建；禁止插值、跨 run 拼接或以单点替代整日曲线。低频 curve monitor 在某 provider/run 的整条曲线成功后，会立即把该纯派生物按 `source_revision_id` content-addressed 写入同一隔离 curve root；不完整/冲突曲线一律不写 revision。`scripts/persist_official_nwp_daily_peak_revision.py` 保留为人工只读预览和显式 apply 入口。它不是“伪造的一条原始 provider 响应”：修订仍绑定全部 raw receipt hash/ref 与 point snapshot hash，且 `available_at` 固定为最后一个点的实际 response completion。只有所有点都有 observed response headers 时，`official_nwp_daily_peak_strict_asof_runtime.py` 才能生成 Tier-A P1 弱预测 proposal；唯一 monitor daemon 的同一低频 child 随后运行 `scripts/materialize_official_nwp_daily_peak_p1_ingress.py --apply`，每个 provider revision 各自使用短 P1 transaction append/idempotent。一个 provider 的不完整、Tier-C 或写入失败只能 quarantine 自己，不能阻塞其他 provider 或 AMSC；该步骤只在 `PM_OFFICIAL_NWP_DAILY_PEAK_P1_INGRESS_ENABLED=1` 下启用。`scripts/append_official_nwp_daily_peak_p1_ingress.py` 仍保留为人工只读预览和单 revision 显式 apply 入口。所有入口均不写 canonical event、posterior、fact、decision、intent 或订单；它们与旧单响应 forecast capture 共用 P1 reader，但使用独立 parser version 和官方 source identity，绝不复用旧 `*_model_peak` transport/系数。

ECMWF Open Data SDK 当前没有可用的 response-header arrival hook，故其 direct snapshot 即使 transport 与 raw 正确也只能标记 Tier-C，不能纳入 strict-as-of 训练或 P1 Tier-A 入口；不得把下载完成时间伪称 header 到达。DWD ICON 的每个 valid-time 由 11 个独立 DWD 原始文件组成，collector 必须逐文件保留 receipt/body hash，但可并发获取并将原始 GRIB 合并后一次 CDO 官方权重 remap；这只优化离线采集时延，不改变每一文件的原始身份、到达时间或 source-resolution 输入语义。

生产调度只允许 `system/com.weather.monitor.daemon` 的低频 child 使用 `scripts/run_official_nwp_daily_curve_monitor_job.py`；没有新增 LaunchAgent/LaunchDaemon。job 默认关闭，必须同时显式设置 `PM_OFFICIAL_NWP_CURVE_COLLECTION_ENABLED=1` 才会执行网络采集；默认仅产生 no-network plan。它先选择不晚于当前时刻及目标日开始的最新 nominal run，再以实际 transport/raw receipt 验证可用性。若最新 run 尚未在官方端发布，才可完整改试一个更早的合同允许 run；每一次尝试仍须独立采齐该 run 的完整曲线，严禁跨 run 拼点、插值或把失败 run 标记完成。成功的 provider/run 写入 status 后才去重，失败绝不记作完成。该 child 写入隔离 curve root，禁止在 AMSC/P10/订单热路径中运行。

该 monitor child 对每个 status root 持有 process-lifetime 非阻塞独占 lease；第二个执行实例必须返回 `official_nwp_curve_monitor_run_already_in_progress`，且不能覆盖正在运行实例的 status。lease 随进程退出自动释放，故不需要把“正在运行”伪造为可恢复的 completed run。provider 内可受限并发采集多个 independent valid-time point（当前上限 2），但必须以合同 valid-time 顺序输出结果；每个 point 仍单独落 raw/receipt/snapshot，不能共享或合并 arrival timestamp。

采集预算固定为每个 monitor 周期最多一个 provider，按 UTC 小时在 ECMWF/ICON/GFS 间轮转；不在同一周期触发三源全曲线。每一个 `(provider, run, valid_time)` 都先按 immutable history 的 schema/provider/run/valid/raw SHA/snapshot SHA 精确恢复，命中才跳过网络；任一点失败不会将整个 provider/run 记成完成。这样重启、超时和部分曲线都只能 resume，不能重复下载或伪造完整曲线。

live 弱似然 prewarm 使用独立的 `official_nwp_live_snapshot_monitor_job_v1`，但它只能作为既有 `system/com.weather.monitor.daemon` 的低频 child；旧 `gui/*/com.weather.official_nwp_prewarm` 已退役，不能并行覆写 generation。合同显式固定“目标日 `Asia/Shanghai` 14:00（06Z）峰值锚点”，每次最多抓一个 provider 的该锚点 official snapshot；provider 按 900 秒 collection slot 轮转，3600 秒 freshness window 覆盖完整三源轮转及一次短暂失败。最新 run 未发布时只可完整回退一个更早 declared run。collector 写入 `data/runtime/official_nwp_sources/{provider}` 后，才以**本轮 receipt 已完成后的** publication as-of 调用既有 no-network prewarm publisher。因而新快照不会被早于它的 planning clock 错判为未来数据；generation 只含当时 fresh 的 source subset，缺源仍作为唯一弱似然模型的显式可用性输入，绝不阻塞 AMSC 事实入库。该低频 child 不得进入 AMSC/P10/订单热路径，不写 archive/posterior/order。

`scripts/weather_rebuild_readiness_audit.py` 是本次重构的只读汇总口径：同时报告官方 generation 的可用/缺失掩码、prospective curve history、forward final labels、P10 runtime/promotion blocker 与 production trade gate-zero。它必须把 strict-as-of/OOS/final-label 缺失列为**训练/晋升证据**状态，而不是误报为 AMSC source fact 入库阻断。

## 当前实施状态

- 已完成：Open-Meteo 已从 AMSC 强观测、P1、native arrival index 和 formal/live authority 中移除；四源 native catalog、source-set/缺失掩码与 source-integrity validator 已建立。
- 已完成：native-first worker 已建立；native 缺失时只尝试带 lineage 的 Open-Meteo no-live P0/P0-M fallback，并以独立历史 OOS activation gate 保护 runtime。旧无 lineage/无 strict-as-of receipt 的 Open-Meteo artifact 仍被拒绝。
- 已完成：ecCodes 已锁定；NOAA/NOMADS GFS 官方 Filter GRIB 已真实抓取、验 host、封存 raw、最近格点解码。DWD ICON 已按官方 11 原始场 + CDO grid/weights 投影后取点。ECMWF Open Data SDK 已安装在项目 runtime；2026-07-12 已用 00Z/30h 官方 server-side subset 成功采集并解码 ZUUU。ECMWF SDK 没有 response-header arrival hook，故该源是 Tier-C live 弱预测，不能被伪称 Tier-A strict-as-of 训练数据。
- 已完成：JMA GSM collector 已按授权交付合同实现；合同未配置时只产生 `jma_authorized_delivery_not_configured` 缺失状态，不猜 URL、不走第三方、不以其他模型补位。
- 已完成：live prewarm snapshot 已作为现有 monitor daemon 的独立低频 child 接入；2026-07-12 ECMWF 00Z/30h anchor snapshot 已写入 live root，并发布只含 `ecmwf` 的 fresh generation，证明缺源组合通过 availability mask 进入唯一弱似然主模型，而非整条后验停摆。
- 已完成：已批准预校准 baseline artifact 已升级并物化为 `data/runtime/joint_forecast_weak_likelihood/approved/joint_weak_operational_baseline_v3.json`；它以 exact source-set/context template 处理正式源组合并显式降低缺源信息，不读取 D0 实况或旧 v3.1/Open-Meteo 系数。v3 将逐源无样本 residual distribution、源间情景分歧与联合置信度分离，峰形不再被人为强制；v1/v2 已退出默认 runtime 选择，仅保留既有 P0 的不可变 as-of 血缘。
- 待完成：live prewarm 的连续 freshness/SLO 验收、ECMWF header-arrival transport、JMA 已批准交付 host/凭据和 catalog allowlist、官方源组合 strict-as-of 训练覆盖与 artifact approval。P1 Tier-A forecast + closed X label + forecast-only context 的事件组装入口已就绪，但真实 final-label 覆盖尚未达到训练/OOS门槛。
- 明确不做：页面爬虫、模型转发源伪装、未获批准 artifact 的参数 fallback、旧系数同名字段映射。允许的 Open-Meteo fallback 仅是有界的 historical strict-OOS no-live P0/P0-M candidate；不能绕过 native-first、OOS、P1 或 utility 合同。
