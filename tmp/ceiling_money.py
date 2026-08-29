"""What the ceiling is worth where it costs money: the out-of-window argmax.

The phenomenon that started this was a bucket the model liked at 31%, the book
priced at a cent, and that never happened.  If the ceiling is the mechanism,
correcting it should move the argmax off that bucket rather than merely
re-scoring it.
"""
import json, sqlite3, statistics, sys
from bisect import bisect_right
from collections import defaultdict
sys.path.insert(0, "tmp")
from stage_pick import DB, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
arch = sqlite3.connect("file:data/archive.db?mode=ro", uri=True, timeout=120.0)
truth_max = {(str(r[0]), str(r[1])): float(r[2]) for r in arch.execute(
    "SELECT station_id,event_date,observed_max_c FROM"
    " reported_trajectory_canonical_station_day_bounds"
    " WHERE observed_max_c IS NOT NULL")}
settled = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
held = {}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,"
                      "window_buckets_json,leg_shares_json"
                      " FROM paper_trade_position ORDER BY decision_at_utc"):
    k = (str(r[0]), str(r[1]))
    e = held.setdefault(k, {"h": set(), "open": str(r[2])})
    legs = json.loads(r[4] or "{}")
    e["h"].update([b for b, s in legs.items() if (s or 0) > 0] or json.loads(r[3]))
books = {}
for r in conn.execute("SELECT station_id,event_date,observed_at,buckets_json"
                      " FROM basket_official_ws_snapshot ORDER BY observed_at"):
    books.setdefault((str(r[0]), str(r[1])), []).append((str(r[2]), r[3]))

def asks_at(k, when):
    h = books.get(k)
    if not h:
        return {}
    i = bisect_right([x[0] for x in h], when)
    return ({b["bucket"]: float(b["best_ask"]) for b in json.loads(h[i - 1][1])
             if b.get("best_ask")} if i else {})

def holding(iv, v):
    for b, (lo, hi) in iv.items():
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return b

burst = {}
for r in conn.execute(
    "SELECT station_id,event_date,decision_at_utc,bucket_intervals_json,"
    "envelope_json FROM basket_probability_bridge_history"
    " WHERE state='COMMITTED' AND stage='P1' ORDER BY decision_at_utc, rowid"):
    k = (str(r[0]), str(r[1]))
    if k not in truth_max or k not in tz:
        continue
    env = json.loads(r[4])
    run = (env.get("lineage") or {}).get("metar_trajectory_running_high_c")
    if run in (None, ""):
        continue
    key = (k[0], k[1], str(r[2])[:19])
    prev = burst.get(key)
    if prev is None or float(run) >= prev[0]:
        burst[key] = (float(run), r[3], env, str(r[2]))

rows = []
for (st, day, _), (run, iv_s, env, at_s) in burst.items():
    k = (st, day)
    close = local_close(day, tz[k]); at = parse(at_s)
    h = (close - at).total_seconds() / 3600.0
    if h < 0:
        continue
    iv = json.loads(iv_s); o = env["bucket_ordinals"]
    rb = holding(iv, run); tb = holding(iv, truth_max[k])
    if rb is None or tb is None or rb not in o or tb not in o:
        continue
    rows.append({"st": st, "day": day, "h": h, "rb": rb, "tb": tb, "o": o,
                 "p": env["probabilities"], "at_s": at_s,
                 "above": 1 if o[tb] > o[rb] else 0})

E = [0, 1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 24, 1e9]
hb = lambda x: next(i for i in range(len(E) - 1) if E[i] <= x < E[i + 1])
days = sorted({r["day"] for r in rows})
scored = []
for day in days:
    tr = [r for r in rows if r["day"] < day]
    if len(tr) < 400:
        continue
    per = defaultdict(list)
    for b in tr:
        per[hb(b["h"])].append(b["above"])
    ov = sum(x for v in per.values() for x in v) / sum(len(v) for v in per.values())
    tab = {i: (sum(per[i]) / len(per[i]) if len(per.get(i, [])) >= 40 else ov)
           for i in range(len(E) - 1)}
    for b in rows:
        if b["day"] == day:
            b["astar"] = tab[hb(b["h"])]
            scored.append(b)

def corrected(b):
    if b["h"] >= 10:
        return b["p"]
    a, t = sum(v for k, v in b["p"].items() if b["o"].get(k, -1) > b["o"][b["rb"]]), b["astar"]
    if a <= 0 or a >= 1:
        return b["p"]
    up, dn = t / a, (1 - t) / (1 - a)
    out = {k: v * (up if b["o"].get(k, -1) > b["o"][b["rb"]] else dn)
           for k, v in b["p"].items()}
    s = sum(out.values())
    return {k: v / s for k, v in out.items()}

# One belief per settled station-day: the last one a top-up could act on.
best = {}
for b in scored:
    k = (b["st"], b["day"])
    if k not in settled or k not in held:
        continue
    inside = sum(v for x, v in b["p"].items() if x in held[k]["h"])
    if inside <= 0.05 or b["at_s"] < held[k]["open"]:
        continue
    if k not in best or b["at_s"] > best[k]["at_s"]:
        best[k] = b

def tail_table(name, getter):
    cells = defaultdict(lambda: {"n": 0, "hit": 0, "claim": [], "ask": []})
    for k, b in best.items():
        q = getter(b)
        asks = asks_at(k, b["at_s"])
        out = {x: v for x, v in q.items() if x not in held[k]["h"]}
        if not out:
            continue
        tgt = max(out, key=out.get)
        ask = asks.get(tgt)
        if ask is None:
            continue
        edge = out[tgt] - ask
        band = (">0.25" if edge > .25 else "0.10-0.25" if edge > .10
                else "0-0.10" if edge > 0 else "<=0")
        c = cells[band]
        c["n"] += 1
        c["hit"] += tgt == settled[k]
        c["claim"].append(out[tgt]); c["ask"].append(ask)
    print("  %s" % name)
    print("    边际档       n    模型说   市场价   实际发生")
    for band in (">0.25", "0.10-0.25", "0-0.10", "<=0"):
        c = cells[band]
        if c["n"]:
            print("    %-11s %3d   %4.0f%%   %5.0f%%   %6.0f%%"
                  % (band, c["n"], 100 * statistics.mean(c["claim"]),
                     100 * statistics.mean(c["ask"]), 100 * c["hit"] / c["n"]))

print("已结算站日的窗外 argmax（n=%d 个站日）\n" % len(best))
tail_table("修正前", lambda b: b["p"])
print()
tail_table("修正后（仅 <10h）", corrected)
