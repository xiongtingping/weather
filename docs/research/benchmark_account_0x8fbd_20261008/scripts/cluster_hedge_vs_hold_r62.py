"""Round 62 (熊先生 10-13: 「持有到结算的亏损低还是超过成本配对的成本低？」).

The cluster's two-sided station-day x bucket groups (round 58 universe).  First side = the side of
the group's first BUY fill; hedge leg = the other side's fills.  Over the matched shares m
(avg first-side price a, avg hedge price h, W = 1 when the first side wins):
  paired P&L      m (1 - a - h)          (what the cluster locked)
  hold-to-settle  m (W - a)              (the same first-side shares, never hedged)
  difference      m ((1 - W) - h)        = the hedge leg's own settlement P&L.
Split by pair cost (a + h) <= 0.95 / 0.95-1.00 / > 1.00; 95% intervals by event-day block
bootstrap; both halves of the dates.
"""
import collections
import gzip
import json
import sys
from pathlib import Path

import numpy as np

B = Path(sys.argv[1])
FILLS = json.load(open(B / "cluster_fills_r45.json"))
FR = {(r["st"], r["ed"]): r for r in json.load(gzip.open(B / "first_rows.json.gz", "rt"))}

groups = collections.defaultdict(list)
for f in FILLS:
    key = (f["station"], f["event_date"])
    if key in FR and f["bucket"] in FR[key]["labels"] and FR[key].get("truth") in FR[key]["labels"]:
        groups[(key, f["bucket"])].append(f)

rows = []
for ((st, ed), lab), fl in groups.items():
    fl.sort(key=lambda f: f["ts"])
    first = fl[0]["outcome"]
    a_s = sum(f["size"] for f in fl if f["outcome"] == first)
    h_s = sum(f["size"] for f in fl if f["outcome"] != first)
    if a_s <= 0 or h_s <= 0:
        continue
    a = sum(f["size"] * f["price"] for f in fl if f["outcome"] == first) / a_s
    h = sum(f["size"] * f["price"] for f in fl if f["outcome"] != first) / h_s
    m = min(a_s, h_s)
    yes_wins = FR[(st, ed)]["truth"] == lab
    W = 1.0 if (yes_wins if first == "Yes" else not yes_wins) else 0.0
    hedge_minutes = (next(f["ts"] for f in fl if f["outcome"] != first) - fl[0]["ts"]) / 60
    rows.append({"ed": ed, "first": first, "m": m, "a": a, "h": h, "pc": a + h, "W": W,
                 "paired": m * (1 - a - h), "hold": m * (W - a), "cost": m * a, "mins": hedge_minutes})

days = sorted({r["ed"] for r in rows})
mid = days[len(days) // 2]
rng = np.random.default_rng(62)
print(f"{len(rows)} two-sided groups over {len(days)} event days ({days[0]}..{days[-1]})\n")
print(f"{'pair cost':12s} {'groups':>6s} {'matched sh':>10s} {'first wins':>10s} {'paired P&L':>10s} {'hold P&L':>10s}"
      f" {'hedge leg':>10s} {'95% CI':>18s} {'halves':>16s} {'hedge after (min, median)':>26s}")
for name, sel in (("<= 0.95", lambda r: r["pc"] <= 0.95), ("0.95-1.00", lambda r: 0.95 < r["pc"] <= 1.0),
                  ("> 1.00", lambda r: r["pc"] > 1.0), ("all", lambda r: True)):
    rs = [r for r in rows if sel(r)]
    by = collections.defaultdict(list)
    for r in rs:
        by[r["ed"]].append(r["paired"] - r["hold"])
    dd = sorted(by)
    bs = [sum(x for d in rng.choice(dd, size=len(dd)) for x in by[d]) for _ in range(1000)]
    halves = [sum(r["paired"] - r["hold"] for r in rs if (r["ed"] < mid) == hf) for hf in (True, False)]
    ms = sum(r["m"] for r in rs)
    print(f"{name:12s} {len(rs):6d} {ms:10.0f} {np.average([r['W'] for r in rs], weights=[r['m'] for r in rs]):10.0%}"
          f" {sum(r['paired'] for r in rs):+10.0f} {sum(r['hold'] for r in rs):+10.0f} {sum(r['paired'] - r['hold'] for r in rs):+10.0f}"
          f" [{np.percentile(bs, 2.5):+7.0f}, {np.percentile(bs, 97.5):+7.0f}] {halves[0]:+7.0f}/{halves[1]:+7.0f}"
          f" {float(np.median([r['mins'] for r in rs])):26.0f}")
over = [r for r in rows if r["pc"] > 1.0]
print("\npair cost > 1.00, by the first leg's fate (USDC on the matched shares):")
for w in (1.0, 0.0):
    rs = [r for r in over if r["W"] == w]
    print(f"  first side {'won ' if w else 'lost'}: groups {len(rs)}, paired {sum(r['paired'] for r in rs):+.0f}, hold {sum(r['hold'] for r in rs):+.0f},"
          f" avg first price {np.average([r['a'] for r in rs], weights=[r['m'] for r in rs]):.3f}, avg hedge price {np.average([r['h'] for r in rs], weights=[r['m'] for r in rs]):.3f}")
