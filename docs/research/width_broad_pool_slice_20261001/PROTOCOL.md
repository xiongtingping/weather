# 预注册协议：P0 宽度层「宽池」切片（入座 ≥4 家）— 与宪章 239 薄池切片对称

写于 2026-10-01，跑结果之前。裁决人：熊先生（「开」）。

## 问题
线性池方差 = 组内 + 组间，入座越多越宽；融合中心误差随家数下降。统一 λ=0.75 只能对齐一个家数：
生产 P0 09-22..30（codex 重放 + 本会话复核）宽度层后 z²：全体 0.98、六家 0.74–0.78、>36h 两家 1.23。
薄的一侧归第 239 条；本协议只问：**入座 ≥4 家的决策单独一个 λ（预期 <0.75）是否按第 240 条判决过门。**

## 数据与机器（全部生产入口，不手抄规则）
- 格：`center_width_store.serving_cells_by_day(conn, _truth(conn), stage="P0", min_decision_at_utc=floor)`，
  floor = `_evidence_floor(conn, stage="P0", caller_bound="2026-08-27T09:20:00Z")`（与每日 `calibration_refit_sweep.sh` 同一口径）。
  每站日每 3 小时块一格、真值取最新修订、pre_band、按今日宽度函数重服务，与生产拟合完全一致。
- 车道：`center_width_store._thin_far_slice_lane(...)` 原函数，只把 `slice_by_day` 换成本协议的切片格；
  `cut = max(1, int(len(days) * (1 - FORWARD_WINDOW_FRACTION)))`，`pooled_admitted=False`、`served_residual=RESIDUAL_IDENTITY`、
  `served_slice_lambda=0.75`（在役阶段 λ，无在役切片）、`sigma_floor_rungs=0.0`。
- 判决：车道自带的第 240 条门（`_proper_score_gate`：LogScore 上界 <0；CRPS、Brier 下界 ≤0）且折多数为正，
  证据下限 8 日／60 格／折内训练 ≥30 格——与 239 一字不差。

## 主臂与副臂（事前固定）
- **主臂 broad4**：`seated_system_count ≥ 4`，不分提前量。过门 → 落地为第二个切片；不过 → 留档不改。
- 副臂（只报不裁）：broad5（≥5）、broad6（=6）、broad4_near（≥4 且 ≤36h）、broad4_far（≥4 且 >36h）、
  以及对照 thin_far（≤3 且 >36h，应复现 10-01 07:26Z 日志里的 REFUSED_FORWARD、候选 0.825）。
- 预期方向：候选残差 λ<1（收窄）。若候选 ≥1 则机制假设被证伪，不论门是否过都不落地。

## 不做的事
不改 SSOT、不改服务参数、不重启、不下单。本协议之外的任何切分不进结论。
