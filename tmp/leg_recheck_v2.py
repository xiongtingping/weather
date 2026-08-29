"""Why do the legs beside the centre hit 2% when the distribution says 16%?

The distribution puts the truth within one bucket of its own centre 57.6% of
the time, so a leg placed beside that centre should hit around 16%.  Measuring
2% therefore says the leg is not beside that centre -- the ladder is somewhere
else.  This lines the two centres up against each other and prices what the
gap costs, and it uses the ledger's own banding rather than restating it:
centre is exempt, one index either way on the posted window is the shoulder,
everything further shares the value clamp.

``leg_shares_json`` is this stage's own legs, not the chain's, so summing over
stage rows counts each purchase once.
"""
import json, sqlite3, statistics, sys
from collections import Counter, defaultdict
sys.path.insert(0, "tmp")
from stage_pick import local_close, parse

t = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                    uri=True, timeout=30)
t.execute("PRAGMA query_only=1")
a = sqlite3.connect("file:data/archive.db?mode=ro", uri=True, timeout=30)
truth_c = {(str(r[0]), str(r[1])): float(r[2]) for r in a.execute(
    "SELECT station_id,event_date,observed_max_c FROM"
    " reported_trajectory_canonical_station_day_bounds"
    " WHERE observed_max_c IS NOT NULL")}
settled = {(str(r[0]), str(r[1])): str(r[2]) for r in t.execute(
    "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
if not settled:
    raise SystemExit("账本仍在重建，稍后再跑")

# The model's own centre at each position's decision time.
model_centre, intervals_by = {}, {}
for r in t.execute(
    "SELECT station_id,event_date,decision_at_utc,bucket_intervals_json,"
    "envelope_json FROM basket_probability_bridge_history"
    " WHERE state='COMMITTED' ORDER BY decision_at_utc"
):
    k = (str(r[0]), str(r[1]))
    lin = json.loads(r[4]).get("lineage") or {}
    mc = lin.get("model_center_bucket")
    if mc:
        model_centre[(k[0], k[1], str(r[2]))] = str(mc)
        intervals_by[k] = r[3]

def nearest_centre(key, at):
    best = None
    for (st, day, stamp), mc in model_centre.items():
        if (st, day) != key:
            continue
        if stamp <= at and (best is None or stamp > best[0]):
            best = (stamp, mc)
    return best[1] if best else None

def holding(iv, v):
    for b, (lo, hi) in iv.items():
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return b

bands = defaultdict(lambda: {"n": 0, "shares": 0.0, "stake": 0.0,
                             "payout": 0.0, "hit": 0, "px": [], "claim": []})
offset = Counter()
for r in t.execute(
    "SELECT station_id,event_date,decision_at_utc,ladder_center_bucket,"
    "window_buckets_json,leg_shares_json,prices_json,probabilities_json"
    " FROM paper_trade_position WHERE leg_shares_json NOT IN ('','{}')"
):
    k = (str(r[0]), str(r[1]))
    if k not in settled or k not in intervals_by:
        continue
    window = [str(b) for b in json.loads(r[4] or "[]")]
    centre = str(r[3] or "")
    if centre not in window:
        continue
    mc = nearest_centre(k, str(r[2]))
    if mc and mc in window:
        offset[window.index(centre) - window.index(mc)] += 1
    elif mc:
        offset["中心不在窗内"] += 1
    legs = json.loads(r[5] or "{}")
    prices = json.loads(r[6] or "{}")
    probs = json.loads(r[7] or "{}")
    for bucket, sh in legs.items():
        if not sh or bucket not in window:
            continue
        d = abs(window.index(bucket) - window.index(centre))
        band = "中心(豁免)" if d == 0 else "肩(距1·λ0.40)" if d == 1 else "其余(距≥2·λ0.53)"
        c = bands[band]
        px = float(prices.get(bucket) or 0.0)
        c["n"] += 1; c["shares"] += float(sh)
        c["stake"] += px * float(sh)
        c["payout"] += float(sh) if bucket == settled[k] else 0.0
        c["hit"] += bucket == settled[k]
        c["px"].append(px)
        if bucket in probs:
            c["claim"].append(float(probs[bucket]))

print("腿级复测（%d 个已结算站日）\n" % len(settled))
print("  档                    腿数   投入u    回款u      净u    命中   声称p   成交价区间")
for band in ("中心(豁免)", "肩(距1·λ0.40)", "其余(距≥2·λ0.53)"):
    c = bands.get(band)
    if not c or not c["n"]:
        continue
    px = sorted(c["px"])
    print("  %-20s %4d %8.1f %8.1f %+8.1f  %4.0f%%  %5.1f%%   %.3f–%.3f (中位%.3f)"
          % (band, c["n"], c["stake"], c["payout"], c["payout"] - c["stake"],
             100 * c["hit"] / c["n"],
             100 * statistics.mean(c["claim"]) if c["claim"] else 0,
             px[0], px[-1], statistics.median(px)))
print("\n梯子中心相对分布中心的偏移（正=梯子偏高温侧）:")
tot = sum(v for v in offset.values())
for k in sorted(offset, key=lambda x: (isinstance(x, str), x)):
    print("   %-14s %4d  %5.1f%%" % (k, offset[k], 100 * offset[k] / tot))
