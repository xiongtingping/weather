"""The regional models' bias calibration, fitted the way the global models' is.

User ruling 2026-09-18: "把所有区域模型也做偏差校准（和全球模型方式一样）".
Until then the five regional lanes (JMA MSM, NCEP HRRR, NCEP NBM, DWD
ICON-EU, CMA GRAPES) reached P1 as raw curves.  Every global system carries a
``bias_c`` fitted per system x station x cycle x lead, recency-weighted,
refitted every day and applied at every serve; the regional lanes had one
hand-run fit from 2026-09-07 that served nowhere, never saw CMA at all (the
lane landed after it), and was never refitted.  Measured the day of the
ruling, raw day-maximum residuals ran +3.0C at RCSS (MSM), +4.0C at KSFO
(HRRR), +2.8C at ZBAA (CMA) -- and those raw curves were feeding the
temperature path's regression, whose phase coefficient reads +0.535 per
degree of claimed remaining warming, so a lane running 3C cold told the
chain the day was done warming about as loudly as any feature it has.

*The form is the global one, by construction rather than by resemblance.*
Each scope is fitted by ``native_ensemble_oos._fit_parameters`` -- the
function every P0 system is fitted by -- and a centre is resolved by
``native_ensemble_oos._resolved_bias_c``, the function P0 serves through.  So
the regional lanes inherit, without a second copy that could drift: the
five-day half-life (charter 62, shortened 2026-08-27), the sixty-day rolling
window, the forecast-level terciles shrunk toward the scope's constant by ten
case-equivalents (2026-08-27 ruling), and the residual sigma about the bias.
The scope key is the global one too: system x station x run cycle x lead
bucket, one case per event day, with the point-forecast minimum of five
training days.  Below that a scope falls back to the station's pooled scope
(one case per day, the day's runs as a lagged ensemble), and below that it is
silent -- a system is never corrected from another system's residuals.

Strict as-of holdout on the ruling day (each event day scored on a fit of
strictly earlier days, every complete run P1 could read at <= 12h lead):

    system     raw MAE   station const   station terciles   cycle x lead
    CMA          1.951        1.255            1.255             1.236
    MSM          2.150        1.101            1.104             1.012
    HRRR         1.566        1.044            1.016             1.001
    NBM          1.144        0.912            0.899             0.905
    ICON-EU      0.960        0.696            0.698             0.692
    all          1.432        0.978            0.961             0.947

*Where it serves: nowhere yet, by measurement.*  The global layer's rule is
that a fitted calibration serves only once it has beaten the incumbent out
of sample, and on the ruling day it did not.  The place a regional curve
reaches P1 -- the joint observation likelihood's headroom covariate,
``remaining_peak - running_high`` (charter 150) -- compares it against
today's own observations, so a lane that runs cold has already been caught
by the day's reports, and a historical shift on top corrects the same offset
twice.  Global members are calibrated before any observation of the day
exists; P1 is the stage where one always does.  The arm is
``FEATURE_CALIBRATION``; the fit is kept current every day regardless, so it
can be re-measured against the joint likelihood as the lanes accumulate.
(The path-prior arm this module also carried until 2026-09-19 left with the
path prior itself.)

*CMA's cycle labels.*  Rows the CMA lane wrote before 2026-09-16
(``...derived_cycle_v1``) carry a cycle stamp that may be fiction while their
curve and knowability are sound, so they train the station scope and never a
cycle scope.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractError

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "bayes_basket_target_regional_point_debias_v2"
RULE_VERSION = "regional_point_global_form_cycle_lead_v2"
DEFAULT_ARTIFACT = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_point_debias_artifact.json"
)
DEFAULT_REPLAY_TABLE = (
    ROOT / "data" / "runtime" / "bayes_basket_target_nwp"
    / "regional_point_debias_replay_table.json"
)

#: The regression's arm: raise the regional curve's remaining peak by its
#: fitted bias before the headroom feature is read off it -- only while that
#: peak is still the curve's own maximum, the stretch the day-maximum residual
#: describes, and a run with no fitted scope is read raw.  The live gap stays
#: raw: it is today's error at the hour in progress, not the day's maximum.
#:
#: Off by measurement (2026-09-18).  Paired walk-forward over 2026-08-31..
#: 09-17, every day's regression refitted on strictly earlier days, the
#: features built with the calibration each morning could have read, scored
#: on the rows a regional run reaches (station-day clustered 95% intervals,
#: log score, positive = calibrated better):
#:
#:     form                     rows    Δ log score           P(worse)
#:     remaining peak, ahead   11024   -0.0080 [-0.014,-0.002]  0.997
#:       where it applied       5223   -0.0145 [-0.027,-0.003]  0.991
#:       |bias| >= 1.5C         1370   -0.0462 [-0.086,-0.015]  0.998
#:     whole curve shifted      9557   -0.0120 [-0.020,-0.003]  0.996
#:       same rows, same lane   8241   -0.0063 [-0.014,+0.002]  0.943
#:       |bias| >= 1.5C         2530   -0.0198 [-0.034,-0.005]  0.995
#:     switched on, no refit   11024   -0.0148 [-0.022,-0.008]  1.000
#:
#: Worse for the MSM, the NBM and CMA, level for HRRR and ICON-EU, and worst
#: exactly where the fitted shift is largest.  The shift is right about the
#: model -- day-maximum MAE 1.43 -> 0.95C on the same holdout -- and wrong
#: about the feature, for the reason the path prior's reading gave: by midday
#: the running high already sits above a cold curve, so ``peak - high`` has
#: absorbed today's share of the offset, and adding the historical offset
#: counts it again.  (Measured against the four-layer chain the joint
#: likelihood replaced; re-measure against it before switching on.)
#: ``PM_REGIONAL_FEATURE_CALIBRATION=on`` is the arm; the
#: sample builder reads the same switch, so the regression the arm is scored
#: with is fitted on the features it serves.
FEATURE_CALIBRATION = (
    os.environ.get("PM_REGIONAL_FEATURE_CALIBRATION", "off").strip().lower()
    not in {"0", "off", "false", "no"}
)

#: Every regional lane is fitted ("所有区域模型"; the global layer has no
#: exception list).  This is the list the regression's arm resolves against.
CALIBRATED_SYSTEMS = frozenset(
    part for part in (
        os.environ.get(
            "PM_REGIONAL_FEATURE_CALIBRATION_SYSTEMS",
            "JMA_MSM_POINT NCEP_HRRR_POINT NCEP_NBM_POINT DWD_ICON_EU_POINT"
            " CMA_GRAPES_GLOBAL_POINT"
            # Charter 201: the family's global members are fitted the same way
            # (the P0 feature's own debias reads these scopes); the P1 arm
            # never reaches them -- P1's pairs are frozen.
            " ECMWF_AIFS_SINGLE_POINT ECMWF_IFS_9KM_RELAY_POINT"
            " UKMO_GLOBAL_10KM_RELAY_POINT",
        ).replace(",", " ").split()
    ) if part
)

#: Hours from run init to the local day's start, the global lead vocabulary
#: extended below zero: a run issued during the local day is ``intraday``.
LEAD_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("intraday", -math.inf, 0.0),
    ("h00_12", 0.0, 12.0),
    ("h12_24", 12.0, 24.0),
    ("h24_48", 24.0, 48.0),
    ("h48_plus", 48.0, math.inf),
)
#: The point-forecast minimum the OOS gate admits a single-member system on
#: (``native_ensemble_oos.POINT_FORECAST_MIN_TRAIN_DAYS``) and the global
#: rolling window (``ROLLING_TRAIN_WINDOW_DAYS``).  Literals because this
#: module sits under ``materialization`` in the import graph and the OOS
#: module sits above it; a test holds the two pairs equal.
MIN_TRAIN_DAYS = 5
WINDOW_DAYS = 60
#: A shift beyond this is a broken fit, not a bias; the global serving guard
#: range-checks at 20C, and nothing measured here is within half of it.
MAX_CORRECTION_C = 10.0
#: Rows whose cycle stamp is inferred and may be wrong.  See the docstring.
UNRELIABLE_CYCLE_EXTRACTIONS = frozenset({
    "cma_grapes_global_open_meteo_derived_cycle_v1",
})
#: What a fitted scope keeps of ``_fit_parameters``' output: the resolver's
#: inputs and the evidence behind them.  The climate samples stay out -- the
#: blend they fed is retired (charter 86) and they are most of the bytes.
_KEPT_PARAMETERS = (
    "bias_c", "correction_form", "bias_bin_cuts_c", "bias_bin_shifts_c",
    "train_residual_sigma_c", "train_rmse_c", "train_n_effective",
    "fit_through_date", "fit_case_count",
)

_CACHE: dict[str, Any] = {}


def lead_bucket(lead_hours_to_local_day_start: float) -> str:
    value = float(lead_hours_to_local_day_start)
    for name, low, high in LEAD_BUCKETS:
        if low <= value < high:
            return name
    return LEAD_BUCKETS[-1][0]


def cycle_scope_key(run_cycle_hour: int, bucket: str) -> str:
    return f"{int(run_cycle_hour):02d}|{bucket}"


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# fit
# ---------------------------------------------------------------------------

def _kept(parameters: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    kept = {key: parameters[key] for key in _KEPT_PARAMETERS if key in parameters}
    errors = [
        float(case["actual_tmax_c"])
        - sum(map(float, case["values"])) / len(case["values"])
        for case in cases
    ]
    kept["train_days"] = len({str(case["event_date"]) for case in cases})
    kept["raw_mae_c"] = sum(map(abs, errors)) / len(errors)
    return kept


def _fit_scope(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    from .native_ensemble_oos import _fit_parameters  # noqa: PLC0415

    if len({str(case["event_date"]) for case in cases}) < MIN_TRAIN_DAYS:
        return None
    try:
        return _kept(_fit_parameters(cases), cases)
    except (ContractError, ValueError, ZeroDivisionError):
        return None


def fit_calibration(
    runs: Iterable[Mapping[str, Any]],
    truth: Mapping[tuple[str, str], float],
    *,
    before_date: str,
) -> dict[str, Any]:
    """The calibration a decision on ``before_date`` may use.

    ``runs`` are lane rows (system, station, target day, run init, cycle,
    lead, forecast maximum, completeness, extraction version); ``truth`` is
    the settled daily high by (station, day).  Only event days strictly
    before ``before_date`` and inside the rolling window train, so a
    production fit on today and a replay entry for today are the same
    object.  A run whose peak window was not complete is not a statement
    about the day's maximum and never trains.
    """

    bound = date.fromisoformat(str(before_date)[:10])
    floor = (bound - timedelta(days=WINDOW_DAYS)).isoformat()
    ceiling = bound.isoformat()
    by_cycle: dict[tuple[str, str, str], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    by_station: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        day = str(run["target_local_date"])
        station = str(run["station_id"]).upper()
        if not (floor <= day < ceiling) or (station, day) not in truth:
            continue
        if not bool(run.get("remaining_peak_complete")):
            continue
        system = str(run["ensemble_system_id"])
        by_station[(system, station)][day].append(float(run["forecast_tmax_c"]))
        if str(run.get("extraction_version") or "") in UNRELIABLE_CYCLE_EXTRACTIONS:
            continue
        key = cycle_scope_key(
            int(run["run_cycle_hour"]),
            lead_bucket(float(run["lead_hours_to_local_day_start"])),
        )
        # One run per day per cycle scope, as in the global layer; a rerun
        # of the same cycle (a later fetch of the same label) replaces it.
        by_cycle[(system, station, key)][day] = run
    systems: dict[str, dict[str, Any]] = {}
    for (system, station), days in sorted(by_station.items()):
        station_fit = _fit_scope([
            {"values": values, "actual_tmax_c": truth[(station, day)],
             "event_date": day}
            for day, values in sorted(days.items())
        ])
        if station_fit is None:
            continue
        cycles: dict[str, Any] = {}
        for (cycle_system, cycle_station, key), per_day in by_cycle.items():
            if cycle_system != system or cycle_station != station:
                continue
            fitted = _fit_scope([
                {"values": [float(run["forecast_tmax_c"])],
                 "actual_tmax_c": truth[(station, day)], "event_date": day}
                for day, run in sorted(per_day.items())
            ])
            if fitted is not None:
                cycles[key] = fitted
        systems.setdefault(system, {"stations": {}})["stations"][station] = {
            "station": station_fit,
            "cycles": dict(sorted(cycles.items())),
        }
    trained = [
        scope["station"]["fit_through_date"]
        for system in systems.values()
        for scope in system["stations"].values()
    ]
    return {
        "schema": SCHEMA,
        "rule_version": RULE_VERSION,
        "fit_before_date": ceiling,
        "trained_through": max(trained) if trained else "",
        "window_days": WINDOW_DAYS,
        "min_train_days": MIN_TRAIN_DAYS,
        "lead_buckets": [name for name, _, _ in LEAD_BUCKETS],
        "systems": systems,
    }


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------

def _load(path: Path, schema: str, key_prefix: str) -> dict[str, Any] | None:
    key = key_prefix + str(path)
    if key not in _CACHE:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if payload is not None and payload.get("schema") != schema:
            payload = None
        _CACHE[key] = payload
    return _CACHE[key]


def load_artifact(path: Path | None = None) -> dict[str, Any] | None:
    target = Path(
        path or os.environ.get("PM_REGIONAL_POINT_DEBIAS_ARTIFACT")
        or DEFAULT_ARTIFACT
    )
    return _load(target, SCHEMA, "debias_artifact::")


def load_replay_table(path: Path | None = None) -> dict[str, Any] | None:
    target = Path(
        path or os.environ.get("PM_REGIONAL_POINT_DEBIAS_REPLAY_TABLE")
        or DEFAULT_REPLAY_TABLE
    )
    return _load(target, SCHEMA + "_replay", "debias_replay::")


def replay_curve(
    event_date: str, table: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """The calibration this event date may use: fitted only on earlier days."""

    payload = table if table is not None else load_replay_table()
    if not payload:
        return None
    entry = (payload.get("dates") or {}).get(str(event_date))
    if not entry:
        return None
    return {
        "schema": SCHEMA, "systems": entry.get("systems") or {},
        "fit_before_date": str(event_date), "trained_through": "",
    }


# ---------------------------------------------------------------------------
# resolve and apply
# ---------------------------------------------------------------------------

def resolve_correction(
    artifact: Mapping[str, Any] | None,
    *,
    system_id: str,
    station_id: str,
    run_init_utc: str,
    lead_hours_to_local_day_start: float,
    forecast_level_c: float,
    systems: "frozenset[str] | set[str] | None" = None,
) -> dict[str, Any] | None:
    """This run's fitted shift, or ``None`` for silence.

    The cycle x lead scope when it has five training days, else the
    station's pooled scope, else nothing.  The shift is resolved at the
    run's own forecast level, through the resolver P0 serves by.

    ``systems`` is the admission list; ``None`` means every regional lane.
    """

    from .native_ensemble_oos import _resolved_bias_c  # noqa: PLC0415

    admitted = CALIBRATED_SYSTEMS if systems is None else systems
    if str(system_id) not in admitted:
        return None
    scope = (
        ((artifact or {}).get("systems") or {}).get(str(system_id)) or {}
    ).get("stations", {}).get(str(station_id).upper())
    if not scope:
        return None
    try:
        cycle = _parse(run_init_utc).astimezone(timezone.utc).hour
        key = cycle_scope_key(cycle, lead_bucket(float(lead_hours_to_local_day_start)))
    except (TypeError, ValueError):
        return None
    parameters = (scope.get("cycles") or {}).get(key)
    source = "cycle_lead"
    if parameters is None:
        parameters = scope.get("station")
        source = "station"
    if not parameters:
        return None
    try:
        bias = float(_resolved_bias_c(float(forecast_level_c), parameters))
    except (ContractError, KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(bias) or abs(bias) > MAX_CORRECTION_C:
        return None
    return {
        "bias_c": bias,
        "source": source,
        "scope": key if source == "cycle_lead" else "station",
        "train_days": int(parameters.get("train_days") or 0),
        "sample_n": int(parameters.get("fit_case_count") or 0),
        "fit_system_id": str(system_id),
        "fit_through_date": str(parameters.get("fit_through_date") or ""),
    }



__all__ = [
    "CALIBRATED_SYSTEMS", "FEATURE_CALIBRATION",
    "LEAD_BUCKETS", "MAX_CORRECTION_C", "MIN_TRAIN_DAYS", "RULE_VERSION",
    "SCHEMA", "UNRELIABLE_CYCLE_EXTRACTIONS", "WINDOW_DAYS",
    "cycle_scope_key", "fit_calibration", "lead_bucket", "load_artifact",
    "load_replay_table", "replay_curve", "resolve_correction",
]
