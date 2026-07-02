# Weather Data Source Charter

更新日期：2026-06-14

本文定义成都/ZUUU 天气市场概率系统的数据源宪章、证据通道、重复计算边界和上线最佳实践。核心目标是让 WU、Weather.com/TWC、NOAA AviationWeather/AWC、GFS/ECMWF/ICON/QM、TAF、METAR、物理证据、历史先验、偏差校准和市场盘口各归其位，避免同一信息链路被多次当作独立证据计入后验。

## 1. 总宪章

历史数据不是当前事件似然，主要是先验和校准集。

WU 历史最高温、历史误差、同季节同站点残差、历史模型表现，应用于 `P_history`、偏差校准、季节性校准、权重回测和 walk-forward 标签。它不能作为当前事件的实时证据，除非事件已经结算并形成 `settlement_actual`。

实时数据是强似然或硬约束。

最强实时证据不是单独的云、雨、风字段，而是：

1. 已观测温度轨迹。
2. 已观测最高温下界。
3. 升温率。
4. 距离本地最高温窗口还有多久。

云、雨、风、低云、辐射、湿球、气压和降水是解释和修正温度轨迹的物理证据。它们优先用于残差修正和质量门控，而不是取代最高温主分布。

预报数据是软证据。

WU/Weather.com forecast high、ZUUU TAF、GFS/ECMWF/ICON/QM 都是预报，但角色不同：模型集合是主预测骨架，WU/Weather.com 是结算生态软预报，TAF 是机场强短临软预报。任何预报都不能冒充实况或结算真值。

市场盘口不是天气证据。

Polymarket bid/ask、成交量、钱包和盘口跳变只能用于展示、交易、edge、异常监控和流动性判断，不进入天气后验。

## 2. 推荐 Bayes 拆法

当前系统应明确为 Dirichlet/BMA 伪样本融合，不是严格独立似然连乘：

```text
posterior_i = normalize(
  S_history * P_history_i
+ S_model   * P_model_i
+ S_wu_fcst * P_wu_forecast_i
+ S_taf     * P_taf_peak_i
+ S_obs     * P_observed_nowcast_i
+ S_phys    * P_independent_physics_residual_i
+ S_climo   * P_climo_regime_i
)
```

通道定义：

1. `P_history`：只用历史结算和历史残差，不含当前预报。
2. `P_model`：GFS/ICON/ECMWF/QM 的最高温分布；模型内部已经包含云雨风辐射等物理过程。
3. `P_wu_forecast`：WU 页面当天 forecast high；Weather.com/TWC forecast 若同源，只能并入同一个软预报通道。
4. `P_taf_peak`：ZUUU TAF 的 TX/TN 或短临趋势，强软证据。
5. `P_observed_nowcast`：AWC METAR + WU 结算页实时观测形成的温度轨迹、已观测最高温下界和升温率。
6. `P_independent_physics_residual`：独立或半独立物理证据对 `P_model` 的残差修正。
7. `P_climo_regime`：多年气候/环流背景弱参考，只提供历史同季基线、季风湿度、亚热带副高/环流 proxy、盆地通风 proxy，不覆盖实况轨迹。

当前代码兼容旧命名：

1. `P_observed_nowcast` 在部分运行时字段中仍叫 `P_obs` / nowcast evidence。
2. `P_independent_physics_residual` 在部分运行时字段中仍叫 `P_physics` / physical likelihood。
3. `P_wu_forecast` 的输入字段目前主要是 `wu_forecast_peak_c` / `settlement_source_forecast_peak_c`，运行时已拆出独立软预报通道，输出 `q_bayes_wu_forecast_*`。

### 2.1 计算口径

`i` 是某个温度桶，例如 `29°C`、`30°C`、`31°C`。

每个 `P_*_i` 都必须是同一组温度桶上的概率分布，单个通道内各桶概率和应为 1。`S_*` 不是概率，而是该通道的证据强度或伪样本权重，表示这一路证据在当前提前量、质量门控和回测表现下有多该被听。

某个桶的未归一分数为：

```text
score_i =
  S_history * P_history_i
+ S_model   * P_model_i
+ S_wu_fcst * P_wu_forecast_i
+ S_taf     * P_taf_peak_i
+ S_obs     * P_observed_nowcast_i
+ S_phys    * P_independent_physics_residual_i
+ S_climo   * P_climo_regime_i
```

最终后验为：

```text
posterior_i = score_i / sum(score_all_buckets)
```

示例：

```text
score_30 =
  2.0 * P_history_30
+ 5.0 * P_model_30
+ 2.0 * P_wu_forecast_30
+ 3.0 * P_taf_peak_30
+ 8.0 * P_observed_nowcast_30
+ 3.0 * P_independent_physics_residual_30
+ 0.5 * P_climo_regime_30

posterior_30 = score_30 / sum(score_all_buckets)
```

证据强度必须随可见性和质量变化：D-2 时 `S_model` 与 `S_history` 通常更重要；D0 且临近本地最高温窗口时，若 AWC METAR 轨迹质量正常，`S_obs` 应升高；若独立雷达、卫星、实测辐射缺失，`S_phys` 应降权或 shrink 到 0。

关键边界：同一信息不能重复算。如果 `P_model` 已经用了 GFS/ECMWF/ICON 的云雨风，那么 `P_independent_physics_residual` 不应再强吃同一批模型云雨风；它优先吃 AWC METAR、雷达、卫星、实测辐射和实况降水等更独立的证据。WU forecast 和 TAF 也必须各走自己的软预报通道，不能混进 nowcast 或物理残差。

## 3. 历史、实时、预报三分表

| 来源 | 历史 | 实时 | 预报 |
|---|---|---|---|
| WU | 结算真值/历史校准集，用作 `P_history`、回测标签、偏差和季节性校准 | 结算展示页实时观测，只能作已观测下界/轨迹锚点，并接受 AWC METAR 校验 | 结算页生态软预报，作 `P_wu_forecast`，不能当硬事实 |
| Weather.com / TWC | 历史 API 可作校准集，但注意它可能是模型/融合产品 | TWC blended current，可作实时展示证据，但要标记为融合源并和 WU dedupe | TWC proprietary forecast，作软预报候选，和 WU forecast 高相关，只能合并或二选一 |
| AviationWeather / AWC | 近 15 天 METAR/TAF 档案可作校验、回测和延迟/变形审计 | METAR 是 ZUUU 机场硬实况：温度、风、云、天气现象、气压、露点 | TAF 是机场强短临预报，作独立 `P_taf_peak` 或 `P_taf_conditions` |
| GFS/ECMWF/ICON/QM | 历史预报误差用于模型权重、QM、偏差和季节性校准 | 不提供实况；D0 最新模型仍是预报，不是 observed | 最高温分布作 `P_model`，模型云雨风只作模型内部解释或弱残差 |
| 雷达/卫星/实测辐射/降水站 | 历史可作 regime 校准和物理残差校准 | 独立/半独立实况物理证据 | 短临外推可作物理残差弱预报，必须质量门控 |
| Polymarket 盘口 | 历史成交可作交易策略校准 | 盘口、订单簿、钱包用于交易和异常监控 | 市场隐含概率不能进入天气后验 |

## 4. 数据源定位表

| 数据源 | source_id | 主角色 | 可进入通道 | 禁止用途 |
|---|---|---|---|---|
| WU 历史结算页 | `wu_settlement_page` | 结算展示页/历史真值 | 事件结束后生成 `settlement_actual`、`P_history`、校准标签 | D0 nowcast、传感器源头、物理实况 |
| WU 实时/当前显示 | `wu_settlement_page_realtime_display` | Polymarket/WU 结算生态展示源/实时包装源 | `P_observed_nowcast` 的轨迹锚点，必须被 AWC METAR 校验 | 最终最高温、Raw METAR、独立二次实况 |
| WU forecast high | `wu_forecast_high` | 结算生态软预报 | `P_wu_forecast` | observed nowcast、settlement actual、物理残差强证据 |
| Weather.com/TWC | `twc_provider` | WU 背后的 provider/API 包装方 | source lineage / provider dedupe | 机场原始观测、独立于 WU 的第二证据 |
| Weather.com 实时页 | `twc_realtime_display` | TWC blended current | 与 WU 实时合并为同 provider 显示证据 | 与 WU 实时分别加权 |
| Weather.com 预报页 | `twc_forecast_peak` | TWC proprietary soft forecast | `P_wu_forecast` 候选；若与 WU forecast 同源，只能合并 | 与 WU forecast 双计 |
| NOAA AWC Raw METAR | `zuuu_metar_raw_awc` | 官方机场 Raw METAR 硬实况/审计源 | `P_observed_nowcast`、已观测最高温下界、温度轨迹、AWC/WU 延迟/变形告警 | 事件最终结算、软预报 |
| IEM ASOS archive | `zuuu_metar_archive_iem` | 历史 METAR 归档 | 事后校验 AWC/WU 延迟或变形、历史复盘 | 实时 nowcast、结算事实 |
| ZUUU TAF TX/TN | `zuuu_taf_forecast_peak` | 机场短临强软预报 | `P_taf_peak`，权重由 created_at walk-forward 校准 | `P_physics`、硬实况、最终结算、已观测下界 |
| GFS/ECMWF/ICON/QM | `current_model_ensemble` | 数值模型成员分布 | `P_model`；QM/偏差/季节性校准后形成桶概率 | 再作为强物理独立证据 |
| GFS/ECMWF/ICON 物理字段 | `model_derived_physics` | 模型派生解释/残差上下文 | 解释、质量门控、低权重残差 | 与 `P_model` 同时强加权 |
| 独立/半独立物理观测 | `independent_physics_observed` | 实测物理证据 | `P_independent_physics_residual` | 替代模型主分布、替代结算 |
| 多年气候/环流参考 | `climo_regime_background` | 气候背景弱参考 | `P_climo_regime` | observed nowcast、结算事实、物理强似然、模型主分布 |
| 市场报价 | `market_realtime_orderbook` | 交易/展示/执行 | 展示概率、edge、流动性、异常检测 | 主后验证据 |

## 5. 证据通道定义

### 5.1 `P_history`

定义：从已结算历史市场、历史 WU final、同季节同站点残差、历史模型误差提取的先验和校准上下文。

允许：

1. `settlement_actual` 已解析且事件结束。
2. 当前样本 `created_at` 之前已经可见的历史结算与历史误差。
3. 防泄漏 walk-forward 生成的偏差校准、季节性校准、权重校准。

禁止：

1. 当前预报、当前市场价格、D0 WU 实时显示。
2. 任何 `created_at` 之后才可见的事实。
3. 把历史命中率当作当前事件似然。

### 5.2 `P_model`

定义：由 GFS/ECMWF/ICON 等数值模型成员，经偏差校准、QM、残差扩展、整数桶模拟得到的当前最高温分布。

允许：

1. 当前 `created_at` 之前可见的模型成员。
2. 由历史 walk-forward 校准得到的模型权重、偏差、QM、季节性修正。
3. Chengdu/ZUUU local residual model，但必须有 provenance、样本数和防泄漏证明。

禁止：

1. WU final actual。
2. TAF、WU forecast、Weather.com forecast 直接混入模型成员云。
3. 市场价格。
4. 把模型派生云雨风再当独立强物理似然重复加权。

### 5.3 `P_wu_forecast`

定义：结算生态展示页的当天软预报高温，例如 WU forecast high；Weather.com/TWC daily high 若与 WU 同源，只能合并为同一通道。

允许：

1. `wu_forecast_peak_c`。
2. `settlement_source_forecast_peak_c`。
3. dedupe 后的 Weather.com/TWC forecast peak。

禁止：

1. 写入 `observed_nowcast`。
2. 写入 `settlement_actual`。
3. 和 WU realtime/current 混成模糊的“WU 数据”。
4. 同时在 `P_wu_forecast` 和 `P_independent_physics_residual` 中重复计入。

### 5.4 `P_taf_peak`

定义：从 ZUUU TAF 原文解析 `TXxx/DDHHZ` 得到目标事件日期机场预报最高温，并映射成温度桶分布。

定位：机场强短临软预报。它比普通远期模型更贴近机场运行场景，但仍是预报，不是实况、不是结算事实、不是物理残差。

宪章：

```text
source_id: zuuu_taf_forecast_peak
raw_source: https://aviationweather.gov/api/data/taf?ids=ZUUU&format=raw&metar=false
station_required: ZUUU
role: model_evidence / strong_soft_forecast
channel: P_taf_peak
field: taf_forecast_peak_c
visibility: issued_at <= sample_created_at
validity: TAF valid window must cover target event date or TX token local date must match event date
status_default: disabled_until_created_at_walk_forward_calibrated
```

禁止：

1. 塞进 `P_independent_physics_residual`。
2. 当作 METAR observed 或 WU final。
3. 硬排除相邻桶。
4. 在 AWC/WU critical alert 且缺少其它强实况时一票拉偏。

### 5.5 `P_observed_nowcast`

定义：D0 盘中已观测温度轨迹、已观测最高温下界、升温率、距本地最高温窗口小时数，对最终最高温桶的约束。

强来源：

1. `zuuu_metar_raw_awc`：NOAA AviationWeather Raw METAR。
2. AWC/METAR 里的温度、云层、天气现象、风、气压、露点。

中弱来源：

1. WU realtime/current display。
2. Weather.com/TWC realtime/current display。

约束：

1. 必须有 observation_time、source_time、created_at。
2. 已观测最高温只能排除不可能低桶或提高轨迹一致桶概率，不得锁定最终桶。
3. WU/Weather.com 实时显示若与 AWC Raw METAR 差异过大，必须触发 `weather_source_delay_deformation_alert` 并降权或禁用。

### 5.6 `P_independent_physics_residual`

定义：独立或半独立物理证据对 `P_model` 残差的修正，不是第二套天气模型主预测。

优先使用：

1. AWC METAR 实况里的云层、降水/天气现象、风、气压、露点和温度轨迹。
2. 雷达/QPE 降水、卫星低云、实测短波辐射、降水站。
3. 湿球约束、边界层混合、风向/平流、气压趋势。

模型派生字段规则：

1. GFS/ECMWF/ICON 输出的云、雨、风、辐射已经参与其温度预测，只能作为 `model_context`、解释或低权重残差。
2. 若没有独立观测锚点，`S_phys` 必须质量门控降权。
3. `temperature_distribution` 可用于尾部上限或合理性检查，但不得作为独立物理证据再次提高同一桶。

### 5.7 `P_climo_regime`

定义：多年气候/环流背景弱参考层，回答“在不知道当前强实况轨迹前，成都/ZUUU 这个季节和环流 proxy 通常支持哪一段最高温”。

允许：

1. 多年同月/同旬历史最高温 mean/P50/P90/P95。
2. 目标年份以前的历史再分析或站点气候参考，例如 Open-Meteo Archive reference。
3. 露点异常、季风湿度 regime、亚热带副高/环流 proxy、盆地通风 proxy。

禁止：

1. 作为 `P_observed_nowcast` 或已观测最高温下界。
2. 覆盖 AWC METAR/WU 实况温度轨迹。
3. 替代 `P_model` 的当前数值模式最高温分布。
4. 塞进 `P_independent_physics_residual` 当作强物理证据。
5. 使用目标事件年份内尚未在样本 `created_at` 前可见的结算事实。

权重规则：

1. 默认弱权重，D0 越接近最高温窗口越低。
2. 必须可关闭、可回测、可用 created_at-safe walk-forward 校准。
3. UI/日志必须显示“气候/环流弱参考，不覆盖实况温度轨迹”。

### 5.8 `P_market`

定义：Polymarket 盘口价格、订单簿、成交量、钱包行为。

定位：

1. 展示、交易执行、异常监控、操盘识别、edge 计算。
2. 不进入主后验，避免市场价格污染天气概率。
3. 可作为“市场偏离模型”的交易信号，但不是天气事实。

## 6. 云、雨、风和物理数据优先级

1. AWC METAR 实况：最干净，机场站点真实观测，适合硬 nowcast 和轨迹约束。
2. 雷达/卫星/实测辐射/降水站：最适合 `P_independent_physics_residual`，因为更接近独立物理证据。
3. TAF：强短临预报，独立 `P_taf_peak`，不塞进物理似然。
4. TWC/WU 实时融合数据：有用，但要防止和 WU forecast / TWC forecast 重复。
5. 模型云雨风：可以解释模型分布，但不应和模型最高温分布重复加权，除非只做残差修正并经 walk-forward 校准。

## 7. 重复计算风险矩阵

| 风险 | 典型错误 | 正确处理 |
|---|---|---|
| WU forecast 与 Weather.com forecast 双计 | 两个 forecast high 同时加权 | 识别为 TWC/provider 同源，合并为一个 `P_wu_forecast` |
| WU realtime 与 Weather.com realtime 双计 | 两个页面当前温度分别作为实况 | 合并为同 provider display nowcast，并用 AWC Raw METAR 校验 |
| AWC Raw METAR 与 AWC JSON METAR 双计 | Raw 和 JSON 都加入 `P_observed_nowcast` | Raw 为主，JSON 只作解析 fallback 或一致性校验 |
| METAR observed 与 WU history final 混淆 | D0 把 WU 页面高温当 final | D0 只做 observed lower bound；final 只在结算后进入 |
| 模型温度与模型物理字段双计 | `P_model` 用 GFS 温度，`P_physics` 又强用 GFS 云雨风 | 模型派生物理只作解释/弱残差；强证据需独立观测 |
| WU forecast 与物理残差混用 | WU forecast high 同时作为 residual correction 与软预报 | 拆成 `P_wu_forecast`，不能两边加 |
| TAF 与物理证据混用 | TAF 塞进 `P_physics` 或改写模型成员 | 独立 `P_taf_peak`，由 walk-forward 校准权重 |
| 历史先验泄漏 | 用目标日之后结算结果训练当前样本 | 每个样本只读 `created_at` 前可见数据 |
| 市场价格污染后验 | 用盘口概率拉天气概率 | 市场只用于交易/异常，不用于天气后验 |

## 8. 数据血缘必填字段

任何进入后验或校准的数据，必须至少带这些字段：

```text
source_id
provider
station_id
raw_url_or_endpoint
created_at
fetched_at
issued_at
observed_at
valid_start_at
valid_end_at
target_event_date
market_unit
source_role
posterior_channel
source_usable_for_settlement
is_observed
is_forecast
is_provider_wrapper
is_sensor_origin
lineage_parent_source_id
quality_status
na_reason
```

没有 `created_at` 或可见性时间的字段，不能用于 walk-forward 证明。

## 9. 成都/ZUUU 目标架构

```text
WU history final
  -> settlement_actual after close
  -> P_history / calibration labels

GFS + ECMWF + ICON + QM/local/seasonal calibration
  -> calibrated peak-temperature member distribution
  -> P_model

WU forecast high + deduped Weather.com/TWC forecast high
  -> settlement-ecosystem soft forecast
  -> P_wu_forecast

ZUUU TAF TX/TN
  -> airport short-range strong-soft forecast
  -> P_taf_peak after created_at walk-forward calibration

AWC Raw METAR ZUUU
  -> current temp / observed max lower bound / trajectory / rise rate
  -> P_observed_nowcast
  -> WU/TWC delay-deformation alert

WU realtime + Weather.com/TWC realtime
  -> provider display nowcast
  -> P_observed_nowcast only after AWC cross-check and provider dedupe

Observed radiation/cloud/precip/wind/wetbulb
  -> independent physical residual correction
  -> P_independent_physics_residual

Polymarket order book/trades
  -> display/tradeability/anomaly
  -> not posterior evidence
```

## 10. 权重、回测和上线门槛

1. 所有 `S_*` 权重只能由 created_at 防泄漏 walk-forward 或明确质量门控产生。
2. 每个通道单独评估，再评估组合，不得只看组合改善。
3. 分层必须至少包括 D-2/D-1/D0、距最高温窗口、天气 regime、强/弱实况质量。
4. 主指标：Brier、RPS、logloss、MAE/RMSE、校准曲线。
5. 保护指标：D0 不退化、RPS 不退化、尾桶不过度自信、单一弱源不能把桶概率拉极端。
6. 若某通道样本不足，权重必须 shrink 到 0，而不是沿用默认强权重。
7. 临近最高温窗口，刷新频率提高并以秒级 `recommended_interval_sec` / `*_REFRESH_*_SEC` 执行；默认近峰窗口可压到 30 秒，UI/日志必须显示秒级 `reference_time` 和 `latest_observation_time`。证据角色不变：AWC Raw METAR 是硬实况/审计源，TAF 是预报，WU/TWC page 是结算生态展示/包装源。

## 11. 推荐实施顺序

1. 固化 `weather_source_lineage` 与 `weather_source_delay_deformation_alert`，让 UI/日志可见。
2. 把 WU forecast high 从 `P_physics` 中拆成 `P_wu_forecast`，并 provider dedupe；Weather.com forecast 后续并入同一通道。
3. 把 `P_physics` 语义收紧为 `P_independent_physics_residual`，剔除强模型派生重复项，并保留旧字段兼容。
4. 对 `P_taf_peak` 做 created_at walk-forward 校准，先输出 shadow 字段。
5. 用分层回测选择 `S_wu_fcst`、`S_taf`、`S_obs`、`S_phys`，不达标保持 0 或 shadow。
6. 成都小白名单上线前，保留一键回退环境变量，并在 UI 暴露每个通道的 evidence/weight/prob。

## 12. 不变量

1. `settlement_actual` 只代表最终已解析结算事实。
2. `observed_today_max_c` 是 D0 已观测下界，不是最终最高温。
3. WU/TWC 不是 ZUUU 原始传感器源。
4. AWC METAR 是硬实况，TAF 是强软短临预报。
5. 模型派生物理不等于独立物理观测。
6. 市场价格不进入天气后验。
7. 任何新通道上线前必须有 created_at 防泄漏证明。

## 13. 当前代码状态映射

当前已具备：

1. `bayesian_data_role_contract.py` 中已有数据角色审计。
2. `scan_weather_market.py` 已写入 `weather_source_lineage` 和 AWC/WU 延迟/变形告警。
3. `bayesian_probability_runtime.py` 使用 Dirichlet/BMA 伪样本融合，并输出 `score_components`。
4. `P_wu_forecast` 已拆成独立软预报通道，输出 `q_bayes_wu_forecast_*`；默认 evidence 为 0，需 walk-forward 校准后给非零权重。
5. `P_independent_physics_residual` 已作为 `q_bayes_physical_likelihood_*` 的语义别名输出，并新增 `q_bayes_independent_physics_residual_*` 字段。
6. `bayesian_likelihood_runtime.py` 默认不再把 WU forecast high 当作 physical residual 组件；legacy env `PM_BAYES_SETTLEMENT_SOURCE_FORECAST_WEIGHT` 仍可显式开启用于对照实验。
7. `P_taf_peak` 代码路径存在，但默认关闭。

当前仍建议改进：

1. Weather.com realtime/forecast 与 WU 的 TWC 同源关系应在落库字段中显式 dedupe。
2. `P_physics` 旧命名仍需在下游 UI/报告中逐步迁移到 `P_independent_physics_residual`。
3. TAF、WU forecast、独立物理残差权重都需要未来样本或历史可见 snapshot 的 created_at-safe 校准。

一句话：WU 历史是先验和标签；AWC METAR 是硬实况；TAF 是强软短临；WU/Weather.com 预报是结算生态软预报；模型是数值分布；真正的物理似然最好来自实时独立观测、雷达、卫星、辐射和降水，而不是把模型里的云雨风再算一遍。
