"""Does the calibration layer's objective agree with what the basket needs?

The layer grid-searches a centre shift and a width scale to minimise mean NLL,
which is -log of the probability landed on the ONE settled bucket.  The basket
does not buy one bucket: it buys a contiguous window and is paid if the truth
lands anywhere inside it.  Those two are optimised by different parameters
whenever the error distribution is skewed -- a mean-like criterion pulls the
centre toward the tail, a coverage criterion holds it on the mode.

Both objectives are searched over the layer's own grid, on the layer's own
cells, through the layer's own apply function, so the only thing that differs
between the two answers is the objective itself.
"""
import sqlite3, sys, math, statistics
sys.path.insert(0, ".")
from bayes_basket_target.center_width_store import (
    serving_cells_by_day, _mean_nll, SHIFT_GRID_C, LAMBDA_GRID,
    MAX_ABS_SHIFT_C, MIN_WIDTH_LAMBDA, MAX_WIDTH_LAMBDA, _truth,
)
from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
)

conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                       uri=True, timeout=60)
conn.execute("PRAGMA query_only=1")
truth = _truth(conn)
BASKET = 4

def coverage(cells, *, delta_c, width_lambda):
    """Share of cells whose settled bucket lands in the richest 4-bucket window."""
    hit = 0
    for cell in cells:
        probs = cell["probabilities"]
        if delta_c != 0.0 or width_lambda != 1.0:
            probs, _ = apply_center_width_calibration(
                probs, delta_c=delta_c, width_lambda=width_lambda,
                ordered_keys=cell["ordered_keys"],
                key_centers_c=cell["key_centers_c"],
                excluded_buckets=cell["excluded_buckets"])
        order = list(cell["ordered_keys"])
        best, mass = None, -1.0
        for i in range(max(len(order) - BASKET + 1, 1)):
            w = order[i:i + BASKET]
            m = sum(float(probs.get(b, 0.0)) for b in w)
            if m > mass:
                best, mass = w, m
        if best and cell["settled_bucket"] in best:
            hit += 1
    return hit / len(cells) if cells else 0.0

def search(cells, score, better):
    best = None
    for d in SHIFT_GRID_C:
        if abs(d) > MAX_ABS_SHIFT_C:
            continue
        for lam in LAMBDA_GRID:
            if not MIN_WIDTH_LAMBDA <= lam <= MAX_WIDTH_LAMBDA:
                continue
            v = score(cells, delta_c=d, width_lambda=lam)
            if best is None or better(v, best[2]):
                best = (d, lam, v)
    return best

for stage in ("P0", "P0-M", "P1"):
    by_day = serving_cells_by_day(conn, truth, stage=stage)
    cells = [c for day in by_day.values() for c in day]
    if len(cells) < 40:
        print("%-5s cell 太少 (%d)，跳过\n" % (stage, len(cells)))
        continue
    base_nll = _mean_nll(cells, delta_c=0.0, width_lambda=1.0)
    base_cov = coverage(cells, delta_c=0.0, width_lambda=1.0)
    n_best = search(cells, _mean_nll, lambda a, b: a < b)
    c_best = search(cells, coverage, lambda a, b: a > b)
    print("%s  (%d 个 cell)" % (stage, len(cells)))
    print("  不校正             Δ=+0.00 λ=1.000   NLL %.4f   4桶覆盖 %.1f%%"
          % (base_nll, 100 * base_cov))
    print("  NLL 最优           Δ=%+.2f λ=%.3f   NLL %.4f   4桶覆盖 %.1f%%"
          % (n_best[0], n_best[1], n_best[2],
             100 * coverage(cells, delta_c=n_best[0], width_lambda=n_best[1])))
    print("  覆盖率最优         Δ=%+.2f λ=%.3f   NLL %.4f   4桶覆盖 %.1f%%"
          % (c_best[0], c_best[1],
             _mean_nll(cells, delta_c=c_best[0], width_lambda=c_best[1]),
             100 * c_best[2]))
    print("  → NLL 最优相对不校正的覆盖率变化: %+.1f 个百分点\n"
          % (100 * (coverage(cells, delta_c=n_best[0], width_lambda=n_best[1]) - base_cov)))
