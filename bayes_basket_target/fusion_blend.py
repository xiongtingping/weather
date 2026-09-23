"""P0 fusion weights: the seat's weights, re-fitted as of the decision (charter 199).

User ruling 2026-09-23: 「我想上线推进，因为这是我的优化目标，你继续！」「你先打通
融合权重层」.  The product's north star is the Reliability and Resolution of the
served bucket probabilities, with RMSE, CRPS and NLL required to fall together.

The seat (charter 134) certifies which systems may sit and hands over its
weights: production skill weights shrunk halfway toward the family-balanced
baseline, fixed when the seat was minted.  This layer takes those weights as
its prior and re-fits them, per decision, on the settled station-days that
were knowable at that instant, twice:

* **centre** -- minimise the mean squared error of the weighted calibrated
  centre over this station's last 21 calendar days, ridge 1 C^2 back to the
  seat weights; fewer than five station-days leaves the seat weights;
* **distribution** -- minimise the CRPS of the linear pool of the systems'
  own calibrated distributions: first over the fleet at the same per-system
  lead buckets (or the whole fleet when none share them), ridge 1 back to
  the seat weights; then over this station's days, ridge 10/n back to the
  fleet fit;

and serves the average of the two.  Both objectives are convex on the
simplex, so the average scores no worse than the mean of the two ends on
every station-day (Jensen), and the complementary errors are where the
combination gains.

This is the frozen ``blend50`` candidate of
docs/research/fusion_weight_comparison_20260922 (joint_objective, pareto_metrics,
economic_replay): on 515 station-days, 33 stations, 2026-08-28..09-19, RMSE
-0.38%, CRPS -0.28%, NLL -0.21%, bucket Reliability -13%, Resolution +0.6%
against the seat weights -- every interval crossing zero, the first half
window's NLL slightly worse, and a retrospective as-of comparison rather than
an untouched out-of-sample.  The functions below reproduce that candidate's
weights on those 515 rows (tests/test_charter_199_fusion_blend.py).

What it never does: seat a system the seat did not, give weight to a
provisional rider, break a family cap, or serve fewer than two positive-weight
families.  Any failure -- no training file, a station whose labels are not
bound, an optimiser that does not converge, a blended weight at zero -- serves
the seat weights unchanged and says why in the receipt.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

FUSION_BLEND_RULE_VERSION = "center_crps_blend_v1"
FUSION_BLEND_RECEIPT_SCHEMA = "fusion_blend_receipt_v1"
FUSION_BLEND_TRAINING_SCHEMA = "fusion_blend_training_fact_v1"
# Fixed before the candidate was scored (joint_objective/PROTOCOL.md): the
# 50:50 blend was the pre-declared primary; 25:75 and 75:25 were sensitivity.
FUSION_BLEND_CENTER_FRACTION = 0.5
FUSION_BLEND_WINDOW_DAYS = 21
FUSION_BLEND_CENTER_MIN_ROWS = 5
FUSION_BLEND_CENTER_RIDGE = 1.0
FUSION_BLEND_FLEET_RIDGE = 1.0
FUSION_BLEND_LOCAL_RIDGE_SCALE = 10.0
# A blended weight this small is a system the optimisers voted out; the
# contract serves only strictly positive weights, and dropping a certified
# member is a seating question this layer does not answer.
FUSION_BLEND_MIN_WEIGHT = 1e-6
# The research tolerance on the optimiser's own output (compare.assert_weight).
_FIT_TOLERANCE = 1e-7

# US ASOS stations: their historical canonical winner is not bound to one
# label lane, so the study excluded them from training and evaluation alike.
# They keep the seat weights until their labels are bound.
FUSION_BLEND_UNBOUND_LABEL_STATIONS = frozenset({
    "KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD",
    "KSEA", "KSFO",
})

FUSION_BLEND_MODE_BLEND = "blend"
FUSION_BLEND_MODE_SHADOW = "shadow"
FUSION_BLEND_MODE_OFF = "off"
FUSION_BLEND_MODES = (
    FUSION_BLEND_MODE_BLEND, FUSION_BLEND_MODE_SHADOW, FUSION_BLEND_MODE_OFF,
)
# Served default: SHADOW.  The frozen candidate was re-scored before go-live
# under the training density production actually has (production_regime/,
# 2026-09-23): three-down on the 515 station-days it was designed on, but on
# the 143 same-period station-days the study never used RMSE +1.57%
# [+0.4%, +4.2%] and CRPS +1.02%, and on the 96 station-days settled after
# the freeze RMSE +0.53%, CRPS +0.32% -- both halves of the blend worse on
# their own.  So every compile computes the blend and records it, the seat's
# weights are what is served, and the forward record on days nobody has seen
# decides the switch.  ``blend`` serves it (a served-arithmetic change: bump
# the three stages with it); ``off`` skips the computation entirely.
FUSION_BLEND_MODE = (
    os.environ.get("P0_FUSION_BLEND_MODE", FUSION_BLEND_MODE_SHADOW).strip()
    or FUSION_BLEND_MODE_SHADOW
)
if FUSION_BLEND_MODE not in FUSION_BLEND_MODES:
    raise RuntimeError(f"unknown P0_FUSION_BLEND_MODE: {FUSION_BLEND_MODE}")

_ROOT = Path(__file__).resolve().parents[1]
FUSION_BLEND_TRAINING_DB = Path(
    os.environ.get(
        "P0_FUSION_BLEND_TRAINING_DB",
        str(_ROOT / "data" / "fusion_blend_training.db"),
    )
)

TRAINING_DDL = (
    "CREATE TABLE IF NOT EXISTS fusion_blend_training_fact ("
    " station_id TEXT NOT NULL,"
    " event_date TEXT NOT NULL,"
    " truth_revision INTEGER NOT NULL,"
    " settlement_timezone TEXT NOT NULL,"
    " root_decision_at_utc TEXT NOT NULL,"
    " root_checkpoint_id TEXT NOT NULL,"
    " root_envelope_sha256 TEXT NOT NULL,"
    " root_posterior_version TEXT NOT NULL,"
    " truth_c REAL NOT NULL,"
    " truth_known_at_utc TEXT NOT NULL,"
    " truth_id TEXT NOT NULL,"
    " systems_json TEXT NOT NULL,"
    " lead_by_system_json TEXT NOT NULL,"
    " center_error_json TEXT NOT NULL,"
    " crps_systems_json TEXT NOT NULL,"
    " crps_a_json TEXT NOT NULL,"
    " crps_b_json TEXT NOT NULL,"
    " probability_source TEXT NOT NULL,"
    " seat_weights_json TEXT NOT NULL,"
    " family_by_system_json TEXT NOT NULL,"
    " family_caps_json TEXT NOT NULL,"
    " bucket_keys_json TEXT NOT NULL,"
    " truth_bucket_index INTEGER NOT NULL,"
    " probabilities_json TEXT NOT NULL,"
    " fusion_blend_state TEXT NOT NULL,"
    " shadow_weights_json TEXT NOT NULL,"
    " schema TEXT NOT NULL,"
    " created_at_utc TEXT NOT NULL,"
    " content_sha256 TEXT NOT NULL,"
    " PRIMARY KEY (station_id, event_date, truth_revision))",
    "CREATE INDEX IF NOT EXISTS fusion_blend_training_fact_day_idx"
    " ON fusion_blend_training_fact(event_date)",
)


def _stamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- geometry


def representative_points(
    intervals: Mapping[str, Sequence[float | None]],
) -> tuple[list[str], np.ndarray]:
    """Bucket keys in ladder order and the point each bucket is scored at.

    A closed bucket at its midpoint; an open end half the ladder's median
    closed width past its bound -- the study's discrete CRPS proxy
    (docs/research/p0_physical_20260920/model.py ``geometry``).
    """

    keys = sorted(
        intervals,
        key=lambda key: -math.inf
        if intervals[key][0] is None else float(intervals[key][0]),
    )
    if len(keys) < 3:
        raise ValueError("fusion_blend_geometry_too_short")
    finite = [
        float(hi) - float(lo)
        for lo, hi in (intervals[key] for key in keys)
        if lo is not None and hi is not None
    ]
    if not finite or min(finite) <= 0:
        raise ValueError("fusion_blend_geometry_width")
    for left, right in zip(keys, keys[1:]):
        if (
            intervals[left][1] is None or intervals[right][0] is None
            or abs(float(intervals[left][1]) - float(intervals[right][0])) > 1e-8
        ):
            raise ValueError("fusion_blend_geometry_noncontiguous")
    if intervals[keys[0]][0] is not None or intervals[keys[-1]][1] is not None:
        raise ValueError("fusion_blend_geometry_open_tails_required")
    width = float(np.median(finite))
    points = []
    for key in keys:
        lo, hi = intervals[key]
        points.append(
            float(hi) - width / 2 if lo is None
            else float(lo) + width / 2 if hi is None
            else (float(lo) + float(hi)) / 2
        )
    return keys, np.array(points)


def truth_bucket_index(
    intervals: Mapping[str, Sequence[float | None]],
    keys: Sequence[str],
    truth_c: float,
) -> int:
    """Lower-inclusive, upper-exclusive, as the settlement contract reads."""

    hits = [
        index for index, key in enumerate(keys)
        if (intervals[key][0] is None or truth_c >= float(intervals[key][0]))
        and (intervals[key][1] is None or truth_c < float(intervals[key][1]))
    ]
    if len(hits) != 1:
        raise ValueError("fusion_blend_truth_not_uniquely_mapped")
    return hits[0]


def row_statistics(
    *,
    intervals: Mapping[str, Sequence[float | None]],
    truth_c: float,
    centers_c: Mapping[str, float],
    system_probabilities: Mapping[str, Mapping[str, float]] | None,
    systems: Sequence[str],
) -> dict[str, Any]:
    """Everything a settled station-day contributes to either objective.

    Both objectives average per-row quantities that depend on nothing but the
    row: the centre error e_m, and for the pool CRPS
    ``a_m = sum_b p_mb |z_b - y|`` and ``B_mn = sum_bc p_mb p_nc |z_b - z_c|``.
    A fit over any subset of systems reads sub-vectors of these.
    """

    keys, points = representative_points(intervals)
    ordered = sorted(str(system) for system in systems)
    out: dict[str, Any] = {
        "center_error": {
            system: float(centers_c[system]) - float(truth_c)
            for system in ordered
        },
        "crps_systems": [],
        "crps_a": {},
        "crps_b": {},
    }
    if not system_probabilities:
        return out
    with_probability = [
        system for system in ordered if system in system_probabilities
    ]
    if not with_probability:
        return out
    matrix = np.array([
        [float(system_probabilities[system][key]) for key in keys]
        for system in with_probability
    ])
    distances = np.abs(points[:, None] - points[None, :])
    a = matrix @ np.abs(points - float(truth_c))
    b = matrix @ distances @ matrix.T
    out["crps_systems"] = with_probability
    out["crps_a"] = {
        system: float(a[index]) for index, system in enumerate(with_probability)
    }
    out["crps_b"] = {
        left: {
            right: float(b[i, j]) for j, right in enumerate(with_probability)
        }
        for i, left in enumerate(with_probability)
    }
    return out


# ------------------------------------------------------------- training rows


class TrainingRow:
    """One settled station-day, parsed once."""

    __slots__ = (
        "station", "event_date", "at", "known", "systems", "lead",
        "center_error", "crps_systems", "crps_a", "crps_b", "identity",
        "truth_revision",
    )

    def __init__(self, record: Mapping[str, Any]) -> None:
        self.station = str(record["station_id"])
        self.event_date = str(record["event_date"])
        self.at = _stamp(record["root_decision_at_utc"])
        self.known = _stamp(record["truth_known_at_utc"])
        self.systems = frozenset(json.loads(record["systems_json"]))
        self.lead = dict(json.loads(record["lead_by_system_json"]))
        self.center_error = dict(json.loads(record["center_error_json"]))
        self.crps_systems = frozenset(json.loads(record["crps_systems_json"]))
        self.crps_a = dict(json.loads(record["crps_a_json"]))
        self.crps_b = dict(json.loads(record["crps_b_json"]))
        self.identity = str(record["content_sha256"])
        self.truth_revision = int(record["truth_revision"])


_CACHE_LOCK = threading.Lock()
_ROW_CACHE: dict[str, tuple[tuple[int, int], list[TrainingRow]]] = {}


def load_training_rows(path: Path | str | None = None) -> list[TrainingRow]:
    """Every training fact in the file, parsed once per file generation."""

    target = Path(path or FUSION_BLEND_TRAINING_DB)
    stat = target.stat()
    generation = (stat.st_mtime_ns, stat.st_size)
    wal = target.with_name(target.name + "-wal")
    if wal.exists():
        wal_stat = wal.stat()
        generation = generation + (wal_stat.st_mtime_ns, wal_stat.st_size)
    key = str(target)
    with _CACHE_LOCK:
        cached = _ROW_CACHE.get(key)
        if cached is not None and cached[0] == generation:
            return cached[1]
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            TrainingRow(record) for record in conn.execute(
                "SELECT * FROM fusion_blend_training_fact"
                " ORDER BY station_id, event_date, truth_revision"
            )
        ]
    finally:
        conn.close()
    with _CACHE_LOCK:
        _ROW_CACHE[key] = (generation, rows)
    return rows


def as_of_rows(
    rows: Sequence[TrainingRow], decision_at: datetime
) -> list[TrainingRow]:
    """Per station-day, the newest truth revision knowable at the decision."""

    best: dict[tuple[str, str], TrainingRow] = {}
    for row in rows:
        if row.known > decision_at:
            continue
        slot = (row.station, row.event_date)
        held = best.get(slot)
        if held is None or row.truth_revision > held.truth_revision:
            best[slot] = row
    return list(best.values())


# ------------------------------------------------------------------ fitting


class Target:
    """The decision being weighted, in the study's terms."""

    def __init__(
        self,
        *,
        station: str,
        event_date: str,
        decision_at: datetime,
        local_decision_day: date,
        weights: Mapping[str, float],
        families: Mapping[str, str],
        caps: Mapping[str, float],
        lead: Mapping[str, str],
    ) -> None:
        self.station = station
        self.event_date = event_date
        self.at = decision_at
        self.local_decision_day = local_decision_day
        self.weights = {str(k): float(v) for k, v in weights.items()}
        self.keys = sorted(self.weights)
        self.families = {str(k): str(v) for k, v in families.items()}
        self.caps = {str(k): float(v) for k, v in caps.items()}
        self.lead = {str(k): str(v) for k, v in lead.items()}


def eligible(
    rows: Sequence[TrainingRow],
    target: Target,
    days: int,
    *,
    fleet: bool = False,
    lead: bool = False,
    probability: bool = False,
) -> list[TrainingRow]:
    """The study's training filter (compare.eligible), unchanged.

    A calendar window of ``days`` local days before the decision's local
    day, strictly before the event, decided before and settled (knowably)
    by the decision instant; this station unless ``fleet``; every seated
    system present; the same per-system lead bucket when ``lead``; every
    seated system's distribution present when ``probability``.
    """

    floor = (target.local_decision_day - timedelta(days=days)).isoformat()
    end = target.local_decision_day.isoformat()
    keys = set(target.keys)
    out = []
    for row in rows:
        if not (floor <= row.event_date < end and row.event_date < target.event_date):
            continue
        if not (row.at < target.at and row.known <= target.at):
            continue
        if not fleet and row.station != target.station:
            continue
        if not keys <= row.systems:
            continue
        if lead and not all(
            system in target.lead and row.lead.get(system) == target.lead[system]
            for system in target.keys
        ):
            continue
        if probability and not keys <= row.crps_systems:
            continue
        out.append(row)
    return out


def _constraints(target: Target, keys: Sequence[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{
        "type": "eq",
        "fun": lambda w: w.sum() - 1,
        "jac": lambda w: np.ones(len(w)),
    }]
    for family in set(target.families[system] for system in keys):
        mask = np.array(
            [target.families[system] == family for system in keys], dtype=float
        )
        out.append({
            "type": "ineq",
            "fun": lambda w, mask=mask, family=family: target.caps[family] - mask @ w,
            "jac": lambda w, mask=mask: -mask,
        })
    return out


def _check(weights: Mapping[str, float], target: Target) -> None:
    if set(weights) != set(target.keys):
        raise ValueError("fusion_blend_fit_keys")
    if min(weights.values()) < -1e-9 or abs(sum(weights.values()) - 1) > _FIT_TOLERANCE:
        raise ValueError("fusion_blend_fit_not_a_distribution")
    by_family: dict[str, float] = defaultdict(float)
    for system, value in weights.items():
        by_family[target.families[system]] += value
    if any(value > target.caps[family] + _FIT_TOLERANCE for family, value in by_family.items()):
        raise ValueError("fusion_blend_fit_cap")


def fit_stack(
    rows: Sequence[TrainingRow],
    target: Target,
    *,
    probability: bool,
    prior: Mapping[str, float] | None = None,
    ridge: float = 1.0,
) -> dict[str, float]:
    """The study's ``stack``: SLSQP on the simplex under the family caps."""

    from scipy.optimize import minimize  # noqa: PLC0415 -- serving path only

    keys = target.keys
    start = np.array([(prior or target.weights)[system] for system in keys])
    if probability:
        a = np.mean([[row.crps_a[s] for s in keys] for row in rows], axis=0)
        b = np.mean(
            [[[row.crps_b[s][t] for t in keys] for s in keys] for row in rows],
            axis=0,
        )

        def fun(w: np.ndarray) -> float:
            return float(a @ w - .5 * w @ b @ w + ridge * np.sum((w - start) ** 2))

        def jac(w: np.ndarray) -> np.ndarray:
            return a - b @ w + 2 * ridge * (w - start)
    else:
        e = np.array([[row.center_error[s] for s in keys] for row in rows])
        q = e.T @ e / len(e)

        def fun(w: np.ndarray) -> float:
            return float(w @ q @ w + ridge * np.sum((w - start) ** 2))

        def jac(w: np.ndarray) -> np.ndarray:
            return 2 * q @ w + 2 * ridge * (w - start)
    fit = minimize(
        fun, start, jac=jac, bounds=[(0, 1)] * len(keys),
        constraints=_constraints(target, keys), method="SLSQP",
        options={"maxiter": 100, "ftol": 1e-10},
    )
    if not fit.success:
        raise ValueError("fusion_blend_optimisation_failed:" + str(fit.message))
    weights = dict(zip(keys, map(float, fit.x)))
    _check(weights, target)
    return weights


def blend_weights(
    rows: Sequence[TrainingRow], target: Target
) -> tuple[dict[str, float], dict[str, Any]]:
    """centre_stack21 and crps_hier21, averaged (the study's blend50)."""

    base = dict(target.weights)
    detail: dict[str, Any] = {}
    center_rows = eligible(rows, target, FUSION_BLEND_WINDOW_DAYS)
    detail["center_rows"] = len(center_rows)
    center = base
    detail["center_state"] = "SEAT_PRIOR_TOO_FEW_DAYS"
    if len(center_rows) >= FUSION_BLEND_CENTER_MIN_ROWS:
        try:
            center = fit_stack(
                center_rows, target, probability=False,
                ridge=FUSION_BLEND_CENTER_RIDGE,
            )
            detail["center_state"] = "FITTED"
        except ValueError as error:
            detail["center_state"] = "SEAT_PRIOR_" + str(error)
    local = eligible(rows, target, FUSION_BLEND_WINDOW_DAYS, probability=True)
    fleet = eligible(
        rows, target, FUSION_BLEND_WINDOW_DAYS, fleet=True, lead=True,
        probability=True,
    )
    detail["fleet_scope"] = "same_lead"
    if not fleet:
        fleet = eligible(
            rows, target, FUSION_BLEND_WINDOW_DAYS, fleet=True,
            probability=True,
        )
        detail["fleet_scope"] = "all_leads"
    detail["local_rows"] = len(local)
    detail["fleet_rows"] = len(fleet)
    distribution = base
    detail["distribution_state"] = "SEAT_PRIOR_NO_FLEET"
    used = center_rows if center is not base else []
    if fleet:
        try:
            prior = fit_stack(
                fleet, target, probability=True,
                ridge=FUSION_BLEND_FLEET_RIDGE,
            )
            distribution = (
                fit_stack(
                    local, target, probability=True, prior=prior,
                    ridge=FUSION_BLEND_LOCAL_RIDGE_SCALE / len(local),
                )
                if local else prior
            )
            detail["distribution_state"] = (
                "FITTED_LOCAL" if local else "FITTED_FLEET_ONLY"
            )
            used = list(used) + list(fleet) + list(local)
        except ValueError as error:
            distribution = base
            detail["distribution_state"] = "SEAT_PRIOR_" + str(error)
    fraction = FUSION_BLEND_CENTER_FRACTION
    blended = {
        system: fraction * center[system] + (1 - fraction) * distribution[system]
        for system in target.keys
    }
    detail["center_weights"] = dict(sorted(center.items()))
    detail["distribution_weights"] = dict(sorted(distribution.items()))
    detail["training_rows_sha256"] = hashlib.sha256(
        "\n".join(sorted({row.identity for row in used})).encode()
    ).hexdigest()
    return blended, detail


def feasible(
    weights: Mapping[str, float],
    families: Mapping[str, str],
    caps: Mapping[str, float],
) -> dict[str, float]:
    """Clip the optimiser's float noise onto the served contract exactly.

    SLSQP returns points feasible to ~1e-10; the contract admits a family at
    most 1e-12 past its cap and a total at most 1e-9 off one.  A family over
    its cap is scaled down to it and the excess spread over the families
    with room in proportion to their weight -- a move of the order of the
    optimiser's tolerance, never a re-weighting.
    """

    clipped = {system: max(0.0, float(value)) for system, value in weights.items()}
    total = sum(clipped.values())
    clipped = {system: value / total for system, value in clipped.items()}
    for _ in range(len(clipped) + 2):
        by_family: dict[str, float] = defaultdict(float)
        for system, value in clipped.items():
            by_family[families[system]] += value
        over = {
            family for family, value in by_family.items()
            if value > caps[family]
        }
        if not over:
            break
        excess = 0.0
        for system in clipped:
            family = families[system]
            if family in over:
                scaled = clipped[system] * caps[family] / by_family[family]
                excess += clipped[system] - scaled
                clipped[system] = scaled
        room = {
            system: value for system, value in clipped.items()
            if families[system] not in over
        }
        room_total = sum(room.values())
        if room_total <= 0:
            raise ValueError("fusion_blend_caps_infeasible")
        for system, value in room.items():
            clipped[system] = value + excess * value / room_total
    total = sum(clipped.values())
    return {system: value / total for system, value in sorted(clipped.items())}


# ------------------------------------------------------------------ serving


def overlay_system_weights(
    *,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
    timezone_name: str,
    system_weights: Mapping[str, float],
    family_by_system: Mapping[str, str],
    family_caps: Mapping[str, float],
    lead_by_system: Mapping[str, str],
    training_db: Path | str | None = None,
    mode: str | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """The weights P0 serves, and the receipt that says how they were reached.

    ``system_weights`` are the seat's.  Only its positively weighted systems
    are re-weighted; a zero-weight rider stays at zero.  Every path that does
    not end in APPLIED returns the seat weights unchanged -- and so does
    SHADOW, which runs the whole fit and records what it would have served
    as ``shadow_weights`` beside ``shadow_state``.
    """

    active_mode = mode or FUSION_BLEND_MODE
    if active_mode == FUSION_BLEND_MODE_SHADOW:
        weights, receipt = overlay_system_weights(
            station_id=station_id, event_date=event_date,
            decision_at_utc=decision_at_utc, timezone_name=timezone_name,
            system_weights=system_weights, family_by_system=family_by_system,
            family_caps=family_caps, lead_by_system=lead_by_system,
            training_db=training_db, mode=FUSION_BLEND_MODE_BLEND,
        )
        seat = {str(k): float(v) for k, v in system_weights.items()}
        receipt = dict(receipt)
        receipt["shadow_state"] = receipt.pop("state")
        receipt["shadow_weights"] = receipt.pop("served_weights")
        receipt.pop("moved_half_l1", None)
        receipt["shadow_moved_half_l1"] = 0.5 * sum(
            abs(weights[s] - seat[s]) for s in seat
        )
        receipt["state"] = "SHADOW"
        receipt["served_weights"] = dict(sorted(seat.items()))
        return seat, receipt

    seat = {str(k): float(v) for k, v in system_weights.items()}
    receipt: dict[str, Any] = {
        "schema": FUSION_BLEND_RECEIPT_SCHEMA,
        "rule_version": FUSION_BLEND_RULE_VERSION,
        "center_fraction": FUSION_BLEND_CENTER_FRACTION,
        "window_days": FUSION_BLEND_WINDOW_DAYS,
        "seat_weights": dict(sorted(seat.items())),
    }

    def _seat(state: str, **extra: Any) -> tuple[dict[str, float], dict[str, Any]]:
        receipt.update(state=state, served_weights=dict(sorted(seat.items())), **extra)
        return dict(seat), receipt

    if active_mode == FUSION_BLEND_MODE_OFF:
        return _seat("OFF")
    if station_id in FUSION_BLEND_UNBOUND_LABEL_STATIONS:
        return _seat("SEAT_LABELS_NOT_BOUND")
    # A provisional rider certifies at (near) zero and was never given a
    # family or a calibration by the compile; it rides at its seat value.
    weighted = {
        system: value for system, value in seat.items()
        if value > 0.0 and system in family_by_system
    }
    if len({family_by_system[s] for s in weighted}) < 2:
        return _seat("SEAT_FEWER_THAN_TWO_FAMILIES")
    try:
        decision_at = _stamp(decision_at_utc)
        local_day = decision_at.astimezone(ZoneInfo(timezone_name)).date()
        rows = as_of_rows(load_training_rows(training_db), decision_at)
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        return _seat("SEAT_TRAINING_UNAVAILABLE", error=type(error).__name__)
    total = sum(weighted.values())
    target = Target(
        station=station_id,
        event_date=event_date,
        decision_at=decision_at,
        local_decision_day=local_day,
        weights={system: value / total for system, value in weighted.items()},
        families={s: family_by_system[s] for s in weighted},
        caps=family_caps,
        lead={s: lead_by_system[s] for s in weighted if s in lead_by_system},
    )
    try:
        blended, detail = blend_weights(rows, target)
        receipt.update(detail)
        if (
            detail["center_state"] != "FITTED"
            and not str(detail["distribution_state"]).startswith("FITTED")
        ):
            return _seat("SEAT_NO_TRAINING_DAYS")
        served = feasible(blended, target.families, target.caps)
    except (ValueError, KeyError, ZeroDivisionError, FloatingPointError) as error:
        return _seat("SEAT_FIT_FAILED", error=str(error)[:160])
    if min(served.values()) < FUSION_BLEND_MIN_WEIGHT:
        return _seat("SEAT_BLEND_VOTED_OUT_A_MEMBER", blended_weights=dict(sorted(blended.items())))
    if any(not math.isfinite(value) for value in served.values()):
        return _seat("SEAT_FIT_FAILED", error="non_finite")
    out = dict(served)
    for system, value in seat.items():
        if system not in out:
            out[system] = value
    receipt.update(
        state="APPLIED",
        served_weights=dict(sorted(out.items())),
        moved_half_l1=0.5 * sum(abs(out[s] - seat[s]) for s in seat),
    )
    return out, receipt
