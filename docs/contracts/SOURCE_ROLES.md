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

> **当前 target SSOT 覆盖（2026-08-02）**：`bayes_basket_target` 的 45 个正式站点统一使用官方 AWC 整点 `:00` METAR 与有效 SPECI；AMSC 及旧 AMSC lineage 仅保留 `retired_history_only` 离线审计/对照，不得进入正式 truth、P1、篮子或生产 no-live OOS。下文仍保留历史来源合同的字段说明，但凡与本覆盖条款冲突，以本条和 `AGENTS.md` 为准。

| 来源 | 角色 | 禁止事项 |
|---|---|---|
| 历史结算/气候/地形 | 物理 `X`、结算 `S`、市场桶 `B` 的先验/校准/标签桥 | 作为当日实时似然或混淆标签域 |
| ECMWF / ICON / GFS / JMA 官方数值模式 | 四个相互独立的弱预测输入：ECMWF 官方 Open Data/授权交付、DWD ICON Open Data、NOAA NOMADS GFS、JMA 官方/授权交付；每次 generation 显式冻结可用集合与缺失掩码，交给唯一弱似然主模型的对应组合校准。若同一站点×事件日的官方原生对象缺失、无效或未及时到达，可用带明确 `aggregator_republisher` lineage 的 Open-Meteo no-live fallback，但必须绑定已闭日 strict-as-of AWC truth 的历史激活证据 | 将 Open-Meteo 或其他 republisher 当作 native evidence、native arrival、P1、formal/live authority 或未来/forward OOS；用旧缓存伪造缺失源；把四源当事实或重复乘入 posterior |
| TAF / WU / TWC | 各自独立的预测弱信息；必须保留发布/抓取身份与 source family，按已批准的联合弱似然模型处理 | 伪装为 ECMWF/ICON/GFS/JMA 的官方数值模式；抬升硬下界或最终锁桶 |
| AMSC METAR/SPECI | `retired_history_only` 离线审计/对照；不得进入当前 target formal truth | 写入 realtime truth、P1、篮子、production no-live OOS 或订单 |
| AWC METAR/SPECI | 45 站统一正式事实；整点 `:00` METAR 与有效 SPECI 进入 realtime truth、P1、篮子硬事实、校准和历史 strict-OOS | 无 valid_time/first-seen/raw lineage 放行；跨报告类型去重；AWC 缺失、陈旧、重复或未复核更正时必须 fail closed |
| AMSC ZUUU `METAR` `:00/:30` | 仅保留为历史/对照证据 | 用其覆盖 AWC formal truth、写入当前 P1/篮子或伪装为 AWC lineage |
| Wunderground ZUUU daily history | 已登记最终结算标量 `S`：同日最终 daily-high scalar；只用于最终标签收集 | 用 current 页面、TWC 或无 immutable raw/finality attestation 的值替代；在事件未解析前伪造 settlement_actual |
| WU/TWC current、TWC history | 诊断/交叉检查或预测弱信息 | 作为已登记 `S`、硬源或 final 标签 |
| Polymarket Gamma raw rule snapshot | L0 target contract 的不可变原文；只为 slug/日期/resolution source/单位/舍入/桶边界提供 provenance | 读取或解析 outcome、价格、深度、成交、仓位、P&L；写天气 posterior 或订单 |
| Polymarket Gamma resolved-state raw snapshot | finality attestation 的原文：只证明 event 已 closed 且所有 bucket 市场为 resolved | 读取 winning outcome、outcome price、成交/深度/仓位/P&L；把 capture 时刻冒充链上结算时刻 |
| market bucket contract | 由已登记 `S` 推导市场桶 `B`；`or below`/`or higher` 保持显式无界 | 由 AMSC `X`、实时 METAR 或市场价格直接锁桶；用任意温度截断极端桶 |
| 市场/CLOB | 价格、深度、成交与执行 | 进入天气 posterior |

同一物理来源经不同供应商转发时必须按 source family 去重。freshness/as-of 缺失即不可用。

## 数值模式退役与接入边界

`Open-Meteo` 是**显式 republisher fallback**。官方原生数据永远优先；同一站点×事件日的原生对象缺失、无效或未及时到达时，允许用带 `source_role=aggregator_republisher`、`authority_scope=PRODUCTION_NO_LIVE_CANDIDATE`、`native_source_unavailable=true` 的 Open-Meteo 包作为 no-live P0/P0-M 候选，但必须绑定已激活的独立历史 strict-as-of OOS 证据。对已闭日，只有显式 `historical_oos_eligible=1` 且通过版本化历史合同的行才可进入历史 calibration/OOS；未来/forward 行一律 `observational_candidate_not_oos`，不计入任何 OOS 门。Open-Meteo 永远不得创建 native arrival/native run initialization、进入 P1、formal/live authority 或下单；官方包恢复后按 native 优先替换 fallback。Windy MultiModel 页面同样不是可抓取数据接口；即使后续取得其 Point Forecast 商业授权，也只能登记为独立 `windy_point_forecast` 预测供应商，不能还原、冒充或给 ECMWF/ICON/GFS/JMA 再加一次权重。

官方四源 collector 必须各自保留 raw transport、请求/响应时间、run/valid-time、GRIB decoder 与规范化版本；预热器没有网络权限，只校验并原子发布已有源的 generation 及缺失掩码。缺任一预测源不能延迟 AMSC 事实或必然停止 posterior：唯一弱似然模型必须以完整的 `U={ECMWF,ICON,GFS,JMA,TAF,WU,TWC}`、可用集合 `A` 与缺失集合 `M=U\\A` 选择**精确组合** surface；每个获批组合各自拥有校准、误差分布、相关性与信息置信度参数。Open-Meteo fallback 必须经过 native-first 选择和闭日 AWC truth 校验；它不能成为 native/live formal replacement，但在已激活历史 OOS 证据下可作为 no-live P0 fallback。`A=∅` 仅在 artifact 具有获批 `empty` surface 时输出中性弱似然（信息置信度为零），使 prior+strong 可继续更新；没有精确 surface、输入血缘无效或唯一主模型不可用才关闭 posterior。信息不足只能降为 posterior 置信度/仓位能力，绝不能拿旧缓存补位。当前 P0 使用已批准、不可变的 `joint_weak_operational_baseline_v3`；trained artifact 只能经独立版本晋级整体替换该基线，不得热换或字段名映射。

ZUUU 最终 `X/S` 的唯一正确 shadow 登记为 `config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json`，且 `authority_state=accepted_shadow_only`。它冻结 AMSC 44 槽日高口径，以及 Wunderground ZUUU 站点历史观测 API 的 `units=m + startDate=endDate=目标日` 请求合同。S 必须从不可变原始 JSON 中按 `valid_time_gmt → Asia/Shanghai` 筛选后取 `temp` 最大值；v3 渲染 HTML 及其中任何未来预报数组已退役。原始 JSON、脱敏请求 URL、解析器版本、market-rule snapshot 与 finality lineage 缺一不可；bridge 未闭合时仍不授权 production bucket posterior、decision 或 order。

AMSC physical-X capture 仅作为 `retired_history_only` station-day 审计通道保留；它不得向当前 target 写入正式 X/P1/篮子事实。当前正式 AWC truth 的 station-day 物化仍独立于 market finality。每个 target 的 market-rule raw object 则独立归档到 `data/shadow/market_rule_raw`，由 `market_rule_target_contract_parser` 只导出规则语义并 append 到 `data/shadow/resolution_final_labels.db` 的 `resolution_target_contracts`。`target_rule_shadow_capture_runtime` 只读 production discovery 的 target identity，并只接受 registry 初始化 watermark 之后首次发现的市场，禁止历史回填。`market_finality_shadow_capture_runtime` 只对同一 target 的 Gamma 原文检查全桶 resolved；finality 只授权 settlement-S capture 与后续同日 X/S pair join，绝不控制 AWC truth 的生成。所有 stage 只能由唯一 `system/com.weather.monitor.daemon` 的受限 child 调度；不得另设 LaunchDaemon。该 registry 是最终态 attestation 的前置血缘，不是市场结算结果或任何 production 交易输入。

对 primary AMSC `METAR/SPECI`，强观测资格还要求 raw object、签名 poll manifest、source/station/report-type/valid-time/raw hash 与 immutable `first_seen_at` 全部闭合；`first_transport_seen_at` 与 `first_actionable_seen_at` 均取该首见时间。AMSC 重抓同一 raw object 时，attach 后的 `as_of_eligible_at` 必须复用该 immutable first-seen；当前 poll 的 `fetched_at/response_completed_at` 不得制造新到达。AWC 只能 confirmation/substitute，不能重建 AMSC 的 T1。首见/as-of 不闭合的历史行只 quarantine，不以当前时间回填。

### AMSC SPECI 历史入口（当前 target 已退役）

以下流程只适用于离线历史审计/回放，不构成当前 target 的实时 truth 或生产入口。当前正式报文入口统一为 AWC；AMSC raw 可保留但不得写入当前 realtime truth、P1、篮子或订单。任何历史 AMSC replay 仍必须保留 raw、arrival 和修订 lineage，缺失即 quarantine。

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
