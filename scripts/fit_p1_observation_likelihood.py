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

4. **direct update** (charter 161) -- the ``direct_update`` block: the
   update exp f fitted directly on the final P1 (``p1_direct_update``), with
   its own forward, as-of gate against the root and against g**alpha on the
   same held-out rows.  While it says SERVE, it serves in g**alpha's place.

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
import random
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
from bayes_basket_target import p1_direct_update as DU  # noqa: E402
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
    as_ofs, knowns, incumbents, kept = [], [], [], []
    for position, row in enumerate(rows):
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
        kept.append(position)
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
        "incumbent": np.array(incumbents), "row_index": np.array(kept),
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
    by_day = {
        day: (
            float((weight[m] * forward[m]).sum() / weight[m].sum()),
            float((weight[m] * root[m]).sum() / weight[m].sum()),
        )
        for day in forward_values
        for m in [arrays["day"] == day]
    }
    return {
        "knots_hours": [float(k) for k in knots],
        "values": chosen,
        "forward_values_by_day": forward_values,
        "forward_nat_by_day": {day: v[0] for day, v in by_day.items()},
        "forward_root_nat_by_day": {day: v[1] for day, v in by_day.items()},
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


# -- charter 161: the update fitted directly on the final P1 -----------------
#
# P1(k) ~ root(k) M(k) exp f(k), f read by ``p1_direct_update``.  The design
# below is built from that module's own functions (bucket bases, root
# features); only x is assembled in numpy, and a test holds it equal to the
# served ``state_vector`` row by row.  Ridge toward f = 0 (the root); lambda
# chosen on the last ``DIRECT_INNER_DAYS`` training days.  Its gate is its
# own forward walk over the held-out days, strictly as-of, beside alpha's.
DIRECT_LAMBDAS = (0.3, 1.0, 3.0)
DIRECT_INNER_DAYS = 3
DIRECT_MAX_ITER = 400


def direct_design(
    rows: Sequence[Mapping[str, Any]], clock_artifact: Mapping[str, Any]
) -> dict[str, np.ndarray]:
    """Per row: masked prior, live, truth, the three bucket bases, raw state, clocks."""

    width = max(len(row["view"]["ordinals"]) for row in rows)
    count = len(rows)
    prior = np.zeros((count, width)); live = np.zeros((count, width), dtype=bool)
    rise = np.zeros((count, width)); slope = np.zeros((count, width))
    top = np.zeros((count, width)); truth = np.zeros(count, dtype=int)
    raw = np.full((count, len(DU.STATE_COLUMNS)), np.nan)
    hours = np.zeros(count); weight = np.zeros(count)
    day = np.empty(count, dtype=object); as_of = np.zeros(count); known = np.zeros(count)
    # Charter 240: each rung's centre, for the ladder CRPS guardrail; padded
    # columns repeat the last centre so they add no distance.
    centres = np.zeros((count, width))
    for i, row in enumerate(rows):
        view = row["view"]; ordinals = view["ordinals"]
        order = sorted(ordinals, key=lambda b: (ordinals[b], b))
        row_centres = [float(view["centers"][b]) for b in order]
        centres[i, :len(row_centres)] = row_centres
        if len(row_centres) < width:
            centres[i, len(row_centres):] = row_centres[-1]
        bases = DU.bucket_bases(
            bucket_ordinals=ordinals, bucket_centers_c=view["centers"],
            bucket_upper_bounds_c=view["uppers"], running_high_c=row["high"],
            running_high_bucket=row["running"], hard_excluded_buckets=sorted(row["excluded"]),
        )
        for j, bucket in enumerate(order):
            rung, r, s, o = bases[bucket]
            if rung < 0:
                continue
            live[i, j] = True
            prior[i, j] = float(view["probabilities"][bucket])
            rise[i, j], slope[i, j], top[i, j] = r, s, o
        truth[i] = order.index(row["truth"])
        sample = row["sample"]
        features = features_of(sample)
        mass = float((prior[i] * live[i]).sum())
        if mass > 0.0:
            root = DU.root_features(
                view["probabilities"], bases,
                bucket_centers_c=view["centers"], running_high_c=row["high"],
            )
            raw[i] = [
                np.nan if features.get(name) is None else float(features[name])
                for name in DU.FEATURES
            ] + [root[name] for name in DU.ROOT_FEATURES]
        hours[i] = OL.clock_hours(
            float(sample["remaining_hours"]), clock_artifact, str(sample["station_id"])
        )
        weight[i] = float(sample["station_day_weight"]); day[i] = str(sample["event_date"])
        as_of[i] = float(sample["as_of_epoch"]); known[i] = float(sample["truth_known_epoch"])
    return {
        "prior": prior, "live": live, "rise": rise, "slope": slope, "top": top,
        "truth": truth, "raw": raw, "hours": hours, "weight": weight, "day": day,
        "as_of": as_of, "known_at": known, "centres": centres,
        "usable": (prior * live).sum(1) > 1e-12,
    }


def direct_scaling(
    design: Mapping[str, np.ndarray], idx: np.ndarray, gate: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Fallbacks, means and scales of the state on the training rows ``idx``.

    ``gate`` (a v2 ``clock_gate``) rides in the scaling because x is built
    from it, exactly as ``DU.state_vector`` reads it from the block.
    """

    raw = design["raw"][idx]
    fallbacks = {
        name: float(np.nanmean(raw[:, k])) if np.isfinite(raw[:, k]).any() else 0.0
        for k, name in enumerate(DU.FEATURES)
    }
    filled = _direct_filled(raw, fallbacks)
    return {
        "fallbacks": fallbacks,
        "mean": [float(v) for v in filled.mean(0)],
        "scale": [float(v) for v in filled.std(0) + 1e-9],
        **({"clock_gate": dict(gate)} if gate else {}),
    }


def _direct_filled(raw: np.ndarray, fallbacks: Mapping[str, float]) -> np.ndarray:
    filled = raw.copy()
    for k, name in enumerate(DU.FEATURES):
        filled[np.isnan(filled[:, k]), k] = fallbacks[name]
    return filled


def direct_x(design: Mapping[str, np.ndarray], idx: np.ndarray, scaling: Mapping[str, Any]) -> np.ndarray:
    """x for rows ``idx`` -- ``DU.state_vector`` in numpy (a test holds them equal)."""

    standard = (
        _direct_filled(design["raw"][idx], scaling["fallbacks"]) - np.array(scaling["mean"])
    ) / np.array(scaling["scale"])
    hours = design["hours"][idx]
    knots = np.array(DU.KNOTS_HOURS)
    hats = np.stack(
        [np.interp(hours, knots, np.eye(len(knots))[k]) for k in range(len(knots))], 1
    )
    late, mid = clock_gates_np(hours, DU.gate_half_width(scaling))
    return np.hstack([
        np.ones((len(idx), 1)), hats, standard,
        late[:, None] * standard, mid[:, None] * standard,
    ])


def clock_gates_np(hours: np.ndarray, width: float) -> tuple[np.ndarray, np.ndarray]:
    """``DU.clock_gates`` over an array (a test holds them equal)."""

    if width <= 0.0:
        late = (hours < DU.LATE_BELOW_HOURS).astype(float)
        mid = ((hours >= DU.LATE_BELOW_HOURS) & (hours < DU.MID_BELOW_HOURS)).astype(float)
        return late, mid
    below_late = np.clip((DU.LATE_BELOW_HOURS + width - hours) / (2.0 * width), 0.0, 1.0)
    below_mid = np.clip((DU.MID_BELOW_HOURS + width - hours) / (2.0 * width), 0.0, 1.0)
    return below_late, below_mid - below_late


def _direct_parts(theta: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray, float, float]:
    return theta[:width], theta[width:2 * width], float(theta[-2]), float(theta[-1])


def _direct_f(design, idx, x, theta) -> np.ndarray:
    t_rise, t_slope, t_sq, t_top = _direct_parts(theta, x.shape[1])
    slope = design["slope"][idx]
    return (
        design["rise"][idx] * (x @ t_rise)[:, None] + slope * (x @ t_slope)[:, None]
        + slope * slope * t_sq + design["top"][idx] * t_top
    )


def _direct_posterior(design, idx, x, theta) -> np.ndarray:
    live = design["live"][idx]
    z = np.where(
        live,
        np.log(np.where(live, np.maximum(design["prior"][idx], 1e-300), 1.0))
        + _direct_f(design, idx, x, theta),
        -np.inf,
    )
    z = z - z.max(1, keepdims=True)
    p = np.where(live, np.exp(z), 0.0)
    return p / p.sum(1, keepdims=True)


def _direct_loss(theta, design, idx, x, lam):
    p = _direct_posterior(design, idx, x, theta)
    rows = np.arange(len(idx)); truth = design["truth"][idx]; w = design["weight"][idx]
    loss = -(w * np.log(np.maximum(p[rows, truth], 1e-300))).sum() + 0.5 * lam * (theta ** 2).sum()
    g = p.copy(); g[rows, truth] -= 1.0; g *= w[:, None]
    slope = design["slope"][idx]
    grad = np.concatenate([
        x.T @ (g * design["rise"][idx]).sum(1),
        x.T @ (g * slope).sum(1),
        [(g * slope * slope).sum(), (g * design["top"][idx]).sum()],
    ])
    return loss, grad + lam * theta


def direct_fit(design, idx, scaling, lam, start=None) -> np.ndarray:
    x = direct_x(design, idx, scaling)
    theta0 = np.zeros(2 * x.shape[1] + 2) if start is None else start
    from scipy.optimize import minimize  # noqa: PLC0415 - only the refit needs it

    result = minimize(
        _direct_loss, theta0, args=(design, idx, x, lam), jac=True,
        method="L-BFGS-B", options={"maxiter": DIRECT_MAX_ITER},
    )
    return result.x


def direct_scores(design, idx, scaling, theta) -> np.ndarray:
    """Served log scores (zero floor as ``_row_scores``) of rows ``idx``."""

    p = _direct_posterior(design, idx, direct_x(design, idx, scaling), theta)
    live = design["live"][idx]
    under = (live & (p < PROBABILITY_MODEL_ZERO_FLOOR)).any(axis=1)
    lifted = np.where(live, np.maximum(p, PROBABILITY_MODEL_ZERO_FLOOR), 0.0)
    served = np.where(under[:, None], lifted / lifted.sum(axis=1, keepdims=True), p)
    return np.log(served[np.arange(len(idx)), design["truth"][idx]])


def _served_vectors(p: np.ndarray, live: np.ndarray) -> np.ndarray:
    under = (live & (p < PROBABILITY_MODEL_ZERO_FLOOR)).any(axis=1)
    lifted = np.where(live, np.maximum(p, PROBABILITY_MODEL_ZERO_FLOOR), 0.0)
    return np.where(under[:, None], lifted / lifted.sum(axis=1, keepdims=True), p)


def _guard_scores(served: np.ndarray, truth: np.ndarray, centres: np.ndarray) -> np.ndarray:
    """(Brier, ladder CRPS) per row of served vectors -- charter 240's guardrails."""

    rows = np.arange(len(truth))
    onehot = np.zeros_like(served); onehot[rows, truth] = 1.0
    brier = ((served - onehot) ** 2).sum(axis=1)
    cumulative = np.cumsum(served, axis=1)[:, :-1]
    observed = (np.arange(served.shape[1] - 1)[None, :] >= truth[:, None]).astype(float)
    crps = (((cumulative - observed) ** 2) * np.diff(centres, axis=1)).sum(axis=1)
    return np.stack([brier, crps], axis=1)


def direct_guard_deltas(design, idx, scaling, theta) -> np.ndarray:
    """Per row (posterior minus root) of (Brier, CRPS), both served vectors."""

    live = design["live"][idx]; truth = design["truth"][idx]; centres = design["centres"][idx]
    post = _served_vectors(_direct_posterior(design, idx, direct_x(design, idx, scaling), theta), live)
    prior = np.where(live, design["prior"][idx], 0.0)
    root = _served_vectors(prior / prior.sum(axis=1, keepdims=True), live)
    return _guard_scores(post, truth, centres) - _guard_scores(root, truth, centres)


def direct_choose(
    design, idx, start=None, *, gate=None, fixed_lambda=None
) -> tuple[float, dict[str, Any], np.ndarray]:
    """``(lambda, scaling, theta)`` on rows ``idx``, lambda by its last days.

    ``fixed_lambda`` skips the search (the candidate borrows the served
    block's choice for the same day, so its walk costs one fit per day).
    """

    if fixed_lambda is not None:
        scaling = direct_scaling(design, idx, gate)
        return fixed_lambda, scaling, direct_fit(design, idx, scaling, fixed_lambda, start=start)
    days = sorted(set(design["day"][idx].tolist()))
    tail = set(days[-DIRECT_INNER_DAYS:])
    in_tail = np.isin(design["day"][idx], list(tail))
    val = idx[in_tail]
    inner = idx[~in_tail]
    best = DIRECT_LAMBDAS[-1]
    if len(val) and len(inner):
        inner = inner[design["known_at"][inner] <= design["as_of"][val].min()]
    if len(val) and len(inner):
        scaling = direct_scaling(design, inner, gate)
        best_score, theta = -np.inf, start
        for lam in DIRECT_LAMBDAS:
            theta = direct_fit(design, inner, scaling, lam, start=theta)
            w = design["weight"][val]
            score = float((w * direct_scores(design, val, scaling, theta)).sum() / w.sum())
            if score > best_score:
                best, best_score = lam, score
    scaling = direct_scaling(design, idx, gate)
    return best, scaling, direct_fit(design, idx, scaling, best, start=start)


# The candidate clock gate (schema v2).  One hour either side of each edge:
# the smallest ramp that removes the step while leaving the regimes where the
# v1 fit put them.  Fitted only when asked (``--candidate-out``), after the
# served file is written, into its own file; never served from here.
CANDIDATE_CLOCK_GATE = {"shape": DU.CLOCK_GATE_SHAPE, "half_width_hours": 1.0}
CANDIDATE_SCHEMA = "p1_direct_update_candidate_v1"
# The ramps sit on [5, 7] and [9, 11]; the rest is where v1 and v2 agree.
CANDIDATE_CLOCK_BINS_HOURS = (0.0, 5.0, 7.0, 9.0, 11.0, 14.0, float("inf"))


def fit_direct_block(
    samples: Sequence[Mapping[str, Any]],
    roots: RootCache,
    *,
    split_g: Mapping[str, Any],
    body: Mapping[str, Any],
    arrays: Mapping[str, np.ndarray],
    held_rows: Sequence[Mapping[str, Any]],
    curve: Mapping[str, Any],
    margin: float,
    keep_walk: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
    """The ``direct_update`` block: final fit, and its forward walk as the gate.

    The walk scores the same held-out rows, on the same days, as alpha's
    forward walk: each day on a fit to every row whose truth was committed
    before the day's first report.  SERVE when it beats the root by
    ``margin`` and is not worse than g**alpha by more than ``margin``.

    ``keep_walk`` also returns what ``fit_direct_candidate`` needs to walk a
    candidate on exactly the same rows, days and lambdas.
    """

    everything = scorable_rows(samples, roots)
    design = direct_design(everything, split_g)
    usable = np.where(design["usable"])[0]
    held_keys = {
        (held_rows[i]["sample"]["station_id"], held_rows[i]["sample"]["as_of_utc"])
        for i in arrays["row_index"].tolist()
    }
    position = {
        (row["sample"]["station_id"], row["sample"]["as_of_utc"]): i
        for i, row in enumerate(everything)
    }
    scored_by_day: dict[str, tuple[float, float]] = {}
    guard_by_day: dict[str, tuple[float, float]] = {}
    lambdas: dict[str, float] = {}
    walk_days: list[tuple[str, np.ndarray, np.ndarray, float, np.ndarray]] = []
    theta = None
    for day in sorted(curve["forward_nat_by_day"]):
        rows_d = np.array(sorted(
            position[key] for key in held_keys
            if key in position and str(everything[position[key]]["sample"]["event_date"]) == day
        ))
        rows_d = rows_d[design["usable"][rows_d]]
        start = float(design["as_of"][rows_d].min())
        train = usable[design["known_at"][usable] <= start]
        lam, scaling, theta = direct_choose(design, train, start=theta)
        w = design["weight"][rows_d]
        scored_by_day[day] = (
            float((w * direct_scores(design, rows_d, scaling, theta)).sum() / w.sum()),
            curve["forward_nat_by_day"][day],
        )
        guard = direct_guard_deltas(design, rows_d, scaling, theta)
        guard_by_day[day] = (
            float((w * guard[:, 0]).sum() / w.sum()), float((w * guard[:, 1]).sum() / w.sum())
        )
        lambdas[day] = lam
        if keep_walk:
            walk_days.append((day, train, rows_d, lam, theta.copy()))
    gains_root = [scored_by_day[d][0] - curve["forward_root_nat_by_day"][d] for d in scored_by_day]
    gains_alpha = [scored_by_day[d][0] - scored_by_day[d][1] for d in scored_by_day]
    state, reason = _direct_gate(gains_root, gains_alpha, margin)
    # Charter 240: the primary is the log score above; the bucket Brier and the
    # ladder CRPS (posterior minus root, positive is worse) are guardrails,
    # each day-block lower bound not above zero.
    guards = {
        name: _bootstrap_mean([guard_by_day[d][k] for d in scored_by_day])
        for k, name in enumerate(("brier", "crps"))
    }
    guard_low = {
        name: (draws[int(0.025 * len(draws))] if draws else None)
        for name, draws in guards.items()
    }
    if state == DU.GATE_SERVE:
        for name, low in guard_low.items():
            if low is None or low > 0.0:
                state, reason = "IDENTITY", f"{name}_guard:{low}"
                break
    boot = _bootstrap_mean(gains_alpha)
    # The served block: every settled row, the served clock (the body's).
    final_design = direct_design(everything, body)
    final_rows = np.where(final_design["usable"])[0]
    lam, scaling, theta = direct_choose(final_design, final_rows, start=theta)
    served = _direct_block_record(
        samples, final_rows, lam, scaling, theta, state=state, veto=reason,
        walk_forward={
            "training_cut": "strict_as_of_truth_commit_v1",
            "days": len(scored_by_day),
            "direct_nat_by_day": {d: v[0] for d, v in scored_by_day.items()},
            "alpha_nat_by_day": {d: v[1] for d, v in scored_by_day.items()},
            "lambda_by_day": lambdas,
            "gain_vs_root_nat": statistics.mean(gains_root) if gains_root else None,
            "guardrails": {
                "rule": "logscore_primary_crps_brier_guardrail_day_bootstrap_v1",
                **{f"{name}_delta_by_day": {d: guard_by_day[d][k] for d in scored_by_day}
                   for k, name in enumerate(("brier", "crps"))},
                **{f"{name}_ci_low": low for name, low in guard_low.items()},
            },
            "gain_vs_alpha_nat": statistics.mean(gains_alpha) if gains_alpha else None,
            "gain_vs_alpha_ci": [boot[100], boot[3899]] if boot else None,
            "margin": margin,
        },
    )
    if not keep_walk:
        return served
    return served, {
        "samples": samples, "design": design, "walk_days": walk_days,
        "scored_by_day": scored_by_day, "curve": curve, "margin": margin,
        "final_design": final_design, "final_rows": final_rows,
        "final_lambda": lam, "final_theta": theta,
    }


def fit_direct_candidate(walk: Mapping[str, Any], gate: Mapping[str, Any]) -> dict[str, Any]:
    """The v2 candidate, walked on the served walk's rows, days and lambdas.

    Each day warm starts from the served theta of that day and fits once at
    the served lambda, so the two differ only in the clock gate.  State
    SHADOW; the record says whether it would pass the served block's gate
    and how it scored against the served block day by day.
    """

    design = walk["design"]; curve = walk["curve"]; served = walk["scored_by_day"]
    margin = walk["margin"]
    candidate_by_day: dict[str, float] = {}
    edges = CANDIDATE_CLOCK_BINS_HOURS
    by_bin = [[0.0, 0.0, 0.0, 0] for _ in range(len(edges) - 1)]
    for day, train, rows_d, lam, theta in walk["walk_days"]:
        _, scaling, fitted = direct_choose(
            design, train, start=theta, gate=gate, fixed_lambda=lam,
        )
        w = design["weight"][rows_d]
        mine = direct_scores(design, rows_d, scaling, fitted)
        candidate_by_day[day] = float((w * mine).sum() / w.sum())
        # Where the difference lives: the served walk's own rows re-scored
        # under its own scaling (``direct_choose`` ends on exactly this one).
        theirs = direct_scores(design, rows_d, direct_scaling(design, train), theta)
        position = np.searchsorted(edges, design["hours"][rows_d], side="right") - 1
        for k in range(len(by_bin)):
            inside = position == k
            by_bin[k][0] += float(w[inside].sum())
            by_bin[k][1] += float((w[inside] * theirs[inside]).sum())
            by_bin[k][2] += float((w[inside] * mine[inside]).sum())
            by_bin[k][3] += int(inside.sum())
    c_root = [candidate_by_day[d] - curve["forward_root_nat_by_day"][d] for d in candidate_by_day]
    c_alpha = [candidate_by_day[d] - served[d][1] for d in candidate_by_day]
    c_served = [candidate_by_day[d] - served[d][0] for d in candidate_by_day]
    would, would_reason = _direct_gate(c_root, c_alpha, margin)
    boot = _bootstrap_mean(c_served)
    lam = walk["final_lambda"]
    _, scaling, theta = direct_choose(
        walk["final_design"], walk["final_rows"], start=walk["final_theta"],
        gate=gate, fixed_lambda=lam,
    )
    return _direct_block_record(
        walk["samples"], walk["final_rows"], lam, scaling, theta,
        state=DU.GATE_SHADOW, veto="", walk_forward={
            "training_cut": "strict_as_of_truth_commit_v1",
            "lambda_rule": "served_walk_lambda_by_day",
            "days": len(candidate_by_day),
            "direct_nat_by_day": candidate_by_day,
            "served_nat_by_day": {d: served[d][0] for d in candidate_by_day},
            "gain_vs_root_nat": statistics.mean(c_root) if c_root else None,
            "gain_vs_alpha_nat": statistics.mean(c_alpha) if c_alpha else None,
            "gain_vs_served_nat": statistics.mean(c_served) if c_served else None,
            "gain_vs_served_ci": [boot[100], boot[3899]] if boot else None,
            "days_better_than_served": sum(1 for v in c_served if v > 0.0),
            "by_clock_bin": [
                {
                    "from_h": edges[k], "to_h": edges[k + 1], "rows": n,
                    "served_nat": (a / wt if wt > 0.0 else None),
                    "candidate_nat": (b / wt if wt > 0.0 else None),
                }
                for k, (wt, a, b, n) in enumerate(by_bin)
            ],
            "would_pass_served_gate": would == DU.GATE_SERVE,
            "would_pass_reason": would_reason,
            "margin": margin,
        },
    )


def _direct_gate(
    gains_root: Sequence[float], gains_alpha: Sequence[float], margin: float
) -> tuple[str, str]:
    if not gains_root:
        return "IDENTITY", "no_forward_days"
    mean_root = statistics.mean(gains_root); mean_alpha = statistics.mean(gains_alpha)
    if mean_root >= margin and mean_alpha >= -margin:
        return DU.GATE_SERVE, ""
    return "IDENTITY", (
        f"vs_root:{mean_root:+.4f}" if mean_root < margin else f"vs_alpha:{mean_alpha:+.4f}"
    )


def _bootstrap_mean(values: Sequence[float]) -> list[float]:
    rng = random.Random(20260920)
    return sorted(
        statistics.mean(rng.choice(values) for _ in values) for _ in range(4000)
    ) if values else []


def _direct_block_record(
    samples, final_rows, lam, scaling, theta, *, state, veto, walk_forward,
) -> dict[str, Any]:
    width = len(DU.x_layout())
    gate = scaling.get("clock_gate")
    return {
        "schema": DU.SCHEMA_V2 if gate else DU.SCHEMA,
        "rule": DU.RULE_V2 if gate else DU.RULE,
        **({"clock_gate": dict(gate)} if gate else {}),
        "state": state,
        "veto": veto,
        "lambda": lam,
        "x_layout": DU.x_layout(),
        "theta_rise": [float(v) for v in theta[:width]],
        "theta_slope": [float(v) for v in theta[width:2 * width]],
        "theta_slope_sq": float(theta[-2]),
        "theta_open_top": float(theta[-1]),
        "fallbacks": scaling["fallbacks"],
        "mean": scaling["mean"],
        "scale": scaling["scale"],
        "trained_from": min(str(s["event_date"]) for s in samples),
        "trained_through": max(str(s["event_date"]) for s in samples),
        "rows": int(len(final_rows)),
        "walk_forward": walk_forward,
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
        "--candidate-out", default=None,
        help="also fit the continuous-clock candidate into this file (never served)",
    )
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
    if arrays is None:
        target.close()
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
        target.close()
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
    # Charter 161: the directly fitted update, behind its own forward gate.
    walk = None
    direct = fit_direct_block(
        samples, roots, split_g=split_g, body=body, arrays=arrays,
        held_rows=rows, curve=curve, margin=args.margin,
        keep_walk=bool(args.candidate_out),
    )
    if args.candidate_out:
        direct, walk = direct
    body[DU.BLOCK_KEY] = direct
    target.close()
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
        "direct_update": {
            key: body[DU.BLOCK_KEY].get(key) for key in ("state", "veto", "lambda", "rows")
        } | {
            key: body[DU.BLOCK_KEY]["walk_forward"].get(key)
            for key in ("days", "gain_vs_root_nat", "gain_vs_alpha_nat", "gain_vs_alpha_ci")
        },
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
    # The continuous-clock candidate (2026-09-21 review: the v1 step at 10h),
    # only when asked and only after the served file is safely written, into
    # a file of its own that nothing serves.
    if walk is not None:
        candidate = fit_direct_candidate(walk, CANDIDATE_CLOCK_GATE)
        record = {
            "schema": CANDIDATE_SCHEMA,
            "served_artifact_sha256": body["content_sha256"],
            "served_artifact_written": written,
            "fitted_at_utc": body["fitted_at_utc"],
            DU.CANDIDATE_BLOCK_KEY: candidate,
        }
        record["content_sha256"] = content_sha256(record)
        candidate_path = Path(args.candidate_out)
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        staging = candidate_path.with_suffix(candidate_path.suffix + ".new")
        staging.write_text(json.dumps(record, indent=2, sort_keys=True))
        os.replace(staging, candidate_path)
        print(json.dumps({
            "schema": CANDIDATE_SCHEMA,
            "out": str(candidate_path),
            **{
                key: candidate["walk_forward"].get(key)
                for key in (
                    "days", "gain_vs_served_nat", "gain_vs_served_ci",
                    "days_better_than_served", "would_pass_served_gate",
                )
            },
        }, ensure_ascii=False))
    # A veto is the lane working, not failing: the served file stands.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
