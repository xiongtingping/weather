"""H1, final form: collapse each prefix burst before binning.

The chain writes one row per prefix step and each step carries its own
decision_at_utc, milliseconds apart -- so deduping on that timestamp collapses
nothing.  A burst is the group of rows sharing a wall-clock second; the row
holding the largest running high in it is the belief, the rest are the chain
walking up to it.  KLGA 2026-08-21 at local 23:20 writes seven rows carrying
running highs from 20.6 to 25.6; only 25.6 was ever believed.
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
    key = (k[0], k[1], str(r[2])[:19])           # the wall-clock second
    prev = burst.get(key)
    if prev is None or float(run) >= prev[0]:
        burst[key] = (float(run), r[3], env, str(r[2]))

print("塌缩前缀突发后剩 %d 条信念（原始 P1 行 %d）\n" % (
    len(burst),
    conn.execute("SELECT COUNT(*) FROM basket_probability_bridge_history"
                 " WHERE stage='P1' AND state='COMMITTED'").fetchone()[0]))

by_hour = {}
for (st, day, _), (run, iv_s, env, at_s) in burst.items():
    k = (st, day)
    close = local_close(day, tz[k])
    at = parse(at_s)
    if (close - at).total_seconds() < 0:
        continue
    o = env["bucket_ordinals"]
    rb = holding(json.loads(iv_s), run)
    tb = truth[k]
    if rb is None or rb not in o or tb not in o:
        continue
    above = sum(v for b, v in env["probabilities"].items()
                if o.get(b, -1) > o[rb])
    by_hour.setdefault(at.astimezone(close.tzinfo).hour, []).append(
        (above, 1 if o[tb] > o[rb] else 0))

print("模型押在'已测最高温所在桶之上'的质量 vs 当天真的又变热的比例")
print("  当地时  信念数   模型押头顶   真的又变热    差额")
for h in sorted(by_hour):
    g = by_hour[h]
    if len(g) < 25:
        continue
    c = statistics.mean(x[0] for x in g)
    a = sum(x[1] for x in g) / len(g)
    print("   %2d点  %6d      %5.1f%%       %5.1f%%     %+5.1f 点" % (
        h, len(g), 100 * c, 100 * a, 100 * (c - a)))
print()
for name, hrs in (("清晨 6-11点", range(6, 12)), ("峰值窗 12-15点", range(12, 16)),
                  ("傍晚 16-18点", range(16, 19)), ("入夜 19-21点", range(19, 22)),
                  ("深夜 22-23点", range(22, 24))):
    g = [x for h in hrs for x in by_hour.get(h, [])]
    if not g:
        continue
    c = statistics.mean(x[0] for x in g)
    a = sum(x[1] for x in g) / len(g)
    print("  %-16s n=%5d  押头顶 %5.1f%%  真变热 %5.1f%%  差 %+5.1f 点  %s" % (
        name, len(g), 100 * c, 100 * a, 100 * (c - a),
        "高估 %.1f×" % (c / a) if a > 0.005 else "实际≈0，模型仍押 %.0f%%" % (100 * c)))
