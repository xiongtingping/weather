"""Which serving-time variable conditions the centre-window under-claim: sigma (rungs) or core mass?"""
import math, sqlite3, sys
from collections import defaultdict
sys.path.insert(0, ".")
from bayes_basket_target.center_width_store import _truth, serving_cells_by_day, _evidence_floor, load_center_width, load_center_width_floor
from bayes_basket_target.center_width_calibration import sigma_floor_lift_v1, apply_center_width_calibration
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA busy_timeout=60000")
truth = _truth(conn)
for stage in ("P0", "P1"):
    floor = _evidence_floor(conn, stage=stage, caller_bound=None)
    by_day = serving_cells_by_day(conn, truth, stage=stage, min_decision_at_utc=floor)
    cells = [c for d in sorted(by_day) for c in by_day[d]]
    active = load_center_width(conn, stage=stage); fl = load_center_width_floor(conn, stage=stage)
    print(f"\n##### {stage}: {len(cells)} cells from {min(by_day)}..{max(by_day)} (evidence floor {floor}); served (delta,lambda)={active[:2] if active else None} floor={fl}")
    rows = []
    for c in cells:
        p = c["probabilities"]; keys = c["ordered_keys"]; centers = c["key_centers_c"]
        lift, sigma_c, rung_c = sigma_floor_lift_v1(p, ordered_keys=keys, key_centers_c=centers, floor_rungs=0.0)
        sig = sigma_c / rung_c if rung_c > 0 else float("nan")
        total = sum(p.values()); mean_c = sum(centers[i] * p[k] for i, k in enumerate(keys)) / total
        mi = min(range(len(keys)), key=lambda i: (abs(centers[i] - mean_c), i))
        w = [keys[i] for i in range(max(0, mi - 1), min(len(keys), mi + 2))]
        core = sum(p[k] for k in w) / total; hit = 1 if c["settled_bucket"] in w else 0
        rows.append((sig, core, hit, c))
    sig_sorted = sorted(r[0] for r in rows); n = len(rows)
    q = lambda arr, f: arr[int(f * (len(arr) - 1))]
    print(" sigma_rungs quantiles 10/25/50/75/90:", [round(q(sig_sorted, f), 2) for f in (0.1, 0.25, 0.5, 0.75, 0.9)])
    core_sorted = sorted(r[1] for r in rows)
    print(" core(mean±1) mass quantiles 10/25/50/75/90:", [round(q(core_sorted, f), 3) for f in (0.1, 0.25, 0.5, 0.75, 0.9)])
    for name, key_fn, edges in (("sigma_rungs", lambda r: r[0], (1.0, 1.44, 2.0, 2.5, 3.0, 4.0)), ("core_mass", lambda r: r[1], (0.25, 0.35, 0.45, 0.55, 0.7))):
        print(f" containment(mean±1 window)/claim by {name}:")
        bins = defaultdict(list)
        for r in rows:
            v = key_fn(r); b = next((e for e in edges if v < e), "top")
            bins[b].append(r)
        for b in list(edges) + ["top"]:
            rs = bins.get(b, [])
            if not rs: continue
            claim = sum(r[1] for r in rs); hits = sum(r[2] for r in rs)
            print(f"   <{b!s:5}: n={len(rs):5d}  mean core claim {claim/len(rs):.3f}  realised/claimed {hits/claim if claim else float('nan'):.2f}  mean sigma {sum(r[0] for r in rs)/len(rs):.2f}")
