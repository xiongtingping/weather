#!/usr/bin/env python3
"""Post hoc (after scan.py's readings): what drives the LogScore surface, and
how firm the smooth Brier optimum is.  Read-only; writes diagnose.json."""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scan", HERE / "scan.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

CATASTROPHE = 1e-6  # p(truth bucket) below this: the floor, not a forecast


def main() -> int:
    items = S.prepare(S.collect())
    pairs = [(0.07, 0.07), (0.12, 0.04), (0.14, 0.07), (0.24, 0.20), (0.0, 0.0)]
    out = {"station_days": len(items)}
    cat = {}
    for p in pairs:
        hits = [it for it in items if it["scores"][p][0] > -math.log(CATASTROPHE)]
        cat[f"{p[0]:.2f}/{p[1]:.2f}"] = {
            "count": len(hits),
            "station_days": sorted(f"{it['s']} {it['ev']}" for it in hits),
            "nll_share": round(sum(it["scores"][p][0] for it in hits) / sum(it["scores"][p][0] for it in items), 3),
        }
    out["catastrophes"] = cat
    # LogScore with the catastrophes of ANY arm set aside (same station-days for every pair)
    worst = {id(it) for it in items for p in S.GRID for q in S.GRID if it["scores"][(p, q)][0] > -math.log(CATASTROPHE)}
    bulk = [it for it in items if id(it) not in worst]
    out["bulk_station_days"] = len(bulk)
    out["bulk_best_by_logscore"] = S.best(bulk, 0)
    out["bulk_best_by_brier"] = S.best(bulk, 1)
    bb = S.best(items, 1)
    out["best_by_brier"] = bb
    out["brier_best_minus_served"] = S.paired_ci(items, (0.12, 0.04), bb, 1)
    out["brier_best_minus_symmetric"] = S.paired_ci(items, (0.07, 0.07), bb, 1)
    out["served_minus_symmetric_brier"] = S.paired_ci(items, (0.07, 0.07), (0.12, 0.04), 1)
    out["bulk_logscore"] = {f"{p[0]:.2f}/{p[1]:.2f}": round(S.mean_scores(bulk, p)[0], 5) for p in pairs + [out["bulk_best_by_logscore"]]}
    out["bulk_served_minus_symmetric_nll"] = S.paired_ci(bulk, (0.07, 0.07), (0.12, 0.04), 0)
    days = sorted({it["ev"] for it in items})
    half = days[len(days) // 2]
    first = [it for it in items if it["ev"] < half]
    second = [it for it in items if it["ev"] >= half]
    oos = {}
    for name, fit_on, test_on in (("fit_first_test_second", first, second), ("fit_second_test_first", second, first)):
        pick = S.best(fit_on, 1)
        oos[name] = {"picked_by_brier": pick,
                     "test_brier": {f"{p[0]:.2f}/{p[1]:.2f}": round(S.mean_scores(test_on, p)[1], 5)
                                    for p in (pick, (0.12, 0.04), (0.07, 0.07))}}
    out["brier_out_of_sample_halves"] = oos
    (HERE / "diagnose.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
