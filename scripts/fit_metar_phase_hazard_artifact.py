#!/usr/bin/env python3
"""Fit the phase and remaining-warming layers as one ordered hazard chain.

Two layers, one object.  The chain models the remaining warming as a sequence
of conditional survivals -- P(delta >= 1), then P(delta >= 2 | delta >= 1), and
so on -- and the split between the first rung and the rest is exactly the split
between what METAR can say on its own and what it can only say with the shape
of the model's day beside it:

* **rung one is the phase layer.**  Will the day rise at all from here?  Its
  features are the clock and the reports: hours left, the drawdown from the
  day's own high, the momentum, cloud, precipitation.  No model quantity of
  any kind.  It replaces the cooling-side ceiling's constant per
  bin with a rate conditioned on the state, and with the state at its fleet
  mean it must land back on that constant -- the nested check the acceptance
  run prints.

* **rungs two and up are the remaining-warming layer.**  Given that the day
  will rise, how far?  Here the model's *shape* is allowed in -- how much of
  its own diurnal rise the curve says is already behind us -- but never its
  level.  Two curves five degrees apart that peak at the same hour give the
  identical feature, which is what keeps this layer independent of the root it
  multiplies.

The layer is priced by a likelihood ratio against its own phase-only twin, not
against a flat baseline.  Fitting the same rungs twice, once on the full
covariate set and once on the phase subset, makes the ratio express only what
the physical state and the shape add beyond the clock; a flat denominator would
have counted the clock twice, once in the ratio and once in the phase split
that is applied on top of it.

Estimation is L2-regularised logistic regression by Newton steps, weighted so
each station-day counts once however often its station files.  Hours-left
enters rung one as binned intercepts on the ceiling artifact's own thirteen
bins, isotonically projected so the fitted rate rises with the hours the day
still has -- unsmoothed bins invert on a thin bin, and a morning cannot be
less likely to warm than the evening that follows it.

    python3 scripts/fit_metar_phase_hazard_artifact.py \
        --samples data/runtime/bayes_basket_target_nwp/metar_phase_hazard_samples.db \
        --through 2026-09-01 \
        --out data/runtime/bayes_basket_target_nwp/metar_phase_hazard_artifact.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402

SCHEMA = "bayes_basket_target_metar_phase_hazard_v1"
# The ceiling artifact's own edges.  Sharing them is what makes the nested
# check a check: rung one with the clock alone has to land on the rates that
# artifact already publishes, bin for bin.
HOURS_EDGES = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 13.0, 16.0,
               20.0, 24.0, 1e9)
MAX_RUNG = 12
# Below this a rung has not seen enough survivors to be fitted; it inherits
# the last fitted rung's conditional rate, which is the flattest honest
# continuation rather than a coefficient read off a handful of rows.
MIN_RUNG_SAMPLES = 200.0
L2 = 1.0
NEWTON_STEPS = 40
NEWTON_TOLERANCE = 1e-9

# The clock and the reports.  Nothing here is derived from a forecast.
#
# ``hours_since_high_refresh`` is recorded on every sample and deliberately not
# fitted on.  Its marginal is exactly the physics -- inside the ceiling's
# serving window the day still rises 25.4% of the time while the temperature
# sits on its own high, 3.4% half an hour later and 0.5% after three hours --
# but the drawdown says the same thing on a scale the logistic can use, and
# with both in the design the collinearity flips the hours coefficient
# positive.  Measured on a held-out tail (2026-08-28..09-01, 7,493 samples):
# linear hours, log1p hours and no hours score -0.1978, -0.1979 and -0.1979.
# It buys nothing and reads as nonsense in review, so it is out.
PHASE_FEATURES = (
    "drawdown_from_high_c",
    "momentum_c_per_hour",
    "cloud_cover_fraction",
    "precipitation_present",
)
# Everything the phase layer sees, plus the physical state that says how much
# more the air can still take, plus the model's shape -- never its level.
FULL_FEATURES = PHASE_FEATURES + (
    "dewpoint_depression_c",
    "wind_speed_mps",
    "warming_so_far_c",
    "curve_shape_realized",
    "remaining_hours",
)


def hours_bin(hours: float) -> int:
    for index in range(len(HOURS_EDGES) - 1):
        if HOURS_EDGES[index] <= hours < HOURS_EDGES[index + 1]:
            return index
    return len(HOURS_EDGES) - 2


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def fit_logistic(
    design: np.ndarray,
    outcome: np.ndarray,
    weight: np.ndarray,
    *,
    l2: float = L2,
    penalise: np.ndarray | None = None,
) -> np.ndarray:
    """Weighted L2-logistic by Newton steps; the ridge never touches intercepts.

    Penalising an intercept would shrink the base rate toward a half, which is
    the one number here that is not a nuisance parameter: at one hour left the
    honest rate is two in a thousand.
    """

    columns = design.shape[1]
    beta = np.zeros(columns)
    ridge = np.ones(columns) * l2 if penalise is None else penalise * l2
    for _ in range(NEWTON_STEPS):
        eta = np.clip(design @ beta, -30.0, 30.0)
        mu = 1.0 / (1.0 + np.exp(-eta))
        gradient = design.T @ (weight * (outcome - mu)) - ridge * beta
        variance = weight * mu * (1.0 - mu) + 1e-12
        hessian = (design * variance[:, None]).T @ design + np.diag(ridge)
        # A singular Hessian is a design problem -- an all-zero dummy column
        # from an empty bin is the way it arrives here -- and returning the
        # zero vector for it would publish an artifact that says every rate
        # is one half while reporting success.  The first version of this
        # file did exactly that, and the only sign was a column of 0.500
        # where the ceiling artifact publishes 0.002.
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError as exc:
            raise SystemExit(
                "metar_phase_hazard_design_singular: "
                "a column carries no weight"
            ) from exc
        beta = beta + step
        if float(np.max(np.abs(step))) < NEWTON_TOLERANCE:
            break
    return beta


def isotonic_increasing(
    values: Sequence[float], weights: Sequence[float]
) -> list[float]:
    """Pool adjacent violators so the sequence never falls.

    The bins run from least hours left to most, so the constraint is that the
    modelled rate rises with the hours the day still has: a morning cannot be
    less likely to warm than the evening that follows it.  Written the other
    way round -- decreasing in bin index, which reads correctly in English as
    "the rate falls as the day runs out" -- the projection pools every bin
    into one block and publishes a single rate for the whole clock.  That is
    what the first fit did, and the sign of the error was a flat column of
    0.648 where the ceiling artifact publishes 0.002 to 0.929.

    Blocks carry their own span so the projection lands back on the original
    positions rather than on a weight-proportional guess at them.
    """

    blocks: list[list[float]] = []
    for value, weight in zip(values, weights):
        blocks.append([float(value), max(float(weight), 1e-9), 1.0])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0] + 1e-12:
            value_b, weight_b, span_b = blocks.pop()
            value_a, weight_a, span_a = blocks.pop()
            total = weight_a + weight_b
            blocks.append([
                (value_a * weight_a + value_b * weight_b) / total,
                total,
                span_a + span_b,
            ])
    projected: list[float] = []
    for value, _weight, span in blocks:
        projected.extend([value] * int(round(span)))
    return projected[: len(values)]


def load_samples(
    path: Path, *, through: str, since: str | None
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    clause = "WHERE event_date<=?"
    params: list[Any] = [through]
    if since:
        clause += " AND event_date>=?"
        params.append(since)
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM metar_phase_hazard_sample {clause}"
            " ORDER BY event_date, station_id, as_of_utc",
            params,
        )
    ]


def feature_matrix(
    samples: Sequence[Mapping[str, Any]],
    names: Sequence[str],
    *,
    fallbacks: Mapping[str, float],
) -> np.ndarray:
    """Design columns, with a missing feature filled at its fleet mean.

    A None only ever arrives from the curve shape, which is absent whenever
    the root carried no curve or the curve had run out.  Filling it at the
    fleet mean makes the row say "nothing known about the shape here" rather
    than "the shape is zero", which would read as a day that has not started.
    """

    return np.array(
        [
            [
                float(
                    sample[name]
                    if sample.get(name) is not None
                    else fallbacks[name]
                )
                for name in names
            ]
            for sample in samples
        ],
        dtype=float,
    )


def fleet_means(
    samples: Sequence[Mapping[str, Any]], names: Sequence[str]
) -> dict[str, float]:
    means: dict[str, float] = {}
    for name in names:
        values = [
            float(sample[name])
            for sample in samples
            if sample.get(name) is not None
        ]
        means[name] = sum(values) / len(values) if values else 0.0
    return means


def fit_phase_rung(
    samples: Sequence[Mapping[str, Any]], fallbacks: Mapping[str, float]
) -> dict[str, Any]:
    """Rung one: will the day rise at all from here.  Model-free by contract."""

    raw_bins = np.array(
        [hours_bin(float(sample["remaining_hours"])) for sample in samples]
    )
    # Only bins the fleet actually occupies get a column.  A local day is
    # twenty-four hours, so the ceiling's open-ended last bin is empty here
    # by construction, and an all-zero dummy column makes the Newton step
    # singular.  Unoccupied bins inherit their nearest fitted neighbour
    # below, which is the same answer with the arithmetic left standing.
    occupied = sorted({int(value) for value in raw_bins})
    column_of = {bin_index: column for column, bin_index in enumerate(occupied)}
    bins = np.array([column_of[int(value)] for value in raw_bins])
    bin_count = len(occupied)
    dummies = np.zeros((len(samples), bin_count))
    dummies[np.arange(len(samples)), bins] = 1.0
    covariates = feature_matrix(samples, PHASE_FEATURES, fallbacks=fallbacks)
    design = np.hstack([dummies, covariates])
    outcome = np.array(
        [1.0 if int(sample["delta_c"]) >= 1 else 0.0 for sample in samples]
    )
    weight = np.array([float(sample["station_day_weight"]) for sample in samples])
    penalise = np.concatenate([
        np.zeros(bin_count), np.ones(len(PHASE_FEATURES))
    ])
    beta = fit_logistic(design, outcome, weight, penalise=penalise)
    fitted_weight = [
        float(weight[bins == column].sum()) for column in range(bin_count)
    ]
    projected = isotonic_increasing(list(beta[:bin_count]), fitted_weight)
    # Back onto the full edge list: an unoccupied bin takes the nearest
    # occupied one below it, or the first occupied one if it sits before them
    # all.  The serving side indexes by edge, so the published vector has to
    # be the full length whatever the fleet happened to occupy.
    full_intercepts: list[float] = []
    full_weight: list[float] = []
    for index in range(len(HOURS_EDGES) - 1):
        if index in column_of:
            column = column_of[index]
            full_intercepts.append(float(projected[column]))
            full_weight.append(fitted_weight[column])
        else:
            full_intercepts.append(
                full_intercepts[-1] if full_intercepts
                else float(projected[0])
            )
            full_weight.append(0.0)
    return {
        "hours_edges": list(HOURS_EDGES),
        "occupied_bins": occupied,
        "bin_intercepts": full_intercepts,
        "bin_intercepts_fitted": [float(value) for value in projected],
        "bin_weight": full_weight,
        "coefficients": {
            name: float(value)
            for name, value in zip(PHASE_FEATURES, beta[bin_count:])
        },
        "sample_n": len(samples),
        "positive_rate": float(
            (weight * outcome).sum() / max(weight.sum(), 1e-12)
        ),
    }


def fit_continuation(
    samples: Sequence[Mapping[str, Any]],
    names: Sequence[str],
    fallbacks: Mapping[str, float],
) -> dict[str, Any]:
    """Rungs two and up: given the day will rise, how far.

    One shared covariate vector with a per-rung intercept -- the proportional
    odds form.  Fitting each rung its own slopes would spend the sample on
    twelve nearly identical shapes and leave the tail rungs on a handful of
    rows apiece.
    """

    # Only rungs the window actually reaches get a column.  A fit window with
    # no twelve-degree afternoon in it leaves that rung's dummy all zero, the
    # Newton step singular, and -- before the solve was made to raise -- the
    # whole vector zero.  The rungs above the last fitted one inherit its
    # conditional rate, which is the flattest honest continuation rather than
    # a coefficient read off three rows.
    reached: dict[int, float] = {}
    for sample in samples:
        for rung in range(2, MAX_RUNG + 1):
            if int(sample["delta_c"]) >= rung - 1:
                reached[rung] = reached.get(rung, 0.0) + 1.0
    minimum = MIN_RUNG_SAMPLES
    rungs = [rung for rung in range(2, MAX_RUNG + 1) if reached.get(rung, 0.0) >= minimum]
    if not rungs:
        rungs = [rung for rung in range(2, MAX_RUNG + 1) if reached.get(rung, 0.0) > 0.0]
    if not rungs:
        raise SystemExit("no continuation rows; widen the window")
    highest = max(rungs)
    rows: list[np.ndarray] = []
    outcomes: list[float] = []
    weights: list[float] = []
    rung_index: list[int] = []
    covariates = feature_matrix(samples, names, fallbacks=fallbacks)
    for position, sample in enumerate(samples):
        delta = int(sample["delta_c"])
        for offset, rung in enumerate(rungs):
            if delta < rung - 1:
                break
            rows.append(covariates[position])
            # Above the highest fitted rung the outcome is folded into that
            # rung: a row that ran past it still says the day kept rising, so
            # dropping it would bias the tail toward stopping.
            outcomes.append(1.0 if delta >= rung else 0.0)
            weights.append(float(sample["station_day_weight"]))
            rung_index.append(offset)
    if not rows:
        raise SystemExit("no continuation rows; widen the window")
    dummies = np.zeros((len(rows), len(rungs)))
    dummies[np.arange(len(rows)), np.array(rung_index)] = 1.0
    design = np.hstack([dummies, np.array(rows)])
    outcome = np.array(outcomes)
    weight = np.array(weights)
    penalise = np.concatenate([np.zeros(len(rungs)), np.ones(len(names))])
    rung_weight = [
        float(weight[np.array(rung_index) == offset].sum())
        for offset in range(len(rungs))
    ]
    beta = fit_logistic(design, outcome, weight, penalise=penalise)
    return {
        "rungs": rungs,
        "highest_fitted_rung": highest,
        "max_rung": MAX_RUNG,
        "rung_intercepts": [float(value) for value in beta[: len(rungs)]],
        "rung_weight": rung_weight,
        "rung_reached_rows": {str(k): v for k, v in sorted(reached.items())},
        "minimum_rung_rows": MIN_RUNG_SAMPLES,
        "coefficients": {
            name: float(value)
            for name, value in zip(names, beta[len(rungs):])
        },
        "row_n": len(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", required=True)
    parser.add_argument("--through", required=True)
    parser.add_argument("--since", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fitted-at-utc", default=None)
    args = parser.parse_args()

    samples = load_samples(
        Path(args.samples), through=args.through, since=args.since
    )
    if len(samples) < 500:
        raise SystemExit(f"only {len(samples)} samples; refusing to fit")
    fallbacks = fleet_means(samples, FULL_FEATURES)
    phase = fit_phase_rung(samples, fallbacks)
    risers = [sample for sample in samples if int(sample["delta_c"]) >= 1]
    full = fit_continuation(risers, FULL_FEATURES, fallbacks)
    phase_only = fit_continuation(risers, PHASE_FEATURES, fallbacks)

    body = {
        "schema": SCHEMA,
        "fitted_at_utc": args.fitted_at_utc or _iso(datetime.now(timezone.utc)),
        "trained_from": min(sample["event_date"] for sample in samples),
        "trained_through": max(sample["event_date"] for sample in samples),
        "station_n": len({sample["station_id"] for sample in samples}),
        "station_day_n": len({
            (sample["station_id"], sample["event_date"]) for sample in samples
        }),
        "sample_n": len(samples),
        "riser_sample_n": len(risers),
        "feature_fallbacks": fallbacks,
        "phase_features": list(PHASE_FEATURES),
        "full_features": list(FULL_FEATURES),
        "phase": phase,
        "continuation_full": full,
        "continuation_phase_only": phase_only,
    }
    body["content_sha256"] = content_sha256(body)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(body, indent=2, sort_keys=True))
    print(json.dumps({
        "schema": SCHEMA,
        "sample_n": body["sample_n"],
        "riser_sample_n": body["riser_sample_n"],
        "trained_through": body["trained_through"],
        "out": str(out_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
