#!/usr/bin/env python3
"""Read the charter 149 replay: does the own-lead key score better, and does
the centre/width layer need a new fit on the corrected output?

1. Retest.  On every update where the two arms differ, the served score of
   NEW (own lead) against OLD (packet lead), averaged per station-day-lead
   and bootstrapped by station-day.  Split by lead, by path, by region.
2. Centre/width.  The lane's own ``_search`` (``center_width_store``) on
   cells built from each arm's pre-band served vectors -- one per
   station-day-3h block, as ``serving_cells_by_day`` samples -- gives the
   residual (delta, lambda) the incumbent layer would still need.  Fitted on
   the earlier half of event days and scored on the later half, so a
   residual that only fits noise shows up as no gain out of sample.
"""
from __future__ import annotations

import gzip
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import center_width_store as cws  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402

UPDATES = ROOT / "tmp" / "member_own_lead_replay_20260918" / "updates"
METRICS = ("crps", "nll", "brier")
ASIA = {"RCSS", "RJTT", "RKPK", "RKSI", "RPLL", "WMKK", "WSSS", "ZBAA", "ZGGG",
        "ZGSZ", "ZHHH", "ZSPD", "ZSQD", "ZUCK", "ZUUU", "VILK", "OPKC"}


def _records():
    for path in sorted(UPDATES.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                yield json.loads(line)


def _boot(diffs, seed=7, n=2000):
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(n)
    )
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def retest(records) -> dict:
    cells = defaultdict(lambda: {"NEW": [], "OLD": [], "err_new": [], "err_old": [], "meta": None})
    for r in records:
        if not r["differs"]:
            continue
        key = (r["station"], r["event_date"], r["lead"])
        slot = cells[key]
        slot["NEW"].append(r["arms"]["NEW"]["served"][:3])
        slot["OLD"].append(r["arms"]["OLD"]["served"][:3])
        slot["err_new"].append(r["actual"] - r["arms"]["NEW"]["mean"])
        slot["err_old"].append(r["actual"] - r["arms"]["OLD"]["mean"])
        kinds = {
            "borrow" if p[1].startswith("borrow") else "certified_wrong_lead"
            for s, p in r["paths"].items() if p[0] != p[1]
        }
        slot["meta"] = {"lead": r["lead"], "asia": r["station"] in ASIA,
                        "kind": "+".join(sorted(kinds))}

    def summarise(keys):
        out = {"station_day_leads": len(keys), "station_days": len({k[:2] for k in keys})}
        for i, m in enumerate(METRICS):
            new = [statistics.fmean(v[i] for v in cells[k]["NEW"]) for k in keys]
            old = [statistics.fmean(v[i] for v in cells[k]["OLD"]) for k in keys]
            diffs = [a - b for a, b in zip(new, old)]
            lo, hi = _boot(diffs) if len(diffs) > 5 else (None, None)
            base = statistics.fmean(old)
            out[m] = {"old": round(base, 4), "new": round(statistics.fmean(new), 4),
                      "rel": round((statistics.fmean(new) - base) / base, 4),
                      "ci": [None if lo is None else round(lo, 4), None if hi is None else round(hi, 4)],
                      "better": sum(1 for d in diffs if d < -1e-9), "worse": sum(1 for d in diffs if d > 1e-9)}
        out["mean_err_c"] = {
            "old": round(statistics.fmean(statistics.fmean(cells[k]["err_old"]) for k in keys), 3),
            "new": round(statistics.fmean(statistics.fmean(cells[k]["err_new"]) for k in keys), 3),
        }
        out["mae_c"] = {
            "old": round(statistics.fmean(statistics.fmean(abs(e) for e in cells[k]["err_old"]) for k in keys), 3),
            "new": round(statistics.fmean(statistics.fmean(abs(e) for e in cells[k]["err_new"]) for k in keys), 3),
        }
        return out

    keys = list(cells)
    report = {"all": summarise(keys)}
    for lead in sorted({cells[k]["meta"]["lead"] for k in keys}):
        report[f"lead={lead}"] = summarise([k for k in keys if cells[k]["meta"]["lead"] == lead])
    for kind in sorted({cells[k]["meta"]["kind"] for k in keys}):
        report[f"kind={kind}"] = summarise([k for k in keys if cells[k]["meta"]["kind"] == kind])
    report["asia"] = summarise([k for k in keys if cells[k]["meta"]["asia"]])
    report["not_asia"] = summarise([k for k in keys if not cells[k]["meta"]["asia"]])
    return report


def _cell(vector: dict, actual: float):
    settled = oos._temperature_bucket(actual)
    ints = [int(k) for k in vector] + [int(settled)]
    keys = [str(k) for k in range(min(ints) - 3, max(ints) + 4)]
    probabilities = {k: float(vector.get(k, 0.0)) for k in keys}
    total = sum(probabilities.values())
    probabilities = {k: v / total for k, v in probabilities.items()}
    return {
        "probabilities": probabilities, "ordered_keys": keys,
        "key_centers_c": [float(int(k)) for k in keys],
        "settled_bucket": settled, "excluded_buckets": (),
    }


GRID = [(d, l) for d in cws.SHIFT_GRID_C if abs(d) <= cws.MAX_ABS_SHIFT_C
        for l in cws.LAMBDA_GRID if cws.MIN_WIDTH_LAMBDA <= l <= cws.MAX_WIDTH_LAMBDA]


def _nll_row(cell) -> list[float]:
    """The lane's own per-cell NLL at every grid pair, in ``_search`` order."""

    return [cws._cell_nll(cell, delta_c=d, width_lambda=l) for d, l in GRID]


def _best(matrix, rows):
    """``_search`` over a cached matrix: first minimum in grid order."""

    import numpy as np  # noqa: PLC0415

    if not rows:
        return None
    means = matrix[rows].mean(axis=0)
    index = int(np.argmin(means))
    return GRID[index][0], GRID[index][1], float(means[index])


def width_lane(records, workers: int = 3) -> dict:
    import numpy as np  # noqa: PLC0415
    from multiprocessing import get_context  # noqa: PLC0415

    blocks = defaultdict(list)
    for r in records:
        blocks[(r["station"], r["event_date"], r["at"][:10], int(r["at"][11:13]) // 3)].append(r)
    meta, new_cells, old_cells, old_index = [], [], [], {}
    for key in sorted(blocks):
        rows = sorted(blocks[key], key=lambda r: r["at"])
        r = rows[len(rows) // 2]
        meta.append({"day": r["event_date"], "differs": r["differs"], "asia": r["station"] in ASIA})
        new_cells.append(_cell(r["arms"]["NEW"]["pre_band"], r["actual"]))
        if r["differs"]:
            old_index[len(meta) - 1] = len(old_cells)
            old_cells.append(_cell(r["arms"]["OLD"]["pre_band"], r["actual"]))
    identity = GRID.index((0.0, 1.0))
    with get_context("fork").Pool(workers) as pool:
        new_m = np.array(pool.map(_nll_row, new_cells, chunksize=64))
        old_part = np.array(pool.map(_nll_row, old_cells, chunksize=64))
    old_m = new_m.copy()
    for i, j in old_index.items():
        old_m[i] = old_part[j]
    np.save(UPDATES.parent / "nll_new.npy", new_m)
    np.save(UPDATES.parent / "nll_old.npy", old_m)
    days = sorted({m["day"] for m in meta})
    cut = days[len(days) // 2]
    out = {"cells": len(meta), "differing_cells": len(old_cells), "days": len(days), "split_day": cut,
           "grid_pairs": len(GRID)}
    scopes = {
        "all": lambda m: True,
        "differs": lambda m: m["differs"],
        "asia": lambda m: m["asia"],
    }
    for arm, matrix in (("NEW", new_m), ("OLD", old_m)):
        for name, keep in scopes.items():
            rows = [i for i, m in enumerate(meta) if keep(m)]
            early = [i for i in rows if meta[i]["day"] < cut]
            late = [i for i in rows if meta[i]["day"] >= cut]
            delta, lam, fitted = _best(matrix, rows)
            d_e, l_e, _ = _best(matrix, early)
            k_e = GRID.index((d_e, l_e))
            out[f"{arm}|{name}"] = {
                "n": len(rows),
                "in_sample": {"delta_c": delta, "lambda": lam,
                              "nll_identity": round(float(matrix[rows, identity].mean()), 4),
                              "nll_fitted": round(fitted, 4),
                              "gain": round(float(matrix[rows, identity].mean()) - fitted, 4)},
                "early_fit": {"delta_c": d_e, "lambda": l_e, "n": len(early)},
                "late_oos": {"n": len(late),
                             "nll_identity": round(float(matrix[late, identity].mean()), 4),
                             "nll_early_fit": round(float(matrix[late, k_e].mean()), 4),
                             "gain": round(float(matrix[late, identity].mean() - matrix[late, k_e].mean()), 4)},
            }
    return out


def main() -> int:
    records = list(_records())
    report = {"updates": len(records), "differs": sum(r["differs"] for r in records),
              "retest": retest(records), "centre_width": width_lane(records)}
    target = UPDATES.parent / "summary.json"
    target.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
