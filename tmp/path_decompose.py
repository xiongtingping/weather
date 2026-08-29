"""Is the missing ceiling the temperature path's doing, or its omission?

P1 is P0 reweighted by the METAR trajectory likelihood plus the hard exclusion.
So the evening mass above the already-measured high is either mass the path
put there, or mass the path left alone.  Comparing each P1 belief against its
own named P0 root separates the two: if the two carry the same mass above the
running high, the path never touched the ceiling; if P1 carries less but not
zero, the path pushes the right way and stops short.
"""
import json, sqlite3, statistics, sys
sys.path.insert(0, "tmp")
from stage_pick import DB, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
truth = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
roots = {}
for r in conn.execute("SELECT checkpoint_id,probabilities_json,bucket_ordinals_json"
                      " FROM basket_probability_envelope WHERE stage='P0'"):
    roots[str(r[0])] = (json.loads(r[1]), json.loads(r[2]))

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

by_band, strengths, missing = {}, [], 0
for (st, day, _), (run, iv_s, env, at_s) in burst.items():
    k = (st, day)
    close = local_close(day, tz[k])
    at = parse(at_s)
    if (close - at).total_seconds() < 0:
        continue
    lin = env.get("lineage") or {}
    root = roots.get(str(lin.get("p0_root_checkpoint_id") or ""))
    if root is None:
        missing += 1
        continue
    prior_p, prior_o = root
    o = env["bucket_ordinals"]
    rb = holding(json.loads(iv_s), run)
    if rb is None or rb not in o or rb not in prior_o:
        continue
    post_above = sum(v for b, v in env["probabilities"].items()
                     if o.get(b, -1) > o[rb])
    prior_above = sum(v for b, v in prior_p.items()
                      if prior_o.get(b, -1) > prior_o[rb])
    hour = at.astimezone(close.tzinfo).hour
    band = ("清晨 6-11点" if 6 <= hour < 12 else "峰值窗 12-15点" if 12 <= hour < 16
            else "傍晚 16-18点" if 16 <= hour < 19 else "入夜 19-21点"
            if 19 <= hour < 22 else "深夜 22-23点" if hour >= 22 else "凌晨 0-5点")
    by_band.setdefault(band, []).append((prior_above, post_above))
    s = lin.get("metar_trajectory_applied_strength")
    if s not in (None, ""):
        strengths.append((hour, float(s)))

print("找不到 P0 根的信念: %d\n" % missing)
print("头顶质量：P0 先验 → P1 后验（温度路径把它压掉了多少）")
print("  时段              n      P0先验   P1后验   路径压掉")
for band in ("清晨 6-11点", "峰值窗 12-15点", "傍晚 16-18点", "入夜 19-21点", "深夜 22-23点"):
    g = by_band.get(band)
    if not g:
        continue
    pr = statistics.mean(x[0] for x in g)
    po = statistics.mean(x[1] for x in g)
    print("  %-16s %5d    %5.1f%%   %5.1f%%   %+5.1f 点 (%.0f%%)" % (
        band, len(g), 100 * pr, 100 * po, 100 * (po - pr),
        100 * (pr - po) / pr if pr else 0))

print("\n路径强度 applied_strength 按时段")
for band, lo, hi in (("清晨 6-11点", 6, 12), ("峰值窗 12-15点", 12, 16),
                     ("傍晚 16-18点", 16, 19), ("入夜 19-21点", 19, 22),
                     ("深夜 22-23点", 22, 24)):
    g = [s for h, s in strengths if lo <= h < hi]
    if g:
        print("  %-16s n=%5d  中位 %.2f  为0的占比 %.0f%%" % (
            band, len(g), statistics.median(g),
            100 * sum(1 for s in g if s == 0) / len(g)))
