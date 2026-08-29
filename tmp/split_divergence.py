"""Split the diverged station-days into "should have moved" vs "correctly stood still".

For every station-day the ledger settled, find the last recompute where the held
buckets were still alive, then ask what the book could actually have bought at
that moment: the model's new argmax outside the held window, priced at the
best ask on the book at (or just before) that timestamp.  Positive edge there
means the top-up was left on the table; non-positive means standing still was
the objective doing its job.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right

DB = "file:data/bayes_basket_target_ssot.db?mode=ro"
conn = sqlite3.connect(DB, uri=True, timeout=60.0)

chains = {}
for row in conn.execute(
    "SELECT station_id,event_date,decision_at_utc,window_buckets_json,leg_shares_json"
    " FROM paper_trade_position ORDER BY decision_at_utc"):
    key = (str(row[0]), str(row[1]))
    e = chains.setdefault(key, {"held": set(), "opened": str(row[2])})
    legs = json.loads(row[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0] or json.loads(row[3]))

settled = {}
for row in conn.execute(
    "SELECT station_id,event_date,hit,pnl_usdc,stake_usdc,settled_bucket"
    " FROM paper_trade_settlement"):
    k = (str(row[0]), str(row[1]))
    cur = settled.setdefault(k, {"hit": 0, "pnl": 0.0, "stake": 0.0, "truth": str(row[5])})
    cur["hit"] = max(cur["hit"], int(row[2]))
    cur["pnl"] += float(row[3])
    cur["stake"] += float(row[4] or 0.0)

# Book history per station-day, ordered so we can take the last quote at or
# before the recompute instant.
books = {}
for row in conn.execute(
    "SELECT station_id,event_date,observed_at,buckets_json"
    " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    books.setdefault((str(row[0]), str(row[1])), []).append((str(row[2]), row[3]))

def quote_at(key, when):
    hist = books.get(key)
    if not hist:
        return None
    i = bisect_right([h[0] for h in hist], when)
    if i == 0:
        return None
    stamp, blob = hist[i - 1]
    asks = {}
    for b in json.loads(blob):
        ask = b.get("best_ask")
        if ask and ask > 0:
            asks[b["bucket"]] = float(ask)
    return stamp, asks

rows = []
for (st, day), e in chains.items():
    out = settled.get((st, day))
    if out is None:
        continue
    late = None
    for r in conn.execute(
        "SELECT decision_at_utc,envelope_json FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND decision_at_utc>=?"
        " ORDER BY decision_at_utc", (st, day, e["opened"])):
        env = json.loads(r[1])
        p, o = env["probabilities"], env["bucket_ordinals"]
        inside = sum(v for b, v in p.items() if b in e["held"])
        if inside <= 0.05:
            continue
        if not [o[b] for b in e["held"] if b in o]:
            continue
        late = {"at": str(r[0]), "inside": inside, "p": p, "o": o}
    if late is None:
        continue
    outside = {b: v for b, v in late["p"].items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    q = quote_at((st, day), late["at"])
    rec = {
        "st": st, "day": day, "held": sorted(e["held"]), "nheld": len(e["held"]),
        "at": late["at"], "out": 1 - late["inside"],
        "target": target, "p_target": outside[target],
        "truth": out["truth"], "hit": out["hit"],
        "pnl": out["pnl"], "stake": out["stake"],
        "ask": None, "quote_at": None,
    }
    if q:
        rec["quote_at"] = q[0]
        rec["ask"] = q[1].get(target)
        rec["asks"] = q[1]
        rec["p_all"] = late["p"]
        rec["ord"] = late["o"]
    rows.append(rec)

json.dump(rows, open("tmp/divergence_split.json", "w"))
have = [r for r in rows if r["ask"] is not None]
print("evaluated station-days: %d" % len(rows))
print("with a book quote at the recompute instant: %d" % len(have))
print("hot (>50%% out): %d, of those quoted: %d" % (
    len([r for r in rows if r["out"] > .5]),
    len([r for r in have if r["out"] > .5])))
if have:
    lag = [r for r in have if r["quote_at"] > r["at"]]
    print("quote taken after the recompute (should be 0): %d" % len(lag))
