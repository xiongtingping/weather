"""H1 redone with the right field: is there a ceiling after dark?

The hard bound only ever cuts from below -- an observation says "it already
reached 33" and every bucket under 33 is zeroed.  Nothing cuts from above.
After sunset the day's high can no longer rise, so mass sitting above the
running high should collapse toward zero across the evening.  Whether it does
is the question; the previous attempt asked it with reported_high_bucket,
which is min(surviving) and degenerates to ordinal 0 whenever the exclusion
set is empty.
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

def holding(intervals, value):
    """The bucket whose interval contains this temperature."""
    for b, (lo, hi) in intervals.items():
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return b
    return None

by_hour, skipped = {}, {"no_run": 0, "post_close": 0, "no_tz": 0}
for r in conn.execute(
    "SELECT station_id,event_date,decision_at_utc,bucket_intervals_json,"
    "envelope_json FROM basket_probability_bridge_history"
    " WHERE state='COMMITTED' AND stage='P1' ORDER BY decision_at_utc"
):
    k = (str(r[0]), str(r[1]))
    if k not in truth or k not in tz:
        skipped["no_tz"] += 1
        continue
    try:
        close = local_close(k[1], tz[k])
    except Exception:
        skipped["no_tz"] += 1
        continue
    at = parse(str(r[2]))
    if (close - at).total_seconds() < 0:
        skipped["post_close"] += 1
        continue
    env = json.loads(r[4])
    lin = env.get("lineage") or {}
    run = lin.get("metar_trajectory_running_high_c")
    if run in (None, ""):
        skipped["no_run"] += 1
        continue
    intervals = json.loads(r[3])
    o = env["bucket_ordinals"]
    rb = holding(intervals, float(run))
    tb = truth[k]
    if rb is None or rb not in o or tb not in o:
        continue
    above = sum(v for b, v in env["probabilities"].items()
                if o.get(b, -1) > o[rb])
    hour = at.astimezone(close.tzinfo).hour
    by_hour.setdefault(hour, []).append((above, 1 if o[tb] > o[rb] else 0))

print("跳过: %s" % skipped)
print()
print("P1 分布押在'已测最高温所在桶之上'的质量，按当地小时")
print("  当地时  重算数   模型押头顶   当天真的又变热了   高估倍数")
for h in sorted(by_hour):
    g = by_hour[h]
    if len(g) < 40:
        continue
    c = statistics.mean(x[0] for x in g)
    a = sum(x[1] for x in g) / len(g)
    print("   %2d点  %6d      %5.1f%%        %5.1f%%        %s" % (
        h, len(g), 100 * c, 100 * a,
        "%.1f×" % (c / a) if a > 0 else "∞  (真值从未更高)"))

print()
for name, hrs in (("清晨 6-11点", range(6, 12)),
                  ("峰值窗 12-15点", range(12, 16)),
                  ("傍晚 16-18点", range(16, 19)),
                  ("入夜 19-21点", range(19, 22)),
                  ("深夜 22-23点", range(22, 24))):
    g = [x for h in hrs for x in by_hour.get(h, [])]
    if not g:
        continue
    c = statistics.mean(x[0] for x in g)
    a = sum(x[1] for x in g) / len(g)
    print("  %-16s n=%5d   模型押头顶 %5.1f%%   实际又变热 %5.1f%%   %s" % (
        name, len(g), 100 * c, 100 * a,
        "高估 %.1f×" % (c / a) if a > 0 else "实际为 0，模型仍押 %.0f%%" % (100 * c)))
