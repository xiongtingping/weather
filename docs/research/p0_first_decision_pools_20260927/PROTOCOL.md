# 生产 P0 首决策入座几家，与「等到四家」的价格侧（计算前写定，2026-09-27）

熊先生：「继续查」。只读研究，生产未改。SSOT 在本机，云端读不到，所以脚本 `audit.py` 须在本机跑：

```
python3 docs/research/p0_first_decision_pools_20260927/audit.py
```

## 起因

- `p0_far_lead_width_20260927` 的回放里，首决策 73%（检验期）／88%（全期）是两家池。
- 这些两家池 70% 是 AIFS＋AIGEFS。回放里这一对的后灌到货是模拟的；而生产中 AIFS 09-20、AIGEFS 09-24 才入座。
- `p0_replay_219x216_20260925/REPORT.md` 已记录：「两家池在生产中从未出现」。
- 所以回放读数不能代表生产，要在生产数据上直接量。本协议写于任何生产读数之前。

## 数据（生产 SSOT，只读）

- **首决策**：与 Proper 线同一口径，即 `tiebreak_lane.stage_first_decisions` 加 `proper_bet_lane._fact`。
  - 取 `basket_policy_training_fact` 中 stage='P0' 的每站日最早 `decision_at_utc`；
  - 同刻有多行时，按 `created_at_utc`、`fact_id` 取第一行。
- **入座**：
  - 该 fact 的 `probability_envelope_id` 指向一个信封，读其 lineage 的 `system_weights_json`，权重 >0 即入座；
  - 家族取同一 lineage 的 `system_family_ids_json`；缺则取 `independence_family_ids_json`；都缺记为 UNKNOWN。
- **距结算**：生产 `settlement_time.hours_to_settlement`。
- **盘口**：fact 绑定的 `ws_snapshot_id` 在 `basket_official_ws_snapshot` 里的 `buckets_json`，经生产 `market_reference_v1` 得归一中价 q（Proper 线的 q）。
- **真值**：
  - 表 `calibration_station_daily_high_truth`，条件为 qc ACCEPTED、`source_lane IN FORMAL_TRUTH_LANES_SQL`，按修订号取最新；
  - 落格用信封自己的 `bucket_intervals_c_json`，下沿含、上沿不含，同 `p0_zero_tail_20260925`。
- **Proper 线实评**：表 `proper_bet_lane_evaluation` 若存在，取其中 stage='P0' 的行，按其 envelope_id 读入座。

## 分期（按事件日）

| 期 | 事件日 | 状况 |
|---|---|---|
| 甲 | ≤09-19 | AIFS、AIGEFS 都未入座 |
| 乙 | 09-20..09-23 | AIFS 已入座，AIGEFS 未入座 |
| 丙 | ≥09-24 | 六家都可入座，即今天的状况 |

## 问题一（主）：首决策入座几家

各期报：
- 首决策数、家数分布、距结算的 p10／p50／p90、h>36 的占比；
- h>36 首决策的家数分布，以及最常见的五种系统组合；
- 对照：同期 h>36 的全部 P0 fact 的家数分布。站日等权，每站日内按不同信封均分。
- Proper 线实评行的家数分布，全部与已发单（planned_legs>0）两组。

**判定（事前写定）**：看丙期 h>36 首决策中入座 ≤3 家的占比。
- **<10%**：结论为「首决策薄池不是生产现象，回放读数是模拟到货造成的」。问题二只报不判。
- **≥10%**：问题二按下面的口径读。

## 问题二（条件）：等到四家的价格侧

- **配对**：已结算的站日，首决策 h>36 且入座 ≤3 家；配上同站日之后首个仍 h>36、入座 ≥4 家的 P0 fact。
- **两个时点各算**：
  - 模型 p 取信封概率，归一后过生产 `floor_probabilities`，同 Proper 线；
  - 市场 q 取 `market_reference_v1`，为了能打分也过同一下限；
  - 在同一副梯子上算两者的 LogScore 与桶 Brier；
  - 实现 edge ＝ LogScore(q) − LogScore(p)。正数表示模型更准，也就是 log-proper 下注者每单位的实现对数增长，未计价差；
  - q 在两个时点之间的半 L1 移动，以及 KL(p‖q)。
- **读数**：Δ ＝ 等到四家 − 首决策。站日等权，按事件日三日块自助 2,000 次（seed 20260927）。
- **口径外**：梯子不对齐、q 不完整、没有真值的配对，各自计数，不补。
- **只报不判**：改不改下注时点由熊先生裁决。本读数不计成交、价差与挂单成交率。

## 局限

- 首决策只认已写入训练 fact 表的决策。编译了但没有写 fact 的（例如没有盘口时），不算首决策。
- Proper 线 09-26 才上线。更早的首决策是「假如按今天的规则，会读到的那一次」。
