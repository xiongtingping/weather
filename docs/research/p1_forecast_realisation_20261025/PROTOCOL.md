# METAR「预报云雨是否兑现」作 P1 似然特征（新根上，预先登记，2026-10-25）

熊先生 10-25「按协议重测 METAR「预报云雨是否兑现」作 P1 似然特征（新根上）」。09-23 测试二：P1 已吃掉
实况云量本身，但「实况 − 预报」控制实况后偏相关 云 −0.105、降水 −0.085（宪章 209 未上线）。

## 样本

`p1_observation_samples.db`（每条 METAR 一行，P1 生产用的同一张样本表），根取宪章 281 新根
（`p0_ngr_root_replay.db` 的 `p1_root`，09-29 起；P0-M 根不乘 TAF——宪章 281 补记二）。

## 新特征（每行在其 `as_of_utc` 时已知）

1. `real_cloud_nwp` ＝ METAR 云量 − 当日 NWP 预报日均云量／100（`calibration_station_forecast_physics_version`
   前向版本，取 `captured_at_utc ≤ as_of` 最新）
2. `real_precip_nwp` ＝ METAR 有降水(0/1) − 1[该当地小时 NWP 逐小时降水 ≥ 0.1 mm]（宪章 208 版本表，as-of）
3. `real_cloud_taf` ＝ METAR 云量 − 该小时 TAF 基础组云量（TAF 取 as_of 前最新一份覆盖该小时者，原文归档）
4. `real_precip_taf` ＝ METAR 有降水 − 1[该小时 TAF 基础组或 TEMPO/PROB 有降水]

缺失按 P1 现有规则以训练集机队均值填（「此处未知」）。

## 臂

| 臂 | g 的特征 |
|---|---|
| B（现行） | `PHASE_FEATURES` / `FULL_FEATURES` |
| A | B ＋ 四个兑现特征（两层都加） |
| A_nwp / A_taf | 只加 1–2 / 只加 3–4（读数） |

α 两臂相同：取现行产物的 α 曲线（只比较 g 的特征）；其余（站钟、硬界、根）全同。

## 前推与判据

逐事件日前推：每个打分日 D，g 只用 D 之前且真值已提交的样本拟合；打 D 的全部行（root × mask × g^α，
下限后）。打分日 10-06 起（≥7 个训练日）。主比较 **A − B**：行级 LogScore 差按事件日 bootstrap，宪章 240
（95% 上界 < 0、RPS/Brier 不显著变差、≥20 可评事件日；不足 20 只叫读数，满 20 原样复跑）。过门才提请上线。

## 不做

不改生产；SSOT 只读；不调用 Open-Meteo。
