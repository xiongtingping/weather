#!/usr/bin/env python3
"""Review of the today's-P0 reading (2026-09-26): strict 24 <= h < 60, model vs market.

The review's claim: LogScore +0.1019 nat and Brier +0.0370 behind the market,
behind in all three periods and at both C and F stations, and still so with
three-day blocks resampled.  Reproduced on the study's own cache and scores
(``decompose.score``: served floor on the model, none on the market), plus
three checks the review did not run: the replay's truth against today's
formal truth (production's ``knowable_labels``), the opening update alone, and
the per-station sign.  Read-only; writes audit_review.json.

    python3 audit_review.py cache.pkl      # the cache decompose.py writes
"""
from __future__ import annotations

import json
import pickle
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import decompose as D  # noqa: E402
from bayes_basket_target.label_knowability import knowable_labels  # noqa: E402

STRICT_BINS = (2, 3, 4)  # 24-36h, 36-48h, 48-60h
PERIODS = (("2026-08-15", "2026-08-31"), ("2026-09-01", "2026-09-13"), ("2026-09-14", "2026-09-24"))
BLOCK_DAYS = 3
DRAWS = 2000
SEED = 20260926


def day_sums(items, value_of):
    count = Counter((d["station"], d["date"]) for d in items)
    days = sorted({d["date"] for d in items})
    index = {day: i for i, day in enumerate(days)}
    num, den = np.zeros(len(days)), np.zeros(len(days))
    for d in items:
        w = 1.0 / count[(d["station"], d["date"])]
        num[index[d["date"]]] += w * value_of(d)
        den[index[d["date"]]] += w
    return days, num, den


def resample(num, den, rng):
    draws = []
    for _ in range(DRAWS):
        pick = rng.integers(0, len(num), len(num))
        draws.append(num[pick].sum() / den[pick].sum())
    return [float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))]


def reading(items, value_of, rng):
    days, num, den = day_sums(items, value_of)
    first = date.fromisoformat(days[0])
    block = np.array([(date.fromisoformat(day) - first).days // BLOCK_DAYS for day in days])
    blocks = sorted(set(block))
    bnum = np.array([num[block == b].sum() for b in blocks])
    bden = np.array([den[block == b].sum() for b in blocks])
    return {"mean": float(num.sum() / den.sum()), "day_ci": resample(num, den, rng),
            "block3_ci": resample(bnum, bden, rng), "days": len(days), "blocks": len(blocks)}


def panel(items, rng):
    return {"decisions": len(items), "station_days": len({(d["station"], d["date"]) for d in items}),
            "logscore_gap": reading(items, lambda d: d["ls_gap"], rng),
            "brier_gap": reading(items, lambda d: d["br_gap"], rng)}


def fahrenheit(d):
    return "°F" in d["keys"][0] or "°F" in d["keys"][-1]


def main():
    _served, _s, today, _t = pickle.loads(Path(sys.argv[1]).read_bytes())
    strict = [d for d in today if d["bin"] in STRICT_BINS]
    rng = np.random.default_rng(SEED)
    out = {"strict_24_60h": panel(strict, rng)}
    out["periods"] = {f"{lo}..{hi}": panel([d for d in strict if lo <= d["date"] <= hi], rng) for lo, hi in PERIODS}
    out["units"] = {"F": panel([d for d in strict if fahrenheit(d)], rng),
                    "C": panel([d for d in strict if not fahrenheit(d)], rng)}

    # Today's formal truth (charter 225's revisions included) against the replay's own.
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
    conn.execute("PRAGMA query_only=ON")
    labels = knowable_labels(conn)
    conn.close()
    replay_bucket = {(d["station"], d["date"]): d["keys"][d["hit"]] for d in strict}
    differs = sorted(f"{s} {day}: {bucket} -> {labels[(s, day)]['bucket']}"
                     for (s, day), bucket in replay_bucket.items()
                     if (s, day) in labels and str(labels[(s, day)]["bucket"]) != bucket)
    rescored = []
    for d in strict:
        label = labels.get((d["station"], d["date"]))
        if label is None or str(label["bucket"]) not in d["keys"]:
            continue
        hit = d["keys"].index(str(label["bucket"]))
        ls_p, br_p = D.score(d["p"], d["keys"], hit)
        ls_m, br_m = D.score(d["m"], d["keys"], hit, floor=False)
        rescored.append({"station": d["station"], "date": d["date"], "ls_gap": ls_p - ls_m, "br_gap": br_p - br_m})
    out["formal_truth"] = {"station_days_differing": len(differs), "differing": differs,
                           "unlabelled": sum(1 for key in replay_bucket if key not in labels),
                           **panel(rescored, rng)}

    # The opening update alone: the first replayed P0 update of each station-day inside the window.
    opening = {}
    for d in sorted(strict, key=lambda d: d["at"]):
        opening.setdefault((d["station"], d["date"]), d)
    opening = list(opening.values())
    out["opening_update_only"] = {**panel(opening, rng),
                                  "station_days_model_better": float(np.mean([d["ls_gap"] < 0 for d in opening]))}

    by_station = defaultdict(list)
    for d in strict:
        by_station[d["station"]].append(d)
    station_gap = {s: day_sums(v, lambda d: d["ls_gap"]) for s, v in by_station.items()}
    station_gap = {s: float(num.sum() / den.sum()) for s, (_days, num, den) in station_gap.items()}
    out["stations"] = {"count": len(station_gap),
                       "model_better": {s: round(g, 3) for s, g in sorted(station_gap.items(), key=lambda x: x[1]) if g < 0}}

    (HERE / "audit_review.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
