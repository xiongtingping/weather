"""Reliability of EVERY bucket claim at the same 104 decision instants,
split by whether the bucket sits above / at / below the measured-high bucket,
and by clock.  This is where the 'good unconditional calibration' comes from."""
import json, sqlite3, statistics, sys
sys.path.insert(0, "tmp")
from stage_pick import parse

rows = json.load(open("tmp/h1_edge_targets_rows.json"))
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                       uri=True, timeout=120)
truth = {(str(a), str(b)): str(c) for a, b, c in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
pairs = []
for r in rows:
    k = (r["station"], r["date"])
    env = [json.loads(b) for (b,) in conn.execute(
        "SELECT envelope_json FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND decision_at_utc=?"
        " AND state='COMMITTED' AND stage='P1' ORDER BY rowid",
        (k[0], k[1], r["at"]))][-1]
    o, p = env["bucket_ordinals"], env["probabilities"]
    tb = truth[k]
    for b, v in p.items():
        if b not in o or r["ceil_ord"] is None:
            continue
        rel = ("above" if o[b] > r["ceil_ord"]
               else "at" if o[b] == r["ceil_ord"] else "below")
        pairs.append({"p": v, "won": b == tb, "rel": rel,
                      "late": r["local_hour"] >= 17})
brier = statistics.mean((x["p"] - (1 if x["won"] else 0)) ** 2 for x in pairs)
print("all bucket-claims at these instants: n=%d  Brier=%.4f  "
      "claimed=%.4f  actual=%.4f" % (
          len(pairs), brier, statistics.mean(x["p"] for x in pairs),
          sum(x["won"] for x in pairs) / len(pairs)))
print("\n%-22s%6s%10s%9s%9s" % ("", "n", "claimed", "actual", "Brier"))
for lab, sel in (
    ("LATE(>=17) above",  [x for x in pairs if x["late"] and x["rel"] == "above"]),
    ("LATE(>=17) at ceil", [x for x in pairs if x["late"] and x["rel"] == "at"]),
    ("LATE(>=17) below",  [x for x in pairs if x["late"] and x["rel"] == "below"]),
    ("EARLY(<17) above",  [x for x in pairs if not x["late"] and x["rel"] == "above"]),
    ("EARLY(<17) at ceil", [x for x in pairs if not x["late"] and x["rel"] == "at"]),
    ("EARLY(<17) below",  [x for x in pairs if not x["late"] and x["rel"] == "below"]),
):
    if not sel:
        continue
    print("%-22s%6d%10.4f%9.4f%9.4f" % (
        lab, len(sel), statistics.mean(x["p"] for x in sel),
        sum(x["won"] for x in sel) / len(sel),
        statistics.mean((x["p"] - (1 if x["won"] else 0)) ** 2 for x in sel)))
