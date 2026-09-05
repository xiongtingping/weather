"""Is the centre window's under-claim a function of the claim, of the served sigma, or both?

Read-only.  Population: the centre/width layer's serving cells; window: the
mean bucket +-1 (the seat window, charter 49/79); outcome: whether truth
settled inside it.  A calibration curve is realised/claimed per cell.
"""
import sqlite3, sys
from collections import defaultdict
sys.path.insert(0, ".")
from bayes_basket_target.center_width_store import _truth, serving_cells_by_day, _evidence_floor
from bayes_basket_target.center_width_calibration import sigma_floor_lift_v1

conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
truth = _truth(conn)
SIGMA = ((0.0,1.44,"σ<1.44"),(1.44,2.0,"σ1.44-2"),(2.0,2.5,"σ2-2.5"),(2.5,99.,"σ≥2.5"))
CLAIM = ((0.0,0.35,"c<0.35"),(0.35,0.55,"c0.35-0.55"),(0.55,0.75,"c0.55-0.75"),(0.75,1.01,"c≥0.75"))

def cells_for(stage):
    floor = _evidence_floor(conn, stage=stage, caller_bound=None)
    by_day = serving_cells_by_day(conn, truth, stage=stage, min_decision_at_utc=floor)
    out = []
    for d in sorted(by_day):
        for c in by_day[d]:
            p, keys, centers = c["probabilities"], c["ordered_keys"], c["key_centers_c"]
            a = truth.get((c["station_id"], c["event_date"]))
            if a is None: continue
            _l, sig, rung = sigma_floor_lift_v1(p, ordered_keys=keys, key_centers_c=centers, floor_rungs=0.0)
            if rung <= 0: continue
            tot = sum(p.values()); mean_c = sum(centers[i]*p[k] for i,k in enumerate(keys))/tot
            mi = min(range(len(keys)), key=lambda i:(abs(centers[i]-mean_c), i))
            w = keys[max(0,mi-1):mi+2]
            claim = sum(p[k] for k in w)/tot
            out.append((sig/rung, claim, 1 if c["settled_bucket"] in w else 0, d))
    return out

def ratio(rows):
    c = sum(r[1] for r in rows); h = sum(r[2] for r in rows)
    return (h/c if c else float("nan")), len(rows), (c/len(rows) if rows else 0)

for stage in ("P0", "P0-M"):
    rows = cells_for(stage)
    print(f"\n########## {stage}: {len(rows)} 格")
    print("--- 边际：按声称档 ---")
    for lo, hi, name in CLAIM:
        sel = [r for r in rows if lo <= r[1] < hi]
        if sel:
            r, n, mc = ratio(sel); print(f"  {name:12} n={n:5d} 平均声称 {mc:.3f} 实际/声称 {r:.2f}  平均σ {sum(x[0] for x in sel)/n:.2f}")
    print("--- 边际：按 σ 档 ---")
    for lo, hi, name in SIGMA:
        sel = [r for r in rows if lo <= r[0] < hi]
        if sel:
            r, n, mc = ratio(sel); print(f"  {name:12} n={n:5d} 平均声称 {mc:.3f} 实际/声称 {r:.2f}")
    print("--- 二维：行=σ 档，列=声称档（实际/声称，括号 n） ---")
    header = " " * 12 + "".join(f"{c[2]:>16}" for c in CLAIM)
    print(header)
    for slo, shi, sname in SIGMA:
        line = f"  {sname:10}"
        for clo, chi, cname in CLAIM:
            sel = [r for r in rows if slo <= r[0] < shi and clo <= r[1] < chi]
            if len(sel) < 25: line += f"{'--':>16}"
            else:
                r, n, _ = ratio(sel); line += f"{f'{r:.2f} (n{n})':>16}"
        print(line)
