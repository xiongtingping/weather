"""Are flat-day centre misses a station's standing bias or flat-day specific? Sign consistency by day."""
import sqlite3, sys
from collections import defaultdict
sys.path.insert(0, ".")
from bayes_basket_target.center_width_store import _truth, serving_cells_by_day, _evidence_floor
from bayes_basket_target.center_width_calibration import sigma_floor_lift_v1
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
truth = _truth(conn)
floor = _evidence_floor(conn, stage="P0", caller_bound=None)
by_day = serving_cells_by_day(conn, truth, stage="P0", min_decision_at_utc=floor)
per = defaultdict(lambda: {"flat": defaultdict(list), "peaked": defaultdict(list)})
for d in by_day:
    for c in by_day[d]:
        p = c["probabilities"]; keys = c["ordered_keys"]; centers = c["key_centers_c"]
        actual = truth.get((c["station_id"], c["event_date"]))
        if actual is None: continue
        _l, sig, rung = sigma_floor_lift_v1(p, ordered_keys=keys, key_centers_c=centers, floor_rungs=0.0)
        if rung <= 0: continue
        total = sum(p.values()); mean_c = sum(centers[i] * p[k] for i, k in enumerate(keys)) / total
        e = (actual - mean_c) / rung
        bucket = "flat" if sig / rung >= 2.5 else ("peaked" if sig / rung < 2.0 else None)
        if bucket: per[c["station_id"]][bucket][d].append(e)
def summarise(days):
    if not days: return "--"
    means = [sum(v) / len(v) for v in days.values()]
    m = sum(means) / len(means); same = sum(1 for x in means if (x > 0) == (m > 0)) / len(means)
    return f"{m:+5.2f} over {len(means)} days, {same*100:3.0f}% same sign"
print(f"{'station':8} | {'flat days (sigma>=2.5): bias per day-mean':45} | {'peaked days (sigma<2.0)':45}")
ranked = sorted(per.items(), key=lambda kv: -sum(len(v) for v in kv[1]["flat"].values()))
for st, d in ranked[:14]:
    print(f"{st:8} | {summarise(d['flat']):45} | {summarise(d['peaked']):45}")
# fleet: correlation between a station's flat-day bias and its peaked-day bias
pairs = []
for st, d in per.items():
    if d["flat"] and d["peaked"]:
        f = [sum(v)/len(v) for v in d["flat"].values()]; q = [sum(v)/len(v) for v in d["peaked"].values()]
        pairs.append((sum(f)/len(f), sum(q)/len(q)))
if len(pairs) > 3:
    n = len(pairs); mx = sum(a for a, _ in pairs)/n; my = sum(b for _, b in pairs)/n
    cov = sum((a-mx)*(b-my) for a, b in pairs); vx = sum((a-mx)**2 for a, _ in pairs); vy = sum((b-my)**2 for _, b in pairs)
    print(f"\nstations with both: {n}; corr(flat-day bias, peaked-day bias) = {cov/((vx*vy)**0.5):+.2f}; agree in sign: {sum(1 for a,b in pairs if (a>0)==(b>0))}/{n}")
