# Bayes 温度后验重构最终方案

更新时间：2026-06-29

## 结论

当前 `bayesian_probability_runtime.py` 的主 Bayes 池是“加性证据池”：

```text
score_i =
  prior_strength * prior_i
+ model_evidence * model_i
+ wu_forecast_evidence * wu_i
+ weather_com_forecast_evidence * weather_com_i
+ taf_peak_evidence * taf_i
+ climo_regime_evidence * climo_i
+ nowcast_evidence * nowcast_i
+ temperature_trajectory_evidence * trajectory_i
+ physical_likelihood_evidence * physics_i

posterior_i = score_i / sum(score)
```

它不是严格的 `posterior proportional prior * likelihood`。`prior`、`climo_regime`、WU、TWC、TAF、轨迹、物理证据都以 pseudo-count 形式投票，优点是稳定、易解释、不会因单源异常直接爆炸；缺点是权重主要靠人工配置，先验与似然边界不清，相关预测源会重复计数，难以用 CRPS/分位数等连续分布指标校准。

最终方案：新增连续温度后验引擎，直接替换旧加性证据池；旧池不作为 fallback、诊断路径或并行运行口径保留。结算日最高温 `T` 是唯一目标变量：

```text
P(T | climate prior, forecasts, observations, intraday path, physics)
  -> temperature posterior distribution
  -> bucket probabilities
  -> Bayes 二元结算/目标桶交易候选
```

## Goal 契约

GOAL:

在同一事实层与同一 ET 日口径下，实现 `Continuous Temperature Posterior Engine v1`，并把它作为唯一温度概率引擎直接替换旧加性 evidence Bayes 池：支持范围内的最高温市场 100% 输出 `posterior_temperature_distribution`、`bucket_probabilities`、`cdf_values`、`quantile_grid`，旧加性池运行时入口数量降为 0，且在走步回测中满足 D0/hard-source 零违约与连续分布质量门槛。

DONE WHEN:

以下目标尽量同时达成，才算理想完成；其中标注为 hard gate 的项目不允许降级：

- hard gate：支持范围内 fact 的 `posterior_temperature_distribution` 覆盖率 = 100%，字段包含 `grid`、`cdf_values`、`quantiles_c`、`quantile_grid`、`bucket_probabilities`、`source_components`、`calibration`、`observation_constraints`。
- hard gate：旧加性 evidence Bayes 池不作为 fallback、诊断路径或长期并行口径保留；运行时旧池入口数量 = 0，UI/API 暴露旧池概率字段数量 = 0。
- hard gate：D0 violation rate = 0，即任何输出分布都不得给 `T < observed_today_max_c` 留概率质量。
- hard gate：hard-source locked events consistency = 100%，官方最终最高温或 hard-source direct SSOT target 出现后，目标桶/结算概率必须锁定一致。
- hard gate：UI 和 API 只读事实层 `posterior_temperature_distribution`，前端温度概率复算路径数量 = 0。
- hard gate：Bayes 二元结算/目标桶交易语义不变；旧 `edge_min`、`hold_edge_realizable`、`lcb_*`、`stop_loss`、`trail_stop`、`weather_repricing*` 真实卖单触发数量 = 0。
- quality gate：在最近可用已结算最高温样本上，CRPS ≤ 旧加性池历史回测基线；若样本数 ≥ 80，目标为 CRPS 至少改善 3%。
- quality gate：Bucket Brier score ≤ 旧加性池历史回测基线；若样本数 ≥ 80，目标为至少改善 3%。
- quality gate：Bucket logloss ≤ 旧加性池历史回测基线；若样本数 ≥ 80，目标为至少改善 3%。
- quality gate：最后 3 小时 target churn ≤ 旧加性池历史回测基线，目标为至少下降 10%。
- quality gate：p10 / p50 / p90 quantile coverage 误差分别 ≤ 5pp / 8pp / 5pp；若样本数不足 80，报告置信区间并不得声称显著改善。
- reliability gate：核心测试通过率 = 100%，新增 CTP 单元测试、回测指标测试、事实层契约测试、UI 只读契约测试。
- reliability gate：无严重新 bug 引入；运行异常必须进入 hard fail / no-trade 并写明确 `*_na_reason`，不得回退旧加性池。

VERIFY:

必须实际运行以下命令验证；若命令尚不存在，补齐该脚本或测试本身就是实现范围的一部分：

```bash
python3 -m py_compile temperature_posterior_runtime.py bayesian_probability_runtime.py chengdu_intraday_summary.py ui/server.py
pytest tests/test_temperature_posterior_runtime.py tests/test_temperature_posterior_backtest.py tests/test_bayesian_probability_runtime.py tests/test_bayesian_likelihood_runtime.py tests/test_chengdu_intraday_summary.py tests/test_ui_server_runtime.py -q
python3 scripts/temperature_posterior_backtest.py --engine ctp_v1 --baseline additive_pool --min-settled 50 --metrics crps,brier,logloss,d0,quantile_coverage,target_churn --fail-on-gate
python3 scripts/check_ctp_direct_replacement.py --fail-on-additive-runtime-path --fail-on-ui-recompute --fail-on-fallback
python3 scripts/check_ssot_alignment.py
python3 scripts/weather_decision_core_audit.py
```

ANTI-HACKING SAFEGUARDS & HARD CONSTRAINTS:

- 严禁通过修改/删除测试、跳过断言、缩小样本集、排除失败城市/日期来达成指标。
- 严禁硬编码成都、某一天、某个桶、某个已知结算值，或 mock 掉真实预测/实况/事实层读取。
- 严禁用旧加性池作为 fallback、诊断运行路径、UI/API 并行口径或异常兜底。
- 严禁弱来源覆盖 hard observation；`observed_today_max_c`、官方最终最高温、hard-source direct SSOT target 必须优先。
- 严禁前端复算温度概率；UI 只能渲染事实层输出。
- 禁止未经确认触发历史大范围回填或宽表重写；需要样本构建时先输出影响面并确认。
- 数据口径固定 ET 日；报告必须写清样本范围、样本数、过滤条件、baseline 版本与缺失原因。

EXECUTION RULES:

- 先做只读样本体检，确认可用样本数、缺失模式、source calibration 层级回退可训练节点。
- 先实现度量与回测，再替换运行路径；不能在无法验证 CRPS/Brier/logloss/D0 的情况下切主。
- 先写 CTP 输出契约测试，再接 UI/API；事实层字段不完整时必须 fail closed。
- 每次主要迭代都产出一份量化报告，包含当前指标、距离 gate 的差距、失败样本明细。
- 所有参数必须有统计含义：bias、sigma、precision、样本量 shrinkage、group rho、precision cap；不得再引入无校准来源的 evidence 常数作为主权重。

STOP RULES:

- 达到 DONE WHEN 并通过 VERIFY 后，立即停止，不继续扩大重构范围。
- 最多允许 3 次主要迭代：样本体检/指标脚本、CTP 引擎与事实层接入、直接替换与 UI/API 契约；第 3 次后仍未通过全部 gate，停止并输出已达成指标、失败样本、剩余问题和下一步建议。
- 任一 hard gate 失败时不得切主；若已切主则进入 hard fail / no-trade，并回滚到“无温度概率输出/不交易”状态，不回退旧加性池。
- 若可用已结算样本数 < 50，只允许完成引擎与契约，不允许声称预测质量达标；必须等待更多样本或补充经确认的数据集。

CHECKLIST:

- [ ] 只读盘点 settled highest-temperature 样本，按 `source / station_id / similar_city_group / climate_zone / lead_bin / hour_bin / regime` 输出样本量、bias、sigma、MAE/RMSE、CRPS-ready 摘要。
- [ ] 建立旧加性池历史回测基线，固定 baseline 版本、样本范围和指标计算口径。
- [ ] 新增 `temperature_posterior_runtime.py`，输出连续分布、CDF、分位数、桶积分和 source calibration provenance。
- [ ] 实现 hierarchical shrinkage / 层级回退：station -> similar_city_group -> climate_zone -> global -> conservative_default。
- [ ] 实现 source group shrinkage / capped precision，避免 WU/TWC/TAF/AWC/model 重复计数。
- [ ] 实现 D0 hard constraint 和 hard-source settlement lock。
- [ ] 写入事实层 `posterior_temperature_distribution`，并保证 `/api/ui_snapshot`、`/api/ui_snapshot_summary` 只读该字段。
- [ ] 禁用旧加性 evidence Bayes 池运行路径，静态/运行时审计确认入口数量 = 0。
- [ ] 补齐 VERIFY 命令对应脚本与测试，运行并归档量化报告。
- [ ] 通过 gate 后直接切 primary；失败进入 hard fail / no-trade，不回退旧池。

## 设计目标

- `climo` 是真正先验：`P(T | station, date, climate_zone, regime)`。
- WU/TWC/TAF/AWC/model 是校准后的似然或预测分布，不再直接投票。
- AWC/METAR/已观测最高温是实况约束和日内更新，不允许被弱预测源覆盖。
- 温度轨迹、温度动量、物理证据估计“剩余上行空间”和尾部形状，而不是单独给桶投票。
- 输出连续分布、CDF、分位数、桶概率，供 UI、复盘、交易层统一读取。
- 旧加性 evidence Bayes 池完成切换后下线，不能作为运行时 fallback 或 UI/API 诊断口径继续存在。
- 交易策略仍只保留 Bayes 二元结算/目标桶实现语义；旧 Edge/LCB 字段只作展示、诊断和兼容。

## 新核心对象

新增运行模块：

```text
temperature_posterior_runtime.py
```

新增输出对象：

```json
{
  "version": "continuous_temperature_posterior_v1",
  "target_variable": "settlement_daily_max_c",
  "event_key": {
    "slug": "...",
    "event_date": "2026-06-29",
    "station_id": "..."
  },
  "as_of": "...",
  "mean_c": 31.8,
  "sigma_c": 0.7,
  "grid": [
    {"temp_c": 30.0, "pdf": 0.02}
  ],
  "cdf_values": [
    {"temp_c": 31.0, "cdf": 0.42}
  ],
  "quantiles_c": {
    "p05": 30.6,
    "p10": 30.9,
    "p50": 31.8,
    "p90": 32.7,
    "p95": 33.0
  },
  "quantile_grid": [
    {"q": 0.05, "temp_c": 30.6},
    {"q": 0.10, "temp_c": 30.9},
    {"q": 0.50, "temp_c": 31.8},
    {"q": 0.90, "temp_c": 32.7},
    {"q": 0.95, "temp_c": 33.0}
  ],
  "bucket_probabilities": [
    {"outcome": "31", "probability": 0.31},
    {"outcome": "32", "probability": 0.46}
  ],
  "source_components": {},
  "calibration": {},
  "observation_constraints": {}
}
```

落库建议字段名：

```text
posterior_temperature_distribution
```

该对象写入事实层，由 `/api/ui_snapshot` 读出；UI 不做前端复算。

## 概率结构

### 1. 气候先验

先验只来自历史气候和季节/站点条件：

```text
T ~ climate_prior(station, day_of_year, climate_zone, regime)
```

`climo_regime` 不再作为单独证据通道与 `prior` 并列投票，而是进入先验的条件变量或 regime mixture。

### 2. 预测源似然

每个预测源先变成校准后的连续分布：

```text
T | source_s ~ Normal(forecast_peak_s - bias_s, sigma_s)
```

其中 `bias_s`、`sigma_s` 由走步校准产生。WU、TWC、TAF、AWC、模型集合不再靠固定 evidence 直接加权，而是通过校准误差决定精度：

```text
precision_s = 1 / sigma_s^2
```

### 3. 相关源重复计数

第一版不做 Gaussian copula，先用 source group shrinkage：

```text
forecast_group = {WU, TWC, TAF, AWC, model_ensemble}
group_mean = precision_weighted_mean(source_distributions)
precision_eff = max_precision + rho * (sum_precision - max_precision)
```

初始 `rho` 建议为 `0.25-0.40`，并设置上限：

```text
precision_eff <= precision_cap_by_group
```

可选更简单实现：

```text
precision_eff = sqrt(sum_precision)
```

但首选 capped shrinkage，因为可解释性更好。

### 4. 实况约束

实况层是约束，不是普通弱证据：

```text
support(T) >= observed_today_max_c
```

若已有官方最终最高温或 hard-source direct SSOT target，则结算锁定：

```text
P(T = settlement_actual) = 1
```

若只是日内已观测最高温，低于该值的概率清零并重归一化。

### 5. 日内轨迹、动量、物理证据

温度轨迹：

```text
remaining_upside ~ f(current_temp, observed_max, local_time, historical_intraday_shape, cloud/rain/radiation)
T = max(observed_max, current_temp + remaining_upside)
```

温度动量：

```text
momentum = robust_slope(last 5m/10m/15m/30m/45m/60m observations)
```

物理证据：

```text
tail_adjustment = g(cloud_cover, precipitation, wind, humidity, solar_window, diurnal_stage)
```

三者进入观测/日内更新层，影响连续分布的均值、方差和上尾/下尾，不作为主池里的独立桶投票。

## 校准层级回退

避免 `station x lead_time x hour x regime` 切太细导致低样本。采用层级回退和 shrinkage：

```text
source + station + lead_bin + hour_bin + regime
source + station + lead_bin + hour_bin
source + station + lead_bin
source + similar_city_group + lead_bin + hour_bin
source + climate_zone + lead_bin
source + global + lead_bin
source_global
conservative_default
```

每层使用样本量收缩：

```text
alpha = n / (n + k)
effective_bias = alpha * local_bias + (1 - alpha) * parent_bias
effective_sigma = alpha * local_sigma + (1 - alpha) * parent_sigma
```

建议第一版参数：

```text
k_bias = 20
k_sigma = 40
min_n_local = 30
min_n_hour_regime = 80
```

`similar_city_group` 由气候相似城市/站点组成，优先补齐成都样本不足时的中间层；`climate_zone` 是更粗层。这里的“回退”只表示校准样本不足时向更粗统计层收缩，不表示保留旧加性 Bayes 池。

## 事实层与 API 契约

事实层写入：

```text
event_fact_history.fact_json.posterior_temperature_distribution
event_fact_latest -> 指向最新 fact
```

API：

```text
/api/ui_snapshot
/api/ui_snapshot_summary
```

只读 fact，不得在前端或端点内二次拼装另一套温度概率。若某项缺失，必须写明确 `*_na_reason`。

交易层只消费：

```text
bucket_probabilities
hard-source direct SSOT target
target_realization_*
posterior settlement target
```

不允许旧 `edge_min`、`hold_edge_realizable`、`lcb_*`、`stop_loss`、`trail_stop`、`weather_repricing*` 触发真实卖单。

## 验收指标

核心指标：

- Bucket Brier score：目标桶概率质量。
- Bucket logloss：尾部错误的惩罚。
- CRPS：连续温度分布质量，作为 v2 主指标。
- D0 violation rate：违反 `T >= observed_today_max` 的次数，目标为 0。
- Quantile coverage：p10/p50/p90 覆盖率。
- Hard-source consistency：官方最终/硬实况锁定后概率是否一致。
- Target churn：临近结算前目标桶概率的无效抖动。

上线门槛：

```text
D0 violation rate = 0
CRPS 优于 climo-only / naive historical baseline
Bucket Brier/logloss 不劣于旧主池历史回测基线
最后 3 小时 target churn 不高于旧主池历史回测基线
hard-source locked events 一致率 100%
```

## 迁移步骤

### Phase 0：只读样本体检

只读统计可训练样本：

```text
source, station_id, similar_city_group, climate_zone, lead_bin, hour_bin, regime
```

输出每层样本数、bias、sigma、MAE/RMSE、CRPS-ready 摘要，确认哪些层级回退节点可训练。

### Phase 1：影子验收

新增 `temperature_posterior_runtime.py`，做切换前只读验收。影子期只用于确认指标和输出契约，不作为长期并行口径：

```text
PM_TEMPERATURE_POSTERIOR_ENGINE=shadow
```

写入：

```text
posterior_temperature_distribution
q_temperature_bucket_posterior_v2_shadow
```

影子期结束后不保留 `q_temperature_bucket_posterior_v2_shadow` 作为 UI/API 口径。

### Phase 2：UI 对照

UI 增加只读诊断：

```text
v2 continuous posterior
quantiles/cdf
source calibration path
observation constraints
```

不得前端复算，只展示 fact。UI 不展示旧加性池与 v2 的长期并行对照；旧池只允许出现在一次性迁移验收报告中。

### Phase 3：走步回测 gate

按 ET 日口径回测，比较：

```text
old additive pool historical baseline
new continuous posterior
new posterior without TAF
new posterior with TAF
new posterior with group shrinkage on/off
```

验收通过后进入直接替换。

### Phase 4：直接替换

配置：

```text
PM_TEMPERATURE_POSTERIOR_ENGINE=primary
```

切换后，旧加性池入口下线：

```text
PM_TEMPERATURE_POSTERIOR_ENGINE=primary
PM_BAYES_ADDITIVE_EVIDENCE_POOL_ENABLED=0
```

任何 D0 violation、hard-source inconsistency、运行异常，进入 hard fail / no-trade 状态并写明确 `*_na_reason`，不得自动回退到旧加性池。

## 参数治理

当前 `.env` 中的 `prior_strength=12`、`TWC evidence=12`、`WU evidence=6`、`TAF evidence=2` 等参数，在 v2 中不再作为长期主权重。

切换原则：

- 旧 evidence 参数只允许用于一次性迁移验收报告，不得进入新引擎运行路径。
- 切换完成后删除或禁用旧加性池 env 入口，避免误触发。
- 新参数必须能映射到统计含义：bias、sigma、precision、样本量 shrinkage、group rho、precision cap。
- 参数变更必须先只读报告，再确认写入 `.env` 和重启。

## 最小实现清单

1. 增加只读样本体检脚本，产出 calibration availability report。
2. 新增 `temperature_posterior_runtime.py`，实现连续分布、CDF、分位数、桶积分。
3. 增加校准表/视图读取层，包含 hierarchical shrinkage / 层级回退和 similar city/climate zone。
4. 实现 source group shrinkage，避免 WU/TWC/TAF/model 重复计数。
5. 把实况最高温作为 D0 hard constraint。
6. 写入 `posterior_temperature_distribution` 到 fact JSON。
7. 增加 shadow 验收字段；切换后不得作为长期 UI/API 字段保留。
8. 增加回测指标：Brier、logloss、CRPS、D0 violation、quantile coverage、target churn。
9. 通过 gate 后直接切 primary；失败进入 no-trade/hard fail，不回退旧加性池。

## 不做项

- 不在第一版引入 Gaussian copula。
- 不做历史大范围回填，除非先出影响面并确认。
- 不让 UI 或前端重新计算温度概率。
- 不把 weak source 覆盖 hard observation。
- 不保留旧加性 evidence Bayes 池作为 fallback、诊断路径或长期并行口径。
- 不让旧 Edge/LCB/repricing 字段重新进入真实交易硬门槛。
