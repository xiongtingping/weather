---
status: research
authority: L2
owner: basket-target
effective_from: 2026-07-24
---

# Bayes × Basket 目标架构：Production No-Live 编译层

状态：`TARGET_PRODUCTION_NO_LIVE`；生产订单授权：`false`；独立 target SSOT 已初始化，resident live 服务未切换。

该实现是目标生产语义的只读编译与证据边界，保留完整的 SSOT、lineage、现金阶段和 fail-closed 合同；target DB 仅承载独立 no-live schema/activation/capital epoch，不提交订单、不调用 gateway、不增加生产服务。

```text
CurrentAsOfModelRun
  → strict-as-of + promoted CalibrationActivation
  → ModelCalibrationArtifact + ModelEnsembleArtifact
  → P0-M ProbabilityEnvelope（完整桶向量 + lineage）
  → StationBasketPolicyActivation（target/reserve/cadence）
  → TargetBasketPlan（zero-order production_no_live）
  → BasketCashPhasePolicyArtifact
  → BasketCashPhaseDecision + OrderSizingDecision
  → production_no_live evidence capture
```

## 模型时间基准

- calibration 按固定 `lead_bucket`（当前正式为 `d1`）和 `run_cycle` 分层；`d1`、`d2` 不混训。
- P0-M 选择决策时刻 `decision_at_utc` 之前最新的完整共享模型 run；三模型必须共享同一 `run_init_utc`，不能按各模型的最新报文拼接。
- 每个模型的 `published_at_utc`/`received_at_utc` 只作为该模型 strict-as-of 成熟证据；共享 bundle 的成熟边界为各模型最新 `received_at_utc` 的最大值。
- P0 的 `decision_at_utc` 是概率实际物化时刻，不等于任一模型发布时间；上述 run、bundle 成熟边界和决策时刻全部写入 lineage。

## 双风险层、赔率曲线与动态选桶（v1）

### 1. 风险语义

篮子优化使用两个彼此独立、可审计的风险 overlay；overlay 只影响候选桶、排序、预算和组合覆盖价值，不修改已物化的 P0/P1 概率、regime、硬 NO 证明或官方事实。

- `underreach_risk`（未达成风险）：P1 主桶可能达不到。设 `M=argmax(P1)`、`R=official_reported_high`、`G=max(0, M-R)`；当当前最高温长期低于 `M`、距 ET 截止时间缩短、最近升温速度不足、或校准后三模型的剩余升温包络不足以填平 `G` 时，风险升高。若 `M=30℃`、`R=25℃`，候选下方尾部是 `D1=29℃`、`D2=28℃`；这不是“实况从 30℃跌到 29℃”，也不把当前 25℃的下一个桶 26℃误判为上行风险。
- `upside_breakthrough_risk`（上行突破风险）：官方实况形成新的已报最高温后，才将 `U1=successor(R)` 纳入突破风险；只有上行剩余时间、校准后三模型上尾质量和盘口深度均支持时，才扩展 `U2=successor(U1)`。例如官方实况突破 31℃后，`U1=32℃`，必要时 `U2=33℃`。没有官方新高事件时，不凭 P1 或普通预报单独制造突破事件。

当 `R` 是当日已报最高温时，`R` 是事实下界/进度，不是未达成尾部桶。它允许 29℃、28℃继续作为最终结果候选，但不会单独证明 29℃或 28℃一定发生。若官方硬事实已经数学排除某桶，该桶先于任何 overlay 被剔除。

### 2. 风险压力

`τ` 为距 ET 日界的剩余时间，`E_rem` 为校准后三模型在 `τ` 内可实现的剩余升温包络；`E_rem` 必须绑定 strict-as-of、模型组合 artifact 和证据新鲜度，不得由策略层重新训练或读取盘口生成。

```text
shortfall_gap      = max(0, M - R)
shortfall_pressure = f(shortfall_gap - E_rem, τ, warming_slope, model_agreement, evidence_freshness)
breakthrough_pressure = f(new_official_high, upper_tail_mass, τ, model_agreement, evidence_freshness)
```

`shortfall_pressure` 低时只保留 P1 基础候选；升高时按 `predecessor(M)`、`predecessor²(M)` 依次加入近端和深度下方尾部。`breakthrough_pressure` 仅在新高事件闭合后按 `successor(R)`、`successor²(R)` 依次加入上行桶。所有 successor/predecessor 都由 bucket contract 解析，禁止硬编码绝对温度或假定固定 1℃间隔。默认最多向任一方向扩展两层，避免风险扩散导致无限选桶。

### 3. 候选集与动态切换

```text
BaseSet       = P1 中通过 lineage、硬事实、盘口和预算门的候选桶
DownsideSet   = {predecessor(M), predecessor²(M)}  when underreach_risk is active
UpsideSet     = {successor(R), successor²(R)}       when upside_breakthrough_risk is active
CandidateSet  = BaseSet ∪ DownsideSet ∪ UpsideSet
```

每次新的官方事实、ET 剩余时间跨过风险阈值、模型组合更新或 WS 快照变更时，重新计算风险状态和候选集。新事实不会把旧风险事件伪装成新报文；同温、旧温和 reference-only 报文不触发动态选桶。若两类风险同时很高，允许扩大候选集但必须降低单桶集中度并收紧盘口/证据门；无法证明组合收益改善时保持 `fail closed`，不得用扩大候选桶替代证据。

### 4. 赔率曲线与篮子级目标

赔率曲线按官方 CLOB WS 的逐档 YES 卖盘计算 `all_in_yes_ask(b, q)`，不使用单一 best ask、REST 或历史 tick。风险项不直接改写概率，而是计算新增一桶对整个篮子的情景覆盖价值：

```text
BasketScore(B)
  = P1ExpectedValue(B)
  + λ_shortfall    × ShortfallCoverage(B)
  + λ_breakthrough × BreakthroughCoverage(B)
  - λ_depth        × SlippageRisk(B)
  - λ_concentration× ConcentrationRisk(B)
  - λ_revision     × RevisionRisk(B)
  - TotalCost(B)
```

其中 `ShortfallCoverage` 衡量下方近端/深度桶对“P1 主桶未达成”情景的增量覆盖，`BreakthroughCoverage` 衡量官方新高后的下一/再下一桶对上行突破情景的增量覆盖；29℃/28℃、32℃/33℃和31℃均只是整数桶说明性示例。它们是组合价值，不是新的概率；`P1` 缺失时才按既定规则回退 P0，不能由 overlay 合成概率。

每个桶同时记录风险调整的 `reservation_value` 和真实逐档 `all_in_yes_ask`。只有新增桶的边际组合价值为正、盘口深度可验证、预算充足且满足 `sum(all_in_yes_ask) < 1` 时，才可纳入当前阶段；未成交挂单不计入已实现地板，成交后必须按实际成交差额重算。选桶仍遵守既定 4→3→2 staged 规则、逐腿 FOK、持仓到结算和 reconcile-required 阻断。

### 5. 典型情景

```text
P1 主桶 M = 30℃
官方已报最高温 R = 25℃
距 ET 日界只剩 2 小时
剩余预计升温 E_rem ≈ 1.5℃
```

`shortfall_gap=5℃` 且剩余包络不足，`underreach_risk` 高：30℃仍是 P1 参考中心，但降低过度集中；29℃为第一下方尾部，28℃为第二下方尾部，按赔率和覆盖价值择优加入。此时没有官方新高事件，不加入 26℃作为上行突破桶。

若随后官方实况形成新的已报最高桶，则低于该事实下界的桶在 resolution contract 下若已被硬排除，立即剔除；新的事实桶成为锚点，`successor(R)`成为第一上行突破桶，`successor²(R)`只有在上尾质量和盘口支持时加入。31℃→32℃→33℃仅是说明性示例。任何 overlay 都不能解除硬 NO，也不能改变 `production_no_live` 或 `live_submit_authorized=false`。旧线 `019f7a84-53ec-7183-9c5f-7f9cfa62e1f0` 已退役，不再是运行或 readiness 依赖。

实现位置：

- `bayes_basket_target/`：合同、校准、stage 路由、站点策略、目标篮子内核、SSOT、production-no-live evidence。
- `bayes_basket_target/database.py` + `scripts/initialize_bayes_basket_target_ssot.py`：独立 target DB 边界；显式指向 resident live `archive.db` 直接拒绝。
- `bayes_basket_target/market_registry.py` + `scripts/export_bayes_basket_target_market_registry.py` + `scripts/ingest_bayes_basket_target_market_registry.py`：共享 registry 仅作一次只读、内容寻址导出；target runtime 只读取追加式 `basket_target_market_registry_snapshot`，不回读共享 live 库。
- `config/trading/bayes_basket_target_production_no_live_activation_v1.json`：目标 lane 激活合同。
- `scripts/accept_delta_basket_score_v2_no_live.py`：真实 SSOT 的 v2 no-live 有界验收。
- `scripts/bayes_basket_target_resource_audit.py`：只读核对 target raw lineage、无引用对象、临时残片、进程 RSS 与 DB/WAL/磁盘预算；不执行清理。
- `config/bayes_basket_target/performance_budget_v1.json` + `bayes_basket_target/performance_budget.py`：只声明 target-owned、`bounded_read_only_audit_only` 的 P95/P99、WS 年龄、锁等待和资源上限；它不是 live/订单授权，且不能超过内存、DB/WAL、raw 与剩余磁盘硬边界。
- `config/bayes_basket_target/no_live_runtime_v1.json` + `bayes_basket_target/runtime_config.py`：声明 target-owned 的 `production_no_live` 预算，默认 `target_remaining_budget_usdc=0.0`；worker 不从 resident daemon 配置、activation `basket` 或共享 live 资本池回退取值，避免 no-live 演练隐式借用生产资金。
- `scripts/bayes_basket_target_performance_audit.py`：只读、有限样本测量 target 审计 P95/P99、读事务获取延迟与 WS 年龄；即使预算已声明，没有 target-owned writer lock-wait telemetry 或可执行 WS snapshot 时仍保持 `BLOCKED_EVIDENCE`，不把 evidence-only cache 冒充 snapshot。
- `scripts/bayes_basket_target_production_facts_baseline.py`：只读冻结 target activation、SSOT DB 与 runtime closure；共享 system 服务的 no-live 边界由服务验收独立核验，两次运行的 `facts_sha256` 必须一致。
- `bayes_basket_target/delta_v2_training.py` + `bayes_basket_target/forward_training_validation.py`：在 M5 原子事务内以 v2-native schema 将 decision、intent、唯一 exact execution vector 或显式 `NO_EXECUTION` policy 写成内容寻址 `d2fact_`；旧 strict-delta writer/fallback 已删除。
- `bayes_basket_target/readiness_scope.py`：readiness 唯一逐 scope resolver；按 active registry 的 `station_id + event_date + settlement_timezone` 复用 production compiler 精确解析 P1、官方 WS、inventory、order-state、站点策略、cash authority 和 strict-delta artifact。全局 `COUNT(*)` 只作观测，不能再授权 readiness。
- `bayes_basket_target/forward_scorecard.py`：read-only forward scorecard 公共接口；readiness 以同一 `decision_at` 生成正式 receipt，并要求非空训练事实、标签、exact execution vector、cash/station 两类 scorecard 均通过。缺样本、截断、未来泄漏或任一 blocker 都不能满足 readiness；它不写库、不晋级、不调用 gateway。
- `bayes_basket_target/promotion_evidence.py`：M8-12/M8-13 前向 scorecard 的结构化证据门；要求 target-owned、严格 as-of、真实样本覆盖、逐族/逐阶段归因与故障矩，缺文件或结构不完整即 fail closed；它只验证，不晋级、不写库。
- target basket kernel 已移除旧 source-wallet clip 金额的隐式默认；`clip_max_notional_usdc` 缺失时直接 `fail closed`，必须由显式自有 policy/cash-phase 输入绑定。定向合同回归 25 passed，target + adaptive 回归 403 passed（254 + 149）。
- `bayes_basket_target/calibration_evidence.py` + `scripts/build_bayes_basket_target_calibration_evidence.py`：从冻结 strict-as-of 文件生成残差、候选校准/组合 artifact 和拒绝晋级 receipt；缺 canonical source、published/received 时间或三模型时只保留证据，不写 SSOT activation。
- `bayes_basket_target/et_history.py` + `scripts/build_bayes_basket_target_et_history.py`：历史证据先经过固定 `America/New_York` 日界与 target registry AMSC/AWC lane ownership；旧 `Asia/Shanghai` 日界、缺显式 target timezone、修订报文、跨 lane fallback 和不完整三模型 join 均只生成 bounded `BLOCKED_EVIDENCE`，并在 `truth_missing` 上附带 ET 窗口 `not_started/open/closed` 与 UTC 起止边界；绝不重标、复制 raw 或写 target DB。当前 receipt：`output/audits/bayes_basket_target_et_history_evidence.json`。
- `scripts/rebuild_bayes_basket_target_historical_truth.py`：从官方原始报文重建历史 truth；中国站点的 AMSC 整点 `:00` `METAR` 是正式主来源，AMSC 半点 `METAR` 另写 `REFERENCE_ONLY`，针对中国站点的 AWC 整点 `METAR` 也另写独立 reference evidence。参考证据可用于交叉核验和缺口诊断，但合同固定 `formal_truth_fallback_allowed=false`、`enters_et_join=false`、`enters_calibration=false`，AWC 不得覆盖或替代 AMSC 整点 truth，也不得写入生产输入。
- `scripts/bayes_basket_target_post_et_refresh.py`：post-ET 唯一入口；目标日未闭合时只写 bounded `WAITING_FOR_ET_CLOSE` receipt，闭合后才调用既有只读 evidence refresh，避免重复轮询或提前把日内观测当作最终 truth。
- `bayes_basket_target/native_model_decode.py` + `scripts/decode_bayes_basket_target_native_model_arrivals.py`：只读解码已保留的原生 ECMWF/GFS/ICON GRIB2；ECMWF/GFS 直接用 ecCodes，ICON 使用 DWD 官方 remap 资产；raw 仅按既有 SHA 引用，不复制 payload。单点只形成 `VALID_BLOCKED_NATIVE_DECODE_EVIDENCE`，必须覆盖完整目标本地日曲线后才可作为 calibration model rows。ECMWF 采集器使用官方 `.index` 的 `2t` byte-range，并强制 HTTP `206 Content-Range` 精确匹配，避免保存约 140 MB 的全 GRIB 文件。
- `bayes_basket_target/model_arrival.py` 的 arrival index v2 以 `provider_id + station_id + target_local_date + run_initialization_utc + valid_time_utc` 定位单点；同一 native run 的多个合法 valid time 可组成曲线，同一点重复或冲突仍 fail closed。当前真实 evidence 已形成681条 arrival，其中 `2026-07-24` 为675点，覆盖45站×ECMWF/GFS/ICON×5点；按 AWC `stationinfo` 坐标注册表逐站解码后，681点全部通过，形成135条完整目标日曲线。target ET history 现在只读取 native decode receipt，不再读取 legacy Asia/Shanghai CSV；目标日尚无 canonical settlement truth exact join，因此 calibration evidence 仍 blocked，不能晋级。
- `bayes_basket_target/station_registry.py` 是 native model decode 的唯一坐标来源，保留 AWC stationinfo 快照 SHA；缺 station 坐标直接 fail closed，禁止回退到 ZUUU 或任何猜测坐标。
- calibration CLI 默认先检查 ET history receipt；当前 `et_history_evidence=BLOCKED_EVIDENCE` 时，calibration artifact/ensemble/promotion 均固定为 0，任何旧历史文件不能绕过该门。
- `scripts/bayes_basket_target_production_readiness.py` 会校验该 receipt 的 hash、候选/拒绝状态并单独展示；candidate evidence 永远不能满足 production readiness。

硬边界：

- 所有输入绑定 `decision_at`、scope、generation、有效窗和内容 hash；缺失、冲突、过期、未来时间或未知状态均 fail closed。
- target 默认 SSOT 为 `data/bayes_basket_target_ssot.db`；共享 live `data/archive.db` 不是 target 的 fallback、迁移目标或写入目标。
- 真实 M5/SSOT 迁移必须使用带有 `bayes-basket-target-m5-migration-v1` 专属 token 的暂停标记；仅存在同名 pause 文件或其他业务暂停原因不得作为维护窗口证明。
- 独立 target DB 不要求暂停 resident live writer；任何 target 激活/物化入口仍必须通过 database-scope guard。
- 盘口只来自官方 CLOB WebSocket 物化快照；target lane 不读取 REST、历史 tick 或旧候选包。
- inventory 与 order-state 必须覆盖全部桶并与同一 market snapshot 版本一致；未知/未对账状态禁止形成证据。
- cash phase 只提供 `C_base`；不得改变概率、中心、方向、支持集、目标库存或排序。份数严格由量化后的 `C_order / r_limit` 产生。
- 资源与留存是 target-only 硬边界：单次 JSON/输入、进程 RSS、DB/WAL、raw aggregate 和剩余磁盘均先验限额；原生 raw 只在 receipt lineage 引用时保留，死 PID 临时残片和超过宽限期的无引用 content-addressed 对象由 target collector 清理，禁止外置盘、共享 live DB 或无界旧数据作为依赖。
- target market registry 当前已物化 1 个快照、5,214 行；其中 1,783 行为来源合格且可交易记录，target runtime 缺快照或快照损坏直接 fail closed。
- 结果固定 `execution_mode=production_no_live`、`target_only=true`、`orders_submitted=0`、`writes_production_db=false`、`calls_order_gateway=false`。
- 该目标 lane 只能作为 `reported_trajectory_no` 内部无下单 worker 的候选输入，不形成第四个服务，也不触碰另一条 live 线。
- readiness/release 的 forward scorecard 是正式硬门；缺训练样本、标签、向量或任一 cash/station 结构证据时保持 `BLOCKED_NOT_RELEASE`。即使 scorecard 验证通过也只产生 `EVIDENCE_COMPLETE_NO_LIVE_REVIEW`，不改变 no-live activation 或共享 system 服务。

验收入口：

```bash
./.venv/bin/python scripts/accept_delta_basket_score_v2_no_live.py \
  --db data/bayes_basket_target_ssot.db \
  --allow-waiting-input
./.venv/bin/python scripts/audit_cash_phase_promotion_gate.py
./.venv/bin/python scripts/bayes_basket_target_official_ws_dependency_audit.py
```

当前 release 仍为 `BLOCKED_NOT_RELEASE`：独立 target M5/SSOT、no-live activation、零资本 epoch 和 target-local market registry 已安装并对齐；目标日45站三模型 native 曲线已到达（675点/135条完整曲线），但目标日 canonical truth exact join、ET-compatible 历史校准/OOS truth、首批 calibration/policy/probability、WS 全 scope、inventory/order-state 尚未物化；当前 `joined_station_days=0`。因此本文件不代表 live readiness，也不影响另一条 live 生产线。

只读 readiness 同时输出 `unfinished_target_inputs`、native model curve completeness、target DB/raw/WAL/磁盘资源预算和 `retired_resident_line`。旧线只记录 `RETIRED` 事实，不读取其数据库、配置或回执；共享 system 服务状态由服务探针独立验收。

生产事实基线入口：

```bash
python3 scripts/bayes_basket_target_production_facts_baseline.py
```

该入口只写 `output/audits/bayes_basket_target_production_facts_baseline.json`；不会关闭、重启、切换或修改 `basket_activation_not_live_enabled` 保护线。
