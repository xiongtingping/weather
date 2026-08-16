#!/usr/bin/env python3
"""Find the tail factor the settled record actually wants, by scoring for it.

Research-only, read-only.  ``measure_tail_physical_conditioning`` established
that the served tails are over-claimed on the live lane -- warm 0.17, cold 0.46
realised over claimed, both intervals excluding one -- but a realised-over-
claimed ratio is a description, not a decision: it is computed on the seven and
twenty-seven settled tail events the lane holds, and a ratio built on seven
events cannot on its own justify moving a served multiplier.

Scoring can, because it uses every station-day rather than only the ones that
landed in a tail.  This sweeps one extra multiplier over the tail buckets of
the final served claim and reports CRPS, NLL and Brier under it, by the same
definitions ``native_ensemble_oos._metrics`` scores the gate with.  A minimum
at ``c < 1`` says the tail layers overshoot in aggregate; a minimum at 1 says
the ratio is small-sample noise and the layers are right where they are.

This deliberately sweeps *on top of* the served claim instead of trying to
invert ``apply_conditional_tail_skew``.  The receipt that would let a reader
undo that layer is not persisted with the OOS prediction, and an inverse
reconstructed from the served numbers alone would be a guess about which
factor produced them.  Composition needs no such guess: whatever the inner
layers did, the sweep asks only what should be multiplied on next, and
``c = 0.5`` means the same thing either way -- halve what is being claimed out
there.

Nothing here changes a served factor.  The output is the evidence a change
would have to rest on, and the band gate and the OOS gate remain the paths by
which any of it reaches serving.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.cross_consensus import (  # noqa: E402
    CONDITIONAL_SKEW_THRESHOLD,
)
from scripts.measure_tail_physical_conditioning import (  # noqa: E402
    DEFAULT_DB,
    LIVE_LEAD_BUCKETS,
    _connect,
    _load_cases,
)

EPS = 1e-12
BOOTSTRAP_SEED = 20260816


def _prepare(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Reduce each station-day to ordinals, so scoring is pure arithmetic."""

    prepared: list[dict[str, Any]] = []
    for case in cases:
        claim = case["claim_by_bucket"]
        centers = case["bucket_centers_c"]
        ordered = sorted(claim, key=lambda key: float(centers[key]))
        values = [float(claim[key]) for key in ordered]
        center_ordinal = max(range(len(ordered)), key=lambda i: values[i])
        prepared.append(
            {
                "event_date": case["event_date"],
                "values": values,
                "center_ordinal": center_ordinal,
                "landed_ordinal": ordered.index(case["landed_bucket"]),
            }
        )
    return prepared


def _reweighted(
    values: Sequence[float],
    *,
    center_ordinal: int,
    warm_factor: float,
    cold_factor: float,
) -> list[float]:
    """The served claim with one more multiplier on its tail buckets."""

    scaled: list[float] = []
    for ordinal, value in enumerate(values):
        if 0.0 < value < CONDITIONAL_SKEW_THRESHOLD:
            if ordinal > center_ordinal:
                value *= warm_factor
            elif ordinal < center_ordinal:
                value *= cold_factor
        scaled.append(value)
    total = sum(scaled)
    return [value / total for value in scaled]


def _score(values: Sequence[float], landed_ordinal: int) -> tuple[float, float, float]:
    """Brier, NLL and CRPS, by ``native_ensemble_oos._metrics`` definitions."""

    brier = sum(
        (value - (1.0 if index == landed_ordinal else 0.0)) ** 2
        for index, value in enumerate(values)
    )
    nll = -math.log(max(values[landed_ordinal], EPS))
    crps = 0.0
    cumulative = 0.0
    for index, value in enumerate(values):
        cumulative += value
        observed = 1.0 if landed_ordinal <= index else 0.0
        crps += (cumulative - observed) ** 2
    return brier, nll, crps


def _mean_scores(
    prepared: Sequence[Mapping[str, Any]],
    *,
    warm_factor: float,
    cold_factor: float,
) -> dict[str, float]:
    totals = [0.0, 0.0, 0.0]
    for case in prepared:
        values = _reweighted(
            case["values"],
            center_ordinal=case["center_ordinal"],
            warm_factor=warm_factor,
            cold_factor=cold_factor,
        )
        for index, value in enumerate(_score(values, case["landed_ordinal"])):
            totals[index] += value
    count = len(prepared)
    return {
        "brier": totals[0] / count,
        "nll": totals[1] / count,
        "crps": totals[2] / count,
    }


def _sweep(
    prepared: Sequence[Mapping[str, Any]],
    *,
    factors: Sequence[float],
    side: str,
) -> None:
    baseline = _mean_scores(prepared, warm_factor=1.0, cold_factor=1.0)
    print(f"\n{side} tail multiplier on top of the served claim")
    print(
        f"  {'c':>5}  {'CRPS':>8} {'d%':>7}  {'NLL':>8} {'d%':>7}"
        f"  {'Brier':>8} {'d%':>7}"
    )
    for factor in factors:
        warm = factor if side in ("warm", "both") else 1.0
        cold = factor if side in ("cold", "both") else 1.0
        scores = _mean_scores(prepared, warm_factor=warm, cold_factor=cold)
        marks = []
        for key in ("crps", "nll", "brier"):
            delta = 100.0 * (scores[key] - baseline[key]) / baseline[key]
            marks.append(f"{scores[key]:8.5f} {delta:+7.3f}")
        flag = "  <-- served" if abs(factor - 1.0) < 1e-9 else ""
        print(f"  {factor:5.2f}  " + "  ".join(marks) + flag)


def _best_factor(
    prepared: Sequence[Mapping[str, Any]],
    *,
    factors: Sequence[float],
    side: str,
    metric: str,
) -> float:
    best = None
    for factor in factors:
        warm = factor if side in ("warm", "both") else 1.0
        cold = factor if side in ("cold", "both") else 1.0
        value = _mean_scores(prepared, warm_factor=warm, cold_factor=cold)[metric]
        if best is None or value < best[0]:
            best = (value, factor)
    return float(best[1])


def _bootstrap_best(
    prepared: Sequence[Mapping[str, Any]],
    *,
    factors: Sequence[float],
    side: str,
    metric: str,
    reps: int,
) -> tuple[float, float, float]:
    """Percentile interval for the minimising factor, resampling days."""

    by_day: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for case in prepared:
        by_day[case["event_date"]].append(case)
    days = sorted(by_day)
    rng = random.Random(BOOTSTRAP_SEED)
    draws: list[float] = []
    for _ in range(reps):
        sample: list[Mapping[str, Any]] = []
        for _ in days:
            sample.extend(by_day[days[rng.randrange(len(days))]])
        draws.append(
            _best_factor(sample, factors=factors, side=side, metric=metric)
        )
    draws.sort()
    return (
        draws[int(0.025 * len(draws))],
        draws[len(draws) // 2],
        draws[min(int(0.975 * len(draws)), len(draws) - 1)],
    )


def _report_holdout(
    prepared: Sequence[Mapping[str, Any]], *, factors: Sequence[float]
) -> None:
    """Choose the factor on one half of the calendar, score it on the other.

    The sweep above is fitted on the same days it scores, so its minimum is
    guaranteed to look good there.  What decides whether the factor is real is
    whether a value chosen without seeing a stretch of weather still improves
    that stretch.  Both directions are reported: a factor that only transfers
    forwards is a factor that has learned the season, not the tails.
    """

    days = sorted({case["event_date"] for case in prepared})
    midpoint = days[len(days) // 2]
    early = [case for case in prepared if case["event_date"] < midpoint]
    late = [case for case in prepared if case["event_date"] >= midpoint]
    print(
        f"\nheld-out transfer (fit on one half, scored on the other; "
        f"split at {midpoint})"
    )
    print(
        f"  {'fit on':>7} {'scored':>7}  {'side':>5}  {'metric':>6}"
        f"  {'c':>5}  {'held-out d%':>12}"
    )
    for fit_name, fit_half, score_name, score_half in (
        ("early", early, "late", late),
        ("late", late, "early", early),
    ):
        for side in ("warm", "cold", "both"):
            for metric in ("crps", "nll"):
                factor = _best_factor(
                    fit_half, factors=factors, side=side, metric=metric
                )
                warm = factor if side in ("warm", "both") else 1.0
                cold = factor if side in ("cold", "both") else 1.0
                baseline = _mean_scores(
                    score_half, warm_factor=1.0, cold_factor=1.0
                )[metric]
                scored = _mean_scores(
                    score_half, warm_factor=warm, cold_factor=cold
                )[metric]
                delta = 100.0 * (scored - baseline) / baseline
                print(
                    f"  {fit_name:>7} {score_name:>7}  {side:>5}  "
                    f"{metric:>6}  {factor:5.2f}  {delta:+12.3f}"
                )
    # A served factor is shrunk toward one rather than dropped on the point
    # estimate, so what matters operationally is how the conservative end of
    # the range transfers, not the minimum.
    print("\ncandidate served factors on each half, both sides together")
    print(
        f"  {'c':>5}  " + "  ".join(f"{half:>7} {metric:>6}"
        for half in ("early", "late")
        for metric in ("crps", "nll", "brier"))
    )
    for factor in (0.3, 0.4, 0.5, 0.6, 0.7):
        cells: list[str] = []
        for half in (early, late):
            baseline = _mean_scores(half, warm_factor=1.0, cold_factor=1.0)
            scored = _mean_scores(half, warm_factor=factor, cold_factor=factor)
            for metric in ("crps", "nll", "brier"):
                delta = (
                    100.0 * (scored[metric] - baseline[metric])
                    / baseline[metric]
                )
                cells.append(f"{delta:+14.3f}")
        print(f"  {factor:5.2f}  " + "  ".join(cells))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reps", type=int, default=400)
    parser.add_argument(
        "--lead-buckets",
        default=",".join(LIVE_LEAD_BUCKETS),
        help="comma-separated lead buckets to keep, or 'all'",
    )
    args = parser.parse_args()

    lead_buckets = (
        None
        if args.lead_buckets.strip().lower() == "all"
        else tuple(part for part in args.lead_buckets.split(",") if part)
    )
    conn = _connect(args.db)
    cases = _load_cases(conn, one_per_day=True, lead_buckets=lead_buckets)
    prepared = _prepare(cases)
    days = sorted({case["event_date"] for case in prepared})
    print(
        f"sample: {len(prepared)} station-days, {len(days)} days "
        f"({days[0]}..{days[-1]}); lead buckets {lead_buckets or 'all'}"
    )
    baseline = _mean_scores(prepared, warm_factor=1.0, cold_factor=1.0)
    print(
        "served claim as it stands:  "
        f"CRPS {baseline['crps']:.5f}  NLL {baseline['nll']:.5f}  "
        f"Brier {baseline['brier']:.5f}"
    )
    factors = [round(0.1 * step, 2) for step in range(1, 21)]
    for side in ("warm", "cold", "both"):
        _sweep(prepared, factors=factors, side=side)
    _report_holdout(prepared, factors=factors)
    print("\nminimising factor, bootstrapped over days")
    print(f"  {'side':>5}  {'metric':>6}  {'2.5%':>6} {'median':>7} {'97.5%':>6}")
    for side in ("warm", "cold", "both"):
        for metric in ("crps", "nll", "brier"):
            low, mid, high = _bootstrap_best(
                prepared,
                factors=factors,
                side=side,
                metric=metric,
                reps=args.reps,
            )
            print(
                f"  {side:>5}  {metric:>6}  {low:6.2f} {mid:7.2f} {high:6.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
