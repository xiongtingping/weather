"""H4: is "says 31%, happened 0/19" actually surprising, or small-sample noise?

Rebuild the stage-aware cohort, then for the edge>0.25 group compute the exact
Poisson-binomial P(0 successes) under the model's OWN per-case claims, count
distinct stations / dates, jackknife the biggest contributor, and bootstrap.
"""
import json, sqlite3, statistics, sys, random, itertools
from bisect import bisect_right
from collections import Counter
sys.path.insert(0, "tmp")
from stage_pick import DB, load_chains, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)

pos = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json"
                      " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    e = pos.setdefault(k, {"held": set(), "opened": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0]
                     or json.loads(r[3]))

truth = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    k = (str(r[0]), str(r[1]))
    if k in truth:
        books.setdefault(k, []).append((str(r[2]), r[3]))

def asks_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    return ({b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i - 1][1])
             if b.get("best_ask")} if i else {})

chains = load_chains(conn, keys=set(truth))
rows = []
for k, e in pos.items():
    if k not in truth or k not in chains or k not in tz:
        continue
    close = local_close(k[1], tz[k])
    alive = None
    for stage, at, blob in chains[k]:
        if stage != "P1" or at < e["opened"]:
            continue
        if (close - parse(at)).total_seconds() < 0:      # lead < 0: after settle
            continue
        env = json.loads(blob)
        if sum(v for b, v in env["probabilities"].items()
               if b in e["held"]) > 0.05:
            alive = (at, env)
    if alive is None:
        continue
    at, env = alive
    p, o = env["probabilities"], env["bucket_ordinals"]
    tb = truth[k]
    if tb not in o:
        continue
    outside = {b: v for b, v in p.items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    ask = asks_at(k, at).get(target)
    if ask is None:
        continue
    rows.append({"k": k, "at": at, "p": outside[target], "ask": ask,
                 "edge": outside[target] - ask, "won": target == tb,
                 "lead_h": (close - parse(at)).total_seconds() / 3600.0,
                 "target": target, "truth": tb,
                 "t_ord": o[target], "truth_ord": o[tb]})

BANDS = (("edge<=0", -9, 0), ("0..0.10", 0, .10),
         ("0.10..0.25", .10, .25), (">0.25", .25, 9))
def sel(lo, hi):
    return [r for r in rows if lo < r["edge"] <= hi] if lo != -9 \
        else [r for r in rows if r["edge"] <= hi]

print("COHORT REPRODUCTION (target = argmax outside held window)")
for name, lo, hi in BANDS:
    s = sel(lo, hi)
    print("  %-11s n=%-3d  model %3.0f%%  market %3.0f%%  happened %3.0f%%"
          % (name, len(s), 100*statistics.mean(r["p"] for r in s),
             100*statistics.mean(r["ask"] for r in s),
             100*sum(r["won"] for r in s)/len(s)))
print("  total n=%d" % len(rows))
json.dump([{kk: (list(v) if isinstance(v, tuple) else v)
            for kk, v in r.items()} for r in rows],
          open("tmp/h4_cohort.json", "w"))
