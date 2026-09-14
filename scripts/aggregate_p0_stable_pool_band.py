#!/usr/bin/env python3
"""Charter 134: does the band table the pairs were fitted on suit the pool?

Declared before the band results were read (2026-09-14, committed with this
file).  For the contest (A) and the pool at kappa 0.5 (C0.5), both at the
width the tables served, three band treatments are scored on the same
updates:

  served   the band table active at the update (what the gate passed under)
  none     no band layer
  refit    a band table fitted, per decision date, by production's
           ``band_lane_report`` on this scheme's own first-decision pre-band
           claims from event days at least two days earlier (distance axis,
           production interval, 30-station cell floor), conservative factors
           for qualified cells, identity elsewhere; with fewer than
           MIN_QUALIFYING_DAYS inner days the served table is held, as the
           lane holds its incumbent

Decision for the switch, per scheme C0.5: take ``refit`` if its CRPS against
``served`` has a 95% station-cluster upper bound below zero; else take
``none`` if that holds for ``none``; else keep the incumbent table.  The
bootstrap inside each fit uses 400 draws (production 1000) to keep the run
inside a foreground slice; the interval end it serves moves by a few
thousandths at that count.
"""
from __future__ import annotations

import gzip
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.band_reliability_store import (  # noqa: E402
    BAND_AXIS_CENTRE_DISTANCE,
    DEFAULT_INTERVAL_ALPHA,
    MIN_CELL_STATIONS,
    MIN_QUALIFYING_DAYS,
    band_lane_report,
    cells_for_axis,
)
from bayes_basket_target.cross_consensus import apply_band_reliability  # noqa: E402

BASE = ROOT / "tmp" / "stable_pool_replay_20260914"
SCHEMES = ("A", "C0.5")
BOOTSTRAP = 400


def load() -> list[dict]:
    records = []
    for path in sorted((BASE / "updates").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            records.extend(json.loads(line) for line in handle)
    return [r for r in records if "A" in r["schemes"]]


def claims(vec: dict[str, float], actual: float) -> list[tuple[float, int]]:
    bucket = oos._temperature_bucket(actual)
    keys = sorted(vec, key=int)
    return [(float(vec[k]), 1 if k == bucket else 0) for k in keys]


def score(vec: dict[str, float], actual: float) -> tuple[float, float]:
    metrics = oos._metrics(vec, actual)
    return metrics["crps"], metrics["nll"]


def cluster_ci(rows, fn, n=2000, seed=23):
    groups = defaultdict(list)
    for row in rows:
        groups[row["station"]].append(row)
    keys = sorted(groups)
    rng = random.Random(seed)
    values = sorted(
        fn([row for k in (rng.choice(keys) for _ in keys) for row in groups[k]])
        for _ in range(n)
    )
    return [values[int(0.025 * n)], values[int(0.975 * n)]]


def main() -> int:
    records = load()
    first = defaultdict(dict)
    for r in sorted(records, key=lambda r: r["at"]):
        first[(r["station"], r["event_date"])].setdefault("r", r)
    by_decided = defaultdict(list)
    for r in records:
        by_decided[r["at"][:10]].append(r)
    cells = cells_for_axis(BAND_AXIS_CENTRE_DISTANCE)
    rows = []
    fits = {}
    for decided in sorted(by_decided):
        cutoff = (date.fromisoformat(decided) - timedelta(days=2)).isoformat()
        inner_days = sorted({d for (_s, d) in first if d <= cutoff})
        for scheme in SCHEMES:
            if len(inner_days) < MIN_QUALIFYING_DAYS:
                fits[(decided, scheme)] = None
                continue
            by_day = defaultdict(list)
            stations_by_day = defaultdict(list)
            for (station, day), holder in first.items():
                if day > cutoff:
                    continue
                r = holder["r"]
                by_day[day].append(claims(r["schemes"][scheme]["pre_band"], r["actual"]))
                stations_by_day[day].append(station)
            report = band_lane_report(
                by_day, bands=cells, bootstrap_samples=BOOTSTRAP,
                axis=BAND_AXIS_CENTRE_DISTANCE, interval_alpha=DEFAULT_INTERVAL_ALPHA,
                stations_by_day=stations_by_day, min_cell_stations=MIN_CELL_STATIONS,
            )
            fits[(decided, scheme)] = tuple(
                (cell, float(report[cell]["conservative_factor"]) if cell in report and report[cell]["qualified"] else 1.0)
                for cell in cells
            )
        for r in by_decided[decided]:
            row = {"station": r["station"], "decided": decided}
            for scheme in SCHEMES:
                data = r["schemes"][scheme]
                pre = {k: float(v) for k, v in data["pre_band"].items()}
                ordinals = {k: i for i, k in enumerate(sorted(pre, key=int))}
                factors = fits[(decided, scheme)]
                if factors is None:
                    refit_vec = None
                else:
                    refit_vec, _ = apply_band_reliability(pre, factors=factors, ordinals=ordinals)
                row[f"{scheme}|served"] = data["served"][:2]
                row[f"{scheme}|none"] = data["noband"][:2]
                row[f"{scheme}|refit"] = list(score(refit_vec, r["actual"])) if refit_vec else data["served"][:2]
                row[f"{scheme}|refit_active"] = factors is not None
            rows.append(row)
    scored = [row for row in rows if row["C0.5|refit_active"]]

    def rel(x, y, rs):
        return sum(r[x][0] - r[y][0] for r in rs) / sum(r[y][0] for r in rs)

    def nll_diff(x, y, rs):
        return statistics.mean(r[x][1] - r[y][1] for r in rs)

    summary = {"updates_with_refit": len(scored), "decision_dates": sorted({r["decided"] for r in scored})}
    for scheme in SCHEMES:
        for variant in ("none", "refit"):
            x, y = f"{scheme}|{variant}", f"{scheme}|served"
            summary[f"{scheme}: {variant} vs served"] = {
                "rel_crps": rel(x, y, scored), "rel_crps_ci95": cluster_ci(scored, lambda rs, x=x, y=y: rel(x, y, rs)),
                "nll_diff": nll_diff(x, y, scored), "nll_diff_ci95": cluster_ci(scored, lambda rs, x=x, y=y: nll_diff(x, y, rs)),
            }
    summary["C0.5_vs_A_under_each_band"] = {
        variant: {"rel_crps": rel(f"C0.5|{variant}", f"A|{variant}", scored),
                  "rel_crps_ci95": cluster_ci(scored, lambda rs, v=variant: rel(f"C0.5|{v}", f"A|{v}", rs))}
        for variant in ("served", "none", "refit")
    }
    summary["refit_factors_C0.5_last"] = [[list(c), f] for c, f in (fits[(summary["decision_dates"][-1], "C0.5")] or ())]
    summary["refit_factors_A_last"] = [[list(c), f] for c, f in (fits[(summary["decision_dates"][-1], "A")] or ())]
    refit = summary["C0.5: refit vs served"]
    none = summary["C0.5: none vs served"]
    summary["decision"] = (
        "refit" if refit["rel_crps_ci95"][1] < 0
        else "none" if none["rel_crps_ci95"][1] < 0
        else "keep_incumbent"
    )
    (BASE / "band_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True))
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
