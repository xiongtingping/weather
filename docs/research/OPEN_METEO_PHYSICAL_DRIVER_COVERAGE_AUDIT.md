---
status: research
authority: L2
owner: weather-system
effective_from: 2026-07-15
---

# Open-Meteo 物理驱动字段覆盖与接入边界审计（ZUUU）

> 状态：research-only；审计日期：2026-07-15；点位：成都双流机场 ZUUU 附近 `30.5785, 103.947`。  
> 本文只回答“Open-Meteo 能否承载第一性原理最高温方案所需的 NWP 弱驱动数据”，不改变任何 production 来源合同、posterior、decision 或 order。

## 1. 结论先行

Open-Meteo **足够支撑物理状态空间方案的数据结构原型和研究探针；在官方 native 对象不可用时，还可在独立 lineage 下补位 no-live P0/P0-M，但不足以独立支撑整套最高温系统**。

更直白地说：

1. 它能提供大部分 `U_t` 弱驱动：2 m 温度/露点、云、雨、风、气压、短波辐射、土壤、850/700 hPa 环境场；集合 API 还能提供成套天气情景。
2. GFS 是本次探测中字段最全的 transport：额外返回 PBL、感热通量、潜热通量；ECMWF 9 km HRES 也返回 PBL。
3. 它不能提供机场 canonical METAR/SPECI、TAF 原文与修订身份、AMSC 最终物理 `X`、市场结算 `S/B`，因此不能替代强观测、预测报文或结算源；它只能在 native 缺失时作为 no-live P0/P0-M fallback。
4. 它没有完整能量闭合：没有统一可比的四分量净辐射、地表储热、完整 `C_eff` 或站点级水平平流。现有字段只能约束隐状态和诊断量，不能把 NWP 字段当成真实状态。
5. 当前合同 [OFFICIAL_NWP_WEAK_SOURCE_CUTOVER.md](../contracts/OFFICIAL_NWP_WEAK_SOURCE_CUTOVER.md) 要求 ECMWF/DWD/NOAA/JMA native 直连优先；Open-Meteo 只能作为有 lineage、历史 strict-as-of 激活的 no-live fallback，不得成为 native arrival、P1 或 formal/live authority。

因此，正确定位是：

```text
Open-Meteo = native 缺失时的有界 no-live P0/P0-M fallback
           + 研究期字段可得性探针 / schema 原型 / legacy 对照
           ≠ canonical observation
           ≠ native arrival / P1 / formal-live authority
           ≠ 结算源
           ≠ 最终最高温答案
```

## 2. 审计范围与判定方法

### 2.1 粒度

- deterministic：`model × grid-cell × valid-hour`；
- ensemble：`model family × member × valid-hour`；
- probe 窗口：主要为未来 24 或 48 小时，UTC；
- 现场探测时间：2026-07-15 06:45–07:00 UTC 左右；
- 每个请求显式指定 model；禁止 `models=auto`、Best Match 和 provider seamless 混合被解释为独立模型。

### 2.2 “支持”的严格定义

本文把三件事分开：

- **文档声明**：API 页面列出字段；
- **结构存在**：响应里有字段 key；
- **现场可用**：ZUUU 邻近网格在 probe 窗口至少有非空值。

字段 key 存在但全部为 `null` 时，结论是 **NA**，不是“支持”。一次成功 probe 只证明当时字段可取，不构成长期 SLO、准确率或 strict-as-of 资格。

## 3. 与状态空间模型的唯一角色映射

固定低维状态仍为：

```text
Z_t = (T_tilde, q_t, G_net_t, W_t, C_cp_t, R_t)
```

Open-Meteo 变量的唯一合法研究语义是：进入外生弱驱动 `U_t`、未来天气情景，或条件诊断 `P(Psi_t | Z_t, U_t)`；**不得直接写成 `Z_t` 的事实值**。

| Open-Meteo 字段族 | 对应物理含义 | 合法模型角色 | 明确禁止 |
|---|---|---|---|
| `temperature_2m` | NWP 近地温度路径 | `U_t` 弱路径；逐模型误差校准后约束 `T_tilde` 转移 | 冒充 METAR 温度；直接作为最终 `X` |
| `dew_point_2m`、`relative_humidity_2m` | 水汽背景 | `U_t` 水汽驱动；给 `q_t` 提供弱先验 | 把 RH 与 T/Td 再拆成独立证据重复计数 |
| `cloud_cover*` | 云层及短波遮蔽 | `U_t` 云情景；约束 `R_n` 与 `G_net` 的条件分布 | 冒充 METAR 云组、TAF BKN/OVC token |
| `precipitation`、`rain`、`showers` | 未来降水/冷池可能性 | `U_t` 未来情景；给 `R_t`、`C_cp_t` 的转移先验 | 冒充实况雨；同源三字段各乘一次 LR |
| `weather_code` | 由模型字段派生的天气码 | 研究摘要或情景标签 | 当作 METAR present-weather 原文 |
| `wind_*`、`pressure_*` | 环流、通风和气压背景 | `U_t`；多点网格可派生平流/梯度弱驱动 | 单站点风直接等同水平温度平流 `A` |
| `shortwave/direct/diffuse_radiation` | 短波输入 | `U_t`；`Psi.R_n` 的部分约束 | 当作完整净辐射；重复消费分量与合计 |
| `sensible_heat_flux`、`latent_heat_flux` | 模型地表湍流分流 | `Psi.H/LE` 的弱诊断约束 | 当作站点真值；拆成独立来源；跨模型假定同口径 |
| `boundary_layer_height` | 混合层深度 | `Psi.C_eff`、通风/混合效率的弱约束 | 直接等同 `C_eff` 或温度上限 |
| `soil_temperature*`、`soil_moisture*` | 地表记忆和可蒸发水背景 | `U_t`；`W_t` 与 `Psi.G/LE` 的弱先验 | 覆盖由报文/历史后验更新的 `W_t` |
| 850/700 hPa 温湿风、位势高度、垂直速度 | 四川盆地大尺度气团、下沉/抬升、通风 | `U_t` 大尺度先验；多点派生空间梯度 | 直接写入站点状态；地形以下层位强行外推 |
| ensemble members | 同一模型族的扰动天气世界 | coherent day-world particles / future scenarios | 把成员当相互独立 provider；把 spread 当已校准概率 |
| `temperature_2m_max` | API 对温度路径的日聚合 | 研究对照或弱源摘要 | 绕过状态转移，直接成为唯一最高温预测 |
| `current=*` | 最新模型时刻的字段 | 只作 NWP nowcast 弱驱动 | 冒充实时观测；进入 canonical observation |

## 4. 模型现场覆盖矩阵

### 4.1 六个物理驱动字段组

比较组固定为：近地/天气、短波辐射、土壤、850/700 hPa、PBL、感热/潜热通量。`完整=1`、`部分=0.5`、`空值/未暴露=0`。该分数只表达字段宽度，不表达模型准确率、独立性或生产资格。

| 显式模型 | 近地/天气 | 辐射 | 土壤 | 850/700 hPa | PBL | H/LE | 宽度分 | 现场/文档要点 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| GFS Global | 1 | 1 | 1 | 1 | 1 | 1 | **6.0/6** | 24h probe 全部非空；PBL、`sensible_heat_flux`、`latent_heat_flux` 均为 `W/m²`/`m` |
| ECMWF IFS HRES 9 km | 1 | 1 | 1 | 1 | 1 | 0 | **5.0/6** | 6h probe 的 PBL 全非空；其余字段组由 ECMWF API 文档声明 |
| ECMWF IFS 0.25° | 1 | 1 | 1 | 1 | 0 | 0 | **4.0/6** | 48h broad probe 中 PBL 48/48 为空且 unit=`undefined` |
| CMA GRAPES Global | 1 | 1 | 1 | 1 | 0 | 0 | **4.0/6** | surface/soil/radiation/850/700 均返回；CMA 文档警告上游服务近期过载 |
| ICON Global | 1 | 1 | 1 | 0.5 | 0 | 0 | **3.5/6** | 温湿风/位势高度可用；850/700 垂直速度为空，PBL 为空 |
| JMA GSM | 1 | 0 | 0 | 1 | 0 | 0 | **2.0/6** | surface 与 850/700 可用；短波/直射/散射辐射 24/24 为空 |

现场返回的网格中心不同：GFS 约 `30.63456,103.94531`，ECMWF/JMA 0.25°/0.5° 类网格约 `30.5,104.0`，CMA 约 `30.5625,104.0`，ICON 约 `30.625,104.0`。API 响应 elevation 均为 494 m，但这并不表示各模型原生网格地形相同；Open-Meteo 默认包含网格选择和统计降尺度。

### 4.2 集合情景

| ensemble model | 现场每变量 series 数 | 研究用途 | 关键限制 |
|---|---:|---|---|
| NOAA GEFS seamless | 31 | 未来温度路径与天气情景粒子 | control+30 members 属于一个 source family |
| ECMWF IFS 0.25 ensemble | 51 | 多天气世界、峰值/时点联合分布 | control+50 members；不能与 deterministic 当两个独立 provider |
| ICON EPS seamless | 40 | surface 情景粒子 | 本次联合请求中 850 hPa series 为空；逐字段缺失必须保留 |

Open-Meteo 集合 API 的 individual members 只保留最多约 3 天历史；更长历史主要是 ensemble mean/spread。mean/spread 不能替代成员级 coherent day-world 训练样本。

## 5. 覆盖缺口：为什么它不是整套系统

| 系统所需信息 | Open-Meteo 是否提供 | 正确来源/处理 |
|---|---|---|
| AMSC/AWC METAR、SPECI 原文及发布时间 | 否 | canonical observation 链 |
| TAF、AMD、BECMG/TEMPO 修订身份 | 否 | 独立 TAF 原文与发布血缘 |
| 已结算 physical `X` | 否 | closed AMSC station-day |
| 市场规则、结算 `S/B` | 否 | 冻结市场合同与 resolution bridge |
| 完整净辐射和能量闭合 | 不完整 | 直连 NWP 原始场 + 冻结诊断模型；缺失边缘化 |
| 站点水平平流 | 不直接提供 | 同 run 多点/网格温度与风场派生，绑定母网格协方差 |
| 真正地表热储/机场下垫面 | 不直接提供 | 隐状态 + 历史校准 + 必要时独立地表资料 |
| exact run/arrival lineage | live payload 不完整 | 显式 model + metadata availability + 本地 first-seen/raw SHA；生产仍以官方 raw receipt 为准 |

## 6. 主要数据质量风险

### Critical — 生产治理不相容

当前 L1 合同禁止 Open-Meteo 冒充 native：native snapshot 仍要求 `open_meteo=false`、`aggregator_or_republisher=false`。但 native 对象缺失、失效或未及时到达时，独立 republisher ingress 可生成带 `aggregator_republisher` lineage 的 no-live P0/P0-M fallback；没有历史 strict-as-of OOS activation 时必须保持 candidate/fail-closed，不能进入 P1、native arrival 或 formal/live authority。

### High — 来源和 run 身份不能只靠 forecast payload

Generic Forecast API 默认 Best Match 会自动拼接最合适模型；provider seamless 也会在同一提供者内拼接。普通 forecast JSON 返回坐标、时间和值，但不天然带齐每个值的 run、原始 provider receipt 和实际可用时刻。Metadata API 另有 initialization、modification、availability time，而且官方说明多服务器最终一致，availability 后仍建议等待约 10 分钟。

研究探针至少必须保存：显式 `models` 参数、请求/响应完成时间、metadata availability、local first-seen、grid center、elevation、单位、原始 JSON SHA 和缺失掩码。

### High — “current conditions”不是观测

官方文档明确：`current` 基于 15 分钟天气模型数据。它不能替代 METAR/SPECI，更不能进入 canonical strong likelihood。

### High — 历史接口的 as-of 语义不同

- Historical Forecast API 把每个 run 的最初几小时拼成连续序列，不是完整单次预报；
- Previous Runs API 是相对 valid time 的 day1…day7 固定 lead offset，不等于精确 run 身份；
- Single Runs API 才能用 `run=` 取完整 horizon，但 ECMWF HRES 仅从 2024-03 起，多数模型从 2026-04-02 起。

所以 Historical Forecast 可用于 schema/粗研究，不能冒充 strict-as-of 全 run 训练集。

### Medium — 模型字段异质和空值

PBL、垂直速度、辐射、土壤和通量都有模型依赖。字段 key 存在并不代表非空。adapter 必须按 `model_id × field × run/valid` 输出 availability mask 和 `*_na_reason`，禁止统一填 0。

### Medium — 小时值可能是插值或区间统计

- JMA GSM 原生 6 小时，Open-Meteo 插值为 1 小时；
- ECMWF 0.25° 原生 3/6 小时，但 API 可动态插值为 1 小时；
- ensemble 也统一插值到 1 小时；
- shortwave/direct/diffuse 是前一小时平均，gust 是前一小时最大，precipitation 是前一小时累计；
- 四川的 15 分钟字段是小时数据插值，不是新增观测信息。

时间适配器必须保留原生 temporal resolution 和 valid interval，禁止把区间均值当瞬时值。

### Medium — 辐射字段不是完整 `R_n`

短波 GHI/直射/散射不能闭合地表净辐射；部分模型的直射/散射由 API 近似分解。长波、反照率、地表温度与下垫面储热仍需独立诊断和不确定性。

### Medium — 同源重复信息

同一 provider 的 deterministic、ensemble、不同分辨率、日最大与逐小时路径高度相关。它们必须作为同一 source family 的联合向量/情景消费，不能拆成多个独立 LR。

## 7. 研究接入合同

### 7.1 允许

仅在与 production import graph 隔离的 research lane 中：

1. 用显式单模型 endpoint 验证字段 schema、单位、时间语义和缺失模式；
2. 用 exact Single Runs（在有覆盖的日期）构造方法学样本；
3. 用 ensemble members 验证 coherent day-world 粒子接口；
4. 与官方直连字段做 transport 对账，发现 decoder/单位/时间差异；
5. 作为 legacy 研究基线，但必须标记 `research_only=true`、`production_weight_input=false`。

### 7.2 禁止

1. 禁止 `models=auto` 或 Best Match 进入可复现模型样本；
2. 禁止把 `current` 当观测；
3. 禁止把 `daily temperature_2m_max` 当第一性原理模型的最终答案；
4. 禁止把空值填 0、跨 run 补值或用另一模型同名字段替代；
5. 允许的 Open-Meteo fallback 只能进入 no-live P0/P0-M candidate；没有历史 strict-as-of OOS activation 时不得进入 posterior、decision 或 order；任何 forward 数据均为 `observational_candidate_not_oos`；
6. 禁止把同 provider 的 deterministic/ensemble/member 当独立来源重复计权；
7. 禁止按同字段名把 legacy Open-Meteo v3.1 系数迁移到官方 GRIB 特征。

## 8. 若只为验证方案，建议最小字段集

### Tier R0：路径和天气型

```text
temperature_2m, dew_point_2m,
cloud_cover, cloud_cover_low, cloud_cover_mid, cloud_cover_high,
precipitation, rain, showers,
surface_pressure, wind_speed_10m, wind_direction_10m
```

### Tier R1：能量与下垫面弱约束

```text
shortwave_radiation, direct_radiation, diffuse_radiation,
soil_temperature_*, soil_moisture_*,
boundary_layer_height,
sensible_heat_flux, latent_heat_flux
```

其中 PBL、H/LE 必须按模型声明 optional，不能成为全模型 required field。

### Tier R2：四川盆地大尺度背景

```text
temperature_850hPa, relative_humidity_850hPa,
wind_speed_850hPa, wind_direction_850hPa,
vertical_velocity_850hPa, geopotential_height_850hPa,
temperature_700hPa, relative_humidity_700hPa,
wind_speed_700hPa, wind_direction_700hPa,
vertical_velocity_700hPa, geopotential_height_700hPa
```

要估计水平平流和盆地通风，必须使用固定多点 stencil 或母网格，不应只取 ZUUU 一个点。

### Tier R3：情景不确定性

```text
GEFS / ECMWF EPS / ICON EPS members
× 同一成员内完整日 temperature/cloud/rain/wind/upper-air path
```

粒子必须以成员为单位保持一天路径一致性；不能逐小时从不同成员随机拼接。

## 9. 自动验收清单

- [ ] request 明确 model_id，拒绝 `auto` 和 Best Match；
- [ ] 保存 endpoint、requested coordinate、returned grid center、elevation；
- [ ] 保存 request start/response complete/local first-seen；
- [ ] 保存 metadata initialization/availability，且不把 modification time 当 availability；
- [ ] 保存 raw JSON SHA、字段单位、原生与 API 输出时间分辨率；
- [ ] 每字段统计 total/non-null/NA reason；
- [ ] 区分 instant、preceding-hour mean/max/sum；
- [ ] ensemble 保存 member identity，不把成员当 provider；
- [ ] daily max 不进入 hourly trajectory；
- [ ] 所有产物标记 `research_only=true`、`production_weight_input=false`；
- [ ] production import graph 与 runtime catalog 拒绝无 lineage 的 Open-Meteo；有 lineage 的 fallback 只能在 native 缺失且历史 OOS activation 通过时进入 no-live P0/P0-M；
- [ ] 任何合同变更先走 ADR/L1，不在代码中隐式放宽。

## 10. 对当前第一性原理方案的具体完善

Open-Meteo 的优秀之处不是“直接给最高温”，而是暴露了一组足以验证以下结构的数据：

```text
四川盆地/大尺度先验
  + 多模型/集合未来天气情景 U
  + 条件能量诊断 Psi
  + D0 METAR/SPECI canonical update
  -> P(Z_t, X | observations_as_of_t)
```

据此，方案应坚持三层分工：

1. **弱层**：NWP/TAF 只给未来情景与 prior/transition，不写事实；
2. **强层**：METAR/SPECI 用 T/Td/云/雨/风序贯更新隐状态与各情景可达性；
3. **结算层**：closed physical `X` 只用于事后校准，市场 `S/B` 只经冻结 bridge 投影。

Open-Meteo 能帮助快速证明第 1 层的数据结构，但第 2、3 层必须来自独立 canonical/settlement 系统；生产第 1 层也继续使用官方直连 transport。

## 11. 复现命令骨架

```bash
curl --get 'https://api.open-meteo.com/v1/gfs' \
  --data-urlencode 'latitude=30.5785' \
  --data-urlencode 'longitude=103.947' \
  --data-urlencode 'forecast_hours=24' \
  --data-urlencode 'models=gfs_global' \
  --data-urlencode 'hourly=temperature_2m,dew_point_2m,cloud_cover,precipitation,shortwave_radiation,boundary_layer_height,sensible_heat_flux,latent_heat_flux,soil_temperature_0_to_10cm,soil_moisture_0_to_10cm,temperature_850hPa,relative_humidity_850hPa,wind_speed_850hPa,vertical_velocity_850hPa,geopotential_height_850hPa,temperature_700hPa,relative_humidity_700hPa,wind_speed_700hPa,vertical_velocity_700hPa,geopotential_height_700hPa'
```

响应验收不能只检查 HTTP 200，必须逐字段统计非空率：

```bash
jq '. as $r | [.hourly | to_entries[] | select(.key != "time") |
  {field: .key,
   non_null: (.value | map(select(. != null)) | length),
   total: (.value | length),
   unit: $r.hourly_units[.key]}]'
```

## 12. 官方资料

- [Weather Forecast API](https://open-meteo.com/en/docs)
- [GFS API](https://open-meteo.com/en/docs/gfs-api)
- [ECMWF API](https://open-meteo.com/en/docs/ecmwf-api)
- [DWD ICON API](https://open-meteo.com/en/docs/dwd-api)
- [CMA API](https://open-meteo.com/en/docs/cma-api)
- [JMA API](https://open-meteo.com/en/docs/jma-api)
- [Ensemble API](https://open-meteo.com/en/docs/ensemble-api)
- [Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api)
- [Single Runs API](https://open-meteo.com/en/docs/single-runs-api)
- [Previous Runs API](https://open-meteo.com/en/docs/previous-runs-api)
- [Model Updates & Metadata](https://open-meteo.com/en/docs/model-updates)
