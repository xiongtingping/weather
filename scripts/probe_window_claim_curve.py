"""The claim->realised curve on the windows the gate actually enumerates.

Centre-containing contiguous windows of 3 and 4 buckets (charter 49/50), the
same shapes _support_windows offers, scored by claimed mass against whether
truth settled inside.  This is the population charter 81's factor multiplies.
"""
import sqlite3, sys
sys.path.insert(0, ".")
from bayes_basket_target.center_width_store import _truth, serving_cells_by_day, _evidence_floor
from bayes_basket_target.center_width_calibration import sigma_floor_lift_v1
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
truth = _truth(conn)
BANDS = ((0.0,0.35),(0.35,0.55),(0.55,0.75),(0.75,1.01))
SHIPPED = ((0.35,1.0),(0.55,1.21),(1.01,1.08))
def shipped(c):
    for b,f in SHIPPED:
        if c < b: return f
    return 1.0
for stage in ("P0","P0-M","P1"):
    floor = _evidence_floor(conn, stage=stage, caller_bound=None)
    by_day = serving_cells_by_day(conn, truth, stage=stage, min_decision_at_utc=floor)
    rows = {3: [], 4: []}
    for d in sorted(by_day):
        for c in by_day[d]:
            p, keys, centers = c["probabilities"], c["ordered_keys"], c["key_centers_c"]
            a = truth.get((c["station_id"], c["event_date"]))
            if a is None: continue
            _l, sig, rung = sigma_floor_lift_v1(p, ordered_keys=keys, key_centers_c=centers, floor_rungs=0.0)
            if rung <= 0: continue
            tot = sum(p.values()); mean_c = sum(centers[i]*p[k] for i,k in enumerate(keys))/tot
            mi = min(range(len(keys)), key=lambda i:(abs(centers[i]-mean_c), i))
            n = len(keys)
            for width in (3, 4):
                for start in range(max(0, mi-width+1), min(mi, n-width)+1):
                    w = keys[start:start+width]
                    if keys[mi] not in w: continue
                    claim = sum(p[k] for k in w)/tot
                    rows[width].append((claim, 1 if c["settled_bucket"] in w else 0))
    print(f"\n### {stage}")
    for width in (3, 4):
        print(f"  {width} 桶含中心窗，{len(rows[width])} 个：")
        for lo, hi in BANDS:
            sel = [r for r in rows[width] if lo <= r[0] < hi]
            if len(sel) < 30: continue
            cl = sum(r[0] for r in sel); hit = sum(r[1] for r in sel)
            ratio = hit/cl
            print(f"    声称 {lo:.2f}-{hi:.2f}: n={len(sel):6d} 平均声称 {cl/len(sel):.3f} 实际/声称 {ratio:.2f} | 收缩30% {1+(ratio-1)*0.7:.2f} | 现行 {shipped((lo+hi)/2):.2f}")
