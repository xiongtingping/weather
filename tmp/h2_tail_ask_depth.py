"""H2: is the 1-cent ask on the model's out-of-window target a real price?

Stage-aware cohort (P1>P0-M>P0, latest, lead>=0, held window still alive);
for each row measure the ASK LADDER DEPTH on the target bucket and contrast
with the buckets the market prices richly.
"""
import json, pickle, sqlite3, statistics, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import DB, pick, local_close, parse

SCRATCH = ("/private/tmp/claude-501/-Users-xiong-weather/"
           "4ef1fc86-6e89-403a-8a21-dfce91c6b976/scratchpad/h2_rows.pkl")
conn = sqlite3.connect(DB, uri=True, timeout=120.0)

truth = {}
for r in conn.execute("SELECT station_id,event_date,settled_bucket,settled_tmax_c"
                      " FROM paper_trade_settlement"):
    truth[(str(r[0]), str(r[1]))] = (str(r[2]), r[3])
keys = set(truth)

pos = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json"
                      " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    if k not in keys:
        continue
    e = pos.setdefault(k, {"held": set(), "opened": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0]
                     or json.loads(r[3]))

tz, books, chains = {}, {}, {}
for k in sorted(pos):
    for r in conn.execute(
            "SELECT observed_at,buckets_json,settlement_timezone"
            " FROM basket_official_ws_snapshot WHERE station_id=? AND"
            " event_date=? ORDER BY observed_at", k):
        tz[k] = str(r[2])
        books.setdefault(k, []).append((str(r[0]), r[1]))
    for r in conn.execute(
            "SELECT stage,decision_at_utc,envelope_json"
            " FROM basket_probability_bridge_history WHERE station_id=? AND"
            " event_date=? AND state='COMMITTED' ORDER BY decision_at_utc", k):
        chains.setdefault(k, []).append((str(r[0]), str(r[1]), r[2]))


def book_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    if not i:
        return {}
    return {b["bucket"]: b for b in json.loads(h[i - 1][1])}


rows = []
for k, e in pos.items():
    if k not in truth or k not in chains or k not in tz:
        continue
    close = local_close(k[1], tz[k])
    cand = [(s, at, b) for s, at, b in chains[k]
            if at >= e["opened"] and parse(at) <= close]
    got = pick(cand, predicate=lambda env: sum(
        v for b, v in env["probabilities"].items() if b in e["held"]) > 0.05)
    if got is None:
        continue
    _, at, stage, env = got
    p, o = env["probabilities"], env["bucket_ordinals"]
    tb = truth[k][0]
    if tb not in o:
        continue
    outside = {b: v for b, v in p.items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    bk = book_at(k, at)
    tgt = bk.get(target)
    if not tgt or not tgt.get("best_ask"):
        continue
    rows.append({"k": k, "at": at, "stage": stage, "target": target,
                 "p_t": outside[target], "ask": float(tgt["best_ask"]),
                 "edge": outside[target] - float(tgt["best_ask"]),
                 "won": target == tb, "book": bk, "tgt": tgt,
                 "held": e["held"], "truth": tb, "probs": p, "ords": o,
                 "lineage": env.get("lineage") or {},
                 "lead_h": (close - parse(at)).total_seconds() / 3600.0})

print("cohort n=%d" % len(rows))
BANDS = [("edge<=0", lambda r: r["edge"] <= 0),
         ("0..0.10", lambda r: 0 < r["edge"] <= .10),
         ("0.10-0.25", lambda r: .10 < r["edge"] <= .25),
         (">0.25", lambda r: r["edge"] > .25)]
for name, f in BANDS:
    s = [r for r in rows if f(r)]
    if not s:
        print("  %-10s n=0" % name); continue
    print("  %-10s n=%-3d  model %.0f%%  market %.0f%%  happened %.0f%%" % (
        name, len(s), 100 * statistics.mean(x["p_t"] for x in s),
        100 * statistics.mean(x["ask"] for x in s),
        100 * sum(x["won"] for x in s) / len(s)))
pickle.dump(rows, open(SCRATCH, "wb"))
