---
status: superseded
authority: L2
owner: weather-system
effective_from: 2026-07-12
supersedes: []
superseded_by: WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR.md
implementation_symbols:
  - temperature_prior_runtime.py:build_temperature_prior_distribution
  - posterior_ssot_projection_runtime.py:build_fact_projection_no_apply
  - sequential_remaining_warming_bayes_runtime.py:build_sequential_remaining_warming_posterior
  - scripts/prior_layer_contract_audit.py
verification_gates:
  - python3 scripts/prior_layer_contract_audit.py --latest-only --active-only
  - python3 -m pytest -q tests/test_temperature_prior_runtime.py tests/test_prior_layer_contract_audit.py tests/test_temperature_prior_continuity_audit.py tests/test_temperature_prior_policy_confirmation.py
last_verified_commit: working-tree-2026-07-12
---

# Temperature Prior v2 重构设计

> 本文是已归档的先验模块历史研究稿。当前统一方案及后续修改只维护在[天气概率系统 v2 重构总方案](WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR.md)。

## 1. 文档状态与目的

本文是先验模块重构的研究设计和实施合同草案，不是当前生产参数变更单。

在 Temperature Prior v2 完成实现、严格时序验证、Accepted ADR 和唯一主模型切换前：

- 当前生产概率模型仍为 v3.1-only；
- 不改变 v3.1 posterior；
- 不降低 fail-closed 门槛；
- 不恢复旧 v2/v3 或桶级乘法 fallback；
- 不执行历史事实回填、历史 payload 重写或生产参数自动更新。

本文解决六个问题：

1. 先验究竟描述物理站点最高温还是市场结算温度；
2. 哪些数据可以进入先验，哪些必须留在弱似然或观测层；
3. 历史标签如何按 resolution authority、可见时刻和来源域选择；
4. 如何消除 `min_samples=3`、±15 日硬窗口和人工温度偏置；
5. 如何建立单一连续先验 SSOT；
6. 未来如何在唯一主模型中消费先验且不重复计算气候信息。

## 2. 当前实现快照

当前 `temperature_prior_runtime.py` 的实现为：

- 历史跨度默认 730 天；
- 季节窗口为目标 DOY ±15 天；
- 默认 `min_samples=3`；
- 桶先验使用历史桶计数加 0.5 smoothing；
- 连续先验使用 0.1°C 网格 KDE；
- KDE bandwidth 使用稳健 Silverman 规则并限制在 0.45–2.25°C；
- 支持 `weather_type`、`terrain_class`、`site_class`、`urbanization_class` 条件；
- 支持 site、terrain、urban heat island 三类人工温度 bias；
- 历史数据来自 `event_daily_temps` 和 `event_runs`；
- 只读构造，不读取市场价格、当前预测或当前观测。

2026-07-12 当前 active fact 的先验快照：

| 字段 | 当前值 |
|---|---:|
| 宽历史样本 | 391 |
| DOY ±15 日样本 | 39 |
| 宽历史 bandwidth | 2.089°C |
| 季节 bandwidth | 0.898°C |
| `date_window_weight` | 1.0 |
| `min_samples` | 3 |
| `weather_type` | 空 |
| terrain/site/urbanization | 空 |
| 人工温度 bias | 0°C |

当前 active latest prior contract 审计通过。离散桶先验与连续 KDE 积分的 total variation 约为 0.0006，当前数值基本一致，但两者仍是两个独立构造路径。

当前 production composer 在 v3.1 all-day posterior 可用时设置：

```text
observation_weight = 1
forecast_climate_weight = 0
```

因此当前先验虽然被物化、落库和展示，但不改变最终 production posterior。任何先验参数调整在现阶段都只能作为研究结果，不能声称改善生产预测。

## 3. 目标变量与正式定义

### 3.1 两个变量

定义：

```text
Z = resolution 对应物理站点的目标本地日最高温
Y = 市场 resolution source 最终采用的结算温度及其结算桶
```

物理气候先验：

```text
p0_Z(z)
= p(Z=z | station, local_date, static_geography, training_cutoff)
```

经过 resolution label bridge 后的市场先验：

```text
p0_Y(y)
= Integral[p(Y=y | Z=z, resolution_contract) * p0_Z(z) dz]
```

生产可消费的先验对象只能是 `p0_Y`。`p0_Z` 是内部物理基础，不能直接冒充市场结算概率。

### 3.2 唯一定义

先验是在不读取当前事件任何预测、实况、市场与交易信息的前提下，仅依据严格 as-of 的历史标签、目标日期、站点和静态地理条件，对目标最高温形成的连续基础分布。

先验不是：

- 当前 GFS/JMA/ECMWF/ICON 等预测；
- TAF；
- METAR、SPECI、AMSC、AWC 或 PWS 实况；
- 当前温度轨迹、温度动量或物理兑现状态；
- 当天环流、通风、冷池、湿度、云雨和边界层状态；
- WU/TWC current；
- 市场价格、盘口、成交、仓位或 P&L；
- 模型近期命中率；
- 未经 OOS 训练的人工加减温度。

## 4. Temperature Prior 宪章

以下条款是 v2 设计的不变量：

1. 先验内部物理变量必须在连续温度域定义，最后才映射或积分到结算桶。
2. 系统唯一市场目标仍是 resolution source 的 `Y`；物理站点温度 `Z` 只是潜变量。
3. 每条训练标签必须显式声明 `label_domain`：`resolution_settlement`、`resolution_replay` 或 `physical_proxy`。
4. `physical_proxy` 只能训练 `p0_Z`，不得直接当作 `p0_Y` 的市场结算标签。
5. 生产可消费的先验必须绑定 `resolution_contract_id` 和 label bridge identity。
6. 先验不得读取当前事件的预测、TAF、实况、轨迹、动量、市场或交易信息。
7. 盆地只允许表达静态地理归属；当天盆地通风、冷池和边界层状态属于弱似然或观测层。
8. 所有历史标签必须满足 `available_at < training_cutoff`；缺少决定性时间字段时不可用。
9. 同一市场事件日只能选择一个权威标签，不得由数据库表读取顺序决定。
10. prior snapshot 在事件首次生成后冻结；同一事件随后的实况或预测更新不得改变 prior。
11. 先验唯一 SSOT 是连续密度；桶概率只能从该密度按 resolution bucket contract 积分得到。
12. 小样本条件层必须连续收缩到父层，不允许达到少量样本后突然满权。
13. 实时请求不得训练、调参或改写 prior artifact。
14. prior artifact 必须不可变、可重放并绑定数据 manifest、训练 cutoff、代码版本和参数哈希。
15. 若未来唯一主模型要求先验输入，则先验身份、时间血缘或 label bridge 缺失时必须 fail closed。
16. 同一气候祖先只能消费一次；先验作为模型 offset 后，不得再把相同 climo 作为独立高权特征重复输入。

## 5. 数据边界

### 5.1 允许输入

| 输入 | 角色 | 条件 |
|---|---|---|
| 历史 resolution 结算值 | `p0_Y` 强标签 | resolution identity、resolved_at、available_at 完整 |
| 历史物理站点日最高温 | `p0_Z` 标签 | 明确标记 `physical_proxy` |
| 目标本地日、循环 DOY | 季节项 | 仅使用静态日期信息 |
| 站点、海拔、经纬度 | 静态站点条件 | 绑定 resolution contract |
| 静态盆地/地形类别 | 分层 grouping | 不能作为人工加温项 |
| 历史年份和长期趋势 | 气候趋势 | 必须严格 as-of、离线拟合 |
| 历史城市化特征 | 长期趋势 | 只允许冻结 artifact 中的学习结果 |
| resolution 舍入/单位/日界规则 | label bridge | 必须版本化 |

### 5.2 禁止输入

| 输入 | 禁止原因 | 正确归属 |
|---|---|---|
| 当前数值模型预测 | 当前事件信息 | 预测弱似然/唯一主模型特征 |
| TAF | 当前事件信息 | 预测弱似然 |
| METAR/SPECI/AMSC/AWC | 当前观测 | 强观测/物理状态 |
| 轨迹与动量 | 观测派生 | 唯一观测模型 |
| 当天通风、冷池、环流 | 动态天气状态 | 预测或观测层 |
| WU/TWC current | 当前事件信息 | 诊断或观测层 |
| 模型历史 Brier 乘数 | 模型技能信息 | forecast calibration |
| 市场价格、成交、仓位 | 非天气证据 | 执行层 |
| 手工 site/terrain/UHI bias | 不可审计参数 | 离线分层模型 |

### 5.3 成都盆地边界

对于当前单站 ZUUU：

- 长期盆地效应已经隐含在 ZUUU 的历史温度分布中；
- 额外加入“成都盆地加温”会重复计算；
- `terrain_class` 只有在多站点、多城市分层模型中才具有可识别增量；
- 当天冷池是否打开、通风是否增强、边界层是否发展不是 prior；
- 这些动态状态只能进入预测或 canonical observation 模型。

## 6. 标签选择与 authority

### 6.1 当前问题

当前实现先读取 `event_daily_temps`，再读取 `event_runs`，并按 `event_date` 的 first-seen 结果去重。这意味着标签权威可能由表顺序决定，而不是由 resolution source 决定。

当前过滤还有以下 fail-open 风险：

- `created_at` 为空时，记录仍可能被使用；
- station 不匹配但 city 文本匹配时可能被接受；
- unit 或 market_var 缺失时可能被接受；
- 没有显式 `label_domain`；
- 没有正式 authority 排序。

### 6.2 v2 authority 顺序

建议标签 authority 为：

```text
resolution_settlement
  > resolution_replay
  > physical_proxy_same_station
  > physical_proxy_reference_station
```

不同 authority 不得在同一 prior 目标域内直接混合。`physical_proxy` 必须先进入 label bridge。

### 6.3 必填字段

每条标签至少包含：

```text
event_id
event_date_local
station_id
resolution_contract_id
resolution_source
label_domain
temperature_value
temperature_unit
rounding_rule
resolved_at
available_at
raw_resolution_ref
source_hash
parser_version
```

缺少 `available_at`、`label_domain` 或 `resolution_contract_id` 时不得用于 production prior。

## 7. Prior Snapshot 冻结合同

每个事件第一次生成 prior 时，写入不可变身份：

```text
prior_snapshot_id
prior_artifact_id
prior_schema
target_domain
station_id
event_date_local
timezone
resolution_contract_id
training_cutoff
prior_frozen_at
label_manifest_sha256
artifact_sha256
source_code_sha256
```

同一事件后续 posterior 更新必须复用同一个 `prior_snapshot_id`。以下信息不得导致 prior 重算：

- 新 METAR/SPECI；
- 新 TAF；
- 新数值模型 run；
- 新市场价格；
- 新温度轨迹或动量；
- 同一事件发生后的新结算样本入库。

如果 prior artifact 在事件期间发生版本切换，当前事件继续使用已冻结版本；新 artifact 只对尚未冻结的新事件生效。

## 8. 统计模型设计

### 8.1 删除硬季节窗口

当前 ±15 日窗口会制造边界。v2 使用循环季节核：

```text
season_weight_i
= exp[-2 * sin^2(pi * delta_doy_i / 365) / season_length_scale^2]
```

这样年末与年初自然连续，且不存在 ±15 日的硬切换。

### 8.2 增加严格历史时间权重

允许离线拟合长期气候趋势：

```text
recency_weight_i = exp(-age_days_i / recency_scale)
```

`recency_scale` 必须通过 rolling-origin OOS 选择，不得在实时环境变量中人工调整。

### 8.3 使用有效样本量

当前实现使用：

```text
condition_weight = min(1, sample_count / min_samples)
```

默认 `min_samples=3` 会造成 2→3 条样本的权重断崖。v2 改为：

```text
ESS = (sum(w_i))^2 / sum(w_i^2)
child_share = ESS / (ESS + shrinkage_kappa)
```

`shrinkage_kappa` 由 OOS artifact 固定。原始行数只用于数据覆盖报告，不直接等于统计有效样本量。

### 8.4 分层收缩

多城市扩展时采用：

```text
station
  -> city
  -> static basin class
  -> regional climate
```

站点样本不足时逐级向父层收缩。当前单站 ZUUU 不单独叠加 basin bias。

### 8.5 连续密度候选

候选模型至少包括：

1. 当前 KDE；
2. 循环季节 KDE；
3. 循环季节 + recency KDE；
4. Student-t 残差模型；
5. KDE + 极端尾部模型；
6. 分层有序/分位数模型。

模型选择以结算桶 NLL、RPS、连续 CRPS、校准和尾部保护为准，不以单一 MAE 或 top-1 hit rate 决定。

## 9. 连续 SSOT

v2 只允许一个概率律：

```text
continuous_prior_density
```

桶概率必须派生：

```text
P(bucket=k) = Integral(bucket_bounds_k, p0_Y(y))
```

禁止分别训练或平滑一份桶 prior 和一份连续 prior。输出应包含：

```text
continuous_distribution
bucket_probabilities_derived
bucket_integration_contract
normalization
quantiles
tail_mass
```

验收必须证明：

- 连续密度积分为 1；
- 桶概率和为 1；
- 所有桶概率均由连续密度积分得到；
- 没有第二套独立桶平滑；
- 舍入和边缘桶规则来自 resolution contract。

## 10. 建议 Schema

```json
{
  "schema": "temperature_prior_v2",
  "state": "available",
  "target_domain": "resolution_settlement_temperature",
  "physical_target_domain": "station_daily_max_temperature",
  "prior_snapshot_id": "prior_...",
  "prior_artifact_id": "prior_artifact_...",
  "station_id": "ZUUU",
  "event_date_local": "YYYY-MM-DD",
  "timezone": "Asia/Shanghai",
  "resolution_contract_id": "resolution_...",
  "training_cutoff": "...",
  "prior_frozen_at": "...",
  "label_manifest_sha256": "...",
  "artifact_sha256": "...",
  "physical_prior": {
    "schema": "continuous_physical_temperature_prior_v2",
    "distribution": []
  },
  "resolution_label_bridge": {
    "schema": "resolution_label_bridge_v1",
    "state": "available",
    "artifact_id": "bridge_..."
  },
  "settlement_prior": {
    "schema": "continuous_settlement_temperature_prior_v2",
    "distribution": [],
    "bucket_probabilities_derived": []
  },
  "sample_summary": {
    "raw_count": 0,
    "effective_sample_size": 0,
    "seasonal_effective_sample_size": 0,
    "independent_season_count": 0
  },
  "boundaries": {
    "reads_forecasts": false,
    "reads_current_observations": false,
    "reads_market_prices": false,
    "uses_manual_temperature_bias": false,
    "bucket_probabilities_derived_from_continuous": true,
    "frozen_for_event": true
  },
  "na_reasons": []
}
```

若 label bridge 尚未通过：

```text
physical_prior.state = available
settlement_prior.state = research_only | blocked
```

不得把 physical prior 自动提升为 settlement prior。

## 11. 与唯一主模型的接口

### 11.1 当前 v3.1

当前 v3.1 是整体 integrated model，已消费 climo、GFS/JMA 和站点路径。当前外部 prior 保持 benchmark/diagnostic，不改变 production posterior。

禁止为了“让先验生效”而直接：

- 降低 observation gate；
- 增加 prior multiplier；
- 恢复旧桶级乘法 posterior；
- 在 v3.1 不可用时回退 prior-only；
- 同时维护第二 production composer。

### 11.2 未来 v3.2

推荐把 prior 作为唯一模型的基础密度：

```text
log p(Z | F, O)
= log p0_Z(Z)
  + g_theta(Z, F, O)
  - log_normalizer
```

其中：

- `p0_Z` 负责长期气候和静态站点基础；
- `F` 负责模型/TAF等预测增量；
- `O` 负责 canonical observation、轨迹和物理兑现增量；
- label bridge 负责从物理最高温映射到市场结算目标。

如果 `p0_Z` 已作为 offset，模型不得再把相同 climo distribution 作为独立高权输入。任何保留的季节变量必须证明表达不同且通过 source-family/feature-ancestor 消融。

## 12. 验证设计

### 12.1 数据切分

- 独立单位为事件日，不是小时或刷新次数；
- 使用 rolling-origin 或 expanding-window；
- 测试事件只能读取其 cutoff 前可见的标签和 artifact；
- bootstrap 和置信区间按事件日聚类；
- physical prior 和 settlement bridge 分别验证，再验证组合结果。

### 12.2 基线与候选

必须比较：

1. 当前 730 天、±15 日 KDE；
2. 长期宽历史 KDE；
3. 循环季节 KDE；
4. 循环季节 + recency；
5. 分层站点/盆地模型；
6. 物理 prior + resolution label bridge。

### 12.3 指标

主指标：

- resolution bucket NLL；
- RPS；
- 连续 CRPS；
- calibration intercept/slope；
- 90%/95% 区间覆盖率；
- 极端高温尾桶过度自信。

辅助指标：

- 连续 MAE；
- bias；
- top-1/top-3；
- 稳定性和 prior snapshot churn。

### 12.4 Release gate

生产晋级必须同时满足：

- as-of 泄漏为 0；
- label-domain、resolution identity 和时间血缘覆盖完整；
- NLL 与 RPS 点估计不劣于 champion；
- 置信区间不越过预设伤害线；
- CRPS、校准和尾部保护无关键退化；
- 覆盖多个独立季节周期；
- artifact、数据 manifest、代码和配置哈希闭环；
- prior 在事件内保持冻结；
- 与唯一主模型的气候信息无重复消费。

测试通过只能证明工程行为，不能替代统计 release gate。

## 13. 实施阶段

### Phase 0：合同与血缘，不改变生产概率

1. 建立 `temperature_prior_v2` 和 `resolution_label_bridge_v1` schema；
2. 增加 label-domain、resolution authority 和 `available_at`；
3. 同一事件按 authority 选择标签，不再按表顺序；
4. 对缺失时间、单位、站点或 resolution identity 的标签 fail closed；
5. 删除 production prior 的动态 `weather_type` 输入；
6. 删除 production 手工 site/terrain/UHI bias；
7. 建立 per-event immutable prior snapshot；
8. 将旧乘法 prior continuity/policy audit 标为 research-only；
9. current active latest prior audit 保持全绿；
10. 历史缺 prior 的旧事实只列债务，不自动回填。

### Phase 1：Shadow 数据集

分别构建：

- 长期 physical daily max 数据集；
- resolution settlement 数据集；
- physical-to-resolution bridge 数据集；
- 数据质量和 coverage manifest。

所有写入限制在研究目录和版本化 artifact；不修改 `archive.db` 当前事实，不改生产参数。

### Phase 2：Prior v2 候选

1. 实现循环季节核；
2. 实现 ESS 连续收缩；
3. 训练 recency、bandwidth、shrinkage 参数；
4. 比较尾部分布候选；
5. 生成 immutable prior artifact；
6. 运行 rolling-origin OOS 和分段保护。

### Phase 3：Label Bridge

1. 逐事件冻结 resolution contract；
2. 对齐站点、单位、舍入、日界和修订行为；
3. 训练或验证 `p(Y|Z,resolution_contract)`；
4. 输出 settlement prior；
5. 验证 market bucket NLL/RPS 和校准。

### Phase 4：唯一模型集成

1. 生成 v3.2 shadow candidate；
2. 将 prior 作为明确 base density/offset；
3. 删除重复 climo 消费；
4. 完成 source-family/feature-ancestor 消融；
5. 通过完整 release gate 后提交 Accepted ADR；
6. 一次性切换唯一主模型；
7. 不保留旧模型 fallback。

## 14. 代码改造清单

### `temperature_prior_runtime.py`

- 新增 label-domain 和 authority 选择；
- `available_at` 缺失 fail closed；
- city/station/unit/market_var 改为严格 contract 匹配；
- 删除动态 `weather_type` 和人工 bias production 入口；
- 将硬 ±15 日窗口改为循环季节核；
- 将 `min_samples` 阶跃改为 ESS 连续收缩；
- 只输出一个连续概率律；
- 桶概率由连续密度积分派生；
- 输出 immutable artifact/snapshot identity。

### `posterior_ssot_projection_runtime.py`

- 禁止从 current payload 向 prior 注入动态天气型；
- 只引用已冻结 prior snapshot；
- 不在投影期间实时重训 prior；
- 投影缺失身份时输出明确 `prior_na_reason`。

### `weather_fact_snapshot_writer.py`

- 保存 `prior_snapshot_id/prior_artifact_id/training_cutoff`；
- 保存 label manifest 和 bridge identity；
- merge-fill 不得用低 authority 标签覆盖高 authority 标签；
- UI 与执行只读相同 prior snapshot。

### `sequential_remaining_warming_bayes_runtime.py`

- 当前 v3.1 保持不变；
- v3.2 才允许消费 prior v2；
- prior/climo source ancestor 只能消费一次；
- prior 不可用时按 Accepted ADR fail closed，不回退旧 composer。

### 审计和测试

- 更新 `prior_layer_contract_audit.py`；
- 新增 label authority/as-of leakage 审计；
- 新增 continuous-to-bucket equality 审计；
- 新增 per-event prior freeze 审计；
- 新增 prior/climo ancestor dedupe 审计；
- 将旧乘法公式审计改名并标为 counterfactual research；
- 增加真实 fact-builder 集成测试，不只测试手工 fixture。

## 15. 禁止的优化方式

以下方式不得进入本次重构：

- 直接把 `min_samples` 从 3 改成另一个拍脑袋数值；
- 仅扩大 730 天窗口而不处理 label domain；
- 给成都盆地、站点或 UHI 手工加减温；
- 把当天环流或天气型塞回 prior；
- 用当前模型近期命中率修正 prior；
- 用市场价格校准天气 prior；
- 让 prior 随实况更新；
- 同时维护离散桶 prior 和连续 prior 两套概率；
- 为使 prior 生效而降低当前 v3.1 observation gate；
- v3.1 不可用时回退 prior-only 或旧桶乘法模型；
- 根据单日输赢自动更新 prior artifact。

## 16. Definition of Done

先验模块重构只有在以下条件全部满足时才完成：

- 目标变量、label domain 和 resolution bridge 单值定义；
- 所有 production 标签具备完整 authority 和 available-at 血缘；
- prior snapshot 对单事件不可变；
- 动态 weather_type 和人工 bias 不再进入 production prior；
- 季节连续、样本连续收缩，无 `min_samples` 权重断崖；
- 连续密度是唯一概率 SSOT；
- 桶概率完全由连续密度积分得到；
- prior v2 在严格 OOS 上通过 NLL、RPS、CRPS、校准和尾部保护门；
- physical prior 与 settlement prior 明确分离；
- prior 与 v3.2 气候特征无祖先双计；
- production 仍只有一个 posterior composer；
- 当前 v3.1 在正式切换前不受影响；
- 无旧模型 fallback；
- 审计、聚焦测试、真实 fact 集成测试全部通过。

## 17. 下一主线

先完成 Phase 0：标签域、authority、available-at、prior freeze、单一连续 SSOT 和审计合同。Phase 0 不修改 production posterior。

随后构建 prior v2 shadow 数据集和 resolution label bridge；只有统计 gate 通过后，才进入 v3.2 唯一主模型候选集成。
