# 两臂：削峰（推广 253）与补远尾（248 的 ε 在 P0 与 F 站）——协议（先于读数，2026-10-09）

熊先生裁决：第十二轮反推出「峰值高估随声称增大、P0/F 远尾偏薄」后，「都跑」。只读研究，生产未改。

## 数据
`extract_rows.py`：SSOT 已服务向量（`basket_probability_bridge_*`，bit-exact，含当时已施加的 253 压顶与 248 尾混），event_date ≥ 09-05 且有正式真值（`knowable_labels`）；每站日×阶段×距结算档（≥48、36–48、24–36、12–24、0–12h，`settlement_time.hours_to_settlement_if_known`）取档内最后一次决策。15,873 行 / 1,528 站日 / 48 站。
轴与开口端：`served_bucket_axis(intervals, keys)`；排除桶＝服务值恰为 0 的桶；计分：生产 `_served_scores`（LogScore 过 224 下限、桶 Brier、梯子 CRPS）。

## 臂（变换全部调生产函数）
- **K_cal**：`apply_peak_ceiling`，c 按组（阶段 × {>36h, ≤36h}）逐日前推：前缀（event_date ≤ d−2 且 known_at ≤ d 00:00Z，同组）里 0.40..0.70 网格取使「p_top ≥ c 的格最高桶命中率」与 c 之差最小的 c（格数 ≥100；全部 c 上命中率都高于 c 则不压；前缀 <8 事件日不压）——253 的规则推广到全部阶段与提前量。
- **K_ll**：同一变换，c 按前缀平均 LogScore 最小选（含「不压」）。
- **S_knee**：ceiling = 0.40 + α·(p_top − 0.40)，α ∈ {0.3,…,1.0} 按前缀 LogScore 选——随声称增大的收缩。
- **T_ε(scope)**：`apply_tail_mix`，κ=1.5（`TAIL_MIX_SCALE`），ε ∈ `TAIL_MIX_WEIGHT_GRID`，按前缀 LogScore 逐日前推选；作用域：P0 全部；F 站全部阶段；P0 且 >36h；P0-M；P1（对照）。
- **KT**：K_ll 之后再 T_ε（P0），各自用自己的前推参数，顺序同生产（先混再压则为生产顺序；本臂先压再混，若两者都单独过线再按生产顺序复算）。

## 判决（宪章 240，生产门 `_proper_score_gate`，簇＝站日，2,000 次自助）
升级成立＝ΔLogScore 上界 <0 且 ΔCRPS 下界 ≤0 且 ΔBrier 下界 ≤0，在**前推计分行**（有 ≥8 事件日前缀的日子）上成立；另报前半/后半、现行段（event_date ≥ 10-05）、各阶段、C/F。只在作用域内与全部行各判一次。
诊断：最高桶分档（0.4–0.5、0.5–0.6、≥0.6）说对中，|d|≥2 质量说对中，变换前后各一份。

## 落地次序（事先定）
过线的臂作为落地候选，须另在实服信封回执的 `input_probability` 上用生产实现复算同向；都不过则不改生产，如实报。
