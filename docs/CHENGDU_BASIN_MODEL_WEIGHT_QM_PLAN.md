# 成都三大模型 D1/D0 命中率、QM 与四川盆地局地化权重方案

更新时间：2026-06-30

## 结论

本方案回答一个核心问题：三大模型最高温预测在成都 ZUUU / Polymarket 最高温桶结算里，能否因为最近一年 D1/D0 hit rate 更高而给某个模型更高权重，以及怎样把季节、四川盆地地形和天气机制纳入系统，而不是过拟合硬编码。

当前结论是：

1. **不能直接按 exact top1 hit rate 改线上权重。**
   当前 live D1/D0 样本太少：D1 29 个市场日，D0 最高温窗口前 23-24 个市场日。这个样本可以做方向性诊断和 shadow，不足以支撑线上权重切换。

2. **D1 与 D0 的“最高模型”取决于指标。**
   - D1 exact top1：ECMWF 与 GFS 同为 20.7%，ICON 10.3%。
   - D1 分布指标：ECMWF Brier / RPS / logloss 综合最好。
   - D0 最高温窗口前 exact top1：ICON 39.1% 最高，GFS 20.8%，ECMWF 8.7%。
   - D0 最高温窗口前分布指标：ICON Brier 最好，GFS RPS 最好，ECMWF logloss 最好。
   因此当前证据不是“某一个模型绝对更高”，而是“D1 更偏 ECMWF/GFS，D0 pre-peak 出现 ICON/GFS 优势信号，但需 shrink”。

3. **QM 有必要，但只能继续 shadow，不能直接接管。**
   现有 strict created_at walk-forward QM 回测显示整体改善：
   - Brier 改善 6.3%
   - RPS 改善 19.1%
   - logloss 改善 70.5%
   - D0 分层 Brier 改善 8.3%、RPS 改善 21.9%
   但有效市场日只有 5 天，所以结论是：QM 是必要方向，但正式接管必须等待更多 live 样本和分层不退化验证。

4. **正确方案不是硬编码权重，而是分层 precision / shrinkage。**
   模型权重应来自走步验证后的误差方差、Brier/RPS/logloss、DTE、季节、盆地通风/冷池 regime，并向 equal weight 收缩；任何 D0/高温/冷池分层样本不足时，只能展示或 shadow。

## 当前证据

证据源：

- `data/reports/chengdu_model_hit_rate_audit_latest.json`
- `data/reports/chengdu_qm_walk_forward_strict_latest.json`
- 审计脚本：`scripts/chengdu_model_hit_rate_audit.py`

### 1. 样本覆盖

```text
actual_days: 366
historical_reference_market_days: 335
live_d1_market_days: 29
live_d0_pre_peak_market_days: 24
live_d0_pre_peak_rows: 70
```

重要解释：

- `historical_reference` 来自 `open_meteo_historical_forecast`，可用于长期偏差和季节参考，不能当作真实 D1/D0 交易时可见样本。
- `live_d1` 与 `live_d0_pre_peak` 只使用 `official_updated_source=observed` 的模型缓存。
- `D0_pre_peak` 默认定义为成都本地 12:00 最高温窗口开始前最后可见快照；这是保守交易口径，不是事后实际峰值前任意时间。

### 2. Hit Rate 口径必须拆开

`exact_top1_hit_rate` 是最严格的“单个最高概率整数桶必须刚好等于结算桶”，它会明显低于交易方向命中。

D1：

| model | exact top1 | Top3 包含真实桶 | 均值 ±1C | >=30 方向 | >=31 方向 | >=32 方向 |
|---|---:|---:|---:|---:|---:|---:|
| GFS | 20.7% | 72.4% | 65.5% | 72.4% | 75.9% | 79.3% |
| ICON | 10.3% | 48.3% | 51.7% | 69.0% | 72.4% | 82.8% |
| ECMWF | 20.7% | 65.5% | 51.7% | 72.4% | 75.9% | 82.8% |

D0 pre-peak：

| model | exact top1 | Top3 包含真实桶 | 均值 ±1C | >=30 方向 | >=31 方向 | >=32 方向 |
|---|---:|---:|---:|---:|---:|---:|
| GFS | 20.8% | 54.2% | 58.3% | 66.7% | 75.0% | 87.5% |
| ICON | 39.1% | 52.2% | 52.2% | 65.2% | 65.2% | 82.6% |
| ECMWF | 8.7% | 43.5% | 47.8% | 52.2% | 60.9% | 73.9% |

解释：

- exact top1 适合衡量“精准单桶结算命中”，不适合作为唯一交易权重指标。
- Top3 / ±1C / 阈值方向更接近交易判断，尤其 Polymarket 目标常是 `>=30`、`>=31`、`>=32` 这类边界。
- 权重必须以分布指标为主，hit rate 为辅。

### 3. 单模型分布指标

分布指标使用成员落桶概率，越低越好。

D1：

| model | Brier | RPS | logloss |
|---|---:|---:|---:|
| ECMWF | 0.8680 | 0.1118 | 4.4490 |
| GFS | 0.9014 | 0.1150 | 5.2027 |
| ICON | 0.9560 | 0.1270 | 8.8791 |

D0 pre-peak：

| model | Brier | RPS | logloss |
|---|---:|---:|---:|
| ICON | 0.8948 | 0.1223 | 7.4540 |
| GFS | 0.9435 | 0.1152 | 8.0541 |
| ECMWF | 0.9563 | 0.1366 | 6.3454 |

解释：

- D1：ECMWF 分布质量最稳，GFS 次之，ICON 弱。
- D0 pre-peak：ICON 的 Brier 最好，GFS 的 RPS 最好，ECMWF 的 logloss 最好。
- D0 不是单一模型明显胜出，因此更适合“分层 shrinkage 权重”，不适合“硬编码 ICON 更高权重”。

### 4. 历史回填参考

`open_meteo_historical_forecast` 近一年参考集有 335 天，不是 D1/D0 as-of 样本，但可用于长期偏差和季节先验。

整体 exact round hit：

| model | hit | MAE | bias |
|---|---:|---:|---:|
| ECMWF | 76.4% | 1.02C | -0.70C |
| GFS | 70.7% | 1.45C | -0.98C |
| ICON | 69.0% | 1.20C | -0.63C |

夏季参考：

| model | hit | MAE | bias |
|---|---:|---:|---:|
| ECMWF | 50.8% | 0.68C | -0.36C |
| GFS | 38.1% | 1.36C | -1.09C |
| ICON | 30.2% | 1.46C | -0.10C |

解释：

- 历史参考强烈提示 ECMWF 在长期和夏季更稳。
- 但它不能直接证明实时 D1/D0 权重，因为来源是事后 historical forecast proxy。
- 它适合进入层级先验和偏差/QM 训练，不适合直接作为 live hit rate。

## 是否应该设计更高权重

应该设计“可学习的更高权重机制”，但 **现在不应该直接把线上权重改高**。

原因：

1. live D1/D0 样本不足 80 个市场日。
2. D0 指标不一致：ICON exact/Brier 强，GFS RPS/阈值强，ECMWF logloss 强。
3. 三大模型高度相关，直接按 hit rate 加权会重复计数同一大尺度天气信号。
4. 成都高温误差有明显 season/regime 依赖，固定模型权重会在某些 regime 过拟合。

推荐机制：

```text
model_precision(model, dte, season, regime)
  = shrink(
      1 / calibrated_error_variance(model, dte, season, regime),
      target = equal_weight_precision,
      n_eff = live_sample_count,
      shrinkage_floor = global_model_precision
    )

model_weight = precision / sum(precision)
```

上线规则：

- `n_live_market_days < 30`：只展示，不参与 shadow 权重。
- `30 <= n_live_market_days < 80`：允许 shadow weight，不接管 posterior。
- `n_live_market_days >= 80` 且 D1/D0/Brier/RPS/logloss/hot-day/regime 不退化：允许小权重接管。
- 单模型权重上限建议 45%，避免相关模型中某一个因小样本偶然胜出。

## QM 是否必要

必要，但应分层上线。

理由：

1. 三大模型对成都最高温有系统偏冷，尤其 D0/D1 高温日。
2. 单纯 static shift 只修均值，不修尾部和桶边界概率。
3. QM 可以修正成员分布形状、桶概率和尾部，但极易过拟合。
4. 当前 strict walk-forward QM 已显示改善，但样本只有 5 个 live 市场日，必须继续 shadow。

推荐分层：

```text
L0 raw members
L1 station/season static bias
L2 model-wise QM, with DTE and season shrinkage
L3 residual expansion, fixes over/under dispersion
L4 Sichuan Basin local residual model
L5 bucket simulation
L6 CTP posterior fusion
```

推广门槛：

- QM fit 最少 60 个历史日，live shadow 最少 30 个市场日。
- 正式接管至少 80 个 live 市场日。
- D1 与 D0 pre-peak 分层都不能退化。
- 夏季高温日 `actual >= 30C` 不能退化。
- logloss 不能因尾桶过度自信而恶化。
- 成员覆盖率 >= 95%。

## 如何防止过拟合

硬规则：

1. 所有训练样本必须满足 `feature_created_at < prediction_created_at`。
2. `settlement_actual` 只能在事件结算后进入训练，不能进入当前样本特征。
3. 历史回填 forecast proxy 只能训练长期偏差/季节参考，不能当 live D1/D0 证明。
4. 所有 DTE、季节、regime 细分都要层级回退。
5. 禁止按某一天、某个桶、某个近期事件写固定权重。

层级回退：

```text
station + model + dte + season + regime
-> station + model + dte + season
-> station + model + dte
-> station + model
-> basin_similar_city + model + season
-> model_global
-> equal_weight_default
```

有效样本数：

```text
n_eff = n / (1 + rho * (n - 1))
```

其中 `rho` 表示同源/同天气型样本相关性。若没有可靠估计，先用保守 `rho = 0.15-0.30`。

权重收缩：

```text
precision_final =
  alpha * precision_segment
+ (1 - alpha) * precision_parent

alpha = n_eff / (n_eff + shrinkage_k)
```

建议：

- `shrinkage_k = 40` 用于 live D1/D0。
- `shrinkage_k = 80` 用于 regime 细分。
- 单模型最终权重 cap = 45%。
- 单个 regime 修正对均值影响 cap = 1.0C。
- 单个 regime 修正对任意桶概率影响 cap = 8pp。

## 四川盆地与季节机制

目标不是写“成都固定加几度”，而是把地形和天气机制作为残差模型或 regime 条件，让模型自己学何时偏冷、何时偏热。

### 1. 季节

特征：

- month
- season
- day_of_year_sin / cos
- climatological P50 / P90
- 同季节历史残差

作用：

- 进入 climate prior。
- 进入 model bias / QM 分层。
- 不允许单独覆盖 D0 实况轨迹。

### 2. 四川盆地通风

机制：

- 弱风、低层通风差、盆地边界层滞留时，白天近地面热量积累可能超出粗网格模型。
- 强风/降水/厚云时，盆地热积累被打断。

特征：

- 10m wind speed
- wind direction
- boundary layer proxy
- cloud cover
- shortwave radiation
- precipitation
- dew point / humidity
- pressure tendency

输出：

```text
basin_ventilation_regime:
  stagnant_hot
  moderate_ventilation
  ventilated_cool
  rain_cooled
  unknown
```

### 3. 冷池与冷池反弹

冷池不是固定加权项，而是条件 regime：

冷池形成条件：

- D-1 或 D0 早晨降水/雷暴
- 夜间或上午低云
- AWC/METAR 温度低于模型路径
- 湿度高、风弱
- 已观测温度上升慢

冷池维持条件：

- 低云持续
- 短波辐射低
- 降水持续
- 升温率 < 同季节历史 p25

冷池反弹条件：

- 云裂开或降水停止
- 短波辐射恢复
- 低层风弱仍滞留，但地面快速升温
- 30m/45m/1h 稳定升温率确认

模型作用：

```text
if cold_pool_persisting:
  shift upper tail down, widen uncertainty
elif cold_pool_rebound_confirmed:
  shift upper tail up, but require observed trajectory confirmation
else:
  shrink to 0
```

约束：

- 冷池 prior 只影响背景/残差，不允许压过 `observed_today_max_c`。
- 反弹必须由温度轨迹或辐射/云量确认后才能进入 D0 likelihood。
- 样本不足时只展示，不参与自动权重。

## 与当前 CTP 架构的结合

现有重构方向是 `Continuous Temperature Posterior Engine`。本方案应接入 CTP，而不是回到加性 evidence 池。

推荐结构：

```text
P_climo(T | station, season, basin_regime)
  -> slow prior

P_model(T | GFS, ICON, ECMWF, QM, dte, season)
  -> calibrated model likelihood / source component

P_obs(T | observed_today_max, AWC/METAR trajectory)
  -> D0 hard constraint + trajectory likelihood

P_phys_residual(T | radiation, cloud, rain, ventilation, cold_pool)
  -> residual likelihood with quality gate

posterior_temperature_distribution
  -> bucket_probabilities
  -> target settlement probabilities
```

不再允许：

- `climo` 和 `prior` 作为两个并列投票源重复计数。
- 模型云雨风字段既进入模型成员，又作为强独立物理证据重复加权。
- 近期小样本 exact hit 直接写死模型权重。

## 实施计划

### Phase 0: 已完成

- 新增只读脚本 `scripts/chengdu_model_hit_rate_audit.py`。
- 输出 `data/reports/chengdu_model_hit_rate_audit_latest.json`。
- 输出 strict QM 回测 `data/reports/chengdu_qm_walk_forward_strict_latest.json`。
- 增加测试 `tests/test_chengdu_model_hit_rate_audit.py`。

### Phase 1: Shadow 权重（已落地）

`scripts/chengdu_model_hit_rate_audit.py --write-latest` 已输出：

```text
model_weight_shadow:
  status=shadow_only
  applied_to_runtime=false
  weighting_basis=[brier, rps, logloss, crps_c]
  excluded_from_weighting=[exact_top1_hit_rate]
  fallback_order=[
    station+dte+season+basin_regime,
    station+dte+season,
    station+dte,
    station_global,
    similar_city_climate_zone,
    equal_weight
  ]
  segments[]:
    dte
    season
    basin_regime
    sample_market_days
    shadow_display_allowed
    runtime_promotion_allowed
    not_online_reasons
    shrinkage.alpha
    model_weights[].raw_weight
    model_weights[].shrunk_weight
    model_weights[].shadow_weight
    model_weights[].metrics
    model_weights[].diagnostics_only.exact_top1_hit_rate

basin_regime_feature_set:
  status=feature_contract_and_sample_coverage
  applied_to_runtime=false
  features=[dte, season, basin_regime, similar_city_climate_zone]
  sample_strata[]
  fallback_order[]
  promotion_blockers[]
```

规则：

- 不改 `q_raw`。
- 不改 auto trade。
- 不把 `exact_top1_hit_rate` 放入权重公式；它只进入 `diagnostics_only`。
- UI 只展示权重建议、样本数、分层、shrinkage 与不能上线原因。
- `/api/ui_snapshot_summary` 与 `/api/ui_snapshot` busy fallback 均只读附加上述字段，不写 `archive.db`。

### Phase 2: QM shadow

保留当前 QM，但补充：

- D1/D0 pre-peak live day 计数。
- hot-day 分层。
- cold_pool / basin_ventilation 分层。
- season 分层。
- 每层 Brier/RPS/logloss/CRPS。

### Phase 3: Local residual v1

先用 Ridge / Huber，不用复杂黑箱。

目标：

```text
residual_c = settlement_actual_c - model_calibrated_mean_c
```

特征：

- model mean/spread/skew
- model disagreement
- month / season
- basin ventilation regime
- cloud/rain/radiation
- AWC/METAR observed trajectory
- cold_pool state
- hours_to_peak

输出：

- residual mean correction
- residual sigma expansion
- feature_quality
- shrinkage reason

### Phase 4: 小范围接管

仅当以下条件满足：

- live D1/D0 pre-peak 市场日 >= 80。
- QM 或 local residual 对 Brier/RPS/logloss/CRPS 至少两个核心指标改善。
- D1、D0、hot-day、season、cold_pool 分层无显著退化。
- 单模型权重 cap 与 bucket pp cap 均通过。

## 验收标准

主指标：

- exact top1 只作诊断，不作唯一 gate。
- Top3 contains actual 提升。
- mean within 1C 提升。
- Brier 改善 >= 3%。
- RPS 改善 >= 3%。
- logloss 不退化。
- CRPS 改善 >= 3%。

保护指标：

- D0 pre-peak 不退化。
- hot-day `actual >= 30C` 不退化。
- `>=30 / >=31 / >=32` 阈值方向不退化。
- cold_pool_persisting 与 cold_pool_rebound_confirmed 分层不退化。
- D0 hard constraint violation = 0。
- 任意单模型权重 <= 45%。
- 任意单个 local/regime 修正 bucket delta <= 8pp，均值修正 <= 1.0C，除非有 hard observation。

## 当前建议

1. 当前不改线上三大模型权重。
2. D1 shadow 可以轻微偏向 ECMWF/GFS，但必须 shrink 到接近等权。
3. D0 pre-peak shadow 可以记录 ICON exact/Brier 和 GFS RPS/阈值优势，但不硬编码。
4. QM 继续 shadow，并扩大 live 样本。
5. 四川盆地和冷池机制作为 regime / residual model 进入 CTP，不作为固定 evidence 常数。
6. `model_weight_shadow` 和 `basin_regime_feature_set` 已进入只读事实层输出；下一步是积累 live D1/D0 样本并补齐 basin_regime 的真实特征入库。
