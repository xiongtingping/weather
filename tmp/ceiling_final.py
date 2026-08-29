"""H1, third attempt: dedupe to the complete prefix before asking anything.

P1 writes the prefix chain step by step within one decision instant -- each row
swallows one more observation, so running_high climbs across them.  Only the
last row at that instant is the belief; averaging the partial steps in makes
the chain look permanently behind the weather.  KATL 2026-08-21 at local 23:00
writes running highs of 30.6, 32.2, 33.3 and 34.4 -- only 34.4 is what the
chain actually knew.
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

# Last row wins per (station, day, decision instant): the complete prefix.
final = {}
for r in conn.execute(
    "SELECT station_id,event_date,decision_at_utc,bucket_intervals_json,"
    "envelope_json FROM basket_probability_bridge_history"
    " WHERE state='COMMITTED' AND stage='P1' ORDER BY decision_at_utc, rowid"
):
    final[(str(r[0]), str(r[1]), str(r[2]))] = (r[3], r[4])

by_hour = {}
for (st, day, at_s), (iv_s, env_s) in final.items():
    k = (st, day)
    if k not in truth or k not in tz:
        continue
    try:
        close = local_close(day, tz[k])
    except Exception:
        continue
    at = parse(at_s)
    if (close - at).total_seconds() < 0:
        continue
    env = json.loads(env_s)
    run = (env.get("lineage") or {}).get("metar_trajectory_running_high_c")
    if run in (None, ""):
        continue
    o = env["bucket_ordinals"]
    rb = holding(json.loads(iv_s), float(run))
    tb = truth[k]
    if rb is None or rb not in o or tb not in o:
        continue
    above = sum(v for b, v in env["probabilities"].items()
                if o.get(b, -1) > o[rb])
    by_hour.setdefault(at.astimezone(close.tzinfo).hour, []).append(
        (above, 1 if o[tb] > o[rb] else 0))

print("完整前缀行 %d 条（去掉链条中间步后）\n" % sum(len(v) for v in by_hour.values()))
print("模型押在'已测最高温所在桶之上'的质量 vs 当天真的又变热的比例")
print("  当地时  重算数   模型押头顶   真的又变热   差额")
for h in sorted(by_hour):
    g = by_hour[h]
    if len(g) < 25:
        continue
    c = statistics.mean(x[0] for x in g)
    a = sum(x[1] for x in g) / len(g)
    print("   %2d点  %6d      %5.1f%%       %5.1f%%     %+5.1f 个百分点" % (
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
    print("  %-16s n=%5d  模型押头顶 %5.1f%%  真的又变热 %5.1f%%  差 %+5.1f 点  %s" % (
        name, len(g), 100 * c, 100 * a, 100 * (c - a),
        "高估 %.1f×" % (c / a) if a > 0.005 else "≈实际为0"))
