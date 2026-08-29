"""Is the inflated ceiling just renormalisation after cutting from below?

The hard bound zeroes every bucket under the measured high and the posterior
is renormalised, so the surviving mass is divided by (1 - excluded prior mass).
That scaling does not care which side of the distribution it lifts: the far
tail above the high is multiplied by the same factor as the bucket the high
actually sits in.  If that is the whole story, the posterior mass above the
high should equal prior_above / (1 - prior_excluded) whenever the likelihood
is identity, with no room left for the trajectory to explain.
"""
import json, sqlite3, statistics, sys
sys.path.insert(0, "tmp")
from stage_pick import DB, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
truth = {(str(r[0]), str(r[1])) for r in conn.execute(
    "SELECT station_id,event_date FROM paper_trade_settlement")}
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
roots = {str(r[0]): (json.loads(r[1]), json.loads(r[2])) for r in conn.execute(
    "SELECT checkpoint_id,probabilities_json,bucket_ordinals_json"
    " FROM basket_probability_envelope WHERE stage='P0'")}

def holding(iv, v):
    for b, (lo, hi) in iv.items():
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return b
    return None

burst = {}
for r in conn.execute(
    "SELECT station_id,event_date,decision_at_utc,bucket_intervals_json,"
    "envelope_json FROM basket_probability_bridge_history"
    " WHERE state='COMMITTED' AND stage='P1' ORDER BY decision_at_utc, rowid"
):
    k = (str(r[0]), str(r[1]))
    if k not in truth or k not in tz:
        continue
    env = json.loads(r[4])
    run = (env.get("lineage") or {}).get("metar_trajectory_running_high_c")
    if run in (None, ""):
        continue
    key = (k[0], k[1], str(r[2])[:19])
    prev = burst.get(key)
    if prev is None or float(run) >= prev[0]:
        burst[key] = (float(run), r[3], env, str(r[2]))

zero, soft = [], []
for (st, day, _), (run, iv_s, env, at_s) in burst.items():
    k = (st, day)
    close = local_close(day, tz[k])
    at = parse(at_s)
    if (close - at).total_seconds() < 0:
        continue
    lin = env.get("lineage") or {}
    root = roots.get(str(lin.get("p0_root_checkpoint_id") or ""))
    if root is None:
        continue
    prior_p, prior_o = root
    o = env["bucket_ordinals"]
    rb = holding(json.loads(iv_s), run)
    if rb is None or rb not in o or rb not in prior_o:
        continue
    exc = str(lin.get("hard_excluded_buckets") or "NONE")
    excluded = set() if exc == "NONE" else set(exc.split(","))
    prior_cut = sum(v for b, v in prior_p.items() if b in excluded)
    if prior_cut >= 0.999:
        continue
    prior_above = sum(v for b, v in prior_p.items()
                      if prior_o.get(b, -1) > prior_o[rb])
    post_above = sum(v for b, v in env["probabilities"].items()
                     if o.get(b, -1) > o[rb])
    predicted = prior_above / (1.0 - prior_cut)
    s = lin.get("metar_trajectory_applied_strength")
    rec = (prior_above, predicted, post_above, prior_cut,
           at.astimezone(close.tzinfo).hour)
    (zero if (s in (None, "") or float(s) == 0.0) else soft).append(rec)

def show(name, g):
    if not g:
        print("  %s (无样本)" % name); return
    err = [abs(x[2] - x[1]) for x in g]
    print("  %-24s n=%5d" % (name, len(g)))
    print("     先验头顶 %5.1f%%  →  砍掉下方 %5.1f%%  →  归一化后应为 %5.1f%%  实际 %5.1f%%"
          % (100 * statistics.mean(x[0] for x in g),
             100 * statistics.mean(x[3] for x in g),
             100 * statistics.mean(x[1] for x in g),
             100 * statistics.mean(x[2] for x in g)))
    print("     预测与实际的绝对差: 中位 %.4f  90分位 %.4f" % (
        statistics.median(err), statistics.quantiles(err, n=10)[8]))

print("重新归一化能否单独解释头顶被顶高？")
show("似然恒等(strength=0)", zero)
show("似然生效(strength>0)", soft)
print()
night = [x for x in zero + soft if x[4] >= 19]
show("入夜后全部(19点起)", night)
