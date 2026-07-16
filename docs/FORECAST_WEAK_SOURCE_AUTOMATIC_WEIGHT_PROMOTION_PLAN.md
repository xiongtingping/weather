---
status: research
authority: L3
owner: weather-research
effective_from: 2026-07-11
---
# 预测弱似然多源权重自动晋升与 AMSC 曲线淘汰开发方案

状态：外部 forward 采集与每日自动研究链已实现；生产应用保持关闭，等待严格 OOS 晋级。

## 1. 目标与边界

目标是在天气预测弱似然层内，对 ECMWF、GFS、ICON、TAF、WU、TWC 等来源建立可复现的自动权重体系：

1. 历史层根据已结算最高温市场的概率预测技能，学习全局、季节、提前量和天气型权重。
2. 日内层根据 AMSC 实况曲线与各来源同一有效时刻的预测曲线，平滑奖优罚劣。
3. 任一自动晋级必须同时通过严格时序 OOS 的 NLL 与 RPS 门槛。
4. hit rate 只作直观诊断，不能单独决定权重。
5. 市场价格不作为天气真值或天气模型技能证据。

该系统只重分配既有预测弱似然精度，不创建硬观测、不增加弱层总精度、不改变 METAR 强似然所有权。生产概率链仍只有一个主模型；候选不可用或未过门时 fail closed，继续使用已批准 champion。

## 2. 根因与设计原则

固定人工权重无法适应来源技能随季节、天气型和提前量变化；直接按近期命中率提权又会带来小样本噪声、桶宽偏差和追涨杀跌。另一个关键问题是来源并不独立，例如 WU 与 TWC 可能属于同一上游 provider family；若按接口数量累加，会制造虚假精度。

因此采用以下原则：

- 评分单位是完整桶概率分布，不是点温度是否命中。
- 训练与评估按 event day 分组，任何测试日只使用更早已结算日训练。
- 季节用连续 day-of-year Fourier 基函数，不设月份断点。
- 天气型用 cutoff 时已可见预测曲线的连续统计量，不使用结算结果。
- 稀疏上下文自动向父层收缩，不设温度或小时例外。
- provider family 先去重，再在 family 间和 family 内分配份额。
- 缺档是 NA，不是预测错误，不补零分、不做日期特判。

## 3. 数据分层与血缘

### 3.1 外部结算事件

从 Polymarket 外部 API 发现已结算天气事件，保留 event id、站点、当地日期、桶边界、获胜桶、settlement_actual、resolution source 和 resolved_at。最高温结算事实必须可由外部事件和解析源独立重放，不读取本地历史数据库。

### 3.2 预测快照

每个来源快照写入不可变 gzip 信封，至少包含：

- source id 与 provider family；
- request URL/参数、请求与响应时间；
- 预报 run、issued_at、保守 provider_available_at；
- station、target event date、cutoff id；
- 响应 SHA-256、parser version；
- 逐小时曲线和预测最高温。

严格 as-of 配对要求 `as_of_eligible_at <= cutoff_at`。基础 cutoff 为成都当地事件日 00:00；forward 来源另以每个不可变预测快照的首次可见时点自动生成动态 cutoff。动态 cutoff 可继承此前已可见预测，但不能使用未来快照。lead time 连续定义为 cutoff 到当地事件日结束边界的剩余小时，不设置拟合小时阈值。多个 cutoff 在训练时按事件日归一权重，避免高频来源凭更新次数放大样本权重。

### 3.2.1 Tier-A 到达时间宪章

`response_completed_at` 只证明本地已读完 body；它不证明系统在请求开始、或任意更早时已经拥有预测。因而 live forecast snapshot 只有同时满足以下条件，才可进入 strict-as-of Tier A：

1. immutable raw envelope 明确记录 `requested_at`、**响应头真实到达时**的 `response_started_at`、`response_completed_at` 与原始 provider body；
2. envelope 的 `response_headers_observed=true` 由 transport 采集器在读取 body 前写入；derived metrics、parser 或调用方不得补写或提升该标志；
3. raw body 不含 credential-shaped field，source/target/snapshot hash 绑定完整。

缺少该标志、仅有 request 前本地时钟、或仅有 body-complete 的 live 记录一律 Tier C；不可用 batch clock、provider 自报时间或文件 mtime 回填。历史 declared-availability envelope 仍可作为 Tier B coverage/auxiliary evidence，但永远不能因此提升为 Tier A。

### 3.3 来源可用性

- ECMWF/GFS/ICON：使用带明确 run identity 的 Open-Meteo Single Runs。
- TAF：官方历史窗口内使用，超窗部分明确 forward-only。
- WU：在找到权威 issued-forecast archive 前仅 forward snapshot。若页面所嵌 Weather.com API 对采集端返回 403，但同一 WU 页面含逐小时 forecast payload，则解析该页面内嵌 payload 并保留原页面响应血缘；不能把该 transport block 错记为无预报。
- TWC：需要 API 凭据和可验证历史预测档案；否则 forward-only。
- provider 返回 run unavailable 时记录显式 NA，不计入该来源技能分母。

Weather.com/TWC 的 daily maximum 是 `daily_max_event_window`：它以 provider 的 daily record valid-time 表示目标日窗口，可作为“当天最高温”弱预测标量，但 `trajectory_eligible=false`。若另有逐小时产品且与 daily maximum 不同，二者必须作为不同 forecast product/family-correlation evidence 处理；不得把逐小时曲线强行视为 daily maximum 的峰值轨迹，也不得伪造 peak time。

## 4. 概率化与评分

每个来源的点预报、成员预报或显式概率先转换到该市场真实桶 schema。桶概率归一化后计算：

- NLL：主晋级指标，惩罚对真实桶赋极低概率。
- RPS：主晋级指标，利用有序温度桶距离。
- multiclass Brier、Top-1、Top-3、区间 MAE、bias：诊断指标。

Top-1 hit rate 不具备概率校准信息，且受桶宽和边缘桶影响，因此禁止单独驱动晋级。

## 5. 历史上下文权重模型

使用正则化 contextual stacking，直接最小化真实桶 NLL。权重由分层 softmax 生成并受 source/family floor 与 cap 约束。

模型层次按以下顺序逐层消融：

1. `global`：只学习长期来源技能。
2. `seasonal_lead`：增加连续季节 Fourier 项与 lead time。
3. `weather_context`：增加 cutoff 当时已知的连续天气曲线特征。

当前天气上下文特征为：

- 多来源预测最高温均值；
- 多来源预测最高温分歧；
- 预测日较差共识；
- 从当地日开始到预测峰值的升温幅度共识。

特征只来自已通过 strict as-of 选择的预测曲线。任何字段有效样本少于 minimum ESS 时自动置为父层均值效应。优化器按目标函数稳定性通用早停，不按日期、温度或小时终止。

### 5.1 来源覆盖不得跨源借用

三大模式、TAF、WU、TWC 都是**预测弱似然来源**，但它们的 strict-as-of 资格必须分别闭合，不能因为三大模式已有完整 event day 就把其他来源一并放入动态权重。最新只读审计的 363 份 raw envelope 为 Tier A=11、Tier B=294、Tier C=58，三模型已形成 1 个完整 Tier-A event day（2026-07-12）；旧的仅有 request/start/complete 的 live capture 仍保持 Tier C，不能回填。该一日只证明新 response-header capture 合同开始产生真实样本，绝不等于季节级训练数据已闭合。TAF 已有 raw-object→first-poll-manifest 的 strict proposal bridge，但只有同时具备 raw hash、首次 receipt、真实 observed request/header-start/complete，且 first-seen 不晚于 TAF 预测峰值时才能成为 Tier-A。现有 archive 的 26 条 TAF 全缺 response-start，仍为 eligible=0；部分 archive TAF 还在目标峰值之后才首见，必须永久排除为历史 Tier-A 预测。

因此，在 TAF/WU/TWC 各自具备足量 prospective Tier-A event-day、对应 canonical observation 和 final X→S→B label 前：

- 它们可以继续作为当前预测输入或 forward shadow 的独立记录；
- 不能为它们训练 seasonal/weather/curve dynamic weight；
- 不能由三模型或 TWC 的样本替代、插补或推断其历史基础能力；
- 任何包含它们的动态 artifact 均不得 promotion/apply。

## 6. AMSC 日内曲线淘汰

日内淘汰采用 prequential 合同：对每个 AMSC 实况有效时刻，只比较在该有效时刻之前已经发布的预测值。损失由三部分平滑组成：

- 温度水平误差的 Huber loss；
- 相邻时刻斜率误差的 Huber loss；
- 是否刷新日内高点的 Brier loss。

损失经 EWMA 转成可靠度乘数。权重调整分两级：

1. 按 family 聚合成员可靠度，在 family 之间奖优罚劣；
2. 在同一 family 内按成员可靠度重新分配，防止重复接口增加总精度。

每次调整有最小份额、最大下调和最大恢复速度；缺测来源保持中性可靠度而非归零。调整前后所有来源权重之和恒为 1，弱似然总精度不变。相同 curve state id 只能消费一次。

历史 OOS 曲线晋级需要外部、可审计的 AMSC 历史实况与当时已发布预测快照。若缺少该证据，曲线层只能 forward shadow，不能借本地历史或当前结果补做过门。

AMSC 观测 checkpoint 采用原始对象的 `observed_first_seen`，且必须早于市场结算；历史回捞但在结算后首次看到的报告自动排除。WU/TWC 只在预测内容变化时写入新不可变信封，相同轮询响应不重复落盘。

## 7. 严格 OOS 与晋级状态机

滚动评估流程：

1. 至少 30 个已结算 event days 后开始第一折。
2. 每折只用测试日期之前的数据训练，默认测试窗 7 天。
3. 所有差值按 event day 聚合后 bootstrap，避免同日多来源伪增样本量。
4. challenger 的 NLL 与 RPS 点估计都必须优于 champion。
5. 置信区间不得越过伤害容忍线；天气分段和尾桶不得出现灾难性退化。
6. 30 天可进入研究 shadow；至少 80 天且统计门通过才可生成生产晋级提案。
7. 提案每次全局权重变化默认不超过 5 个百分点；更大变化分多次晋级。
8. 生成 proposal 不等于 apply。只有人工批准后的不可变 artifact 才能原子切换引用。

状态流转：`data_ready -> trained_challenger -> strict_oos -> shadow_eligible -> governed_proposal -> approved -> atomic_apply`。任一门失败回到 champion，不启用旧模型 fallback。

## 8. Artifact、审计与回滚

每个候选 artifact 包含：

- feature schema、标准化均值/尺度、系数；
- source 与 provider-family 映射；
- floor/cap、正则化和优化器配置；
- context ESS 与 fallback 状态；
- training manifest SHA-256、parent artifact SHA-256；
- trained-through settlement time 与 artifact SHA-256。

晋级包绑定 challenger、champion 和 OOS evaluation 三个哈希。回滚只需原子恢复上一批准 artifact 引用，不重训、不重算历史概率。runtime 请求永远不能自行改参数。

artifact 的“结构合法”与“可被 runtime 激活”必须分离：研究态 static champion 可以通过 schema/hash 校验，但只要 `research_only=true`、`production_ready!=true` 或 `runtime_integrated!=true`，loader 与 governed apply 都必须拒绝。active pointer 只提供审批血缘，不能在内存中把研究 artifact 重标为可运行。

## 9. 已实现模块

- 外部事件发现与覆盖规划：`scripts/polymarket_settled_weather_event_discovery.py`、`scripts/external_forecast_coverage_manifest.py`
- 有界抓取与不可变信封：`scripts/fetch_open_meteo_source_skill_runs.py`、`scripts/forecast_source_forward_snapshot.py`
- 规范化、cutoff、配对和评分：`scripts/normalize_open_meteo_source_skill_runs.py`、`scripts/build_external_forecast_source_skill_cutoffs.py`、`scripts/build_external_forecast_source_skill_dataset.py`
- 可用性 NA 审计：`scripts/audit_open_meteo_realized_coverage.py`
- 上下文训练与消融：`forecast_source_contextual_weight_model.py`、`scripts/train_forecast_source_contextual_weights.py`、`scripts/evaluate_forecast_source_contextual_ablation.py`
- AMSC 淘汰：`forecast_source_curve_tournament_runtime.py`
- forward 桥接：`scripts/export_amsc_source_skill_forward.py`、`scripts/capture_current_forecast_source_forward.py`、`scripts/normalize_forecast_source_forward_metrics.py`、`scripts/build_forecast_source_curve_shadow_inputs.py`
- OOS、release gate 与提案：`scripts/evaluate_forecast_source_weight_shadow.py`、`scripts/forecast_source_weight_release_gate.py`、`scripts/forecast_source_weight_promotion_packet.py`
- artifact 注册、应用和运行时弱似然：`forecast_source_weight_artifact_registry.py`、`scripts/forecast_source_weight_artifact_apply.py`、`forecast_weak_likelihood_runtime.py`
- 每日编排与验收：`scripts/forecast_source_weight_daily_pipeline.py`、`scripts/forecast_source_weight_daily_acceptance_audit.py`、`launchd/com.weather.forecast_source_weight_research.plist`
- 整点 forward 曲线：`scripts/capture_current_forecast_source_forward.py`、`launchd/com.weather.forecast_source_forward_capture.plist`、`scripts/forecast_source_forward_capture_health_audit.py`。它独立封存 ECMWF/GFS/ICON/WU/TWC 当时曲线；任何来源只有 raw body、真实 request/header-start/complete、`response_headers_observed=true` 与无 credential-shaped field 才可成为 Tier-A。Open-Meteo `urlopen`、WU inline page 和 Weather.com/TWC daily fetch 都以 streamed transport 在 headers 返回、body 尚未读取时写入该事实；direct-proxy session 在 body 读取后显式关闭。WU direct page 不可用/不安全时仅回退 forward-only proxy。该实现只为**之后的新 immutable envelope**提供合格证据，既有 348 份旧 envelope 不可回填；单一来源暂时不可用只记 NA，不阻断其他来源，也不影响生产链。health audit 只证明 collector 当前健康，不能替代 strict-as-of 训练覆盖。
- 日常验收同时核对仓库定义和 `/Library/LaunchDaemons` 的实际安装配置；缺少任一研究任务、或已安装 monitor 未启用 forward archive，审计必须报错，不能以“配置文件存在”冒充运行态已生效。

## 10. 2026-07-11 真实研究执行记录

- 外部已结算市场事件：110 天。
- 计划并执行 Open-Meteo exact-run 请求：306。
- 成功写入不可变信封：291。
- provider HTTP 400 缺档：15，全部显式 NA。
- 至少一个来源严格配对：98 天。
- ECMWF/GFS/ICON 同 event/cutoff 三模型完整配对：95 天。
- 可评分来源行：299（ECMWF 98、GFS 99、ICON 100、TAF 2），全部有 immutable 血缘；严格配对 98 天、100 个 event/cutoff 样本。
- 第一轮完整上下文 challenger 的滚动 OOS：68 天、70 个预测样本。相对生产 legacy champion，NLL 点估计改善 3.63%、RPS 改善 2.11%，但 95% CI 分别为 `[-0.1333, 0.0010]`、`[-0.0088, 0.0051]`，均跨过伤害线，故不晋级。
- 顺序消融：global 的 NLL 改善 4.02%（95% CI 完全为负），RPS 改善 3.78% 但 CI 跨零；seasonal/lead 相对 global 的 NLL/RPS 分别退化 0.87%/1.46%；weather context 相对 seasonal/lead 的 NLL 改善 0.46%，RPS 退化 0.26%。最终 specification 为 `static_champion`。
- AMSC 曲线只有 1 个外部可审计 event day / 32 个 checkpoint；weather challenger 的曲线 NLL/RPS 分别退化 0.48%/1.14%，且样本量远低于门槛，曲线晋级保持关闭。整点 forward 采集已开始积累后续 shadow 证据。
- 当时系统级运行态曾验证：`com.weather.monitor.daemon` 已启用 `FORECAST_SOURCE_FORWARD_ARCHIVE_ENABLED=1`，`com.weather.forecast_source_forward_capture`（每整点）与 `com.weather.forecast_source_weight_research`（每日 02:30）均已安装。首个整点任务已完成三模型与 TWC 的封存，WU 当次缺峰值以 NA 留痕。其后仓库 capture plist 从登录 shell `zsh -lc` 收紧为非登录 shell `zsh -c`；2026-07-12 后续只读 acceptance 已确认 source/installed 合同重新一致，system service 已加载为整点 event trigger、上次退出码为 0。该结论只证明采集定义和安装合同一致；在新增 response-header Tier-A 宪章后，这些旧封存均不构成 strict-as-of 样本覆盖或权重 release gate。
- WU forward 兼容性复核：其页面内嵌 Weather.com API 在采集端返回 403，但 WU 页面本身包含逐小时 forecast payload。解析回退后，2026-07-12 已成功封存完整 WU 曲线（峰值 35.56°C）；该来源仍归入 `twc_provider` family，不会与 TWC 叠加成虚假独立精度。该封存目前缺 header-arrival evidence，故为 Tier C，而非可训练 Tier A。

上述失败是有效研究结论，不通过调温度阈值、挑日期或放宽统计门修饰。

## 11. Definition of Done

工程完成：

- 外部数据、不可变血缘、strict as-of、显式 NA、provider-family 去重均有自动测试；
- 上下文训练、滚动 OOS、消融、curve tournament、release gate、proposal、rollback 均可独立重放；
- 所有研究写入限制在 `data/research/forecast_source_skill`；
- 不写 `archive.db`、`facts.sqlite`、信号、订单或 launchd。

统计完成：

- 某一逐层消融 specification 同时通过 OOS NLL/RPS 与分段保护门；
- 若启用 AMSC 曲线晋级，必须另有曲线层增量 OOS 证据；
- 至少 80 个生产提案样本日；
- artifact 与 promotion packet 哈希闭环。

生产完成：

- 人工明确批准具体 artifact SHA-256；
- 原子切换、验收、7/30/90 天监测和回滚演练全部通过。

在统计门通过以前，系统升级的正确结果是“研究链可运行、候选被阻断”，而不是强行改变生产权重。
