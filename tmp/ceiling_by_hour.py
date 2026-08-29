"""Does the model's mass above the measured high decay as the sun goes down?

Averaged over the whole day that mass looks calibrated, which is what a
correct morning belief and an absurd midnight one produce when pooled.  The
day's high cannot rise after dark, so the honest curve is claimed-mass
falling toward zero across the evening.  Binned by local hour, either the
model tracks that or it does not.
"""
import json, sqlite3, statistics, sys
sys.path.insert(0, "tmp")
from stage_pick import DB, load_chains, local_close, parse

conn = sqlite3.connect(DB, uri=True, timeout=120.0)
truth = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
tz = {(str(r[0]), str(r[1])): str(r[2]) for r in conn.execute(
    "SELECT DISTINCT station_id,event_date,settlement_timezone"
    " FROM basket_official_ws_snapshot")}
chains = load_chains(conn, keys=set(truth))

buckets = {}
for k, tb in truth.items():
    if k not in tz:
        continue
    try:
        zone = local_close(k[1], tz[k]).tzinfo
    except Exception:
        continue
    for stage, at, blob in chains[k]:
        if stage != "P1":
            continue
        env = json.loads(blob)
        p, o = env["probabilities"], env["bucket_ordinals"]
        lin = env.get("lineage") or {}
        rh = lin.get("reported_high_bucket")
        if tb not in o or rh not in o:
            continue
        hour = parse(at).astimezone(zone).hour
        above = sum(v for b, v in p.items() if o.get(b, -1) > o[rh])
        buckets.setdefault(hour, []).append(
            (above, 1 if o[tb] > o[rh] else 0))

print("P1 分布里'高于已测最高温'的那部分质量，按当地小时")
print("（真值确实更高 = 当天后来真的又升温了，这是该质量应该兑现的比例）")
print("  当地时  重算数   模型押在头顶  真值确实更高   高估倍数")
for hour in sorted(buckets):
    g = buckets[hour]
    claim = statistics.mean(x[0] for x in g)
    real = sum(x[1] for x in g) / len(g)
    ratio = claim / real if real > 0 else float("inf")
    print("   %2d点  %6d      %5.1f%%       %5.1f%%      %s" % (
        hour, len(g), 100 * claim, 100 * real,
        "%.1f×" % ratio if real > 0 else "∞ (真值从未更高)"))

print()
print("汇总：")
for name, hrs in (("峰值前 (当地 6-13 点)", range(6, 14)),
                  ("峰值后 (当地 14-18 点)", range(14, 19)),
                  ("入夜后 (当地 19-23 点)", range(19, 24))):
    g = [x for h in hrs for x in buckets.get(h, [])]
    if g:
        claim = statistics.mean(x[0] for x in g)
        real = sum(x[1] for x in g) / len(g)
        print("  %-22s n=%5d  模型押 %5.1f%%  实际 %5.1f%%  %s" % (
            name, len(g), 100 * claim, 100 * real,
            "高估 %.1f×" % (claim / real) if real > 0 else "实际为 0"))
