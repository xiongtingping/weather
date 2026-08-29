"""Independent re-derivation of the target-bucket phenomenon.

Deliberately does NOT copy the prior agent's extra filters (P1-only chain,
"held window still has >5% mass", "a metar trajectory proof must exist").
Uses the mandated stage-first pick + lead>=0 only.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right
sys.path.insert(0, "tmp")
from stage_pick import DB, load_chains, pick, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)

held = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json"
                      " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    e = held.setdefault(k, {"held": set(), "opened": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["held"].update([b for b, s in legs.items() if (s or 0) > 0]
                     or json.loads(r[3]))

settled = {}
for r in conn.execute("SELECT station_id,event_date,settled_bucket,settled_tmax_c"
                      " FROM paper_trade_settlement"):
    settled[(str(r[0]), str(r[1]))] = (str(r[2]), r[3])

tzs = {}
for r in conn.execute("SELECT DISTINCT station_id,event_date,settlement_timezone"
                      " FROM basket_probability_bridge_history"):
    tzs[(str(r[0]), str(r[1]))] = str(r[2])

keys = {k for k in held if k in settled}
print("settled station-days with a paper position: %d" % len(keys))
chains = load_chains(conn, keys)

books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot WHERE state='COMMITTED'"
                      " ORDER BY observed_at"):
    k = (str(r[0]), str(r[1]))
    if k in keys:
        books.setdefault(k, []).append((str(r[2]), r[3]))

def asks_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    if not i:
        return {}
    return {b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i-1][1])
            if b.get("best_ask")}

def build(require_after_open, require_inside, p1_only):
    out = []
    for k in sorted(keys):
        tz = tzs.get(k)
        if not tz:
            continue
        close = local_close(k[1], tz)
        cand = []
        for stage, at, blob in chains.get(k, []):
            if p1_only and stage != "P1":
                continue
            if require_after_open and at < held[k]["opened"]:
                continue
            if (close - parse(at)).total_seconds() < 0:     # lead>=0 only
                continue
            cand.append((stage, at, blob))
        if not cand:
            continue
        pred = None
        if require_inside is not None:
            thr = require_inside
            pred = lambda env: sum(v for b, v in env["probabilities"].items()
                                   if b in held[k]["held"]) > thr
        best = pick(cand, predicate=pred)
        if best is None:
            continue
        rank, at, stage, env = best
        p = env["probabilities"]
        outside = {b: v for b, v in p.items() if b not in held[k]["held"]}
        if not outside:
            continue
        t = max(outside, key=outside.get)
        ask = asks_at(k, at).get(t)
        if ask is None:
            continue
        out.append({"k": k, "stage": stage, "at": at, "target": t,
                    "p_t": outside[t], "ask": ask,
                    "won": settled[k][0] == t,
                    "lead": (close - parse(at)).total_seconds()/3600.0,
                    "env": env})
    return out

def report(tag, rows):
    print("\n%s   n=%d  lead median %.2fh  stages=%s" % (
        tag, len(rows), statistics.median([r["lead"] for r in rows]),
        {s: sum(1 for r in rows if r["stage"] == s) for s in ("P1","P0-M","P0")}))
    for lo, hi, name in ((-9,0,"<=0"),(0,.10,"0~0.10"),(.10,.25,"0.10-0.25"),(.25,9,">0.25")):
        sel = [r for r in rows if (lo <= r["p_t"]-r["ask"] <= hi if lo < 0
                                   else lo < r["p_t"]-r["ask"] <= hi)]
        if not sel: continue
        print("   edge %-10s n=%-3d model %4.1f%%  mkt %4.1f%%  actual %4.1f%%" % (
            name, len(sel), 100*statistics.mean(r["p_t"] for r in sel),
            100*statistics.mean(r["ask"] for r in sel),
            100*sum(r["won"] for r in sel)/len(sel)))

A = build(False, None, False); report("[A] spec: stage-first, lead>=0, no extra filter", A)
B = build(True, None, False);  report("[B] + only rows at/after position open", B)
C = build(True, 0.05, True);   report("[C] prior agent: P1-only, after open, inside>5%", C)
D = build(True, 0.05, False);  report("[D] stage-first, after open, inside>5%", D)
json.dump([{"k": list(r["k"]), "stage": r["stage"], "at": r["at"],
            "target": r["target"], "p_t": r["p_t"], "ask": r["ask"],
            "won": r["won"], "lead": r["lead"]} for r in A],
          open("tmp/refute_h3_specA.json","w"))
json.dump([{"k": list(r["k"]), "stage": r["stage"], "at": r["at"],
            "target": r["target"], "p_t": r["p_t"], "ask": r["ask"],
            "won": r["won"], "lead": r["lead"]} for r in C],
          open("tmp/refute_h3_priorC.json","w"))
