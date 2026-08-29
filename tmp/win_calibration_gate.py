"""Would a win-level calibration gate have caught P0 and passed P0-M?

The layer is admitted on forward NLL, which scores the probability landed on
the single settled bucket.  The decision that opens a position does not use
that number: it compares a WINDOW's summed claim against the window's summed
price.  Sharpening can raise the summed claim while improving the single-bucket
score, because it correlates the errors the sum runs over -- so a fit can pass
the NLL gate and still make the quantity the opening threshold reads dishonest.

This scores that quantity directly: the mass of the model's own best window
against how often the settled bucket actually landed in it, on forward days
only, identity against fitted.
"""
import json, sqlite3, statistics, sys
sys.path.insert(0, ".")
from bayes_basket_target.center_width_store import _truth, serving_cells_by_day
from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
)

BASKET = 4
conn = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",
                       uri=True, timeout=120.0)
truth = _truth(conn)
table = json.load(open(
    "data/runtime/bayes_basket_target_nwp/center_width_replay_table.json"))

def best_window(probs, keys):
    best, mass = None, -1.0
    for i in range(max(len(keys) - BASKET + 1, 1)):
        w = keys[i:i + BASKET]
        m = sum(float(probs.get(b, 0.0)) for b in w)
        if m > mass:
            best, mass = w, m
    return best, mass

print("窗口声称 win vs 实际命中（仅前向日，identity 对 fitted）\n")
print("  stage  日期数  ─ identity ─────────────  ─ fitted ───────────────")
print("                声称win  实际  比值      声称win  实际  比值")
for stage in ("P0", "P0-M", "P1"):
    by_day = serving_cells_by_day(conn, truth, stage=stage)
    per_date = (table["stages"] or {}).get(stage) or {}
    rows_i, rows_f, days = [], [], 0
    for day, cells in by_day.items():
        entry = per_date.get(day)
        if not entry or not cells:
            continue
        days += 1
        for cell in cells:
            keys = list(cell["ordered_keys"])
            w0, m0 = best_window(cell["probabilities"], keys)
            rows_i.append((m0, 1 if cell["settled_bucket"] in w0 else 0))
            adj, _ = apply_center_width_calibration(
                cell["probabilities"],
                delta_c=float(entry["delta_c"]),
                width_lambda=float(entry["width_lambda"]),
                ordered_keys=keys,
                key_centers_c=cell["key_centers_c"],
                excluded_buckets=cell["excluded_buckets"])
            w1, m1 = best_window(adj, keys)
            rows_f.append((m1, 1 if cell["settled_bucket"] in w1 else 0))
    if not rows_i:
        print("  %-6s (无前向日)" % stage); continue
    def stat(rows):
        c = statistics.mean(r[0] for r in rows)
        a = sum(r[1] for r in rows) / len(rows)
        return c, a, (a / c if c > 0 else 0.0)
    ci, ai, ri = stat(rows_i)
    cf, af, rf = stat(rows_f)
    verdict = "★ 会被拒" if rf < ri - 0.005 else ("✓ 会放行" if rf >= ri else "≈ 持平")
    print("  %-6s %4d    %5.1f%%  %5.1f%%  %.3f     %5.1f%%  %5.1f%%  %.3f   %s"
          % (stage, days, 100 * ci, 100 * ai, ri, 100 * cf, 100 * af, rf, verdict))
