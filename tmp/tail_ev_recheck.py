"""Is the book's tail still a loser once the ceiling is off its probability?

The tail was judged on probabilities that inflated exactly the buckets a tail
leg buys.  The clamp that governs those legs is lambda times that probability,
so the number tuned to hold it back was tuned against the inflation.  What the
legs actually paid and actually returned does not move; what moves is what the
model says they were worth, and therefore whether the clamp was reading a real
red line or a manufactured one.
"""
import json, sqlite3, statistics, sys
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

def holding(iv, v):
    for b, (lo, hi) in iv.items():
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return b

# Beliefs, prefix-burst collapsed, with the OOS ceiling rate attached.
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
    rows.append({"st": st, "day": day, "h": h, "rb": rb, "o": o,
                 "p": env["probabilities"], "at": at_s,
                 "above": 1 if o[tb] > o[rb] else 0})
E = [0, 1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 24, 1e9]
hb = lambda x: next(i for i in range(len(E) - 1) if E[i] <= x < E[i + 1])
days = sorted({r["day"] for r in rows})
byday = {}
for day in days:
    tr = [r for r in rows if r["day"] < day]
    if len(tr) < 400:
        continue
    per = defaultdict(list)
    for b in tr:
        per[hb(b["h"])].append(b["above"])
    ov = sum(x for v in per.values() for x in v) / sum(len(v) for v in per.values())
    byday[day] = {i: (sum(per[i]) / len(per[i]) if len(per.get(i, [])) >= 40 else None)
                  for i in range(len(E) - 1)}
belief = {}
for r in rows:
    t = byday.get(r["day"], {}).get(hb(r["h"]))
    key = (r["st"], r["day"])
    if key not in belief or r["at"] > belief[key]["at"]:
        belief[key] = {**r, "astar": t}

def corrected(b):
    if b["astar"] is None or b["h"] >= 10:
        return b["p"]
    a = sum(v for k, v in b["p"].items() if b["o"].get(k, -1) > b["o"][b["rb"]])
    if a <= 0 or a >= 1:
        return b["p"]
    t = min(max(b["astar"], 1e-6), 1 - 1e-6)
    up, dn = t / a, (1 - t) / (1 - a)
    out = {k: v * (up if b["o"].get(k, -1) > b["o"][b["rb"]] else dn)
           for k, v in b["p"].items()}
    s = sum(out.values())
    return {k: v / s for k, v in out.items()}

# The legs the book actually bought, by distance from the ladder centre.
legs = defaultdict(lambda: {"n": 0, "stake": 0.0, "payout": 0.0, "hit": 0,
                            "claim_old": [], "claim_new": []})
for r in conn.execute(
    "SELECT station_id,event_date,ladder_center_bucket,leg_shares_json,"
    "prices_json,stake_usdc FROM paper_trade_position"
    " WHERE leg_shares_json IS NOT NULL"):
    k = (str(r[0]), str(r[1]))
    if k not in settled or k not in belief:
        continue
    b = belief[k]
    o = b["o"]
    centre = str(r[2] or "")
    if centre not in o:
        continue
    shares = json.loads(r[3] or "{}")
    prices = json.loads(r[4] or "{}")
    new_p = corrected(b)
    for bucket, sh in shares.items():
        if not sh or bucket not in o:
            continue
        dist = abs(o[bucket] - o[centre])
        band = "中心" if dist == 0 else "肩(1桶)" if dist == 1 else "尾(≥2桶)"
        px = float(prices.get(bucket) or 0.0)
        c = legs[band]
        c["n"] += 1
        c["stake"] += px * float(sh)
        c["payout"] += float(sh) if bucket == settled[k] else 0.0
        c["hit"] += bucket == settled[k]
        c["claim_old"].append(b["p"].get(bucket, 0.0))
        c["claim_new"].append(new_p.get(bucket, 0.0))

print("账本实际买入的腿，按距梯子中心的距离\n")
print("  腿位      条数    投入u     回款u      净u     命中   旧概率均值  修正后   钳位上限变化")
for band in ("中心", "肩(1桶)", "尾(≥2桶)"):
    c = legs[band]
    if not c["n"]:
        continue
    old = statistics.mean(c["claim_old"]); new = statistics.mean(c["claim_new"])
    lam = 0.53 if band == "尾(≥2桶)" else 0.40 if band == "肩(1桶)" else None
    capline = ("—" if lam is None else
               "λ%.2f: %.3f→%.3f" % (lam, lam * old, lam * new))
    print("  %-9s %4d %8.1f %9.1f %+8.1f  %4.0f%%   %8.1f%% %8.1f%%   %s"
          % (band, c["n"], c["stake"], c["payout"], c["payout"] - c["stake"],
             100 * c["hit"] / c["n"], 100 * old, 100 * new, capline))
