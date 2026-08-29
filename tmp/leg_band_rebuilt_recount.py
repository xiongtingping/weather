"""Re-measure the leg-level bands on the REBUILT ledger, banding by the
production predicate (_is_shoulder / index distance on the posted window),
and splitting the shoulder by which side of the ladder centre it sits on.
"""
import importlib.util
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

ROOT = "/Users/xiong/weather"
spec = importlib.util.spec_from_file_location(
    "ptl", os.path.join(ROOT, "scripts/paper_trading_ledger.py"))
ptl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ptl)
print("SHOULDER_LAMBDA=%r TAIL_LAMBDA=%r"
      % (ptl.MAKER_RESTING_SHOULDER_LAMBDA, ptl.MAKER_RESTING_TAIL_LAMBDA))

conn = sqlite3.connect(
    "file:%s/data/bayes_basket_target_ssot.db?mode=ro" % ROOT, uri=True,
    timeout=60)
conn.row_factory = sqlite3.Row
truth = ptl._truth(conn)

rows = conn.execute(
    "SELECT position_id, station_id, event_date, stage, decision_at_utc,"
    " ladder_center_bucket, window_buckets_json, leg_shares_json,"
    " stake_usdc, shares, prices_json"
    " FROM paper_trade_position ORDER BY station_id, event_date, stage"
).fetchall()

bands = defaultdict(lambda: {"legs": 0, "hits": 0, "cost": 0.0, "pay": 0.0})
side = defaultdict(lambda: {"legs": 0, "hits": 0})
skipped = Counter()
for r in rows:
    key = (r["station_id"], r["event_date"])
    t = truth.get(key)
    if t is None:
        skipped["no_truth"] += 1
        continue
    ladderrows = ptl._settlement_ladder(
        conn, r["station_id"], r["event_date"], r["stage"],
        r["decision_at_utc"])
    if not ladderrows:
        skipped["no_ladder"] += 1
        continue
    landed = ptl._settled_bucket(list(ladderrows), float(t.tmax_c))
    if landed is None:
        skipped["outside_ladder"] += 1
        continue
    settled_bucket, settled_ord = landed
    ords = {str(b): int(o) for b, o, _l, _u, _p in ladderrows}
    window = json.loads(r["window_buckets_json"] or "[]")
    centre = str(r["ladder_center_bucket"])
    legs = json.loads(r["leg_shares_json"] or "{}") or {}
    prices = json.loads(r["prices_json"] or "{}") or {}
    if not legs:
        # fall back: whole-basket share count spread across the window
        skipped["no_leg_shares"] += 1
        legs = {b: float(r["shares"]) for b in window}
    for bucket, qty in legs.items():
        qty = float(qty)
        if qty <= 0:
            continue
        # band exactly as production does: index distance on posted window
        if str(bucket) == centre:
            band = "centre"
        elif ptl._is_shoulder(bucket, centre, window):
            band = "shoulder"
        else:
            band = "rest"
        hit = 1 if str(bucket) == settled_bucket else 0
        px = float(prices.get(str(bucket)) or 0.0)
        b = bands[band]
        b["legs"] += 1
        b["hits"] += hit
        b["cost"] += qty * px * (1.0 + ptl.FEE_RATE)
        b["pay"] += qty if hit else 0.0
        if band == "shoulder" and str(bucket) in ords and centre in ords:
            sgn = ords[str(bucket)] - ords[centre]
            s = side["above" if sgn > 0 else "below"]
            s["legs"] += 1
            s["hits"] += hit

print("skipped:", dict(skipped))
print("\n=== leg bands, rebuilt ledger (distance from ladder_center_bucket"
      " on the posted window, production _is_shoulder)")
for band in ("centre", "shoulder", "rest"):
    b = bands[band]
    if not b["legs"]:
        continue
    print("  %-9s legs %4d  hit %5.1f%%  cost %8.1fu  payout %8.1fu"
          "  net %+8.1fu"
          % (band, b["legs"], 100.0 * b["hits"] / b["legs"], b["cost"],
             b["pay"], b["pay"] - b["cost"]))
print("\n  shoulder split by side of the ladder centre:")
for k in ("below", "above"):
    s = side[k]
    if s["legs"]:
        print("    %-5s legs %4d  hit %5.1f%%"
              % (k, s["legs"], 100.0 * s["hits"] / s["legs"]))
