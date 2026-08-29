"""Was the selection layer obeying its own skew rule on the +1 days?

Charter 55: on a day whose posterior mean sits >= half a bucket from
its mode, the window must not extend against the skew.  For each of
the 51 (+1) chains' P0 rows, compute the posterior's own skew
(mean - mode, in station bucket steps) and the window's lean
(up-reach - down-reach).  A warm-skewed posterior with a cold-leaning
window is a charter violation; a cold-skewed posterior with a
cold-leaning window means selection was faithful and the probability
layer alone put the mass in the wrong place.

Also: pooled mean-gap over every settled chain, and per-day trend, so
the cold bias is read as one number on the whole book.
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
    " p.stage, p.ladder_center_bucket, p.probabilities_json,"
    " p.intended_legs_json, p.posted_buckets_json,"
    " p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL"
    " ORDER BY s.event_date, p.decision_at_utc",
).fetchall()

chains = {}
for station, event_date, landed, stage, center, prj, ij, pj, wj in rows:
    key = (str(station), str(event_date))
    c = chains.setdefault(key, {
        "station": str(station), "event_date": str(event_date),
        "landed": str(landed), "chosen": set(), "p0": None,
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
    if str(stage) == "P0" and c["p0"] is None:
        c["p0"] = {"center": str(center), "probs": prj,
                   "window": chosen}


def posterior(probs_json, step_ref):
    try:
        probs = json.loads(probs_json)
    except (TypeError, ValueError):
        return None
    pts = [(dash._bucket_ordinal(str(k)), float(v))
           for k, v in probs.items()]
    pts = [(o, p) for o, p in pts if o is not None]
    if not pts:
        return None
    step = dash._bucket_step(step_ref)
    total = sum(p for _, p in pts)
    if total <= 0:
        return None
    m = sum(o * p for o, p in pts) / total
    mode = max(pts, key=lambda t: t[1])[0]
    return {"mean": m, "mode": mode, "step": step}


offsets = {}
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
        off = 0
    elif lo > ords[-1]:
        off = max(1, round((lo - ords[-1]) / step))
    elif lo < ords[0]:
        off = -max(1, round((ords[0] - lo) / step))
    else:
        continue
    c["offset"] = off
    offsets.setdefault(off, []).append(c)

# 1. Charter-55 conformance on the +1 chains
viol = faithful_cold = warm_but_capped = flat = 0
details = []
for c in offsets.get(1, []):
    p0 = c["p0"]
    if not p0:
        continue
    post = posterior(p0["probs"], c["landed"])
    ords = sorted({
        o for o in (dash._bucket_ordinal(b) for b in p0["window"])
        if o is not None
    })
    co = dash._bucket_ordinal(p0["center"])
    if not post or not ords or co is None:
        continue
    skew = (post["mean"] - post["mode"]) / post["step"]
    up = (ords[-1] - co) / post["step"]
    down = (co - ords[0]) / post["step"]
    lean = up - down
    if skew >= 0.5 and lean < 0:
        viol += 1
        details.append((c["event_date"], c["station"], skew, up, down))
    elif skew <= -0.5:
        faithful_cold += 1
    elif skew >= 0.5 and lean >= 0:
        warm_but_capped += 1
    else:
        flat += 1
print("== charter-55 read on the 51 (+1) P0 rows ==")
print(f"  warm skew but window leaned cold (violation): {viol}")
print(f"  warm skew, window leaned warm/flat (obeyed): "
      f"{warm_but_capped}")
print(f"  posterior itself cold-skewed (selection faithful): "
      f"{faithful_cold}")
print(f"  |skew| < 0.5 bucket (gate not armed): {flat}")
for d, s, sk, u, dn in details:
    print(f"    {d} {s} skew={sk:+.2f} up={u:.1f} down={dn:.1f}")

# 2. Pooled signed mean-gap across every settled chain
gaps = []
per_day = defaultdict(list)
for group in offsets.values():
    for c in group:
        p0 = c["p0"]
        if not p0:
            continue
        post = posterior(p0["probs"], c["landed"])
        lo = dash._bucket_ordinal(c["landed"])
        if not post or lo is None:
            continue
        g = (lo - post["mean"]) / post["step"]
        gaps.append(g)
        per_day[c["event_date"]].append(g)
print(f"\n== pooled P0 mean-gap, all {len(gaps)} settled chains ==")
print(f"  median={median(gaps):+.2f} mean={mean(gaps):+.2f} buckets"
      f" (positive = day ran hotter than the posterior mean)")
print(f"  share hotter than posterior: "
      f"{sum(1 for g in gaps if g > 0)}/{len(gaps)}")
print("\n== per-day mean gap (chains) ==")
for day in sorted(per_day):
    g = per_day[day]
    print(f"  {day}: n={len(g):3d} mean_gap={mean(g):+.2f}")
