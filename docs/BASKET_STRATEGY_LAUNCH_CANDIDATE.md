---
status: superseded
authority: L2
owner: weather-system
effective_from: 2026-07-19
---

# 纯篮子 + 实况报文硬 NO：上线候选版本

## 边界（已由生产适配器接入）

本文件保留确定性 planner 的输入与数学边界。实盘提交由
`basket_strategy/production.py` 在 `com.weather.weather_basket_yes` 内负责，planner
本身仍不连接 gateway、不写库。

- planner 默认 `live_submit_authorized=false`；生产激活文件单独授权 adapter 提交 YES FOK。
- 不导入订单 gateway；
- 不写 SQLite、facts、决策或资本账本；
- 官方 AWC METAR/SPECI 硬事实只用于排除不可能 YES 桶，不存在 hard-NO 下单腿。
- 不使用分层建仓、选择性配平或 MERGE；
- 不把模型/TAF当作硬排除证据。

当前生产 `weather_basket_yes` 承载纯 YES 篮子 adapter；本 planner 仍是只读计划层。本文档的旧启动候选口径已退役，当前以 trading operating system doctrine 为准。

生产候选入口位于 `basket_strategy/runtime.py`，由调用方传入一次完整快照。

单次准备计划可运行：

```bash
python3 scripts/prepare_basket_plus_hard_no_plan.py \
  --input /path/to/materialized_snapshot.json \
  --output /path/to/basket_plan.json
```

该命令只读取快照并写出审计用 JSON 计划，不拉取行情、不读写数据库、不提交订单。

## 目标链路与两条独立腿

### 纯 YES 篮子

篮子不再用三模型/TAF 点预测均值直接选桶。正确链路是：

```text
三大模型 + TAF + 目标日物理轨迹
        → target_day_regime
        → 联合弱似然 → P0
        → METAR/SPECI 强似然 → P1
        → X→S→B bridge → p(B)
        → argmax p(B) → center_bucket
        → regime 一致性门控
        → 连续 YES 篮子
```

入场优先读取已提交的 P1 市场桶概率；P1 不可用时才回退到 P0。缺少合法的 P1/P0 `p(B)` 时阻断，不回退到点预测、均值或盘口最高桶。

中心桶严格为：

```text
center_bucket = argmax_b p(B=b)
basket = [center-h, ..., center, ..., center+h]
```

篮子不因 UP/DOWN 再平移中心。`regime` 只比较概率中心相对市场锚点的方向：

```text
center > market_anchor 且 regime = UP   → AGREE
center < market_anchor 且 regime = DOWN → AGREE
概率中心与 regime 冲突                 → BLOCKED
regime = NEUTRAL/UNKNOWN                → BLOCKED
```

推荐使用现有 `official_nwp_physical_regime_runtime` 和 `climo_regime_runtime` 已产出的
`forecast_weather_regime`、`basin_regime`、`cloud_regime`、`precipitation_regime`、
`convection_regime`、`basin_heat_mechanism_state` 等字段。当前物理 regime 的
`clear_dry`/低云量/热力累积类标签可与 UP 一致，`convection_or_showers`、
`wet_precipitation`、`cloudy_stable`、`well_ventilated`、过云/湿柱类标签与 DOWN 一致；
未识别或仅中性字段不强行给方向。regime 不能替代 P0/P1，也不能参与 trajectory NO 的硬排除。

默认窗口为 3 个连续精确温度桶，所有桶一次性买入相同数量；不是分层建仓。

保本成本检查只是执行闸门，不替代方向判断。篮子还必须满足：

```text
sum(YES ask × (1 + fee_rate)) < 1
每个桶深度足够
每个桶都有 YES token
```

若每桶买入 `q` 份，则任意一个桶结算时的最差结算额为 `q`，总成本为 `q × sum(all_in_ask)`，因此：

```text
guaranteed_profit = q × (1 - sum(all_in_ask))
```

缺少严格保本边际时，整个篮子阻断。

入场后不根据每条 METAR/SPECI 重新平移 YES 篮子；实况只更新 P1 并独立触发 trajectory NO。这样仍是一次性纯篮子，而不是动态调仓。

### 实况报文硬 NO

只接受指定来源的 METAR/SPECI 原文，使用现有纯函数：

- `parse_metar_temperature`；
- `conservative_market_temperature`；
- 已退役的 hard-NO 不可能桶选择器。

报文硬下界排除所有更低桶，篮子不能再买这些已数学不可能桶。硬 NO 只选择最近一个严格低于硬下界的桶，输出 `FAK BUY NO @ 0.99` 计划；当前不提交。

若同桶已有 YES 库存、报文疑似更正、来源/观测 ID 缺失或没有可执行 NO 盘口，则硬 NO 不生成订单。

### 状态与 SSOT

```text
DRAFT → PROBABILITY_READY → REGIME_CHECKED → BASKET_READY → FROZEN
```

计划输出会标记 `posterior_stage=P1|P0`、`center_bucket`、`center_probability`、
posterior lineage 和 `plan_state`。篮子只消费 p(B)；trajectory NO 只消费 METAR/SPECI
硬下界。两条腿不共享概率重算，也不互相把对方的证据当作硬排除。

## 输入契约

调用 `build_basket_plus_hard_no_plan(request)`，请求至少包含：

- `event`: `event_id`、`market_unit`、`market_anchor_c`；
- `as_of`：带时区的当前快照时间；
- `buckets`：每个桶的 `bucket_c`、`yes_token`、`yes_ask`、`yes_ask_size`、`no_token`、`no_asks`，并包含完整连续梯度的尾桶；
- `probability_projection`：已物化的 `p1` / `p0` 市场桶概率，优先 P1；每个分布必须归一化并带 posterior lineage；
- `regime`：现有 `official_nwp_physical_regime_runtime` / `climo_regime_runtime` 输出；
- `report`：`raw_text`、`source_lane`、`observation_id`、`expected_station`；
- 可选 `held_yes_shares_by_token`。

模型、TAF和目标日前物理轨迹由上游 regime/P0/P1 负责。篮子层只读已物化投影，不在 L4 重建天气概率。

## 上线前未完成项

当前只完成确定性计划与测试。接入实盘前还必须单独确认：

1. 多腿全成/任一腿失败的补偿执行协议；
2. CLOB 盘口快照 SSOT 和订单幂等；
3. 资金预留与篮子原子提交；
4. 生产激活开关、回滚与审计账本；
5. 与现行 `reported_trajectory_no` 的资金池和策略优先级。

这些事项未确认前，不得把 plan 转为 live submit。
