"""Reproduce the edge>0.25 target population, then locate it against the
running-high CEILING (derived from metar_trajectory_running_high_c against
bucket_intervals -- NOT reported_high_bucket, which degenerates)."""
import json, sqlite3, statistics, sys
from bisect import bisect_right
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
for k in sorted(truth):
    b = [(str(a), c) for a, c in conn.execute(
        "SELECT observed_at,buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? ORDER BY observed_at", k)]
    if b:
        books[k] = b
print("books:", len(books), flush=True)

def asks_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    return ({b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i - 1][1])
             if b.get("best_ask")} if i else {})

def bucket_of_temp(iv, t):
    for b, (lo, hi) in iv.items():
        if (lo is None or t > lo) and (hi is None or t <= hi):
            return b
    return None

chains = {}
for k in sorted(truth):
    ch = [(str(a), str(b), c) for a, b, c in conn.execute(
        "SELECT stage,decision_at_utc,envelope_json FROM"
        " basket_probability_bridge_history WHERE station_id=? AND event_date=?"
        " AND state='COMMITTED' ORDER BY decision_at_utc", k)]
    if ch:
        chains[k] = ch
print("chains:", len(chains), flush=True)
rows = []
for k, e in pos.items():
    if k not in truth or k not in chains or k not in tz:
        continue
    close = local_close(k[1], tz[k])
    p1 = [(at, b) for s, at, b in chains[k] if s == "P1" and at >= e["opened"]
          and (close - parse(at)).total_seconds() >= 0]
    alive = None
    for at, blob in p1:
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
    lin = env.get("lineage") or {}
    # TRUE ceiling: bucket containing the measured running high
    iv = json.loads(lin["bucket_intervals_c_json"]) if lin.get(
        "bucket_intervals_c_json") else None
    rh_c = lin.get("metar_trajectory_running_high_c")
    ceil_ord = None
    if iv and rh_c not in (None, ""):
        cb = bucket_of_temp(iv, float(rh_c))
        if cb in o:
            ceil_ord = o[cb]
    outside = {b: v for b, v in p.items() if b not in e["held"]}
    if not outside:
        continue
    target = max(outside, key=outside.get)
    ask = asks_at(k, at).get(target)
    if ask is None:
        continue
    d = parse(at)
    lh = d.astimezone(close.tzinfo)
    rows.append({"station": k[0], "date": k[1], "at": at,
                 "local_hour": lh.hour + lh.minute / 60.0,
                 "lead_h": (close - d).total_seconds() / 3600.0,
                 "p_t": outside[target], "ask": ask,
                 "edge": outside[target] - ask,
                 "target": target, "t_ord": o[target],
                 "truth_ord": o[tb], "won": target == tb,
                 "ceil_ord": ceil_ord, "rh_c": rh_c,
                 "above_ceiling": (ceil_ord is not None and o[target] > ceil_ord),
                 "mass_above_ceiling": (sum(v for b, v in p.items()
                                            if ceil_ord is not None
                                            and o.get(b, -1) > ceil_ord)
                                        if ceil_ord is not None else None),
                 "excluded": lin.get("hard_excluded_buckets", ""),
                 })
json.dump(rows, open("tmp/h1_edge_targets_rows.json", "w"))
print("targets:", len(rows))

def blk(name, sel):
    if not sel:
        print("  %-18s n=0" % name); return
    ac = [r for r in sel if r["ceil_ord"] is not None]
    print("  %-18s n=%-3d  claimed %.3f  ask %.3f  actual %.3f | "
          "target ABOVE ceiling %3.0f%% (n=%d)  above truth %3.0f%%  "
          "median local %4.1fh  mass-above-ceiling %.3f" % (
        name, len(sel),
        statistics.mean(r["p_t"] for r in sel),
        statistics.mean(r["ask"] for r in sel),
        sum(r["won"] for r in sel) / len(sel),
        100 * sum(r["above_ceiling"] for r in ac) / max(len(ac), 1), len(ac),
        100 * sum(1 for r in sel if r["t_ord"] > r["truth_ord"]) / len(sel),
        statistics.median([r["local_hour"] for r in sel]),
        statistics.mean([r["mass_above_ceiling"] for r in ac]) if ac else -1))

blk("edge>0.25", [r for r in rows if r["edge"] > .25])
blk("edge 0.10-0.25", [r for r in rows if .10 < r["edge"] <= .25])
blk("edge 0-0.10", [r for r in rows if 0 < r["edge"] <= .10])
blk("edge<=0", [r for r in rows if r["edge"] <= 0])
