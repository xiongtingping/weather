# NWS 官方点预报最高温，是不是市场在分歧日多掌握的那部分信息（计算前写定，2026-09-26）

熊先生：「先做一个只读的小试。只取美国 11 个站，拿 NWS 官方点预报的最高温，按本次同一方法前推验收，看它是否就是市场在分歧日多掌握的那部分信息。」

续 `regional_feature_centre_20260926`：区域模式补不上 P0 在分歧日的中心缺口，市场的优势来自我们信息集之外。本研究只读，生产与 SSOT 均不改。

## 数据

- **决策**：`model_market_gap_decomposition_20260926` 的「现行算术 P0 × 同刻盘口」，距结算 24–60h，美国 11 站，事件日 08-16..09-23（缓存里的全部美国事件日；NWS 归档不受区域模式采集起点限制）。
- **来源**：各 WFO 发布的 PFM（Point Forecast Matrices）原文，经 IEM AFOS 归档取回。IEM 标 `aggregator_republisher`。原文只抽出本站那一段，存在本目录；不进 SSOT，不进任何生产车道。
- **站点对应**（PFM 点就在站上的 10 站进入判定）：

| 站 | WFO | PFM 点 |
|---|---|---|
| KATL | FFC | Atlanta-Fulton GA（33.64N 84.43W，即 Hartsfield） |
| KAUS | EWX | Austin Bergstrom-Travis TX |
| KBKF | BOU | Buckley AFB-Arapahoe CO |
| KDAL | FWD | Dallas Love Field-Dallas TX |
| KLAX | LOX | Los Angeles Airport CA |
| KLGA | OKX | LaGuardia Airport-Queens NY |
| KMIA | MFL | Miami-Miami Dade FL（25.82N 80.28W，距 ASOS 约 5 km，NWS 迈阿密唯一的迈阿密点） |
| KORD | LOT | Chicago O'Hare International Airport-Cook IL |
| KSEA | SEW | Seattle-King WA（47.44N 122.31W，即 SeaTac） |
| KSFO | MTR | San Francisco Airport-San Mateo CA |

- **KHOU 不进判定**：HGX 的 PFM 只有 College Station、IAH、Galveston 三个点，Hobby 没有点。用 IAH（北约 35 km）作代理，单列描述。
- **R（NWS 最高温）**：决策时刻 t 之前、WMO 报头时刻 + 10 分钟 ≤ t 的最新一份 PFM 里，事件本地日的白天 Max（7AM–7PM），°F 换 °C。
  - 时间轴校验：列时刻由 WMO 报头时刻与 UTC 行推出，本地日由站点时区换算，须与 PFM 本地时行一致、与日期标签一致；不一致的产品整份不用，报件数。
  - 缺本站段或缺该日 Max 的产品跳过，报件数。
- **下限说明**：NWS 格点在两份 PFM 之间还会更新，市场看得到，PFM 看不到。所以本研究只能低估 NWS 的信息量，不会高估。

## 记号

c = P0 均值；m = 市场均值；T = 结算最高温（服务轴，°C）；d = R − c；e = T − c；g = m − c。

- **主口径**：R 取原值。NWS 预报的就是官方最高温，不另去偏。
- **副口径**：R + b，b 为该站前推平均误差（只用 t 时刻已可知真值的更早事件日，至少 10 个事件日，否则 b = 0）。

## 读数（10 站合计；沿海 KSEA、KLAX、KSFO 与其余 7 站分开；逐站只作描述；站日等权）

1. **准确度**：同一批决策上，R、c、m 各自对 T 的 RMSE。
2. **方向**：
   - corr(d, e)，样本内最优收缩 k* = Σde / Σd²；corr(d, g)。
   - 分歧决策（|g| ≥ 0.5°C）上，d 与 g 同号的比例，R 比 c 更接近 T 的比例。区间按事件日整日重抽 2,000 次。
3. **市场是不是就在用 NWS**：
   - m 到 R 的均方根距离，对比 m 到 c 的距离。
   - 市场与 NWS 相差 ≥ 0.5°C 的决策上，市场比 NWS 更接近 T 的比例。
4. **前推修正（第 201 条规则，与区域研究逐字相同）**：
   - k = clip(Σ(T−c)·d / Σd², 0, 1)，每站只用 t 时刻已可知真值的更早站日拟合；不足 10 个事件日时 k = 0。
   - δ = clip(k·d, ±1.5°C)，用宽度层自己的变换（width 1、开口按尾巴）。
   - 过第 224 条下限，与未修正的 P0 比桶 Brier 与 LogScore，事件日整日重抽 2,000 次。
   - 另报修正后对市场的差，以及收回的比例 =（未修正缺口 − 修正后缺口）/ 中心分量。
5. **固定规则（不拟合，天然样本外）**：k = 0.5 与 k = 1，同样的封顶与变换，同样的分数与区间。

## 判定

- **「NWS 就是市场多掌握的信息，且收得回」**：同时满足
  - 分歧决策上 d 与市场同号的比例显著大于 50%；
  - 读数 4 或读数 5 中任一口径两个分数都显著下降。
- **「市场跟 NWS 走，但 NWS 不比 P0 准」**：只满足第一条。
- **「NWS 不是那部分信息」**：两条都不满足。
- **是不是全部**：再看读数 3。市场与 NWS 相差 ≥ 0.5°C 时，若市场更接近 T 的比例显著大于 50%，则市场在 NWS 之外还有信息；否则 NWS 已覆盖市场的中心优势。
