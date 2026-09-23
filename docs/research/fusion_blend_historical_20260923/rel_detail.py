#!/usr/bin/env python3
"""REL / RES of the two arms: the reliability diagram, and an interval.

Re-runs pass 2's blend arm (same functions, same training table) and reads
both arms through the board's ``_core_murphy``.  The bootstrap sums the same
per-bin statistics ``_core_murphy`` sums, resampled by 3-day circular blocks;
its full-sample point equals ``_core_murphy``'s, asserted before any interval
is drawn.  Writes rel_detail.json.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
from bisect import bisect_right
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import score as S  # noqa: E402
from calibration_dashboard import CORE_RELIABILITY_EDGES, _core_murphy  # noqa: E402

BINS = len(CORE_RELIABILITY_EDGES) - 1


def cells_of(records, arm):
    copies = Counter((r["station"], r["event_date"]) for r in records)
    out = []
    for r in records:
        vec = r[arm]["vec"]
        hit = S.oos._temperature_bucket(r["actual"])
        keys = sorted(set(vec) | {hit}, key=int)
        out.append((1.0 / copies[(r["station"], r["event_date"])],
                    {"probabilities": [float(vec.get(k, 0.0)) for k in keys], "hit": keys.index(hit)}))
    return out


def bin_stats(cells):
    """Per cell, the (W, P, Y) sums per bin that ``_core_murphy`` accumulates."""

    stats = np.zeros((len(cells), BINS, 3))
    for i, (weight, core) in enumerate(cells):
        share = weight / len(core["probabilities"])
        for index, value in enumerate(core["probabilities"]):
            slot = min(BINS - 1, max(0, bisect_right(CORE_RELIABILITY_EDGES, value) - 1))
            stats[i, slot, 0] += share
            stats[i, slot, 1] += share * value
            stats[i, slot, 2] += share * (1.0 if index == core["hit"] else 0.0)
    return stats


def rel_res(summed):
    w, p, y = summed[:, 0], summed[:, 1], summed[:, 2]
    total = w.sum()
    base = y.sum() / total
    live = w > 0
    rel = float(((p[live] - y[live]) ** 2 / w[live]).sum() / total)
    res = float((w[live] * (y[live] / w[live] - base) ** 2).sum() / total)
    return rel, res


def main() -> int:
    records = S.load_pass1()
    records.sort(key=lambda r: (r["station"], r["at"]))
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "training.db"
        S.training_db(records, db)
        S.blend_arm(records, db)
    out = {}
    for label, subset in (
        ("all_updates", records),
        ("stable_pool_era", [r for r in records if r["event_date"] >= S.STABLE_POOL_FROM]),
    ):
        seat_cells, blend_cells = cells_of(subset, "seat"), cells_of(subset, "blend")
        seat_m, blend_m = _core_murphy(seat_cells), _core_murphy(blend_cells)
        seat_s, blend_s = bin_stats(seat_cells), bin_stats(blend_cells)
        for murphy, stats in ((seat_m, seat_s), (blend_m, blend_s)):
            rel, res = rel_res(stats.sum(axis=0))
            assert math.isclose(rel, murphy["reliability"], rel_tol=1e-9, abs_tol=1e-15), (rel, murphy["reliability"])
            assert math.isclose(res, murphy["resolution"], rel_tol=1e-9, abs_tol=1e-15), (res, murphy["resolution"])
        days = sorted({r["event_date"] for r in subset})
        day_idx = defaultdict(list)
        for i, r in enumerate(subset):
            day_idx[r["event_date"]].append(i)
        rng = np.random.default_rng(20260924)
        n_blocks = math.ceil(len(days) / S.BLOCK)
        d_rel, d_res = [], []
        for _ in range(S.BOOT):
            idx = np.concatenate([
                day_idx[days[(s + j) % len(days)]]
                for s in rng.integers(0, len(days), n_blocks) for j in range(S.BLOCK)
            ])
            sr, ss = rel_res(seat_s[idx].sum(axis=0))
            br, bs = rel_res(blend_s[idx].sum(axis=0))
            d_rel.append(br - sr)
            d_res.append(bs - ss)
        diagram = []
        for a, b in zip(seat_m["diagram"], blend_m["diagram"]):
            diagram.append({
                "lo": a["lo"], "hi": a["hi"],
                "seat_weight": a["weight"], "seat_promised": a["promised"], "seat_happened": a["happened"],
                "blend_weight": b["weight"], "blend_promised": b["promised"], "blend_happened": b["happened"],
            })
        out[label] = {
            "station_days": len({(r["station"], r["event_date"]) for r in subset}),
            "event_days": len(days),
            "seat": {"rel": seat_m["reliability"], "res": seat_m["resolution"], "brier": seat_m["mean_bucket_brier"]},
            "blend": {"rel": blend_m["reliability"], "res": blend_m["resolution"], "brier": blend_m["mean_bucket_brier"]},
            "delta_rel_ci95": [float(np.percentile(d_rel, 2.5)), float(np.percentile(d_rel, 97.5))],
            "delta_res_ci95": [float(np.percentile(d_res, 2.5)), float(np.percentile(d_res, 97.5))],
            "share_rel_down": float(np.mean(np.array(d_rel) < 0)),
            "diagram": diagram,
        }
    (HERE / "rel_detail.json").write_text(json.dumps(out, indent=1))
    for label, o in out.items():
        print(label, o["station_days"], "sd", o["event_days"], "d")
        print("  REL seat %.4f blend %.4f (x1e-3)  dREL CI %s  P(dREL<0)=%.2f" % (
            o["seat"]["rel"] * 1e3, o["blend"]["rel"] * 1e3, [round(x * 1e3, 4) for x in o["delta_rel_ci95"]], o["share_rel_down"]))
        print("  RES seat %.3f blend %.3f  dRES CI %s  brier %.6f -> %.6f" % (
            o["seat"]["res"] * 1e3, o["blend"]["res"] * 1e3, [round(x * 1e3, 4) for x in o["delta_res_ci95"]], o["seat"]["brier"], o["blend"]["brier"]))
        for d in o["diagram"]:
            print("   [%.3f,%.3f) share %.4f  seat says %.3f got %.3f | blend says %.3f got %.3f" % (
                d["lo"], d["hi"], d["seat_weight"], d["seat_promised"], d["seat_happened"], d["blend_promised"], d["blend_happened"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
