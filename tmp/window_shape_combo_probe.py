"""Can the two anchor-at-edge shapes be combined by a direction signal?

The literal union is a 7-rung window (charter caps at 4).  The honest
version of "combine" is conditioning: pick the shape each day from an
observable signal.  The only standing direction signal is the charter-55
skew state, so score every conditioning strategy the vocabulary allows,
against the flat-shape baselines, on settled P0 station-days.  The
oracle row (pick by the truth's actual side) is the union in disguise --
its number is the ceiling no real signal reaches.  Coverage only, no
prices; a P&L ruling needs the ledger replay.
"""
import json
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.delta_v2_runtime import _skew_extension_sign

HOT_EDGE = (0, 3)      # [m..m+3]
HOT_BUF = (-1, 2)      # [m-1..m+2]
COLD_EDGE = (-3, 0)    # [m-3..m]
COLD_BUF = (-2, 1)     # [m-2..m+1]

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

seen = set()
days = []
for event_date, station, center, prob_json, settled in rows:
    if (event_date, station) in seen:
        continue
    seen.add((event_date, station))
    P = {str(k): float(v) for k, v in (json.loads(prob_json) or {}).items()}
    o = {b: i for i, b in enumerate(P)}
    if center not in o or str(settled) not in o:
        continue
    days.append({
        "delta": o[str(settled)] - o[center],
        "skew": _skew_extension_sign(P, o),
    })

n = len(days)
print(f"n={n} settled P0 station-days"
      f"  (skew right {sum(1 for d in days if d['skew']>0)}"
      f" / sym {sum(1 for d in days if d['skew']==0)}"
      f" / left {sum(1 for d in days if d['skew']<0)})")
print()


def score(name, pick):
    hits = 0
    miss_hot = 0
    miss_cold = 0
    miss_dist = []
    for d in days:
        lo, hi = pick(d)
        if lo <= d["delta"] <= hi:
            hits += 1
        else:
            if d["delta"] > hi:
                miss_hot += 1
            else:
                miss_cold += 1
            miss_dist.append(min(abs(d["delta"] - lo), abs(d["delta"] - hi)))
    md = sum(miss_dist) / len(miss_dist) if miss_dist else 0.0
    print(f"  {name:<44} 命中 {hits}/{n} = {hits/n:.0%}"
          f"   脱靶 热{miss_hot}/冷{miss_cold}  平均脱窗 {md:.2f} 档")


print("-- 单一形状基线 --")
score("全场 [m-1..m+2] 热倾含缓冲", lambda d: HOT_BUF)
score("全场 [m..m+3]   热倾锚为界", lambda d: HOT_EDGE)
score("全场 [m-2..m+1] 冷倾含缓冲", lambda d: COLD_BUF)
score("全场 [m-3..m]   冷倾锚为界", lambda d: COLD_EDGE)
print()
print("-- 偏斜信号条件化（可落地的组合）--")
score("右偏日→[m-3..m]，其余→[m..m+3]（锚为界组合）",
      lambda d: COLD_EDGE if d["skew"] > 0 else HOT_EDGE)
score("右偏日→[m-2..m+1]，其余→[m-1..m+2]（含缓冲组合）",
      lambda d: COLD_BUF if d["skew"] > 0 else HOT_BUF)
score("顺尾巴: 右偏→[m..m+3] 左偏→[m-3..m] 对称→[m-1..m+2]",
      lambda d: HOT_EDGE if d["skew"] > 0
      else (COLD_EDGE if d["skew"] < 0 else HOT_BUF))
score("逆尾巴: 右偏→[m-3..m] 左偏→[m..m+3] 对称→[m-1..m+2]",
      lambda d: COLD_EDGE if d["skew"] > 0
      else (HOT_EDGE if d["skew"] < 0 else HOT_BUF))
print()
print("-- 神谕上限（按真值实际侧选，= 7桶并集的伪装）--")
score("delta>=0→[m..m+3]，delta<0→[m-3..m]",
      lambda d: HOT_EDGE if d["delta"] >= 0 else COLD_EDGE)
