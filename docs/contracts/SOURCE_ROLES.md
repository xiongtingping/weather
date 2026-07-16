---
status: accepted
authority: L1
owner: weather-system
effective_from: 2026-07-12
supersedes: [WEATHER_DATA_SOURCE_CHARTER.md]
implementation_symbols:
  - weather_source_acquisition_manager.py
  - zuuu_metar_reporting_schedule.py
  - amsc_historical_backfill_archive.py
  - amsc_historical_physical_x_samples.py
  - weather_fact_snapshot_writer.py:_persist_source_observations_for_fact_payload
  - temperature_prior_runtime.py
  - forecast_weak_likelihood_runtime.py
  - metar_strong_likelihood_runtime.py
  - resolution_authority_contract_runtime.py:validate_resolution_authority_contract
  - resolution_final_source_runtime.py:build_amsc_physical_final_source_observation
  - resolution_final_source_runtime.py:build_wunderground_settlement_final_source_observation
  - resolution_final_source_shadow_writer.py:persist_final_source_observation_shadow
  - wunderground_final_raw_archive.py:fetch_and_archive_wunderground_daily_raw_capture
  - market_rule_raw_archive.py:archive_market_rule_raw_snapshot
  - market_rule_target_contract_parser.py:parse_market_rule_target_contract_extraction
  - resolution_target_contract_shadow_writer.py:persist_resolution_target_contract_shadow
verification_gates:
  - python3 scripts/source_acquisition_contract_audit.py
  - python3 scripts/prior_layer_contract_audit.py
  - python3 scripts/amsc_historical_backfill_audit.py --event-date YYYY-MM-DD
last_verified_commit: working-tree-2026-07-11
---

# 数据源角色合同

| 来源 | 角色 | 禁止事项 |
|---|---|---|
| 历史结算/气候/地形 | 物理 `X`、结算 `S`、市场桶 `B` 的先验/校准/标签桥 | 作为当日实时似然或混淆标签域 |
| ECMWF / ICON / GFS / JMA 官方数值模式 | 四个相互独立的弱预测输入：ECMWF 官方 Open Data/授权交付、DWD ICON Open Data、NOAA NOMADS GFS、JMA 官方/授权交付；每次 generation 显式冻结可用集合与缺失掩码，交给唯一弱似然主模型的对应组合校准 | Open-Meteo、Windy 页面或其他聚合/转发数据；用旧缓存伪造缺失源；把四源当事实或重复乘入 posterior |
| TAF / WU / TWC | 各自独立的预测弱信息；必须保留发布/抓取身份与 source family，按已批准的联合弱似然模型处理 | 伪装为 ECMWF/ICON/GFS/JMA 的官方数值模式；抬升硬下界或最终锁桶 |
| AMSC METAR/SPECI | 唯一实时事实观测；`report_type` 必须保留为事件身份的一部分。scheduled METAR 提供 X 硬下界，SPECI 只提供实时强证据 | 无 valid_time/first-seen/raw lineage 放行；跨报告类型去重；让 SPECI 填 scheduled slot |
| AWC METAR | 整点 reference/confirmation；AMSC 暂缺时只按明确 substitute 合同保留诊断关系 | 覆盖 AMSC、建立第二 canonical producer、写 posterior/decision/order |
| AMSC ZUUU `METAR` `:00/:30` | 已登记最终物理 `X`：目标 resolution-local-date 内的报文温度最大值；只用于最终标签收集 | 用 SPECI/AWC/current 替代；把该报文定义的 `X` 误称为连续潜在站点最高温；在日窗关闭前写 final |
| Wunderground ZUUU daily history | 已登记最终结算标量 `S`：同日最终 daily-high scalar；只用于最终标签收集 | 用 current 页面、TWC 或无 immutable raw/finality attestation 的值替代；在事件未解析前伪造 settlement_actual |
| WU/TWC current、TWC history | 诊断/交叉检查或预测弱信息 | 作为已登记 `S`、硬源或 final 标签 |
| Polymarket Gamma raw rule snapshot | L0 target contract 的不可变原文；只为 slug/日期/resolution source/单位/舍入/桶边界提供 provenance | 读取或解析 outcome、价格、深度、成交、仓位、P&L；写天气 posterior 或订单 |
| Polymarket Gamma resolved-state raw snapshot | finality attestation 的原文：只证明 event 已 closed 且所有 bucket 市场为 resolved | 读取 winning outcome、outcome price、成交/深度/仓位/P&L；把 capture 时刻冒充链上结算时刻 |
| market bucket contract | 由已登记 `S` 推导市场桶 `B`；`or below`/`or higher` 保持显式无界 | 由 AMSC `X`、实时 METAR 或市场价格直接锁桶；用任意温度截断极端桶 |
| 市场/CLOB | 价格、深度、成交与执行 | 进入天气 posterior |

同一物理来源经不同供应商转发时必须按 source family 去重。freshness/as-of 缺失即不可用。

## 数值模式退役与接入边界

`Open-Meteo` 已从 production 弱似然链**完全退役**：不再采集、不再预热、不再作为 live bundle 或 artifact 输入，也不允许作为缺源 fallback。历史 Open-Meteo 文件只能留在隔离的 legacy/research 证据域，不能被新 artifact、live posterior、决策或订单读取。Windy MultiModel 页面同样不是可抓取数据接口；即使后续取得其 Point Forecast 商业授权，也只能登记为独立 `windy_point_forecast` 预测供应商，不能还原、冒充或给 ECMWF/ICON/GFS/JMA 再加一次权重。

官方四源 collector 必须各自保留 raw transport、请求/响应时间、run/valid-time、GRIB decoder 与规范化版本；预热器没有网络权限，只校验并原子发布已有源的 generation 及缺失掩码。缺任一预测源不能延迟 AMSC 事实或必然停止 posterior：唯一弱似然模型必须以完整的 `U={ECMWF,ICON,GFS,JMA,TAF,WU,TWC}`、可用集合 `A` 与缺失集合 `M=U\\A` 选择**精确组合** surface；每个获批组合各自拥有校准、误差分布、相关性与信息置信度参数。`A=∅` 仅在 artifact 具有获批 `empty` surface 时输出中性弱似然（信息置信度为零），使 prior+strong 可继续更新；没有精确 surface、输入血缘无效或唯一主模型不可用才关闭 posterior。信息不足只能降为 posterior 置信度/仓位能力，绝不能拿旧缓存补位。当前 P0 使用已批准、不可变的 `joint_weak_operational_baseline_v3`；它显式表达缺源和低信息量，不读取旧 Open-Meteo/v3.1 系数。trained artifact 只能经独立版本晋级整体替换该基线，不得热换或字段名映射。

ZUUU 最终 `X/S` 的唯一正确 shadow 登记为 `config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json`，且 `authority_state=accepted_shadow_only`。它冻结 AMSC 44 槽日高口径，以及 Wunderground ZUUU 站点历史观测 API 的 `units=m + startDate=endDate=目标日` 请求合同。S 必须从不可变原始 JSON 中按 `valid_time_gmt → Asia/Shanghai` 筛选后取 `temp` 最大值；v3 渲染 HTML 及其中任何未来预报数组已退役。原始 JSON、脱敏请求 URL、解析器版本、market-rule snapshot 与 finality lineage 缺一不可；bridge 未闭合时仍不授权 production bucket posterior、decision 或 order。

AMSC physical-X capture 是 station-day 通道：只要 ZUUU 当日 44 槽 coverage 完整，即独立追加 X，不读取 target、market finality 或 Wunderground。每个 target 的 market-rule raw object则独立归档到 `data/shadow/market_rule_raw`，由 `market_rule_target_contract_parser` 只导出规则语义并 append 到 `data/shadow/resolution_final_labels.db` 的 `resolution_target_contracts`。`target_rule_shadow_capture_runtime` 只读 production discovery 的 target identity，并只接受 registry 初始化 watermark 之后首次发现的市场，禁止历史回填。`market_finality_shadow_capture_runtime` 只对同一 target 的 Gamma 原文检查全桶 resolved；finality 只授权 settlement-S capture 与后续同日 X/S pair join，绝不控制 X 的生成。所有 stage 只能由唯一 `system/com.weather.monitor.daemon` 的受限 child 调度；不得另设 LaunchDaemon。该 registry 是最终态 attestation 的前置血缘，不是市场结算结果或任何 production 交易输入。

对 primary AMSC `METAR/SPECI`，强观测资格还要求 raw object、签名 poll manifest、source/station/report-type/valid-time/raw hash 与 immutable `first_seen_at` 全部闭合；`first_transport_seen_at` 与 `first_actionable_seen_at` 均取该首见时间。AMSC 重抓同一 raw object 时，attach 后的 `as_of_eligible_at` 必须复用该 immutable first-seen；当前 poll 的 `fetched_at/response_completed_at` 不得制造新到达。AWC 只能 confirmation/substitute，不能重建 AMSC 的 T1。首见/as-of 不闭合的历史行只 quarantine，不以当前时间回填。

### AMSC SPECI 实时入口

唯一 `system/com.weather.monitor.daemon` 每分钟捕获新的 AMSC SPECI。每条新报文必须按顺序完成：`raw archive → SourceObservation → CanonicalObservationEvent → reported-X v5 cumulative strong update → Transaction B SSOT`。该入口不使用通用 task queue、不读市场、不写交易决策或订单；activation/manifest/弱预测 identity 不闭合时 canonical 事实保留且 posterior fail closed，修复后重放。`termination_risk` 是同一强似然剩余升温/降温状态混合的边际读数，专用于加速更新温度动量、轨迹与物理可达性，而非额外似然或交易信号。常规 `METAR` 仍是 final-X 44 槽 schedule 的唯一输入，SPECI 永远不能改变该日终定义。

AMSC 报文检索界面已确认支持按 UTC 时间段、机场和报文类型（含 SA）查询。因此，历史查询结果可在保留检索条件、原始响应、解析版本、报文 `valid_time` 与下载时刻后，进入独立的 **官方历史回补（H）** 数据集。完整 44 槽 H 日可只读构成观测定义的物理 `X` 与温度轨迹/动量**辅助研究样本**；它可服务结构学习、特征设计和离线校准探索，但因没有原始 `first_actionable_seen_at`，不得直接进入 strict-as-of/OOS 合格的 strong-LR artifact 训练或版本批准。H 的下载时刻只说明回补时刻，绝不等同于报文当时的 `first_seen_at`、`first_actionable_seen_at` 或实时可用性；H 不得写入 canonical T1、不得作为 P10 3 秒时延证据，也不得改变任何 live 交易输入。若某日不能满足计划日窗、报文类型、原文身份与完整性合同，则该日显式 NA，不得以邻站、AWC 或插值补齐。

## Forecast strict-as-of 到达分级

预测弱信息的角色不等于可训练证据资格。Tier A 只能由 immutable raw envelope 中的真实 transport 事实建立：response headers 已到达时记录的 `response_headers_observed=true`，以及绑定的 request/header-start/complete、raw body/hash、source/target。body-complete、provider 自报时间、文件 mtime、派生指标和调用方声明都不能升级 Tier A。缺该事实的 live forecast 为 Tier C；历史 provider declared availability 为 Tier B。Tier B/C 可服务覆盖审计或明确的 auxiliary research，但永远不能作为 strict-as-of training/OOS、动态 source-weight 或 strong-artifact 的输入。

## Source identity 与追加语义

`source_observation_id` 是归一化后的**物理报文身份**；相同报文被再次抓取时保持
不变。`source_event_id` 是一次**运输到达身份**，包含抓取时刻，因此同一物理报文的
后续抓取会有不同 id。`source_observations` 只保留首个已持久化物理报文及其 first-seen
证据，不能被后到抓取覆盖，也不是完整的运输到达日志。

因此，`source_observation_id` 相同但 `source_event_id` 不同，且物理字段（来源、站点、
有效时刻、温湿、raw hash/ref、角色与质量标记）完全一致时，必须作为运输重复幂等忽略；
仅运输时间的变化不得阻断事实写入。相同 event identity 的任何不可变字段冲突，或相同
物理 identity 的物理字段冲突，必须 fail closed，绝不覆盖既有首到达记录。
