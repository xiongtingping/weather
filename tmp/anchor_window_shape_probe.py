"""Four-rung window shapes around the mean anchor, scored on settled truth.

For every settled P0 intended station-day: anchor m = the production mean
centre bucket, delta = truth ordinal - m.  Score each candidate shape by
containment -- [m-1..m+2] vs [m..m+3] on the hot side, [m-2..m+1] vs
[m-3..m] on the cold side, with the symmetric 3-rung [m-1..m+1] as the
reference -- counting hits, and for misses which side and how far.  Cut
by the charter-55 skew state, since the shape question is really "which
lean pays in which shape of day".  Counterfactual containment only: no
prices, no leg floors, so this ranks coverage, not P&L.
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.delta_v2_runtime import _skew_extension_sign

SHAPES = [
    ("[m-1..m+2] 热倾含缓冲", -1, 2),
    ("[m..m+3]   热倾锚为界", 0, 3),
    ("[m-2..m+1] 冷倾含缓冲", -2, 1),
    ("[m-3..m]   冷倾锚为界", -3, 0),
    ("[m-1..m+1] 3桶对称参照", -1, 1),
]

conn = sqlite3.connect(
    "file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",
    uri=True,
)
rows = conn.execute(
    "SELECT p.event_date, p.station_id, p.ladder_center_bucket,"
    " p.probabilities_json, s.settled_bucket"
    " FROM paper_trade_position p"
    " JOIN paper_trade_settlement s"
    " ON s.station_id = p.station_id AND s.event_date = p.event_date"
    " WHERE p.stage = 'P0'"
).fetchall()

SKEW_NAME = {1: "right", 0: "sym", -1: "left"}
seen = set()
deltas_by_state = defaultdict(list)
for event_date, station, center, prob_json, settled in rows:
    if (event_date, station) in seen:
        continue
    seen.add((event_date, station))
    probabilities = {
        str(k): float(v) for k, v in (json.loads(prob_json) or {}).items()
    }
    ordinals = {b: i for i, b in enumerate(probabilities)}
    if center not in ordinals or str(settled) not in ordinals:
        continue
    delta = ordinals[str(settled)] - ordinals[center]
    state = SKEW_NAME[_skew_extension_sign(probabilities, ordinals)]
    deltas_by_state[state].append(delta)
    deltas_by_state["all"].append(delta)


def report(state: str) -> None:
    deltas = deltas_by_state.get(state, [])
    if not deltas:
        return
    n = len(deltas)
    dist = Counter(deltas)
    line = "  真值−锚 分布: " + "  ".join(
        f"{k:+d}:{dist[k]}" for k in sorted(dist)
    )
    print(f"=== {state}  (n={n}, 偏差均值 {sum(deltas)/n:+.2f}) ===")
    print(line)
    for name, lo, hi in SHAPES:
        hits = sum(1 for d in deltas if lo <= d <= hi)
        miss = [d for d in deltas if not (lo <= d <= hi)]
        hot_miss = sum(1 for d in miss if d > hi)
        cold_miss = sum(1 for d in miss if d < lo)
        mean_dist = (
            sum(min(abs(d - lo), abs(d - hi)) for d in miss) / len(miss)
            if miss else 0.0
        )
        print(
            f"  {name}  命中 {hits}/{n} = {hits/n:.0%}"
            f"   脱靶 热侧{hot_miss}/冷侧{cold_miss}"
            f" 平均脱窗 {mean_dist:.2f} 档"
        )
    print()


for state in ("all", "sym", "right", "left"):
    report(state)
