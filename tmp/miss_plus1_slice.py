"""Slice the 93 chosen-basket misses, focused on the 51 at +1.

Reuses the dashboard's own chain builder so the rows are byte-identical
to the section the user is reading, then asks the questions the page
does not: which stations, which dates, which stages held the chain,
and where the chosen set sat relative to the truth.
"""

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sqlite3

import selection_market_dashboard as dash

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)

# Rebuild chains but keep station + per-stage detail.
rows = conn.execute(
    "SELECT s.station_id, s.event_date, s.settled_bucket,"
    " s.claimed_win_probability, p.stage, p.intended_legs_json,"
    " p.posted_buckets_json, p.window_buckets_json"
    " FROM paper_trade_settlement s"
    " JOIN paper_trade_position p USING(station_id, event_date)"
    " WHERE s.settled_bucket IS NOT NULL"
    " ORDER BY s.event_date",
).fetchall()

import json

chains = {}
for station, event_date, landed, claimed, stage, ij, pj, wj in rows:
    key = (str(station), str(event_date))
    c = chains.setdefault(key, {
        "station": str(station), "event_date": str(event_date),
        "landed": str(landed), "claimed": float(claimed or 0.0),
        "chosen": set(), "stages": set(), "per_stage": {},
    })
    chosen = []
    try:
        payload = json.loads(ij) if ij else {}
        chosen = [str(b) for b in payload.keys()]
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
    c["stages"].add(str(stage))
    c["per_stage"].setdefault(str(stage), set()).update(chosen)

plus1 = []
minus1 = []
other = []
hits = 0
for c in chains.values():
    if c["landed"] in c["chosen"]:
        hits += 1
        continue
    ords = sorted({
        o for o in (dash._bucket_ordinal(b) for b in c["chosen"])
        if o is not None
    })
    lo = dash._bucket_ordinal(c["landed"])
    if lo is None or not ords:
        continue
    step = dash._bucket_step(c["landed"])
    if lo < ords[0]:
        off = -max(1, round((ords[0] - lo) / step))
    elif lo > ords[-1]:
        off = max(1, round((lo - ords[-1]) / step))
    else:
        continue
    c["offset"] = off
    c["width"] = len(ords)
    c["top"] = ords[-1]
    c["bottom"] = ords[0]
    if off == 1:
        plus1.append(c)
    elif off == -1:
        minus1.append(c)
    else:
        other.append(c)

print(f"chains={len(chains)} hits={hits} plus1={len(plus1)} minus1={len(minus1)} other={len(other)}")

def table(title, keyfn, rows_):
    agg = defaultdict(int)
    for c in rows_:
        agg[keyfn(c)] += 1
    print(f"\n== {title} ==")
    for k, v in sorted(agg.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}")

table("+1 by station", lambda c: c["station"], plus1)
table("+1 by month", lambda c: c["event_date"][:7], plus1)
table("+1 by stage-set", lambda c: "+".join(sorted(c["stages"])), plus1)
table("+1 by chosen width", lambda c: c["width"], plus1)

# For comparison, station mix of ALL settled chains
table("ALL settled by station (top)", lambda c: c["station"],
      list(chains.values()))

# Date detail for +1
print("\n== +1 rows (date, station, chosen span, landed, claimed) ==")
for c in sorted(plus1, key=lambda c: (c["event_date"], c["station"])):
    span = f"[{c['bottom']}..{c['top']}]w{c['width']}"
    print(f"  {c['event_date']} {c['station']:6s} {span:14s}"
          f" landed={c['landed']:14s} claimed={c['claimed']:.3f}"
          f" stages={'+'.join(sorted(c['stages']))}")
