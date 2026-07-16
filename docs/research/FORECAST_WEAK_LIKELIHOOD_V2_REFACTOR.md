---
status: superseded
authority: L2
owner: weather-system
effective_from: 2026-07-12
supersedes: []
superseded_by: WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR.md
implementation_symbols:
  - forecast_weak_likelihood_runtime.py:build_forecast_weak_likelihood
  - forecast_source_contextual_weight_model.py
  - forecast_source_weight_artifact_registry.py
  - weather_source_acquisition_manager.py
  - sequential_remaining_warming_bayes_runtime.py:build_sequential_remaining_warming_posterior
  - scripts/forecast_weak_likelihood_contract_audit.py
verification_gates:
  - python3 scripts/forecast_weak_likelihood_contract_audit.py --latest-only --active-only
  - python3 -m pytest -q tests/test_forecast_weak_likelihood_runtime.py tests/test_forecast_weak_likelihood_contract_audit.py tests/test_forecast_weak_contextual_weight_lane.py tests/test_forecast_source_contextual_weight_model.py tests/test_forecast_source_weight_artifact_apply.py
last_verified_commit: working-tree-2026-07-12
---

# Forecast Weak Likelihood v2 重构设计

> 本文是已归档的弱似然模块历史研究稿。当前统一方案及后续修改只维护在[天气概率系统 v2 重构总方案](WEATHER_PROBABILITY_SYSTEM_V2_REFACTOR.md)。

## 1. 文档状态与目的

本文是预测弱似然模块的重构研究设计和实施合同草案，不是当前生产权重变更单。

在 Weak Likelihood v2、resolution label bridge、v3.2 唯一主模型和 Accepted ADR 全部完成前：

- 当前生产概率模型仍为 v3.1-only；
- 当前弱层维持只读 trace/研究身份；
- 不改变 v3.1 posterior；
- 不启用尚未批准的 source weight artifact；
- 不恢复旧模型 fallback 或第二套生产概率链；
- 不自动修改生产参数、数据库事实、交易信号或订单。

本文解决八个问题：

1. 弱似然到底是预测分布、likelihood，还是 density ratio；
2. ECMWF/GFS/ICON 与 v3.1 的 GFS/JMA 是什么关系；
3. TAF 修订、WU/TWC 同源和多接口镜像如何去重；
4. 每个来源“有时准、有时不准”应如何按因果样本学习；
5. 模型分歧应如何表达不确定性，而不是机械平均；
6. forecast snapshot 的 `issued_at`、`provider_available_at`、`valid_time` 和目标日如何约束；
7. 弱预测、先验和观测证据如何避免重复计算；
8. 弱层如何为未来 v3.2 提供干净证据，而不在 v3.1 外再乘一层概率。

相关先验边界见 `docs/research/TEMPERATURE_PRIOR_V2_REFACTOR.md`。

## 2. 结论先行

### 2.1 当前对象不是严格的 likelihood

当前 `forecast_weak_likelihood_runtime.py` 生成的是多个预测来源的归一化 opinion pool：

```text
q_weak(Y | forecast snapshots)
```

它不是生成式 likelihood：

```text
L_F(Y) proportional_to p(forecast snapshots | Y)
```

也不是相对共同先验的 Bayes factor/density ratio：

```text
r_F(Y) = q_F(Y | F) / p0(Y)
```

因此，当前名称“弱似然”与数学语义不完全一致。旧组合路径把它作为 likelihood 与先验相乘：

```text
p_pre(Y) proportional_to p0(Y) * q_weak(Y)^beta_weak
```

预测分布本身通常已经隐含气候基准率，直接再乘 `p0` 存在 base-rate 双计风险。

### 2.2 当前错误没有改变 v3.1 live posterior，但不能带入 v3.2

当前 v3.1 all-day artifact 可用时：

```text
observation_weight = 1
forecast_climate_weight = 0
```

最终 posterior 由 v3.1 remaining-warming distribution 和硬约束决定。弱层虽然被计算、落库和展示，当前不会改变最终 production posterior。

这不是继续保留错误语义的理由。只要未来恢复弱层权重、改变 gate，或在 v3.1 外再乘预测分布，问题就会立刻进入生产。

### 2.3 推荐方向：两级权重、一个 composer

所有 ECMWF、GFS、ICON、WU、TWC、TAF 都统一归属预测弱信息层。它们的角色始终是预测，不因来源官方、当日暂时领先或贴近实况而升级为事实。

优化后的来源权重分为两级：

```text
w_base_s
  = historical_skill(source_family,
                     season,
                     lead_time,
                     forecast_weather_regime)

w_dynamic_s(t)
  proportional_to w_base_s
                  * exp[-lambda * trajectory_loss_s(0:t)]
```

- `w_base_s` 只由严格历史快照和结算标签离线训练、审批、发布；
- `w_dynamic_s(t)` 只调整当前事件、当前 cutoff 的来源份额；
- D0 动态仲裁只使用 `decision_as_of=t` 以前已经出现的 canonical 报文实况；
- 动态仲裁是软更新，不做 winner-take-all，不把来源权重直接置为 0 或 1；
- 来源数量、镜像或 revision 增加不能提高弱层总信息预算；
- 当日动态结果不得反向在线改写长期基础权重。

推荐 v3.2 使用单一直接条件概率模型/mixture-of-experts：

```text
p_v32(X | A, F*, O*)
p(Y | A, F*, O*)
  = Integral[p(Y | X, resolution_contract) * p_v32(X | A, F*, O*) dX]
```

其中：

- `A`：静态日期、站点、地理与严格历史气候上下文；
- `F*`：按 as-of、revision、provider family 规范化后的唯一预测证据快照；
- `O*`：canonical observation trajectory 及其一次派生物；
- `X`：物理站点目标日最高温；
- `Y`：resolution source 最终结算温度/桶。

弱层的生产职责应从“输出一张可直接相乘的概率”改为“输出经过因果规范化的 forecast evidence snapshot、历史基础权重和 D0 动态仲裁特征”。基础权重、D0 轨迹仲裁、物理观测更新必须在唯一 composer 内形成一个联合条件概率，不能分别乘三次。每个信息祖先只能进入唯一主模型一次。

## 3. 当前实现快照

### 3.1 当前输入来源

当前 runtime 可接收：

- ECMWF、GFS、ICON、Meteoblue 等模型峰温；
- AMSC TAF 及 reference TAF；
- WU forecast；
- Weather.com/TWC forecast；
- 其他标记为 `forecast_weak_source` 的预测。

当前 source group 由 `source_id`、`role` 文本推断：

| group | 当前默认来源 | sigma | confidence | precision cap | shrinkage |
|---|---|---:|---:|---:|---:|
| `model` | ECMWF/GFS/ICON/Meteoblue | 1.15°C | 0.70 | 1.25 | 1.00 |
| `taf` | TAF | 0.90°C | 0.80 | 1.50 | 0.01 |
| `wu_forecast` | WU | 1.05°C | 0.55 | 0.70 | 1.25 |
| `twc_forecast` | TWC/Weather.com | 1.05°C | 0.55 | 0.70 | 1.25 |
| `other_forecast` | 其他预测 | 1.30°C | 0.40 | 0.45 | 1.50 |

这些默认参数是结构常量，不是基于 resolution settlement 标签严格 OOS 拟合后的生产参数。

### 3.2 当前数学流程

对单一来源 `s`：

1. 若来源提供完整桶分布，直接归一化；
2. 否则把 `forecast_peak_c` 按默认或显式 `sigma_c` 转成 Normal 桶分布；
3. 计算精度：

```text
precision_s = confidence_s / sigma_s^2
```

对 group `g`：

```text
raw_precision_g = Sum_s precision_s
capped_precision_g = min(raw_precision_g, cap_g)
q_g = precision-weighted arithmetic mixture of q_s
alpha_g = capped_precision_g / (capped_precision_g + shrinkage_g)
q_g_shrunk = alpha_g * q_g + (1-alpha_g) * uniform
```

跨 group：

```text
q_geo(k) proportional_to
  exp(Sum_g capped_precision_g * log(q_g_shrunk(k)))
```

模型与 TAF 分歧时，再根据 normalized peak distance 或 JS divergence，在几何池与 scenario mixture 之间插值。

### 3.3 当前分歧阈值

当前结构阈值为：

- normalized distance 从 1 sigma 开始混合；
- 2 sigma 达到完整 mixture；
- JS divergence fallback 从 0.25 开始；
- 0.75 达到完整 mixture。

runtime trace 明确记录 `fitted_from_history=false`。这些阈值没有通过历史结算标签拟合。

### 3.4 当前 contextual weight lane

系统已具备 contextual weight 研究链：

- provider family 层级权重；
- 季节、lead time、预测曲线上下文；
- event-day rolling OOS；
- immutable artifact、审批和 atomic apply；
- AMSC curve shadow reweighting。

但 runtime 只有在 artifact 同时满足以下条件时才应用 candidate：

- schema 合法；
- `external_only`；
- artifact hash 完整；
- `production_ready=true`；
- `runtime_integrated=true`；
- `approval_state=approved`。

当前没有 active approved pointer，baseline 路径继续运行，provider-family 去重为 false。

### 3.5 2026-07-12 当前事实证据

当前 latest active fact `event_fact_history.id=107663`：

| 项目 | 当前值 |
|---|---:|
| included weak sources | 7 |
| source groups | 4 |
| total raw precision | 4.560942769602 |
| total capped precision | 3.747732426304 |
| model sources | 3 |
| TAF revisions | 2 |
| WU sources | 1 |
| TWC sources | 1 |
| provider-family dedup | false |
| `beta_weak` trace | 0.7 |
| v3.1 observation weight | 1.0 |
| weak layer final causal weight | 0.0 |

具体重复证据：

- 同一 `zuuu_taf_forecast_amsc`、同一目标日和同一 peak valid time 的 34°C 与 36°C 两个 revision 同时进入 `taf` group；
- 两条 TAF raw precision 合计 1.975308641975，再 cap 到 1.5；
- `wu_forecast_high` 与 `weather_com_forecast_peak` 同属 `twc_provider`，却进入两个 group，各取得 0.498866213152 precision；
- WU 当前 `valid_time` 为空，仍被纳入。

当前 contract audit 报告 `ok=true`，因为 v1 contract 只检查结构、归一化、硬源隔离和现有 guard，并没有把 revision 唯一性、provider-family 去重、完整 as-of 身份列为失败条件。

## 4. 正式定义

### 4.1 ForecastRevision

`ForecastRevision` 是某一预测信息祖先针对明确目标，在明确发布时间和可用时间形成的不可变预测版本。

最小身份：

```text
forecast_revision_id
source_id
provider_family
target_station_id
target_event_date_local
target_variable
unit
issued_at
provider_available_at
as_of_eligible_at
valid_start
valid_end
run_id
raw_sha256
parser_version
```

缺失任何决定性身份字段时，该 revision 只能进入 quarantine/research，不得进入 production evidence snapshot。

### 4.2 Forecast evidence snapshot `F*`

定义：

```text
F*(t) = Canonicalize({all revisions visible at decision_as_of=t})
```

Canonicalize 至少完成：

1. target station/date/variable/unit 对齐；
2. `as_of_eligible_at <= t`；
3. 同一 revision 去重；
4. 同一 information ancestor 只保留 cutoff 时最新可见版本；
5. 同一 provider family 先做 family 内选择或 mixture；
6. 记录缺失、冲突和 NA；
7. 输出稳定、顺序无关的 snapshot hash。

### 4.3 来源预测分布

每个来源可以输出：

```text
q_s(X | F_s, calibration_artifact)
```

这是来源对物理最高温 `X` 的预测分布，不得称为可直接乘入先验的 likelihood。

经 resolution bridge 后可得到：

```text
q_s(Y | F_s, resolution_contract)
```

它可用于评分、校准和解释，但仍不是自动可乘的 Bayes factor。

### 4.4 Weak likelihood 的唯一生产语义

v2 不再把 `q_weak` 作为外置生产 likelihood。生产输出改为 `forecast_weak_evidence_snapshot_v2`。

若唯一 v3.2 composer 使用显式 log-density-ratio 参数化，可定义：

```text
log p_v32(Y | A,F*,O*)
  = log p0(Y | A) + g_theta(Y;F*,O*,A) - log Z
```

其中 `g_theta` 是在唯一模型内严格 OOS 学得的联合证据修正，不是 runtime 对多个预测概率的人工乘法。

只有这种统一参数化下，才允许把：

```text
r_theta(Y;F*,A) = exp(g_theta_forecast)
```

称为 forecast density ratio。它不能与另一套已经消费 GFS/JMA/TAF 的模型同时运行。

## 5. Weak Likelihood 宪章

以下条款是 v2 不变量：

1. 弱预测可以移动和展宽分布，但永远不能创建硬温度下界、最终锁桶或结算事实。
2. 市场价格、盘口、成交、仓位、P&L 和订单结果不得进入天气预测证据。
3. METAR、SPECI、AMSC/AWC current、轨迹和动量不属于弱预测层。
4. 每个预测 revision 必须具有不可变身份和完整 causal time lineage。
5. 生产只允许 `as_of_eligible_at <= decision_as_of`；晚 1ms 也不可使用。
6. 不允许用本机 `fetched_at` 替代上游 `provider_available_at`。
7. 同一目标和信息祖先在一个 cutoff 只能消费一个 canonical revision。
8. 同一 provider family 的镜像、品牌入口和转发接口必须先去重，再分配 family 内份额。
9. 缺档是 NA，不是错误预测；不得补 uniform 后假装来源已提供证据。
10. 来源数量增加不得自动增加弱层总信息量。
11. 同一来源的刷新频率不得扩大训练样本量或生产精度。
12. 来源权重、校准和分歧参数必须按 event day 严格 OOS 学习。
13. 所有评估标签最终必须映射到 resolution settlement `Y`；物理代理指标必须单独标记。
14. TAF 的温度、峰值时段和天气现象属于同一 revision vector，不得拆成多张独立票。
15. 当前三模型展示集与 v3.1 primary model 必须使用不同名称和 lineage。
16. 弱层缺失不得触发旧模型 fallback；唯一主模型按其批准的 missing contract 处理或 fail closed。
17. runtime 请求不得在线训练、改权重、更新 artifact 或按当日盈亏自适应。
18. artifact 必须不可变、可重放、带训练截止、数据 manifest、代码和审批哈希。
19. 预测证据只能进入唯一 production composer 一次；禁止在 v3.1/v3.2 外再次相乘。
20. UI 必须区分 `shadow forecast distribution`、`production input` 和 `final posterior`。

## 6. 数据边界

### 6.1 允许输入

| 输入 | 角色 | 必要条件 |
|---|---|---|
| GFS/JMA/ECMWF/ICON 等模型 run | 预测证据 | run identity、available-at、目标对齐完整 |
| TAF revision | 预测证据 | issue time、valid window、raw hash 完整 |
| WU/TWC forecast | 预测证据或 shadow | provider family、不可变快照完整 |
| 来源逐小时曲线 | 分布/上下文特征 | strict as-of；不得读取未来曲线 |
| ensemble/member 分布 | 来源分布 | 成员定义和 run identity 完整 |
| 静态 source metadata | 校准条件 | 冻结在 artifact 中 |
| lead time、循环 DOY | 校准条件 | cutoff 时可知 |
| 多来源 forecast disagreement | 不确定性特征 | 只由 `F*` 计算 |
| resolution bucket contract | 投影规则 | 版本化、不可变 |

### 6.2 禁止输入

| 输入 | 禁止原因 | 正确归属 |
|---|---|---|
| METAR/SPECI 温度作为预测源输入 | 事实不能伪装成预测 | canonical observation 与 D0 仲裁输入 |
| AMSC/AWC current curve 作为长期权重训练的 live 标签 | 会造成在线结果泄漏 | 当前事件 D0 仲裁或离线已冻结训练集 |
| 温度轨迹/动量作为独立弱源 | 会与观测模型重复计票 | 唯一 composer 的 D0 仲裁/observation features |
| 已观测最高温 | 硬物理证据 | posterior hard condition |
| WU/TWC current observation | 非预测字段 | 诊断/观测合同 |
| 市场价格和订单簿 | 非天气证据 | 策略/执行层 |
| 当日 P&L/命中结果 | 结果泄漏 | 离线研究 |
| 事件结算值 | 未来标签 | 训练/评估，不得进入 live feature |
| 当前 posterior | 循环引用 | 下游输出 |

### 6.3 D0 报文轨迹仲裁边界

允许 D0 使用 canonical METAR/SPECI/AMSC/AWC 报文轨迹，对各预测源在当前事件中的动态份额做 prequential 软更新。这不会改变预测源的角色：三大模型、WU、TWC、TAF 仍全部是弱预测。

边界如下：

- 报文实况不进入 `ForecastEvidenceSnapshot`，而进入唯一 composer 内的 `D0ForecastTrajectoryArbitration`；
- 轨迹仲裁只能使用当前 cutoff 前已出现的观测点，禁止读取未来报文或事后修订；
- 仲裁只改变当前事件的 `w_dynamic_s(t)`，不得在线修改 `w_base_s` 或训练 artifact；
- 轨迹匹配的 level、slope、acceleration、peak-refresh、phase-lag 等特征必须严格 OOS 学习；
- 不能因为某源当前温度最接近实况就硬切换到该源；
- 同一观测轨迹可以在联合模型中同时影响 gating 和 remaining-warming 条件分布，但必须由同一个训练目标联合拟合，禁止将两个结果作为独立 likelihood 再相乘；
- 已观测最高温仍可作为确定性 support/hard-floor 条件，但不得被记成某个预测源的命中票；
- D0 仲裁输出是 composer 内部 trace，不是第二张 posterior SSOT。

因此，本设计允许“D0 根据报文实况与预测轨迹优胜劣汰”，但这里的优胜劣汰正式定义为：因果、软性、总预算守恒、联合训练的动态来源仲裁。

## 7. 来源身份、修订与 family 规则

### 7.1 时间字段语义

| 字段 | 定义 | 用途 |
|---|---|---|
| `issued_at` | 来源声明的预报发布时间 | revision 顺序 |
| `provider_available_at` | 预报最早可被外界获得的保守时间 | 因果门 |
| `fetched_at` | 本系统成功抓取时间 | 采集延迟和可重放性 |
| `as_of_eligible_at` | 生产允许使用的最保守时间 | 唯一 live cutoff 门 |
| `valid_start/end` | 预报适用窗口 | 目标语义对齐 |
| `decision_as_of` | 本次预测事实的截止时间 | snapshot 冻结 |

建议：

```text
as_of_eligible_at = max(provider_available_at, first_successfully_seen_at)
```

不得允许当前实现中的 `fetched_at <= as_of + 60s` 宽容。时钟偏差是数据质量问题，应 block 并报警。

### 7.2 Canonical revision selection

对每个：

```text
provider_family
target_station_id
target_event_date_local
target_variable
valid_window
decision_as_of
```

执行：

1. 排除未来或时间身份不完整的 revision；
2. 排除 station/date/unit/variable 不匹配；
3. 按信息祖先去除完全重复和 transport mirror；
4. 选择 cutoff 前最新可见的正式 revision；
5. 若同一 issued revision 内容冲突，整体标为 conflict，不可并入；
6. 旧 revision 只进入 revision-delta 研究，不作为第二份 live evidence。

### 7.3 Provider family

`provider_family` 表达同一物理/预测祖先，不等同于 UI 品牌或 API endpoint。

当前明确案例：

```text
WU forecast + Weather.com/TWC forecast -> twc_provider family
```

family 内可以学习成员选择或 mixture，但 family 总份额必须先固定。添加镜像接口不能改变 family 总信息量和 posterior。

### 7.4 TAF

TAF canonical identity 至少为：

```text
station_id
issue_time
valid_start
valid_end
raw_sha256
parser_version
```

TAF 规则：

- 每个 cutoff 只消费最新可见、覆盖目标日且语义合法的 revision；
- reference TAF 只做健康交叉检查，不能与 primary 同时计票；
- `forecast_peak_c`、peak time、weather groups 是同一 revision vector；
- 取消默认 `shrinkage=0.01` 带来的近乎满信任；
- 没有足够历史 issued forecast 样本前保持 forward shadow。

## 8. “三大模型”、v3.1 与 TAF 的关系

### 8.1 必须拆分名称

当前 UI/研究中的“三大模型”通常指：

```text
ECMWF + GFS + ICON
```

当前生产 v3.1 core model 是：

```text
00:00-05:59 local: GFS + JMA
06:00-23:59 local: GFS only
```

两者不是同一个“三模型架构”。建议命名：

| 对象 | 正式名称 |
|---|---|
| ECMWF/GFS/ICON | `forecast_display_trio` 或 `external_model_tournament` |
| GFS/JMA v3.1 输入 | `v31_primary_nwp_features` |
| TAF | `official_taf_forecast_vector` |
| WU/TWC | `twc_provider_forecast_family` |

UI 不得把 ECMWF/GFS/ICON 的静态平均解释为当前 production posterior 的三模型权重。

### 8.2 当前重复风险

GFS 已经进入 v3.1 primary model；若再把 GFS 的 weak distribution 乘到 v3.1 posterior 上，就重复消费同一祖先。

同理，v3.2 若已经将 ECMWF/ICON/TAF 作为合法特征，就不得在模型外再乘 `q_weak`。

### 8.3 正确的模型能力问题

“每一个模型都有准和不准的预测”不应翻译成当天手工切换权重，而应翻译为：

```text
p(source skill | season, lead, forecast regime, missingness, disagreement)
```

这些条件必须只来自 cutoff 时可知信息，并通过 event-day OOS 学习。不能依据当天后见实况在弱层即时追奖罚。

## 9. 当前问题清单

### P0-1：预测分布被当作 likelihood

问题：`q_weak` 已归一化为预测分布，却在 legacy pre-observation path 中再乘 `p0`。

后果：

- 气候基准率可能双计；
- `beta_weak` 没有清晰统计含义；
- 调大 beta 不等于提高来源技能；
- 不同先验变更会改变同一 weak distribution 的实际作用。

修复：v2 输出 evidence snapshot；未来由 v3.2 单一 composer 联合消费。

### P0-2：TAF revision 重复计数

问题：同一 TAF 修订序列的 34°C 和 36°C 同时进入 group。

后果：旧信息污染最新预测；group cap 只能限制总量，不能恢复正确语义。

修复：在任何分布构造前完成 canonical revision selection。

### P0-3：WU/TWC 同 provider family 双计

问题：baseline 按 source group 而不是 provider family 聚合。

后果：一个上游通过两个品牌/API 获得两份精度。

修复：family dedupe 变为基础合同硬门，不依赖可选 artifact。

### P0-4：causal time fail-open

问题：

- `event_date` 为空仍可进入；
- `valid_time` 仅记录，不校验；
- `fetched_at` 为空仍可进入；
- 未使用已有的 `provider_available_at/as_of_eligible_at`；
- 允许未来 60 秒的 fetched time。

后果：未来数据、目标不明数据和不可重放数据可能取得默认精度。

修复：所有决定性时间和目标字段缺失即 source-level blocked。

### P0-5：v3.1 外部重复消费

问题：GFS/JMA/气候/轨迹已经在 v3.1 内被消费，弱层仍保留可乘接口。

后果：一旦 gate 改变，重复概率链可能悄然恢复。

修复：current v3.1 状态下 weak output 固定为 diagnostic；v3.2 只保留单一 composer。

### P1-1：静态 sigma、confidence 与 precision

问题：多个来源使用人工默认误差和置信度；ECMWF/GFS/ICON 在 model group 内使用相同默认值。

后果：不能反映季节、lead time、天气型和来源差异；也不能证明 TAF 比模型更可靠。

修复：按 resolution 标签做来源/家庭级概率校准和严格 OOS 评估。

### P1-2：模型分歧阈值未拟合

问题：1/2 sigma 和 JS 0.25/0.75 是手工阈值。

后果：可能在分歧时产生过度中点化、过度混合或尾部错误。

修复：模型分歧作为 v3.2 特征；若保留 mixture，所有参数必须 OOS 学习。

### P1-3：uniform 同时表达“没有来源”和“中性证据”

问题：无弱源时输出 uniform distribution。

后果：UI/下游可能把“无数据”误读为“有一张均匀预测”。

修复：输出 `state=unavailable`、空 evidence set 和明确 NA；中性是 composer 的 missing contract，不是伪来源。

### P1-4：contract audit 通过但语义不正确

问题：当前审计只验证现有 schema 和归一化，没有验证 revision、family、目标和 causal time。

后果：`ok=true` 容易被误解为统计和因果均正确。

修复：v2 audit 增加 identity、dedupe、as-of 和 ancestor uniqueness 门。

### P1-5：来源标签域未闭合

问题：现有研究主要证明对物理/外部样本的 source skill，没有完整 resolution-source label bridge。

后果：不能直接声称提高 Polymarket 结算桶预测能力。

修复：所有 production release gate 以 `Y` 为目标；物理最高温只作辅助指标。

## 10. v2 目标架构

```text
immutable raw forecast envelopes
  -> source contract validation
  -> target/station/unit alignment
  -> causal as-of filter
  -> revision canonicalization
  -> provider-family canonicalization
  -> calibrated source predictive distributions
  -> ForecastEvidenceSnapshot F*

historical settlement skill
  -> season/lead/forecast-regime model
  -> approved BaseSourceSkillArtifact
  -> w_base

canonical D0 METAR/SPECI trajectory O(0:t)
  + per-source forecast trajectories in F*
  -> causal trajectory-match features
  -> w_dynamic(t)

F* + w_base + w_dynamic(t) + O(0:t)
  -> v3.2 unique joint production composer
  -> continuous physical max distribution
  -> resolution label bridge
  -> posterior_ssot_fact
```

### 10.1 唯一输出对象

建议 schema：

```text
forecast_weak_evidence_snapshot_v2
```

核心字段：

```text
schema
state
snapshot_id
snapshot_sha256
decision_as_of
target_station_id
target_event_date_local
target_variable
unit
resolution_contract_id
canonical_revisions[]
provider_families[]
source_predictive_distributions[]
forecast_context_features
forecast_trajectories_by_source
missingness
conflicts
excluded_revisions[]
lineage
artifact_identity
na_reasons[]
```

该 snapshot 只包含预测源及其严格 as-of 血缘，不包含报文事实。v2 不输出可以绕过唯一主模型直接成为 posterior 的 `forecast_weak_likelihood_map`。

### 10.2 Canonical revision schema

每条必须包含：

```text
forecast_revision_id
information_ancestor_id
source_id
provider_family
issued_at
provider_available_at
fetched_at
as_of_eligible_at
valid_start
valid_end
target_station_id
target_event_date_local
target_variable
unit
run_id
raw_sha256
parser_version
calibration_artifact_sha256
distribution_domain
distribution
```

### 10.3 Snapshot 不变量

对相同 causal source set：

- 输入顺序变化，snapshot hash 不变；
- 网络重试重复投递，snapshot hash 不变；
- 加入旧 revision，snapshot hash 不变；
- 加入同 family mirror，family evidence 不变；
- 加入未来 revision，snapshot hash 不变且排除理由可见；
- 缺失字段不得被默认 sigma/confidence 掩盖。

### 10.4 基础能力 artifact

建议 schema：

```text
forecast_source_base_skill_artifact_v2
```

至少包含：

```text
artifact_sha256
trained_through_settlement_at
training_manifest_sha256
resolution_contract_id
source_family_mapping
season_basis
lead_time_basis
forecast_regime_features
source_calibration_artifacts
base_weight_model
family_floor_cap
event_day_oos_metrics
approval_state
```

基础能力 artifact 只能离线生成并人工批准。生产请求只能读取，不能根据当前实况、市场或盈亏改写。

### 10.5 D0 动态仲裁 trace

建议内部 schema：

```text
d0_forecast_trajectory_arbitration_v1
```

至少包含：

```text
decision_as_of
canonical_observation_snapshot_id
forecast_evidence_snapshot_id
base_skill_artifact_sha256
comparable_windows_by_source
trajectory_match_features_by_source
base_family_weights
dynamic_family_weights
family_weight_deltas
coverage_state_by_source
neutral_reasons_by_source
total_information_budget_before_after
consumed_by_posterior_run_id
```

该对象只用于解释唯一 composer 为什么改变来源份额，不能作为独立 posterior 或策略信号。

## 11. 校准、权重和分歧方案

### 11.1 先校准单源，再讨论组合

每个来源/家庭先输出概率校准后的分布：

```text
q_s(X | forecast curve, lead, season, source regime)
```

点温度转 Normal 只能作为冷启动 research baseline，不得作为最终 production calibration。

误差模型至少考虑：

- 系统 bias；
- heteroscedastic sigma；
- lead time；
- 循环季节；
- 日内峰值时段；
- forecast curve 形状；
- 来源缺失模式；
- 模型间分歧。

### 11.2 历史基础权重

每个来源/家庭的基础权重必须根据历史上对目标最高温概率分布的真实能力学习，而不是使用固定人工 confidence。

基础权重函数：

```text
w_base_g(c)
  = softmax_g h_phi(
      source_family=g,
      cyclic_day_of_year,
      lead_time,
      forecast_weather_regime,
      forecast_disagreement,
      source_availability
    )
```

其中 `c` 只能使用 forecast cutoff 时已知的信息：

- 循环季节；
- 距目标日/峰值窗口的连续 lead time；
- 各预测源共同给出的云雨、风、日较差、峰值时刻等 forecast regime；
- 来源间分歧；
- 来源是否可用。

不得使用目标日最终实况定义基础天气型，也不得按事后结果挑选模型。

基础能力的主评分是 NLL、RPS、CRPS 和 calibration；连续最高温 MAE/bias 只作辅助。所有参数按 event day 严格 walk-forward，并向全局/父层连续收缩，避免季节×天气×提前量硬切分后小样本过拟合。

### 11.3 Family-first

组合顺序必须是：

```text
revision -> source member -> provider family -> unique composer
```

不是：

```text
API response count -> group raw precision -> posterior
```

family 内份额之和固定，family 间总信息预算固定。新增成员不能抬高 family 总预算。

建议初始 family：

| family | 成员处理 |
|---|---|
| ECMWF | 独立模型 family |
| GFS | 独立模型 family |
| ICON | 独立模型 family |
| TAF | 以 station+issue+valid-window 为 revision 的官方预测 family |
| TWC provider | WU 与 Weather.com/TWC 先去重、再做 family 内选择或 mixture |

若未来证明两个接口并非同一信息祖先，必须通过 lineage 证据修改 family mapping，不能按品牌名称猜测。

### 11.4 D0 动态轨迹仲裁

D0 对每个预测 family，比较 cutoff 前的预测轨迹与 canonical 报文实况轨迹。每个时刻只允许使用当时已经发布的预测 revision 和当时已经获取的实况点。

候选匹配特征：

```text
level_residual       = forecast_temperature - observed_temperature
slope_residual       = forecast_delta_1h - observed_delta_1h
acceleration_residual
peak_refresh_error
phase_lag
curve_shape_distance
cloud_rain_timing_match
comparable_point_count
comparable_window_hours
```

动态权重建议采用可学习的 soft gating：

```text
g_s(t) = h_theta_s(trajectory_match_features_s(0:t))

w_dynamic_s(t)
  = softmax_s(
      log(max(epsilon, w_base_s))
      + lambda_s * g_s(t)
    )
```

约束：

- `h_theta/lambda` 必须通过历史原始时序回放 OOS 学习；
- 当前事件只能改变当前事件权重，不写回基础 artifact；
- 权重平滑变化，设置经 OOS 证明的 floor、cap、最大单次变化和恢复速度；
- family 总信息预算前后守恒；
- 缺少可比轨迹的来源保持中性 multiplier，不记为预测失败；
- 单个异常报文不能让来源立即归零；
- 相同 observation snapshot 与 forecast snapshot 只能消费一次；
- 每次仲裁都绑定 `posterior_run_id`，禁止重复更新。

### 11.5 不同预测源的轨迹可比性

各来源并非都提供相同粒度的温度曲线：

- ECMWF/GFS/ICON：可比较逐小时温度、斜率、峰值时刻和天气要素；
- WU/TWC：可比较其不可变快照中的逐小时温度曲线，但必须先 family 去重；
- TAF：通常是稀疏时段、TX/趋势/天气组，不得伪造为完整逐小时曲线。

因此每个来源需要 `trajectory_comparability_contract`：

```text
available_dimensions
valid_windows
temporal_resolution
temperature_interval_semantics
weather_group_semantics
missing_dimensions
```

所有损失只在可比维度和重叠有效窗口内计算，并按覆盖度规范化。TAF 无逐小时温度时，只评估其明确声明的最高温、有效时段、趋势和天气现象，不通过插值制造虚假精度。

### 11.6 唯一联合概率公式

推荐以 mixture-of-experts 表达优化后的弱预测与观测联合关系：

```text
p_v32(X | A,F*,O_0:t)
  = Sum_g w_dynamic_g(t)
          * q_g(X | F_g, O_0:t, A)

p_v32(Y | A,F*,O_0:t)
  = Integral[
      p(Y | X, resolution_contract)
      * HardCondition_observed_max(
          p_v32(X | A,F*,O_0:t)
        )
    dX]
```

这里观测轨迹可能同时影响 gating 和每个 expert 的 remaining-warming 条件分布，但两者必须联合训练并在同一个 composer 内归一化。禁止再构造：

```text
dynamic_weak_distribution
  * separate_trajectory_likelihood
  * separate_momentum_likelihood
```

这种外部乘法会把同一报文轨迹重复消费。

### 11.7 分歧不是简单平均

ECMWF/GFS/ICON/TAF/WU/TWC 分歧时，正确结果通常是扩大不确定性或保留多峰场景，而不是把峰值机械拉到中点。

v3.2 推荐：

- 将来源间 spread、pairwise delta、curve-shape disagreement 作为特征；
- 用 mixture-of-experts 或联合概率模型学习 regime；
- 允许多峰连续分布；
- 通过 NLL/RPS/CRPS 约束尾部和校准；
- 禁止手工设定“某模型当天看起来更准”后直接换权。

### 11.8 `beta_weak`

当前 `beta_weak` 只在 legacy pre-observation 公式中具有指数温度含义，在 v3.1 gate=1 时没有最终因果作用。

v2 处理：

- 不把 `beta_weak` 作为来源技能权重；
- v3.2 direct composer 不保留外置 `beta_weak`；
- 若用于 research ablation，必须命名为 diagnostic tempering，并禁止生产入口；
- 不允许通过调高 beta 绕过来源校准和标签桥。

## 12. 数据集与评估合同

### 12.1 真值

主目标：

```text
resolution source -> settlement_actual -> market bucket Y
```

物理目标 `X` 可用于诊断：

- 连续最高温 MAE/bias；
- remaining warming；
- peak timing；
- 物理状态分层。

未建立 `X -> Y` label bridge 前，只能宣称物理代理改进，不能宣称结算概率改进。

### 12.2 样本单位

- 训练/验证按独立 event day 分组；
- 同日多个 cutoff 共享 event-day 权重；
- 不能用小时点、刷新次数或接口数量扩样本；
- bootstrap 按 event day cluster；
- 所有 fold 严格 walk-forward。

### 12.3 主指标

| 指标 | 作用 |
|---|---|
| NLL | 主概率质量与过度自信 |
| RPS | 有序温度桶距离 |
| CRPS | 连续温度分布质量 |
| multiclass Brier | 桶概率误差 |
| calibration intercept/slope | 系统偏置与概率幅度 |
| reliability/ECE | 概率校准 |
| tail overconfidence | 极端桶风险 |
| MAE/bias | 连续最高温诊断 |
| availability latency | 来源反应速度 |

Top-1 hit rate 只作诊断，不能单独决定权重或晋级。

### 12.4 必做分层

- D1、D0 pre-dawn、morning、pre-peak、post-peak；
- season/连续 DOY；
- lead time；
- 模型一致与严重分歧；
- 高温尾桶和低温尾桶；
- 云雨/对流/通风/冷池等 forecast regime；
- source available/missing；
- 每个 source 单独偏高、偏低场景；
- TAF 修订幅度与修订方向。

### 12.5 当前研究证据

截至现有研究记录：

- 外部已结算市场事件 110 天；
- 至少一个来源严格配对 98 天；
- ECMWF/GFS/ICON 三模型完整配对 95 天；
- 可评分来源行 299：ECMWF 98、GFS 99、ICON 100、TAF 2；
- contextual challenger 滚动 OOS 为 68 天/70 个样本；
- NLL 点估计改善 3.63%，RPS 改善 2.11%；
- 95% CI 均跨伤害线，因此没有晋级；
- TAF 只有 2 条可评分历史样本；
- AMSC curve 只有 1 个 event day/32 checkpoints，且候选退化；
- WU/TWC 主要是 forward-only。

结论：研究链方向基本正确，但当前证据不足以提高 production 权重。正确结果是继续阻断晋级，而不是放松门槛。

## 13. Release gates

### 13.1 结构与血缘门

- `as_of_eligible_at > decision_as_of`，哪怕 1ms，必须排除；
- 缺 `as_of_eligible_at`、目标日、站点、provider family 或 revision identity 任一项，必须 blocked；
- 同 family mirror/revision 重复注入后，canonical snapshot 和 posterior hash 不变；
- source input 重排、重复投递和网络重试不改变 canonical output；
- 同一 GFS/JMA/TAF/trajectory ancestor 在 production lineage 只出现一次；
- 每个 excluded revision 有稳定 machine-readable reason；
- 无来源时输出 unavailable/NA，不输出伪均匀来源。

### 13.2 统计门

- 至少 80 个独立 resolution-settled event days 才可提出 production proposal；
- challenger 相对冻结 champion 的 NLL 与 RPS 同时满足预设门；
- event-day clustered CI 不越过伤害容忍线；
- season、lead、phase、regime、tail bucket 均无预设灾难性退化；
- calibration slope/intercept、tail overconfidence 和 hard-floor violation 通过硬门；
- TAF/WU/TWC 无历史 issued forecast 档案时保持 forward shadow；
- label bridge 未通过时不得发布为 settlement probability 改进。

### 13.3 架构门

- production 只有一个 composer；
- v3.1 不读取新的 v2 weak output 作为外置 multiplier；
- v3.2 lineage 证明每个 forecast ancestor 只消费一次；
- 基础权重只由 approved artifact 提供，D0 请求不得写回；
- observation trajectory 只能在唯一 composer 的 D0 arbitration/remaining-warming 联合模型中消费；
- `D0ForecastTrajectoryArbitration` 不能成为独立 likelihood、posterior 或策略信号；
- 动态仲裁前后 family 总信息预算守恒；
- 同一 observation snapshot 与 forecast snapshot 不得重复触发权重更新；
- runtime 不修改 artifact；
- 主模型不可用时 fail closed，不回退旧 weak formula。

### 13.4 性能门

弱预测本身不是 3 秒下单 fast lane 的主要触发源，但其 snapshot 不能阻塞观测热路径。

要求：

- forecast evidence snapshot 异步预构建并缓存；
- 新实况到达时只读取已提交、fresh 的 `F*`；
- 热路径禁止同步抓取 ECMWF/GFS/ICON/TAF/WU/TWC；
- base skill artifact、forecast trajectories 和 calibration artifact 必须常驻内存或本地只读热缓存；
- D0 trajectory alignment、feature calculation 和 soft gating 不得启动子进程或访问网络；
- D0 arbitration 自 observation canonicalized 到 dynamic weights ready 的目标 P95 不高于 100ms；
- dynamic weights、posterior 和 decision 共享同一 correlation id；
- snapshot 读取和 schema 验证有独立延迟预算；
- 缺失或过期时按 v3.2 missing contract 处理或 fail closed；
- 不得为等待弱源刷新而错过 observation-to-order 3 秒预算。

## 14. 实施阶段

### Phase 0：冻结语义和现状

目标：在不改变生产 posterior 的情况下消除误读。

交付：

- 发布 Accepted ADR：当前 weak output 是 diagnostic opinion pool，不是生产 likelihood；
- UI 标记 weak layer `shadow/diagnostic` 和 final causal weight；
- 文档区分 `forecast_display_trio` 与 `v31_primary_nwp_features`；
- contract audit 明确当前 provider-family dedup=false 是风险，不是正常通过项；
- 记录当前 fact、代码和 artifact hash 作为冻结 champion。

### Phase 1：ForecastRevision 与 canonicalization

目标：先把数据身份修对，再谈权重。

交付：

- `ForecastRevision` schema；
- `information_ancestor_id`；
- strict `as_of_eligible_at` gate；
- target station/date/variable/unit gate；
- TAF latest-visible revision selection；
- provider-family mandatory dedupe；
- stable snapshot hash；
- duplicate/reorder/future/missing regression tests。

此阶段仍不改变 production posterior。

### Phase 2：单源校准、基础权重与 resolution label bridge

目标：建立“每个来源何时准、何时不准”的可证伪答案。

交付：

- settlement label dataset；
- source/family probability calibration；
- season/lead/regime walk-forward；
- `forecast_source_base_skill_artifact_v2`；
- 基础权重的 hierarchical shrinkage、family floor/cap；
- NLL/RPS/CRPS/Brier/校准报告；
- source tournament 和 ablation；
- immutable calibration artifacts。

### Phase 3：ForecastEvidenceSnapshot shadow

目标：生成 v2 干净 forecast input，不接入生产。

交付：

- `forecast_weak_evidence_snapshot_v2`；
- 各来源严格 as-of 的 forecast trajectory；
- snapshot lineage、missingness、conflict；
- current v1 与 v2 影子差异报告；
- TAF/WU/TWC forward accumulation；
- family dedupe 反事实分析。

### Phase 4：D0 轨迹仲裁 shadow

目标：在不改变生产 posterior 的情况下，验证“实况兑现程度调整当前来源份额”是否有稳定增量。

交付：

- `d0_forecast_trajectory_arbitration_v1`；
- canonical observation 与各来源 trajectory 的 causal alignment；
- level/slope/acceleration/peak-refresh/phase-lag 特征；
- 不同来源的 trajectory comparability contract；
- base 与 dynamic 权重差异 trace；
- soft gating OOS、消融和灾难性退化报告；
- duplicate observation、late observation、future observation 防线；
- 总信息预算守恒和单次消费审计。

此阶段不得把 shadow dynamic weight 写入当前 v3.1 posterior。

### Phase 5：v3.2 唯一 composer 训练

目标：在一个模型中联合消费 prior context、forecast evidence 和 observation trajectory。

交付：

- direct v3.2 feature contract；
- base-skill gating 与 D0 dynamic gating 联合参数；
- expert remaining-warming conditional distributions；
- ancestor uniqueness audit；
- source/family ablation；
- event-day strict OOS；
- settlement label bridge gate；
- 完整 release packet。

### Phase 6：受控切换

前提：Phase 0-5 全部通过并有明确人工批准。

交付：

- v3.2 成为唯一 production composer；
- 删除 v1 weak 外置乘法生产入口；
- 删除旧 fallback，不保留兼容分叉；
- SSOT、UI、决策和执行只读同一 posterior fact；
- 回滚只能切换到上一批准 artifact，不回退旧模型代码。

## 15. 代码改造地图

### 15.1 `forecast_weak_likelihood_runtime.py`

当前问题：同时承担 source filtering、点温度概率化、group pooling、冲突处理和 artifact lane。

建议拆分：

```text
forecast_revision_contract_runtime.py
forecast_revision_canonicalizer.py
forecast_source_calibration_runtime.py
forecast_source_base_skill_runtime.py
forecast_evidence_snapshot_runtime.py
d0_forecast_trajectory_arbitration_runtime.py
```

v1 文件在 v2 验收前保留为只读 trace；v3.2 切换后删除 production 引用和旧乘法入口。

### 15.2 `weather_source_acquisition_manager.py`

需要：

- 强制透传 `issued_at/provider_available_at/as_of_eligible_at`；
- 补齐 target station/date/variable/unit；
- 建立 `information_ancestor_id`；
- TAF issue time 和 valid window 不得丢失；
- provider family 必须来自 accepted mapping contract。

### 15.3 `sequential_remaining_warming_bayes_runtime.py`

当前 v3.1 保持不变。

v3.2 切换时：

- 删除 `p0 * q_weak^beta_weak` 的生产语义；
- 不接收独立 `forecast_weak_likelihood_map`；
- 只消费唯一 v3.2 predictive distribution；
- 保留硬条件投影和 resolution bucket integration；
- v3.2 不可用时 fail closed。

### 15.4 Contextual weight 研究链

保留：

- strict OOS 数据构造；
- provider-family hierarchical model；
- immutable artifact 和人工批准；
- NLL/RPS release gates。

调整：

- AMSC current curve 只进入唯一 composer 的 D0 arbitration shadow/runtime，不进入 ForecastEvidenceSnapshot；
- 当前事件动态权重不得写回 contextual base artifact；
- champion 不再直接替换 production distribution；
- 研究产物转为 v3.2 base gating、dynamic gating 和 ablation 证据；
- 所有标签切换到 resolution label bridge。

### 15.5 审计脚本

新增/升级：

- revision identity completeness；
- strict as-of；
- target alignment；
- provider-family dedupe；
- ancestor uniqueness；
- snapshot determinism；
- no-external-multiplier；
- active artifact identity；
- base/dynamic weight lineage；
- D0 trajectory strict-cutoff；
- dynamic family-budget conservation；
- observation snapshot single-consumption；
- current fact causal contribution。

## 16. 测试计划

### 16.1 单元测试

- future `as_of_eligible_at` 拒绝，包括 1ms；
- 缺 causal time 拒绝；
- 缺 event date/station/unit 拒绝；
- TAF 多 revision 只选最新可见；
- cutoff 前后选择正确 revision；
- WU/TWC 同 family 只形成一份 family evidence；
- 同 source 镜像不增加信息；
- 输入重排输出 hash 不变；
- 重复投递输出 hash 不变；
- invalid revision 有稳定 NA reason；
- 无来源输出 unavailable，不伪造 uniform evidence；
- 相同历史上下文给出相同 base weight；
- D0 只使用 cutoff 前 observation；
- 轨迹完全一致时动态权重按已学习门控更新，不硬切换；
- 无可比轨迹时动态 multiplier 中性；
- TAF 缺逐小时曲线时不进行虚假插值；
- 单个异常 observation 不使来源权重归零；
- 动态仲裁不写回基础 artifact。

### 16.2 属性测试

- permutation invariance；
- duplicate invariance；
- future-data monotonic exclusion；
- family-budget conservation；
- snapshot hash determinism；
- bucket normalization；
- source distribution 与 continuous projection 一致；
- ancestor single-consumption；
- family information-budget conservation；
- D0 arbitration idempotence；
- future observation exclusion；
- base artifact immutability。

### 16.3 历史回放

按每个真实 cutoff 重建当时可见 revision：

```text
raw envelope
-> first seen
-> canonical revision
-> evidence snapshot
-> v3.2 inference
-> resolution bucket score
```

禁止使用：

- 事后修订；
- 结算后首次回捞的 forecast；
- 当前 parser 对历史未保存字段的猜测；
- cutoff 之后的同日实况调整当前 cutoff 的 dynamic weight；
- 用当日 dynamic weight 反向改写长期 base weight；
- 按最终结果挑选模型。

### 16.4 生产影子验收

- v1/v2 source inclusion diff；
- TAF revision selection diff；
- WU/TWC family diff；
- snapshot freshness；
- base weight 与 artifact identity；
- D0 dynamic weight trajectory；
- family budget before/after；
- dynamic gating 相对 base-only 的 OOS 增量；
- v3.2 shadow posterior diff；
- final posterior 不受 v2 shadow 写入影响；
- 3 秒 fast lane 不同步等待 weak acquisition。

## 17. 禁止的优化方向

以下方向即使短期回测变好，也不得采用：

- 直接调高 TAF、ECMWF、GFS、ICON、WU 或 TWC 的固定权重；
- 用当前两条 TAF 样本决定生产权重；
- 通过 group cap 掩盖 revision 重复；
- 把 WU/TWC 当两份独立来源；
- 使用 `fetched_at + 60s` 容忍未来数据；
- 未经 OOS 的当天轨迹追奖罚、winner-take-all 或硬归零；
- 使用未来报文或后续修订回改旧 cutoff 的 dynamic weight；
- 把 D0 dynamic weight 在线写回长期基础权重；
- 将 D0 仲裁结果与强观测 likelihood 再做外置乘法；
- 把 Top-1 命中率作为唯一晋级指标；
- 在 v3.1 外再次乘 GFS/TAF/气候概率；
- 调大 `beta_weak` 冒充模型技能提升；
- 先改权重、后补 resolution 标签桥；
- 为了“不断链”恢复旧模型 fallback；
- 在 runtime 请求内训练或自动 apply artifact；
- 用市场价格、盈亏或订单结果校准天气概率。

## 18. Definition of Done

### 18.1 定义完成

- weak evidence、source distribution、density ratio、posterior 定义互斥且明确；
- `forecast_display_trio`、v3.1 primary、TAF、WU/TWC family 名称明确；
- base weight、D0 dynamic weight、hard floor 和 posterior 的所有者明确；
- prior、forecast、observation、posterior、execution 边界无未治理重复消费。

### 18.2 数据完成

- ForecastRevision 必填身份完整；
- strict as-of、target alignment、revision 和 family dedupe 全部自动化；
- snapshot 可重放、顺序无关、重复无关；
- resolution label bridge 闭环；
- 每个来源具有 trajectory comparability contract；
- base skill artifact 与 D0 arbitration trace 可重放。

### 18.3 统计完成

- 至少 80 个独立 settlement event days；
- NLL/RPS 及 CI 通过；
- calibration、tail 和关键分层无灾难性退化；
- 每个来源/家庭的增量由消融证明；
- base-only、base+D0 gating、full joint composer 逐层消融通过；
- D0 dynamic gating 在关键阶段没有灾难性退化；
- TAF/WU/TWC 样本不足时明确留在 shadow。

### 18.4 架构完成

- v3.2 是唯一 production composer；
- 同一信息祖先只消费一次；
- base 权重离线冻结、D0 权重只作用当前事件；
- D0 arbitration 与 remaining-warming 由同一目标联合训练；
- weak v1 外置乘法生产入口删除；
- 主模型不可用时 fail closed；
- UI 和策略层不复算第二套概率。

### 18.5 性能完成

- forecast snapshot 预构建，不阻塞 observation fast lane；
- `source observation -> posterior -> decision -> order POST` 同一 correlation id 可审计；
- 端到端 3 秒目标单独通过性能门；
- 缺 weak source 不导致热路径同步拉取或超时等待。

## 19. 当前阶段判定

当前弱层可继续作为诊断和研究工具，但不具备重新进入 production posterior 的条件。

当前最优先的工作不是调权重，而是：

```text
ForecastRevision identity
-> strict as-of
-> TAF revision canonicalization
-> provider-family mandatory dedupe
-> resolution label bridge
-> historical base skill artifact
-> D0 causal soft trajectory arbitration
-> v3.2 single composer OOS
```

只有这条顺序能够提高不同预测源对最高温的真实预测能力，同时避免在错误的概率语义上越调越深。
