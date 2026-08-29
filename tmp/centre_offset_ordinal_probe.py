"""B: signed ordinal offset ladder_center_bucket - model_center_bucket,
and P(settled at distance d) from EACH centre on the same station-days.

Imports the production predicates rather than restating them.
"""
import importlib.util
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

ROOT = "/Users/xiong/weather"
sys.path.insert(0, ROOT)
spec = importlib.util.spec_from_file_location(
    "ptl", os.path.join(ROOT, "scripts/paper_trading_ledger.py")
)
ptl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ptl)

conn = sqlite3.connect(
    "file:%s/data/bayes_basket_target_ssot.db?mode=ro" % ROOT, uri=True,
    timeout=60,
)
conn.row_factory = sqlite3.Row
truth = ptl._truth(conn)
print("truth rows:", len(truth))

rows = conn.execute(
    "SELECT position_id, station_id, event_date, stage, decision_at_utc,"
    " ladder_center_bucket, window_buckets_json, leg_shares_json,"
    " posted_buckets_json"
    " FROM paper_trade_position ORDER BY event_date, station_id, stage"
).fetchall()
print("positions:", len(rows))

recs = []
missing_env = 0
missing_lineage = 0
for r in rows:
    env = conn.execute(
        "SELECT lineage_json, bucket_ordinals_json"
        " FROM basket_probability_envelope"
        " WHERE station_id=? AND event_date=? AND stage=? AND"
        " decision_at_utc=?",
        (r["station_id"], r["event_date"], r["stage"], r["decision_at_utc"]),
    ).fetchone()
    if env is None:
        missing_env += 1
        continue
    lineage = json.loads(env["lineage_json"] or "{}")
    ords = json.loads(env["bucket_ordinals_json"] or "{}")
    model_c_bucket = str(lineage.get("model_center_bucket") or "")
    if not model_c_bucket:
        missing_lineage += 1
        continue
    ladder = str(r["ladder_center_bucket"])
    if model_c_bucket not in ords or ladder not in ords:
        missing_lineage += 1
        continue
    recs.append({
        "position_id": r["position_id"],
        "station_id": r["station_id"],
        "event_date": r["event_date"],
        "stage": r["stage"],
        "decision_at_utc": r["decision_at_utc"],
        "ladder": ladder,
        "model": model_c_bucket,
        "ords": ords,
        "offset": int(ords[ladder]) - int(ords[model_c_bucket]),
        "window": json.loads(r["window_buckets_json"] or "[]"),
        "model_center_c": lineage.get("model_center_c"),
    })

print("joined:", len(recs), "missing_env:", missing_env,
      "missing_lineage:", missing_lineage)

# ---- distribution of the signed offset, per decision (position row)
off = Counter(rec["offset"] for rec in recs)
n = len(recs)
print("\n=== signed offset ord(ladder) - ord(model), per decision (n=%d)" % n)
for d in sorted(off):
    print("  %+d : %4d  %5.1f%%" % (d, off[d], 100.0 * off[d] / n))
by_stage = defaultdict(Counter)
for rec in recs:
    by_stage[rec["stage"]][rec["offset"]] += 1
for st in sorted(by_stage):
    tot = sum(by_stage[st].values())
    print("  stage %-4s n=%3d  " % (st, tot) + " ".join(
        "%+d:%d" % (d, c) for d, c in sorted(by_stage[st].items())))

# ---- distance from each centre to the settled bucket, same station-days
per_day = {}
for rec in recs:
    key = (rec["station_id"], rec["event_date"])
    t = truth.get(key)
    if t is None:
        continue
    ladderrows = ptl._settlement_ladder(
        conn, rec["station_id"], rec["event_date"], rec["stage"],
        rec["decision_at_utc"],
    )
    if not ladderrows:
        continue
    landed = ptl._settled_bucket(list(ladderrows), float(t.tmax_c))
    if landed is None:
        continue
    settled_bucket, settled_ord = landed
    rec["settled_bucket"] = settled_bucket
    rec["settled_ord"] = settled_ord
    rec["d_from_ladder"] = settled_ord - int(rec["ords"][rec["ladder"]])
    rec["d_from_model"] = settled_ord - int(rec["ords"][rec["model"]])
    per_day.setdefault(key, rec)

settled = [r for r in recs if "settled_ord" in r]
print("\nsettled decisions: %d over %d station-days"
      % (len(settled), len(per_day)))

def report(name, recs_, field):
    c = Counter(r[field] for r in recs_)
    tot = len(recs_)
    print("\n--- %s (n=%d)" % (name, tot))
    for d in sorted(c):
        print("   d=%+d : %4d  %5.1f%%" % (d, c[d], 100.0 * c[d] / tot))
    zero = c.get(0, 0)
    one = c.get(-1, 0) + c.get(1, 0)
    print("   |d|=0 %.1f%%   |d|<=1 %.1f%%   d=-1 %.1f%%  d=+1 %.1f%%"
          % (100.0 * zero / tot, 100.0 * (zero + one) / tot,
             100.0 * c.get(-1, 0) / tot, 100.0 * c.get(1, 0) / tot))

for scope_name, scope in (("all decisions", settled),
                          ("one row per station-day", list(per_day.values()))):
    print("\n########## %s" % scope_name)
    report("distance from LADDER centre", scope, "d_from_ladder")
    report("distance from MODEL centre", scope, "d_from_model")

json.dump(
    [{k: v for k, v in r.items() if k != "ords"} for r in recs],
    open("/Users/xiong/weather/tmp/centre_offset_ordinal_probe.json", "w"),
    ensure_ascii=False, indent=1,
)
