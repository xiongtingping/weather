# 预登记（三）：卫星「实测减晴空」作 P1 特征（2026-10-15，熊先生「测试」）

写在读数之前；读数后不改，结果补 RESULTS.md 第 9 节。只读研究，生产不改。

## 问题

P1 读的云只有 METAR `cloud_cover_fraction`。JAXA Himawari 官方逐小时实测短波（P-Tree，10-15 接入）
给出不依赖预报的天空状态 K = 实测 / 晴空 S0。上周研究全是「实测减预报」，没有人测过「实测减晴空」
直接进宪章 161 的直接更新。问：把 K 加进 P1 的特征，样本外 LogScore 变好吗？

## 数据

- 站：JAXA Himawari 覆盖且有转发历史的 16 站（RCSS RJTT RKPK RKSI RPLL WMKK WSSS ZBAA ZGGG ZGSZ
  ZHHH ZSPD ZSQD ZUCK ZUUU NZWN；以 `rad.source='OPENMETEO_jma_jaxa_himawari'` 为准）。
- 历史辐射：Open-Meteo 转发的同一 JAXA 产品（已核逐值相等，ZUUU 10-14 412.7 对 411.8），
  **只作研究**；生产读 P-Tree 官方。可得期 09-01..10-07。
- 行：前缀当前（`ol_state=APPLIED`）的 P1 决策行，事件日 09-01..10-07，受理回执带
  `features`/`root_features`/`clock_hours`，因此可以逐行复刻生产的 x。
- 真值：AWC/SETTLEMENT_SOURCE 最新修订。

## 特征（两项，事先固定）

- **K2**：决策时刻可得的最近两个完整白昼小时（S0 均值 > 50 W/m²）的 K 均值，K = SWR / S0_hour，
  截到 [0, 1.2]。**可得性**按 P-Tree 发布延迟：小时 h 的值在 h 结束后 75 分钟可读
  （`jaxa_ptree_radiation.PUBLISH_AFTER`）。不足两小时取一小时；一小时都没有 → 缺失。
- **K_gap**：K2 − K_metar，K_metar = a + b × `cloud_cover_fraction`，a、b 在训练行上最小二乘，按日前推。
  METAR 云量缺失 → 缺失。
- 缺失值按生产规则落到训练行均值（fallback）。

## 机器（复刻宪章 161）

x = [1, 时钟帽函数(α 结点), 标准化特征, 晚段门 × 特征, 中段门 × 特征]；f(k) = rise·(x·θ_rise)
+ slope·(x·θ_slope) + slope²·θ_sq + open_top·θ_top；P1 ∝ root × 掩码 × exp f；岭 λ = 1.0（现行拟合件值）；
L-BFGS；训练目标最终 P1 的 LogScore（生产下限 1e-3 后评分）。rung/rise/slope/open_top 用
`p1_direct_update.bucket_bases`，root 特征用受理回执里的 `root_features`。

三臂、逐日前推（评 D 日用 ≤ D−1 事件日训练，训练 ≥ 15 日才评）：
- **A**：生产特征集，本机重拟（控制「重拟本身」）；
- **B**：A + K2 + K_gap（主）；
- **C**：A + K2（诊断，分离 K_gap）。

## 判决（宪章 240）

主：**B − A** 的 ΔLogScore，站日等权，按日 bootstrap 2,000，95% 区间；ΔCRPS 结构、ΔBrier 护栏。
辅：A − 服务 v39（应 ≈ 0，验证复刻）；B − 服务 v39。
预登记切片：当地 12–18 时；距结算 6–10 h；|K_gap| > 0.2 的行（卫星与 METAR 不一致）；
ZUUU/ZBAA/WSSS（10-08 测得 METAR 与卫星相关最低的三站）。

**样本量**：16 站 × 约 17 个前推日 ≈ 270 站日，**低于 300 的门，本轮只叫读数**；
若读数为负且显著，须等 P-Tree 官方序列攒满再复核一次才能立项。

## 预期与止损

10-08 卫星总云量对日前 NWP 的 β 稳定 0.18–0.25 但样本外跨零。本测预期 0～0.005 nat。
B − A 区间跨零 → 不采用；ZUUU/ZBAA/WSSS 切片单独为负不构成立项。
