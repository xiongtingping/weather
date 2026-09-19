#!/usr/bin/env python3
"""Fit P1's joint observation likelihood: g(d | s) and its tempering alpha.

Charter 150 (user ruling 2026-09-19).  One artifact, refitted daily:

1. **g** -- the ordered hazard chain over the bucket increment above the
   running high, rung one (does the day cross into another bucket) on the
   clock bins plus ``p1_observation_likelihood.PHASE_FEATURES``, rungs two and
   up on ``FULL_FEATURES``.  The estimator (below) is the ordered hazard chain
   the retired four-layer fit used, moved here whole; the covariates differ:
   the innovations against the root curve and the regional run join the state.

2. **alpha** as a curve in the hours left (charter 156), chosen on a
   held-out day split: g is refitted on the earlier 70% of days -- only those
   whose truth had been committed before the held-out window's first report
   -- and the curve is scored as ``root x mask x g**alpha`` under the served
   zero floor on the later 30% against the settled bucket.  The root is the
   P0/P0-M envelope the sample was compiled on, read out of the store by
   checkpoint.  Every sample is labelled the way the served g reads its
   settled bucket (``serving_label``) before either half of g is fitted.

3. **veto** -- the candidate is written only if, walked forward over the
   held-out days (each scored on a curve chosen from rows whose truth was
   committed before that day began), the tempered likelihood beats the root
   under the hard bound alone by at least ``--margin`` nat per sample, and
   (when the incumbent's own training window ends before the held-out days,
   so it can be scored out of sample) it is not worse than the incumbent by
   more than the margin.  A vetoed refit leaves the served file untouched
   and says why.

The written g is refitted on every day in the window; alpha is the split's.

    python3 scripts/fit_p1_observation_likelihood.py \\
        --samples data/runtime/bayes_basket_target_nwp/p1_observation_samples.db \\
        --through 2026-09-18 --out data/runtime/bayes_basket_target_nwp/p1_observation_likelihood_artifact.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import metar_four_layer as M4  # noqa: E402
from bayes_basket_target.metar_precision import official_temperature_c  # noqa: E402
from bayes_basket_target import p1_observation_likelihood as OL  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.materialization import (  # noqa: E402
    _anchor_bucket_for_temperature,
    floor_probabilities,
    PROBABILITY_MODEL_ZERO_FLOOR,
)
from bayes_basket_target.metar_trajectory import (  # noqa: E402
    hard_bound_excluded_buckets,
)
from bayes_basket_target.contracts import ProbabilityEnvelope  # noqa: E402
from bayes_basket_target.materialization import (  # noqa: E402
    rehydrate_lineage_documents,
)
from bayes_basket_target.p1_compiler import _bucket_geometry_c  # noqa: E402

ALPHA_GRID = tuple(round(0.125 * step, 3) for step in range(25))
HOLDOUT_SHARE = 0.30
MIN_SAMPLES = 2000
MIN_DAYS = 10
DEFAULT_MARGIN = 0.01


# -- the estimator ----------------------------------------------------------
#
# The ordered hazard chain, moved here from the retired four-layer fitter
# (charter 150) unchanged: L2-regularised logistic regression by Newton
# steps, weighted so each station-day counts once however often its station
# files; rung one on binned hours-left intercepts, isotonically projected so
# the rate rises with the hours the day still has; rungs two and up as one
# shared covariate vector with a per-rung intercept (proportional odds).

HOURS_EDGES = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 13.0, 16.0,
               20.0, 24.0, 1e9)
MAX_RUNG = 12
# Below this a rung has not seen enough survivors to be fitted; it inherits
# the last fitted rung's conditional rate.
MIN_RUNG_SAMPLES = 200.0
L2 = 1.0
NEWTON_STEPS = 40
NEWTON_TOLERANCE = 1e-9


def hours_bin(hours: float) -> int:
    for index in range(len(HOURS_EDGES) - 1):
        if HOURS_EDGES[index] <= hours < HOURS_EDGES[index + 1]:
            return index
    return len(HOURS_EDGES) - 2


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


def phase_outcome(sample: Mapping[str, Any]) -> float:
    """Did the day settle above the bucket the running high is in.

    That is the quantity rung one's rate stands for in the likelihood --
    ``1 - rise`` on the running high's own bucket, ``rise`` spread above it --
    so it is the quantity the rung is fitted on.  ``delta_c >= 1`` was the old stand-in: it rounds
    ``truth - running_high`` to whole Celsius degrees and so answers a
    question about a 1C ladder no market runs on.  Rows built before the
    sample set carried the crossing fall back to it, so an older sample
    database still fits rather than failing on a missing column.
    """

    served = sample.get("serving_rise")
    if served is not None:
        return 1.0 if int(served) else 0.0
    crossed = sample.get("crossed_bucket")
    if crossed is not None:
        return 1.0 if int(crossed) else 0.0
    return 1.0 if int(sample["delta_c"]) >= 1 else 0.0


def rise_of(sample: Mapping[str, Any]) -> tuple[int, bool] | None:
    """``(rung, censored)`` for a day that rose as g reads it; None otherwise.

    Labelled rows (``serving_label``) carry the served rise event and rung,
    and a settled open top is right-censored at its rung.  An unlabelled
    row falls back to the rounded warming, as the samples were first built.
    """

    served = sample.get("serving_rise")
    if served is not None:
        if not int(served):
            return None
        return int(sample["serving_rung"]), bool(sample["serving_censored"])
    delta = int(sample["delta_c"])
    return (delta, False) if delta >= 1 else None


def serving_label(
    sample: Mapping[str, Any], view: Mapping[str, Any]
) -> dict[str, Any] | None:
    """How the served g reads this sample's settled bucket.

    Review 2026-09-19 (finding four): rung one was fitted on "crossed a
    market bucket" and the continuation on "rounded warming >= 1C", so 163
    crossed days were left out of the second half and 255 uncrossed ones
    put in -- on F ladders and open tails above all.  Both halves now read
    the settled bucket the way ``evidence_by_bucket`` serves it: the running
    high's bucket is the lowest one the hard bound leaves (as
    ``materialize_p1`` picks it), the settled bucket is on the rise branch
    per ``OL.rise_increment_c``, and its rung is ``OL.nearest_rung``.  A
    settled open top is right-censored at its rung: the day rose at least
    that far, and how much further the ladder cannot say.
    """

    high = float(sample["running_high_c"])
    ordinals = view["ordinals"]
    ordered = sorted(ordinals, key=lambda bucket: (ordinals[bucket], bucket))
    excluded = set(hard_bound_excluded_buckets(ordered, view["uppers"], high))
    survivors = [bucket for bucket in ordered if bucket not in excluded]
    truth = _anchor_bucket_for_temperature(
        float(sample["truth_tmax_c"]),
        root_lineage={"bucket_intervals_c_json": view["intervals_json"]},
        bucket_ordinals=ordinals,
    )
    if not survivors or truth is None or truth in excluded:
        return None
    increment = OL.rise_increment_c(
        ordinals[truth], ordinals[survivors[0]], view["centers"][truth], high
    )
    if increment is None:
        return {"serving_rise": 0, "serving_rung": None, "serving_censored": False}
    return {
        "serving_rise": 1,
        "serving_rung": OL.nearest_rung(increment, range(1, MAX_RUNG + 1)),
        "serving_censored": view["intervals"][truth][1] is None,
    }


def fit_phase_rung(
    samples: Sequence[Mapping[str, Any]], fallbacks: Mapping[str, float],
    names: Sequence[str],
) -> dict[str, Any]:
    """Rung one: will the day cross into another bucket from here."""

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
    covariates = feature_matrix(samples, names, fallbacks=fallbacks)
    design = np.hstack([dummies, covariates])
    outcome = np.array([phase_outcome(sample) for sample in samples])
    weight = np.array([float(sample["station_day_weight"]) for sample in samples])
    penalise = np.concatenate([
        np.zeros(bin_count), np.ones(len(names))
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
            for name, value in zip(names, beta[bin_count:])
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
    rises = [rise_of(sample) for sample in samples]
    reached: dict[int, float] = {}
    for rise in rises:
        if rise is None:
            continue
        delta, censored = rise
        for rung in range(2, MAX_RUNG + 1):
            # A censored day says "at least delta": it answers every rung up
            # to delta (it went on) and none past it.
            if (rung <= delta) if censored else (delta >= rung - 1):
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
        if rises[position] is None:
            continue
        delta, censored = rises[position]
        for offset, rung in enumerate(rungs):
            if delta < rung - 1 or (censored and rung > delta):
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


class RootCache:
    """Root geometry by checkpoint, parsed once per evaluation."""

    def __init__(self, target: sqlite3.Connection) -> None:
        self._target = target
        self._cache: dict[str, dict[str, Any]] = {}

    def get(self, checkpoint_id: str) -> dict[str, Any] | None:
        if checkpoint_id in self._cache:
            return self._cache[checkpoint_id]
        row = self._target.execute(
            "SELECT stage, checkpoint_id, station_id, event_date,"
            " settlement_timezone, decision_at_utc, fresh_until_utc,"
            " bucket_ordinals_json, probabilities_json, posterior_version,"
            " posterior_sha256, bridge_sha256, lineage_json"
            " FROM basket_probability_envelope WHERE checkpoint_id=? LIMIT 1",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            self._cache[checkpoint_id] = None
            return None
        lineage = rehydrate_lineage_documents(
            self._target, json.loads(row[12] or "{}")
        )
        try:
            envelope = ProbabilityEnvelope.from_mapping({
                "stage": row[0], "checkpoint_id": row[1], "station_id": row[2],
                "event_date": row[3], "settlement_timezone": row[4],
                "decision_at_utc": row[5], "fresh_until_utc": row[6],
                "bucket_ordinals": json.loads(row[7]),
                "probabilities": json.loads(row[8]),
                "posterior_version": row[9], "posterior_sha256": row[10],
                "bridge_sha256": row[11], "lineage": lineage,
            })
            centers, uppers = _bucket_geometry_c(
                envelope, unit="C", upper_by_bucket={}
            )
            intervals = json.loads(
                str(lineage.get("bucket_intervals_c_json") or "{}")
            )
        except (ContractError, TypeError, ValueError, KeyError):
            self._cache[checkpoint_id] = None
            return None
        if not intervals:
            self._cache[checkpoint_id] = None
            return None
        view = {
            "probabilities": dict(envelope.probabilities),
            "ordinals": dict(envelope.bucket_ordinals),
            "centers": centers,
            "uppers": uppers,
            "intervals": intervals,
            "intervals_json": str(lineage.get("bucket_intervals_c_json")),
        }
        self._cache[checkpoint_id] = view
        return view


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def features_of(sample: Mapping[str, Any]) -> dict[str, float | None]:
    return OL.feature_vector(
        remaining_hours=sample["remaining_hours"],
        drawdown_from_high_c=sample["drawdown_from_high_c"],
        momentum_c_per_hour=sample["momentum_c_per_hour"],
        cloud_cover_fraction=sample["cloud_cover_fraction"],
        precipitation_present=sample["precipitation_present"],
        dewpoint_depression_c=sample["dewpoint_depression_c"],
        wind_speed_mps=sample["wind_speed_mps"],
        warming_so_far_c=sample["warming_so_far_c"],
        curve_shape_realized=sample["curve_shape_realized"],
        root_bias_recent_c=sample["root_bias_recent_c"],
        regional_headroom_c=sample["regional_headroom_c"],
        regional_live_gap_c=sample["regional_live_gap_c"],
    )


# -- charter 157: each station's clock is shifted by its own peak hour --------
#
# The peak hour is the local hour at which the day's maximum was first
# reached, from the archive's AWC reports (tenths from the remark group where
# the report carries one), the median over the window, shrunk toward the
# fleet median with ``STATION_PEAK_SHRINK_K`` days of prior weight.  The
# held-out scoring reads a table built only from days before the holdout; the
# published table reads the latest window.
STATION_PEAK_WINDOW_DAYS = 45
STATION_PEAK_SHRINK_K = 10.0
STATION_PEAK_MIN_REPORTS = 16


def station_peak_offsets(
    archive_db: Path,
    *,
    zones: Mapping[str, str],
    before_date: str,
    window_days: int = STATION_PEAK_WINDOW_DAYS,
    shrink_k: float = STATION_PEAK_SHRINK_K,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Station peak offsets in hours from the local days in [before - window, before)."""

    first_day = (date.fromisoformat(before_date) - timedelta(days=window_days)).isoformat()
    meta: dict[str, Any] = {
        "schema": "p1_station_clock_offsets_v1",
        "window_from": first_day,
        "window_before": before_date,
        "shrink_k": shrink_k,
    }
    days: dict[tuple[str, str], list[tuple[datetime, float]]] = defaultdict(list)
    try:
        conn = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True, timeout=60.0)
        try:
            for station, valid, raw, whole in conn.execute(
                "SELECT station_id, valid_at, raw_text, reported_temperature_c"
                " FROM reported_trajectory_observations"
                " WHERE source_lane='awc' AND valid_at>=? AND valid_at<?",
                (
                    (date.fromisoformat(first_day) - timedelta(days=1)).isoformat(),
                    (date.fromisoformat(before_date) + timedelta(days=1)).isoformat(),
                ),
            ):
                zone = zones.get(str(station))
                if zone is None:
                    continue
                local = datetime.fromisoformat(str(valid).replace("Z", "+00:00")).astimezone(ZoneInfo(zone))
                day = local.date().isoformat()
                if not first_day <= day < before_date:
                    continue
                try:
                    value, _ = official_temperature_c(str(raw), whole_degree_c=float(whole))
                except (TypeError, ValueError, ContractError):
                    value = float(whole)
                days[(str(station), day)].append((local, float(value)))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        meta["state"] = f"UNAVAILABLE:{type(exc).__name__}"
        return {}, meta
    peaks: dict[str, list[float]] = defaultdict(list)
    for (station, _day), points in days.items():
        if len(points) < STATION_PEAK_MIN_REPORTS:
            continue
        top = max(value for _, value in points)
        first = min(when for when, value in points if value == top)
        peaks[station].append(first.hour + first.minute / 60.0)
    everything = [hour for hours in peaks.values() for hour in hours]
    if not everything:
        meta["state"] = "NO_DAYS"
        return {}, meta
    fleet = statistics.median(everything)
    offsets = {}
    for station in sorted(zones):
        hours = peaks.get(station, [])
        middle = statistics.median(hours) if hours else fleet
        shrunk = (len(hours) * middle + shrink_k * fleet) / (len(hours) + shrink_k)
        offsets[station] = round(
            max(-OL.MAX_STATION_CLOCK_OFFSET_H, min(OL.MAX_STATION_CLOCK_OFFSET_H, shrunk - fleet)), 4
        )
    meta.update({
        "state": "OK",
        "fleet_peak_local_h": fleet,
        "station_days": len(everything),
        "days_by_station": {station: len(hours) for station, hours in sorted(peaks.items())},
    })
    return offsets, meta


def fit_g(
    samples: Sequence[Mapping[str, Any]],
    *,
    station_clock_offsets: Mapping[str, float] | None = None,
    station_clock_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """g on these samples; the body the artifact publishes, alpha excluded."""

    fallbacks = fleet_means(samples, OL.FULL_FEATURES)
    risers = [sample for sample in samples if rise_of(sample) is not None]
    offsets = dict(station_clock_offsets or {})
    # Rung one learns on the station's own clock; the continuation keeps the
    # true hours left, as the measurement did.
    phase_samples = [
        {
            **sample,
            "remaining_hours": max(
                0.0,
                float(sample["remaining_hours"])
                + float(offsets.get(str(sample["station_id"]), 0.0)),
            ),
        }
        for sample in samples
    ] if offsets else samples
    clock_fields: dict[str, Any] = (
        {
            OL.STATION_CLOCK_OFFSET_KEY: offsets,
            "station_clock": dict(station_clock_meta or {}),
        }
        if offsets else {}
    )
    return {
        **clock_fields,
        "schema": OL.SCHEMA,
        "rule": OL.RULE,
        "feature_fallbacks": fallbacks,
        "phase_features": list(OL.PHASE_FEATURES),
        "full_features": list(OL.FULL_FEATURES),
        # Charter 156: the fitted intercepts are served read between their
        # bins' midpoints, so the held-out scoring below reads them the same
        # way the resident will.
        "phase": {
            **fit_phase_rung(phase_samples, fallbacks, OL.PHASE_FEATURES),
            "clock": M4.CLOCK_MIDPOINT_LINEAR,
        },
        "continuation_full": fit_continuation(
            risers, OL.FULL_FEATURES, fallbacks
        ),
        "trained_from": min(str(s["event_date"]) for s in samples),
        "trained_through": max(str(s["event_date"]) for s in samples),
        "sample_n": len(samples),
        "riser_sample_n": len(risers),
        "station_day_n": len({
            (s["station_id"], s["event_date"]) for s in samples
        }),
    }


def scorable_rows(
    samples: Sequence[Mapping[str, Any]], roots: RootCache
) -> list[dict[str, Any]]:
    """Each held-out sample with its root, hard bound and settled bucket."""

    rows: list[dict[str, Any]] = []
    for sample in samples:
        view = roots.get(str(sample["root_checkpoint_id"]))
        if view is None:
            continue
        high = float(sample["running_high_c"])
        ordered = sorted(
            view["ordinals"], key=lambda b: (view["ordinals"][b], b)
        )
        excluded = set(
            hard_bound_excluded_buckets(ordered, view["uppers"], high)
        )
        lineage = {"bucket_intervals_c_json": view["intervals_json"]}
        truth = _anchor_bucket_for_temperature(
            float(sample["truth_tmax_c"]), root_lineage=lineage,
            bucket_ordinals=view["ordinals"],
        )
        # The running high's bucket as ``materialize_p1`` reads it: the
        # lowest one the hard bound leaves.
        running = next((b for b in ordered if b not in excluded), None)
        if truth is None or running is None or truth in excluded:
            continue
        rows.append({
            "sample": sample,
            "view": view,
            "excluded": excluded,
            "truth": truth,
            "running": running,
            "high": high,
            "bin": OL.alpha_bin(float(sample["remaining_hours"])),
        })
    return rows


def _log_score(
    prior: Mapping[str, float], evidence: Mapping[str, float] | None,
    excluded: set[str], truth: str, alpha: float,
) -> float | None:
    weighted = {
        bucket: (
            0.0 if bucket in excluded
            else float(value) * (
                1.0 if evidence is None
                else max(0.0, float(evidence[bucket])) ** alpha
            )
        )
        for bucket, value in prior.items()
    }
    total = sum(weighted.values())
    if total <= 1e-12:
        return None
    # Scored as served: under the zero floor ``_posterior`` applies (review
    # 2026-09-19 -- scored without it, alpha was chosen for a distribution
    # nobody serves: -20.72 nat on a case serving scores -6.91).
    served, _ = floor_probabilities(
        {bucket: value / total for bucket, value in weighted.items()}, excluded
    )
    return math.log(served[truth])


def evidence_of(row: Mapping[str, Any], artifact: Mapping[str, Any]) -> dict[str, float]:
    evidence, _ = OL.evidence_by_bucket(
        features_of(row["sample"]), artifact,
        station_id=str(row["sample"]["station_id"]),
        bucket_ordinals=row["view"]["ordinals"],
        bucket_centers_c=row["view"]["centers"],
        running_high_c=row["high"],
        running_high_bucket=row["running"],
    )
    return evidence


def score_split(
    rows: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    incumbent: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Per-bin alpha choice and the arms the veto reads, on held-out rows."""

    per_bin: dict[int, dict[str, Any]] = {
        index: {"weight": 0.0, "root": 0.0, "grid": defaultdict(float),
                "incumbent": 0.0, "n": 0}
        for index in range(len(OL.ALPHA_EDGES_HOURS) + 1)
    }
    for row in rows:
        prior = row["view"]["probabilities"]
        root_score = _log_score(prior, None, row["excluded"], row["truth"], 1.0)
        if root_score is None:
            continue
        evidence = evidence_of(row, candidate)
        scores = {
            alpha: _log_score(prior, evidence, row["excluded"], row["truth"], alpha)
            for alpha in ALPHA_GRID
        }
        if any(value is None for value in scores.values()):
            continue
        incumbent_score = None
        if incumbent is not None:
            incumbent_score = _log_score(
                prior, evidence_of(row, incumbent), row["excluded"],
                row["truth"],
                OL.alpha_for(
                    OL.clock_hours(
                        float(row["sample"]["remaining_hours"]), incumbent,
                        str(row["sample"]["station_id"]),
                    ),
                    incumbent,
                ),
            )
            if incumbent_score is None:
                continue
        weight = float(row["sample"]["station_day_weight"])
        cell = per_bin[row["bin"]]
        cell["weight"] += weight
        cell["n"] += 1
        cell["root"] += weight * root_score
        for alpha, value in scores.items():
            cell["grid"][alpha] += weight * value
        if incumbent_score is not None:
            cell["incumbent"] += weight * incumbent_score
    alphas: list[float] = []
    report: dict[str, Any] = {"bins": []}
    totals = {"weight": 0.0, "root": 0.0, "candidate": 0.0, "incumbent": 0.0}
    for index in sorted(per_bin):
        cell = per_bin[index]
        if cell["weight"] <= 0.0:
            alphas.append(1.0)
            report["bins"].append({"bin": index, "n": 0, "alpha": 1.0})
            continue
        best = max(ALPHA_GRID, key=lambda alpha: cell["grid"][alpha])
        alphas.append(float(best))
        totals["weight"] += cell["weight"]
        totals["root"] += cell["root"]
        totals["candidate"] += cell["grid"][best]
        totals["incumbent"] += cell["incumbent"]
        report["bins"].append({
            "bin": index,
            "n": cell["n"],
            "alpha": float(best),
            "root_nat": cell["root"] / cell["weight"],
            "candidate_nat": cell["grid"][best] / cell["weight"],
            **(
                {"incumbent_nat": cell["incumbent"] / cell["weight"]}
                if incumbent is not None else {}
            ),
        })
    weight = max(totals["weight"], 1e-12)
    report["alpha_by_bin"] = alphas
    report["root_nat"] = totals["root"] / weight
    report["candidate_nat"] = totals["candidate"] / weight
    if incumbent is not None:
        report["incumbent_nat"] = totals["incumbent"] / weight
    report["station_day_weight"] = totals["weight"]
    return report


# -- charter 156: alpha as a curve in the hours left ------------------------
#
# Chosen on the held-out rows by coordinate descent over ALPHA_GRID, one knot
# at a time (OL.ALPHA_KNOTS_HOURS), three passes.  The score the veto reads is
# walked forward and strictly as-of (review 2026-09-19, finding three): each
# held-out day is scored on a curve chosen only on rows whose settled truth
# had been committed before that day's first report.  It used to be cross-
# fitted on alternate days -- the 6th and 8th scoring the 5th and 7th --
# which is a date-grouped cross-validation, not something the resident could
# have done on the morning in question.  The published curve is then chosen
# on every held-out day.
ALPHA_CURVE_PASSES = 3
# Held-out days a forward curve needs behind it before it scores a day.
ALPHA_FORWARD_MIN_DAYS = 4


def alpha_arrays(
    rows: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    incumbent: Mapping[str, Any] | None = None,
) -> dict[str, np.ndarray] | None:
    """Per scorable row: masked prior, evidence, live buckets, truth, clocks.

    ``live`` marks the buckets the served floor lifts (real, not excluded);
    ``incumbent`` is that artifact's own served log score, NaN without one.
    """

    priors, evidences, lives, truths, hours, weights, days = [], [], [], [], [], [], []
    as_ofs, knowns, incumbents = [], [], []
    for row in rows:
        prior = row["view"]["probabilities"]
        if _log_score(prior, None, row["excluded"], row["truth"], 1.0) is None:
            continue
        evidence = evidence_of(row, candidate)
        buckets = sorted(prior)
        p = np.array([0.0 if b in row["excluded"] else float(prior[b]) for b in buckets])
        e = np.array([max(0.0, float(evidence[b])) for b in buckets])
        # The same rows ``score_split`` keeps: none a grid alpha empties.
        if any(float((p * np.power(e, a)).sum()) <= 1e-12 for a in ALPHA_GRID):
            continue
        priors.append(p)
        evidences.append(e)
        lives.append(np.array([b not in row["excluded"] for b in buckets]))
        truths.append(buckets.index(row["truth"]))
        # The alpha curve reads the station's clock, as serving does.
        hours.append(OL.clock_hours(
            float(row["sample"]["remaining_hours"]), candidate,
            str(row["sample"]["station_id"]),
        ))
        weights.append(float(row["sample"]["station_day_weight"]))
        days.append(str(row["sample"]["event_date"]))
        as_ofs.append(float(row["sample"]["as_of_epoch"]))
        knowns.append(float(row["sample"]["truth_known_epoch"]))
        incumbent_score = None
        if incumbent is not None:
            incumbent_score = _log_score(
                prior, evidence_of(row, incumbent), row["excluded"], row["truth"],
                OL.alpha_for(
                    OL.clock_hours(
                        float(row["sample"]["remaining_hours"]), incumbent,
                        str(row["sample"]["station_id"]),
                    ),
                    incumbent,
                ),
            )
        incumbents.append(np.nan if incumbent_score is None else incumbent_score)
    if not priors:
        return None
    width = max(len(p) for p in priors)
    prior_m = np.zeros((len(priors), width))
    evidence_m = np.ones((len(priors), width))
    live_m = np.zeros((len(priors), width), dtype=bool)
    for index, (p, e, live) in enumerate(zip(priors, evidences, lives)):
        prior_m[index, : len(p)] = p
        evidence_m[index, : len(e)] = e
        live_m[index, : len(live)] = live
    return {
        "prior": prior_m, "evidence": evidence_m, "live": live_m,
        "truth": np.array(truths), "hours": np.array(hours),
        "weight": np.array(weights), "day": np.array(days),
        "as_of": np.array(as_ofs), "known_at": np.array(knowns),
        "incumbent": np.array(incumbents),
    }


def _row_scores(arrays: Mapping[str, np.ndarray], alpha: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Served log scores of the masked rows at ``alpha`` (one per row).

    The zero floor is ``floor_probabilities`` row by row -- lift every live
    bucket under the floor and renormalise, only where one is under it --
    and ``test_fit_p1_observation_likelihood`` holds the two equal.
    """

    prior = arrays["prior"][mask]
    live = arrays["live"][mask]
    weighted = prior * np.power(arrays["evidence"][mask], alpha[:, None])
    posterior = weighted / weighted.sum(axis=1, keepdims=True)
    under = (live & (posterior < PROBABILITY_MODEL_ZERO_FLOOR)).any(axis=1)
    lifted = np.where(live, np.maximum(posterior, PROBABILITY_MODEL_ZERO_FLOOR), 0.0)
    served = np.where(
        under[:, None], lifted / lifted.sum(axis=1, keepdims=True), posterior
    )
    truth = arrays["truth"][mask]
    return np.log(served[np.arange(len(truth)), truth])


def fit_alpha_curve(arrays: Mapping[str, np.ndarray], mask: np.ndarray) -> list[float]:
    knots = np.array(OL.ALPHA_KNOTS_HOURS)
    values = np.ones(len(knots))
    hours = arrays["hours"][mask]
    weight = arrays["weight"][mask]

    def total(candidate: np.ndarray) -> float:
        alpha = np.interp(hours, knots, candidate)
        return float((weight * _row_scores(arrays, alpha, mask)).sum())

    current = total(values)
    for _ in range(ALPHA_CURVE_PASSES):
        for index in range(len(knots)):
            best_value, best_score = values[index], current
            for grid_alpha in ALPHA_GRID:
                trial = values.copy()
                trial[index] = grid_alpha
                score = total(trial)
                if score > best_score + 1e-12:
                    best_value, best_score = grid_alpha, score
            values[index] = best_value
            current = best_score
    return [float(value) for value in values]


def score_alpha_curve(arrays: Mapping[str, np.ndarray]) -> dict[str, Any]:
    """Forward, as-of score of the curve, and the curve chosen on every day."""

    knots = np.array(OL.ALPHA_KNOTS_HOURS)
    count = len(arrays["truth"])
    everything = np.ones(count, dtype=bool)
    forward = np.full(count, np.nan)
    forward_values: dict[str, list[float]] = {}
    for day in sorted(set(arrays["day"].tolist())):
        rows = arrays["day"] == day
        # What the resident could have fitted on that morning: rows whose
        # truth had been committed before the day's first report.
        train = arrays["known_at"] <= float(arrays["as_of"][rows].min())
        if len(set(arrays["day"][train].tolist())) < ALPHA_FORWARD_MIN_DAYS:
            continue
        values = fit_alpha_curve(arrays, train)
        forward_values[day] = values
        forward[rows] = _row_scores(
            arrays, np.interp(arrays["hours"][rows], knots, values), rows
        )
    scored = ~np.isnan(forward)
    weight = arrays["weight"]
    root = _row_scores(arrays, np.zeros(count), everything)
    chosen = fit_alpha_curve(arrays, everything)
    in_sample = _row_scores(arrays, np.interp(arrays["hours"], knots, chosen), everything)
    total_weight = max(float(weight.sum()), 1e-12)
    scored_weight = max(float(weight[scored].sum()), 1e-12)
    # The incumbent is compared only where it scored every forward row.
    incumbent_comparable = bool(
        scored.any() and not np.isnan(arrays["incumbent"][scored]).any()
    )
    return {
        "knots_hours": [float(k) for k in knots],
        "values": chosen,
        "forward_values_by_day": forward_values,
        "rows": count,
        "forward_rows": int(scored.sum()),
        "forward_days": len(forward_values),
        "forward_min_train_days": ALPHA_FORWARD_MIN_DAYS,
        "forward_nat": (
            float((weight[scored] * forward[scored]).sum()) / scored_weight
            if scored.any() else None
        ),
        "forward_root_nat": (
            float((weight[scored] * root[scored]).sum()) / scored_weight
            if scored.any() else None
        ),
        "forward_incumbent_nat": (
            float((weight[scored] * arrays["incumbent"][scored]).sum()) / scored_weight
            if incumbent_comparable else None
        ),
        "root_nat": float((weight * root).sum()) / total_weight,
        "in_sample_nat": float((weight * in_sample).sum()) / total_weight,
    }


def _epoch(text: Any) -> float:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def load_truth_known_at(target: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """When each station-day's settled truth became knowable (epoch seconds).

    The latest commit among its accepted revisions -- the builder labels a
    day with its latest revision, so that revision's commit is the moment
    the label existed (``label_knowability``).
    """

    return {
        (str(station), str(day)): _epoch(committed)
        for station, day, committed in target.execute(
            "SELECT station_id, local_date, MAX(canonical_committed_at_utc)"
            " FROM calibration_station_daily_high_truth"
            " WHERE qc_status='ACCEPTED' AND canonical_committed_at_utc IS NOT NULL"
            " GROUP BY station_id, local_date"
        )
    }


def label_samples(
    samples: Sequence[Mapping[str, Any]],
    roots: RootCache,
    known_at: Mapping[tuple[str, str], float],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Each sample with its served label (``serving_label``) and its two clocks."""

    labelled: list[dict[str, Any]] = []
    dropped: dict[str, int] = defaultdict(int)
    for sample in samples:
        known = known_at.get((str(sample["station_id"]), str(sample["event_date"])))
        if known is None:
            dropped["truth_commit_unknown"] += 1
            continue
        view = roots.get(str(sample["root_checkpoint_id"]))
        if view is None:
            dropped["root_missing"] += 1
            continue
        label = serving_label(sample, view)
        if label is None:
            dropped["settled_bucket_unplaced"] += 1
            continue
        labelled.append({
            **sample, **label,
            "truth_known_epoch": known,
            "as_of_epoch": _epoch(sample["as_of_utc"]),
        })
    return labelled, dict(dropped)


def split_as_of(
    samples: Sequence[Mapping[str, Any]], days: Sequence[str]
) -> tuple[str, list[Mapping[str, Any]], list[Mapping[str, Any]], float]:
    """``(cut, train, held, held_start)``: the later 30% of days held out.

    Strict as-of: the split's g trains only on days before the cut whose truth
    had been committed before the held-out window's first report -- the day
    before the cut, and across time zones often the one before that, had not.
    """

    cut = days[int(len(days) * (1.0 - HOLDOUT_SHARE))]
    held = [s for s in samples if str(s["event_date"]) >= cut]
    held_start = min(float(s["as_of_epoch"]) for s in held)
    train = [
        s for s in samples
        if str(s["event_date"]) < cut and float(s["truth_known_epoch"]) <= held_start
    ]
    return cut, train, held, held_start


def load_samples(path: Path, *, since: str | None, through: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    clause, params = "WHERE event_date<=?", [through]
    if since:
        clause += " AND event_date>=?"
        params.append(since)
    try:
        return [
            dict(row) for row in conn.execute(
                f"SELECT * FROM metar_phase_hazard_sample {clause}"
                " ORDER BY event_date, station_id, as_of_utc", params,
            )
        ]
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--samples", default=str(OL.SAMPLES_PATH))
    parser.add_argument("--target-db", default=str(
        ROOT / "data" / "bayes_basket_target_ssot.db"
    ))
    parser.add_argument("--through", required=True)
    parser.add_argument("--since", default=None)
    parser.add_argument("--out", default=str(OL.ARTIFACT_PATH))
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    parser.add_argument("--fitted-at-utc", default=None)
    parser.add_argument("--archive-db", default=str(ROOT / "data" / "archive.db"))
    parser.add_argument(
        "--force", action="store_true",
        help="write even when the veto refuses (first deployment only)",
    )
    args = parser.parse_args()

    raw_samples = load_samples(
        Path(args.samples), since=args.since, through=args.through
    )
    target = sqlite3.connect(f"file:{args.target_db}?mode=ro", uri=True)
    roots = RootCache(target)
    samples, unlabelled = label_samples(
        raw_samples, roots, load_truth_known_at(target)
    )
    days = sorted({str(sample["event_date"]) for sample in samples})
    if len(samples) < MIN_SAMPLES or len(days) < MIN_DAYS:
        print(json.dumps({"state": "REFUSED_THIN", "samples": len(samples),
                          "days": len(days), "unlabelled": unlabelled}))
        return 3
    cut, train, held, held_start = split_as_of(samples, days)
    rows = scorable_rows(held, roots)
    zones = {
        str(sample["station_id"]): str(sample["settlement_timezone"])
        for sample in samples
    }
    # The held-out split is scored with offsets known before it began; the
    # published body carries the latest window.
    split_offsets, split_meta = station_peak_offsets(
        Path(args.archive_db), zones=zones, before_date=cut,
    )
    full_offsets, full_meta = station_peak_offsets(
        Path(args.archive_db), zones=zones,
        before_date=(date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat(),
    )
    split_g = fit_g(
        train, station_clock_offsets=split_offsets, station_clock_meta=split_meta,
    )
    split_g["alpha_by_bin"] = [1.0] * (len(OL.ALPHA_EDGES_HOURS) + 1)
    out_path = Path(args.out)
    incumbent = OL.load_artifact(out_path)
    incumbent_scorable = (
        incumbent is not None
        and str(incumbent.get("trained_through") or "9999") < cut
    )
    split = score_split(
        rows, split_g, incumbent if incumbent_scorable else None
    )
    arrays = alpha_arrays(
        rows, split_g, incumbent if incumbent_scorable else None
    )
    target.close()
    if arrays is None:
        print(json.dumps({"state": "REFUSED_NO_SCORABLE_ROWS"}))
        return 3
    curve = score_alpha_curve(arrays)
    # The veto reads what will serve, as the resident could have known it:
    # the curve walked forward over the held-out days, and the root and the
    # incumbent on exactly the rows it scored.  The three steps stay
    # published beside it (in-sample, all held rows) for the record.
    split["candidate_nat_three_bins_in_sample"] = split["candidate_nat"]
    split["root_nat_all_held_rows"] = split["root_nat"]
    split["candidate_nat"] = curve["forward_nat"]
    split["root_nat"] = curve["forward_root_nat"]
    if incumbent_scorable:
        split["incumbent_nat_all_held_rows"] = split.get("incumbent_nat")
        split["incumbent_nat"] = curve["forward_incumbent_nat"]
    veto: list[str] = []
    if curve["forward_nat"] is None:
        print(json.dumps({"state": "REFUSED_NO_FORWARD_DAYS",
                          "held_days": len(set(arrays["day"].tolist()))}))
        return 3
    if split["candidate_nat"] - split["root_nat"] < args.margin:
        veto.append(
            f"does_not_beat_root_by_margin:{split['candidate_nat'] - split['root_nat']:+.4f}"
        )
    if incumbent_scorable and split["incumbent_nat"] is not None and (
        split["candidate_nat"] < split["incumbent_nat"] - args.margin
    ):
        veto.append(
            f"worse_than_incumbent:{split['candidate_nat'] - split['incumbent_nat']:+.4f}"
        )
    body = fit_g(
        samples, station_clock_offsets=full_offsets, station_clock_meta=full_meta,
    )
    body["alpha_by_bin"] = split["alpha_by_bin"]
    body["alpha_edges_hours"] = list(OL.ALPHA_EDGES_HOURS)
    body["alpha_knots_hours"] = curve["knots_hours"]
    body["alpha_knot_values"] = curve["values"]
    body["alpha_grid"] = list(ALPHA_GRID)
    body["holdout"] = {
        "from": cut,
        "through": days[-1],
        "scored_rows": len(rows),
        "training_cut": "strict_as_of_truth_commit_v1",
        "split_g_truth_known_by": _iso(datetime.fromtimestamp(held_start, timezone.utc)),
        "split_g_train_samples": len(train),
        "labels": {
            "rule": "served_rise_rung_censored_open_top_v1",
            "samples": len(samples),
            "unlabelled": unlabelled,
            "rise_rate": sum(int(s["serving_rise"]) for s in samples) / len(samples),
            "censored_open_top": sum(
                1 for s in samples if s["serving_rise"] and s["serving_censored"]
            ),
        },
        **{key: value for key, value in split.items() if key != "alpha_by_bin"},
        "alpha_curve": {key: value for key, value in curve.items() if key != "knots_hours"},
        # Charter 157: the offsets the held-out split was scored with.
        "station_clock_split": {
            key: split_meta.get(key)
            for key in ("state", "window_from", "window_before", "fleet_peak_local_h", "station_days")
        },
        "incumbent_scored": bool(incumbent_scorable),
        "margin": args.margin,
        "veto": veto,
    }
    body["fitted_at_utc"] = args.fitted_at_utc or _iso(datetime.now(timezone.utc))
    body["content_sha256"] = content_sha256(body)
    written = False
    if not veto or args.force:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        staging = out_path.with_suffix(out_path.suffix + ".new")
        staging.write_text(json.dumps(body, indent=2, sort_keys=True))
        os.replace(staging, out_path)
        written = True
    print(json.dumps({
        "schema": OL.SCHEMA,
        "state": "WRITTEN" if written else "VETOED",
        "trained_through": body["trained_through"],
        "sample_n": body["sample_n"],
        "alpha_by_bin": body["alpha_by_bin"],
        "alpha_curve": [round(v, 3) for v in body["alpha_knot_values"]],
        "holdout_from": cut,
        "root_nat": round(split["root_nat"], 4),
        "candidate_nat": round(split["candidate_nat"], 4),
        **(
            {"incumbent_nat": round(split["incumbent_nat"], 4)}
            if incumbent_scorable else {}
        ),
        "veto": veto,
        "out": str(out_path),
    }, ensure_ascii=False))
    # A veto is the lane working, not failing: the served file stands.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
