"""Fit the cooling-side ceiling and measure what it would buy, out of sample.

The defect is one number per belief: how much mass sits above the bucket that
already holds the day's measured high.  The model's split of that mass is
wrong and the error is zero-sum -- what it leaves above is taken from the
bucket that actually settles.  So the correction reweights only that split and
keeps the model's shape inside each side, which is the smallest change that
can address the measurement.

The curve is P(settled bucket is above the running-high bucket | hours left to
local settlement), fitted on strictly earlier event dates than the one being
scored, so no belief is ever corrected using its own day or any later one.
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
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}

def holding(iv, v):
    for b, (lo, hi) in iv.items():
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return b
    return None

# Collapse each prefix burst to the belief it walked up to.
burst = {}
for r in conn.execute(
    "SELECT station_id,event_date,decision_at_utc,bucket_intervals_json,"
    "envelope_json FROM basket_probability_bridge_history"
    " WHERE state='COMMITTED' AND stage='P1' ORDER BY decision_at_utc, rowid"
):
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

beliefs = []
for (st, day, _), (run, iv_s, env, at_s) in burst.items():
    k = (st, day)
    close = local_close(day, tz[k])
    at = parse(at_s)
    hours_left = (close - at).total_seconds() / 3600.0
    if hours_left < 0:
        continue
    iv = json.loads(iv_s)
    o = env["bucket_ordinals"]
    rb = holding(iv, run)
    tb = holding(iv, truth_max[k])
    if rb is None or tb is None or rb not in o or tb not in o:
        continue
    above = sum(v for b, v in env["probabilities"].items()
                if o.get(b, -1) > o[rb])
    beliefs.append({
        "st": st, "day": day, "h": hours_left,
        "above_model": above, "above_truth": 1 if o[tb] > o[rb] else 0,
        "p": env["probabilities"], "o": o, "rb": rb, "tb": tb,
    })

print("可用信念 %d 条，覆盖 %d 个站日，%d 天"
      % (len(beliefs), len({(b["st"], b["day"]) for b in beliefs}),
         len({b["day"] for b in beliefs})))

EDGES = [0, 1, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 24, 1e9]
def hbin(h):
    for i in range(len(EDGES) - 1):
        if EDGES[i] <= h < EDGES[i + 1]:
            return i
    return len(EDGES) - 2

def fit(sample):
    """P(truth above the running-high bucket) per hours-left bin, with backoff."""
    per = defaultdict(list)
    for b in sample:
        per[hbin(b["h"])].append(b["above_truth"])
    overall = (sum(x for v in per.values() for x in v)
               / max(sum(len(v) for v in per.values()), 1))
    table = {}
    for i in range(len(EDGES) - 1):
        v = per.get(i, [])
        # Below 40 observations a bin says more about its own thinness than
        # about the hour, so it borrows the pooled rate.
        table[i] = (sum(v) / len(v)) if len(v) >= 40 else overall
    return table

days = sorted({b["day"] for b in beliefs})
scored = []
for day in days:
    train = [b for b in beliefs if b["day"] < day]
    if len(train) < 400:
        continue
    table = fit(train)
    for b in beliefs:
        if b["day"] != day:
            continue
        b["a_star"] = table[hbin(b["h"])]
        scored.append(b)

print("按日期扩展窗打分的信念 %d 条（前几天用于起步训练）\n" % len(scored))

def corrected(b):
    a, a_star = b["above_model"], b["a_star"]
    if a <= 0 or a >= 1:
        return b["p"]
    up, down = a_star / a, (1 - a_star) / (1 - a)
    out = {k: v * (up if b["o"].get(k, -1) > b["o"][b["rb"]] else down)
           for k, v in b["p"].items()}
    tot = sum(out.values())
    return {k: v / tot for k, v in out.items()}

def report(name, getter):
    brier = []
    hits = 0
    above_claim = []
    for b in scored:
        q = getter(b)
        for k, v in q.items():
            brier.append((v - (1 if k == b["tb"] else 0)) ** 2)
        hits += max(q, key=q.get) == b["tb"]
        above_claim.append(sum(v for k, v in q.items()
                               if b["o"].get(k, -1) > b["o"][b["rb"]]))
    print("  %-10s Brier %.4f   argmax 命中 %5.1f%%   押头顶均值 %5.1f%%"
          % (name, statistics.mean(brier), 100 * hits / len(scored),
             100 * statistics.mean(above_claim)))

print("整体（n=%d 条信念）" % len(scored))
report("修正前", lambda b: b["p"])
report("修正后", corrected)
print("  %-10s                                   实际 %5.1f%%"
      % ("真值", 100 * statistics.mean(b["above_truth"] for b in scored)))

print("\n按距结算小时数分档")
for lo, hi, lbl in ((0, 3, "0-3h"), (3, 6, "3-6h"), (6, 10, "6-10h"),
                    (10, 16, "10-16h"), (16, 99, ">16h")):
    g = [b for b in scored if lo <= b["h"] < hi]
    if len(g) < 30:
        continue
    def stat(getter):
        br, hit = [], 0
        for b in g:
            q = getter(b)
            for k, v in q.items():
                br.append((v - (1 if k == b["tb"] else 0)) ** 2)
            hit += max(q, key=q.get) == b["tb"]
        return statistics.mean(br), 100 * hit / len(g)
    b0, h0 = stat(lambda b: b["p"])
    b1, h1 = stat(corrected)
    print("  %-7s n=%4d  Brier %.4f→%.4f  命中 %4.1f%%→%4.1f%%  真值押头顶 %4.1f%%"
          % (lbl, len(g), b0, b1, h0, h1,
             100 * statistics.mean(x["above_truth"] for x in g)))
