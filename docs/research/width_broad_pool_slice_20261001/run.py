#!/usr/bin/env python3
"""Charter 239's lane, re-asked for the broad pool (seated >= 4).  Production
functions only; read-only; see PROTOCOL.md."""
from __future__ import annotations
import json, sqlite3, sys, time, resource
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import center_width_store as store
from bayes_basket_target.center_width_calibration import thin_far_slice_applies

OUT = Path(__file__).resolve().parent
t0 = time.monotonic()
def rss_mib(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20

conn = sqlite3.connect(f"file:{ROOT/'data/bayes_basket_target_ssot.db'}?mode=ro", uri=True, timeout=30)
conn.execute("PRAGMA busy_timeout=30000")
floor = store._evidence_floor(conn, stage="P0", caller_bound="2026-08-27T09:20:00Z")
truth = store._truth(conn)
by_day = store.serving_cells_by_day(conn, truth, stage="P0", min_decision_at_utc=floor)
conn.close()  # release the reader before the long arithmetic (WAL pinning)
days = sorted(by_day)
cells = [c for d in days for c in by_day[d]]
print(f"floor={floor} days={len(days)} cells={len(cells)} load={time.monotonic()-t0:.0f}s rss={rss_mib():.0f}MiB", flush=True)
cut = max(1, int(len(days) * (1.0 - store.FORWARD_WINDOW_FRACTION)))

def seated(c):
    v = c.get("seated_system_count"); return None if v is None else int(v)
def hours(c):
    v = c.get("hours_to_settlement"); return None if v is None else float(v)
ARMS = {
    "thin_far_control": lambda c: thin_far_slice_applies(seated_system_count=seated(c), hours_to_settlement=hours(c)),
    "broad4": lambda c: seated(c) is not None and seated(c) >= 4,
    "broad5": lambda c: seated(c) is not None and seated(c) >= 5,
    "broad6": lambda c: seated(c) is not None and seated(c) >= 6,
    "broad4_near": lambda c: seated(c) is not None and seated(c) >= 4 and hours(c) is not None and hours(c) <= 36.0,
    "broad4_far": lambda c: seated(c) is not None and seated(c) >= 4 and hours(c) is not None and hours(c) > 36.0,
}
from collections import Counter
census = Counter((seated(c), ">36h" if (hours(c) or 0) > 36 else "<=36h") for c in cells)
results = {"floor": floor, "days": days, "cut": cut, "cell_count": len(cells),
           "census_seated_x_horizon": {f"{k[0]}|{k[1]}": v for k, v in sorted(census.items(), key=str)}, "arms": {}}
for name, pred in ARMS.items():
    pooled, sliced = {}, {}
    for d in days:
        pooled[d] = []
        for c in by_day[d]:
            (sliced.setdefault(d, []) if pred(c) else pooled[d]).append(c)
    lane = store._thin_far_slice_lane(
        pooled_by_day=pooled, slice_by_day=sliced, days=days, cut=cut,
        pooled_admitted=False, served_residual=store.RESIDUAL_IDENTITY,
        sigma_floor_rungs=0.0, served_slice_lambda=0.75, incumbent_slices=None,
    )
    lane["arm"] = name
    results["arms"][name] = lane
    g = lane.get("gate") or {}
    print(f"{name}: state={lane['state']} days={lane['slice_day_count']} cells={lane['slice_cell_count']} "
          f"cand_residual={lane.get('candidate_residual_width_lambda')} cand_abs={lane.get('candidate_width_lambda')} "
          f"folds+={lane.get('folds_positive')}/{lane.get('fold_count')} dLog={g.get('d_logscore')} ci={g.get('ci_logscore')} "
          f"dBrier={g.get('d_brier')} dCRPS={g.get('d_crps')} lower={g.get('lower')} t={time.monotonic()-t0:.0f}s rss={rss_mib():.0f}MiB", flush=True)
    if rss_mib() > 1500: raise SystemExit("RSS over 1500MiB; stop")
(OUT / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str))
print("DONE", time.monotonic() - t0)
