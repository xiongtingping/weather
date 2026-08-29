"""Attribute the 51 (+1) basket misses to a layer.

For each missed chain, read the P0 (and last P1) position row's own
posterior at decision time and ask three questions:

1. mass_on_truth: what probability did the served posterior give the
   bucket the day actually landed in?  Below the 0.03 leg floor the
   selection layer never had the option; above ~0.10 it did and passed.
2. mean_gap: posterior mean (bucket ordinals, in station steps) minus
   landed ordinal.  A mean sitting a bucket or more cold says the
   probability layer's centre was wrong, not the window shape.
3. window skew: (top-centre) vs (centre-bottom) of the chosen window.
   If windows extend cold while the truth escapes hot, the selection
   layer is adding to the damage.

Baseline: the same numbers on HIT chains, so the miss rows are read
against what "normal" looks like.
"""

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import selection_market_dashboard as dash

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)

rows = conn.execute(
    "SELECT s.station_id, s.event_date, s.settled_bucket,"
    " p.stage, p.decision_at_utc, p.ladder_center_bucket,"
    " p.probabilities_json, p.intended_legs_json,"
    " p.posted_buckets_json, p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL"
    " ORDER BY s.event_date, p.decision_at_utc",
).fetchall()

chains = {}
for (station, event_date, landed, stage, dat, center, prj, ij, pj,
     wj) in rows:
    key = (str(station), str(event_date))
    c = chains.setdefault(key, {
        "station": str(station), "event_date": str(event_date),
        "landed": str(landed), "chosen": set(), "stage_rows": [],
    })
    chosen = []
    try:
        chosen = [str(b) for b in (json.loads(ij) if ij else {}).keys()]
    except (TypeError, ValueError, AttributeError):
        chosen = []
    if not chosen:
        for fb in (pj, wj):
            try:
                chosen = [str(b) for b in json.loads(fb or "[]")]
            except (TypeError, ValueError):
                chosen = []
            if chosen:
                break
    c["chosen"].update(chosen)
    c["stage_rows"].append({
        "stage": str(stage), "decision_at": str(dat),
        "center": str(center), "probs": prj, "window": chosen,
    })


def posterior_stats(probs_json, landed_label):
    try:
        probs = json.loads(probs_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(probs, dict) or not probs:
        return None
    step = dash._bucket_step(landed_label)
    landed_o = dash._bucket_ordinal(landed_label)
    pts = []
    mass_on_truth = 0.0
    for label, p in probs.items():
        o = dash._bucket_ordinal(str(label))
        if o is None:
            continue
        pts.append((o, float(p)))
        if o == landed_o:
            mass_on_truth = float(p)
    if not pts or landed_o is None:
        return None
    total = sum(p for _, p in pts)
    if total <= 0:
        return None
    m = sum(o * p for o, p in pts) / total
    # gap in buckets: positive = truth ABOVE posterior mean (mean cold)
    gap = (landed_o - m) / step
    return {"mass_on_truth": mass_on_truth, "mean_gap_buckets": gap}


def window_skew(row, landed_label):
    step = dash._bucket_step(landed_label)
    ords = sorted({
        o for o in (dash._bucket_ordinal(b) for b in row["window"])
        if o is not None
    })
    co = dash._bucket_ordinal(row["center"])
    if not ords or co is None:
        return None
    up = (ords[-1] - co) / step
    down = (co - ords[0]) / step
    return {"up": up, "down": down}


plus1, minus1, hits = [], [], []
for c in chains.values():
    ords = sorted({
        o for o in (dash._bucket_ordinal(b) for b in c["chosen"])
        if o is not None
    })
    lo = dash._bucket_ordinal(c["landed"])
    if lo is None or not ords:
        continue
    step = dash._bucket_step(c["landed"])
    if c["landed"] in c["chosen"]:
        hits.append(c)
        continue
    if lo > ords[-1] and max(1, round((lo - ords[-1]) / step)) == 1:
        plus1.append(c)
    elif lo < ords[0] and max(1, round((ords[0] - lo) / step)) == 1:
        minus1.append(c)


def describe(group, name):
    p0_mass, p0_gap, last_mass, last_gap = [], [], [], []
    ups, downs = [], []
    floor_blocked = 0
    passable = 0
    for c in group:
        p0 = next((r for r in c["stage_rows"] if r["stage"] == "P0"),
                  None)
        last = c["stage_rows"][-1]
        if p0:
            st = posterior_stats(p0["probs"], c["landed"])
            if st:
                p0_mass.append(st["mass_on_truth"])
                p0_gap.append(st["mean_gap_buckets"])
            sk = window_skew(p0, c["landed"])
            if sk:
                ups.append(sk["up"])
                downs.append(sk["down"])
        st = posterior_stats(last["probs"], c["landed"])
        if st:
            last_mass.append(st["mass_on_truth"])
            last_gap.append(st["mean_gap_buckets"])
            if st["mass_on_truth"] < 0.03:
                floor_blocked += 1
            elif st["mass_on_truth"] >= 0.10:
                passable += 1
    print(f"\n== {name} (n={len(group)}) ==")
    if p0_mass:
        print(f"  P0 posterior mass on truth bucket:"
              f" median={median(p0_mass):.3f} mean={mean(p0_mass):.3f}")
        print(f"  P0 posterior mean gap (buckets, +=truth hotter):"
              f" median={median(p0_gap):+.2f} mean={mean(p0_gap):+.2f}")
    if last_mass:
        print(f"  LAST-stage mass on truth: median={median(last_mass):.3f}"
              f" mean={mean(last_mass):.3f}")
        print(f"  LAST-stage mean gap: median={median(last_gap):+.2f}"
              f" mean={mean(last_gap):+.2f}")
        print(f"  last-stage mass < 0.03 (leg floor, unbuyable):"
              f" {floor_blocked}/{len(last_mass)}")
        print(f"  last-stage mass >= 0.10 (clearly buyable, skipped):"
              f" {passable}/{len(last_mass)}")
    if ups:
        print(f"  P0 window reach: up median={median(ups):.1f}"
              f" mean={mean(ups):.2f} | down median={median(downs):.1f}"
              f" mean={mean(downs):.2f}")


describe(plus1, "+1 misses (truth hot side)")
describe(minus1, "-1 misses (truth cold side)")
describe(hits, "hits (baseline)")

# Distribution of mass_on_truth for +1 at the last stage, bucketed
print("\n== +1: last-stage mass on truth, distribution ==")
bins = defaultdict(int)
detail = []
for c in plus1:
    last = c["stage_rows"][-1]
    st = posterior_stats(last["probs"], c["landed"])
    if not st:
        continue
    m = st["mass_on_truth"]
    if m < 0.03:
        bins["<0.03 (floor)"] += 1
    elif m < 0.06:
        bins["0.03-0.06"] += 1
    elif m < 0.10:
        bins["0.06-0.10"] += 1
    elif m < 0.20:
        bins["0.10-0.20"] += 1
    else:
        bins[">=0.20"] += 1
    detail.append((c["event_date"], c["station"], m,
                   st["mean_gap_buckets"], last["stage"]))
for k in ("<0.03 (floor)", "0.03-0.06", "0.06-0.10", "0.10-0.20",
          ">=0.20"):
    if bins[k]:
        print(f"  {k}: {bins[k]}")

print("\n== +1 rows: last-stage truth mass and mean gap ==")
for d, s, m, g, stg in sorted(detail, key=lambda t: -t[2])[:20]:
    print(f"  {d} {s:6s} stage={stg:4s} mass_on_truth={m:.3f}"
          f" mean_gap={g:+.2f}")
