"""P0 fusion weights per station x model (charter 199, rule of charter 242).

The seat (charter 134) decides who sits and hands over its weights.  This layer
re-weights the seated systems for the decision's station, on the settled
station-days knowable at the decision:

* **fit** -- the station's log-score-optimal linear mixture of the systems'
  own calibrated distributions: softmax weights over the active registry,
  scored on each past decision's own seated set (renormalised there), by
  gradient ascent on the mean log of the truth bucket's pooled probability
  (floored at the served model-zero floor);
* **shrink, per system** -- served w_j proportional to (1 - a_j) * seat_j +
  a_j * fit_j, a_j = n_j / (n_j + tau), n_j the station-days on which system j
  was scored at this station: a system with little history at a station stays
  at its seat weight, one with a long history follows the station's fit;
* **tau** -- chosen each day on the fleet's last three weeks (event days
  d-22..d-2), each past decision weighted with its own as-of fit, by the same
  pooled log score, from TAU_GRID.

Charter 242 (user, 2026-10-02 「继续」 after 「按照站点*模型」): pre-registered in
docs/research/station_model_weights_20261001 (PROTOCOL.md, addendum 2).  On
2026-01..09 replays of the production single-system rule with all six systems
(IEM truth before July under the 2026-09-24 ruling), 04-01..09-30, 7,093
station-days: LogScore -0.0130 [-0.0194, -0.0067], bucket Brier +0.0002
[-0.0011, +0.0016], CRPS -0.0060; both halves significant on their own.  The
learning curve needs about 120 station-days before the fit pays, which is why
the rule it replaces (charter 199's centre-MSE + CRPS blend over 21 days,
median two local rows) measured ~0.

Parameters are fitted by the daily sweep (``scripts/fit_station_model_weights.py``)
into ``station_model_weight_params`` and served as of the decision; a forward
veto (``station_model_weight_veto``) returns every decision to the seat
weights when the served weights score significantly worse than the seat's.
Any failure serves the seat weights and says why in the receipt.
"""
from __future__ import annotations

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

import numpy as np

FUSION_BLEND_RULE_VERSION = "station_logscore_mixture_per_system_shrink_v1"
FUSION_BLEND_RECEIPT_SCHEMA = "fusion_blend_receipt_v2"
FUSION_BLEND_TRAINING_SCHEMA = "fusion_blend_training_fact_v1"
STATION_WEIGHT_FACT_SCHEMA = "station_model_weight_fact_v1"
STATION_WEIGHT_PARAMS_SCHEMA = "station_model_weight_params_v1"
STATION_WEIGHT_VETO_SCHEMA = "station_model_weight_veto_v1"
# Fixed before scoring (PROTOCOL.md): the shrinkage grid, the tuning window,
# the five horizon bins one station-day contributes (hours before the local
# day's end; the last decision in each), and the fit's iteration rule.
TAU_GRID = (5.0, 10.0, 20.0, 40.0, 80.0, 160.0)
TAU_WINDOW_DAYS = (2, 22)
HORIZON_BINS_H = ((48.0, math.inf), (36.0, 48.0), (24.0, 36.0), (12.0, 24.0), (0.0, 12.0))
FIT_ITERATIONS = 300
FIT_STEP = 2.0
FIT_TOLERANCE = 1e-6
# Days before this come from the replay backfill
# (scripts/backfill_station_model_weight_facts.py); from it on, from envelopes.
FORWARD_FACTS_FROM = "2026-10-01"
# The forward veto (charter 138's shape): served against the seat on the same
# decisions, station-day bootstrap of the pooled log score.
VETO_MIN_DAYS = 3
VETO_MIN_CELLS = 60
VETO_DRAWS = 2000
VETO_SEED = 20261002
VETO_CONFIDENCE = 0.95
# A served weight this small is a member the fit voted out; dropping a
# certified member is a seating question this layer does not answer.
FUSION_BLEND_MIN_WEIGHT = 1e-6
FUSION_BLEND_MODE_BLEND = "blend"
FUSION_BLEND_MODE_SHADOW = "shadow"
FUSION_BLEND_MODE_OFF = "off"
FUSION_BLEND_MODES = (
    FUSION_BLEND_MODE_BLEND, FUSION_BLEND_MODE_SHADOW, FUSION_BLEND_MODE_OFF,
)
# Served default: BLEND since 2026-09-24 (user: 「别等向前日了，先用历史样本」,
# 「上线」).  History before that, kept because it is why the default moved:
# the frozen candidate was re-scored before go-live
# under the training density production actually has (production_regime/,
# 2026-09-23): three-down on the 515 station-days it was designed on, but on
# the 143 same-period station-days the study never used RMSE +1.57%
# [+0.4%, +4.2%] and CRPS +1.02%, and on the 96 station-days settled after
# the freeze RMSE +0.53%, CRPS +0.32% -- both halves of the blend worse on
# their own.  So every compile computes the blend and records it, the seat's
# weights are what is served, and the forward record on days nobody has seen
# decides the switch.  ``blend`` serves it (a served-arithmetic change: bump
# the three stages with it); ``off`` skips the computation entirely.
#
# Charter 211 (2026-09-24, amended the same day): the switch is judged on
# the bucket distribution's two proper scores -- bucket Brier and log score
# both lower -- and Reliability / Resolution (Brier's decomposition) and
# RMSE / CRPS are diagnostics beside it.  Re-judged on the historical replay
# (docs/research/fusion_blend_historical_20260923, 2,908 station-days / 65
# days): Brier 0.72841 -> 0.72857 (+0.02%), log score 1.59774 -> 1.59850
# (+0.05%) -- both worse.  That replay was four families in fact: AIFS ENS
# and AIGEFS history sits in the SSOT with its backfill date as the arrival,
# so the replay never let it arrive.  With modelled arrivals, six families,
# decisions before the local day's end (docs/research/
# recheck_six_families_20260924, 2,457 station-days / 55 days): bucket Brier
# 0.72734 -> 0.72652 [-0.00142, -0.00019], log score 1.6317 -> 1.6297
# [-0.0046, +0.0006]; day-ahead roots both intervals clear of zero; and the
# same gain under charter 216's candidate single-system fit
# (fusion199_x_charter216_20260924).  So the blend serves; the seat weights
# stay its prior and its fallback, and ``shadow`` restores the old default.
FUSION_BLEND_MODE = (
    os.environ.get("P0_FUSION_BLEND_MODE", FUSION_BLEND_MODE_BLEND).strip()
    or FUSION_BLEND_MODE_BLEND
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


# ------------------------------------------------- charter 242: facts and fit

STATION_WEIGHT_DDL = (
    "CREATE TABLE IF NOT EXISTS station_model_weight_fact ("
    " station_id TEXT NOT NULL, event_date TEXT NOT NULL, horizon_bin INTEGER NOT NULL,"
    " truth_revision INTEGER NOT NULL, decision_at_utc TEXT NOT NULL, truth_c REAL NOT NULL,"
    " truth_known_at_utc TEXT NOT NULL, truth_source TEXT NOT NULL,"
    " probability_source TEXT NOT NULL, probability_grid TEXT NOT NULL,"
    " systems_json TEXT NOT NULL, truth_probability_json TEXT NOT NULL,"
    " seat_weights_json TEXT NOT NULL, served_weights_json TEXT NOT NULL,"
    " fusion_rule_version TEXT NOT NULL, fusion_state TEXT NOT NULL, source_ref TEXT NOT NULL,"
    " schema TEXT NOT NULL, created_at_utc TEXT NOT NULL, content_sha256 TEXT NOT NULL,"
    " PRIMARY KEY (station_id, event_date, horizon_bin, truth_revision))",
    "CREATE TABLE IF NOT EXISTS station_model_weight_params ("
    " params_id TEXT PRIMARY KEY, station_id TEXT NOT NULL, computed_at_utc TEXT NOT NULL,"
    " rule_version TEXT NOT NULL, tau REAL NOT NULL, theta_json TEXT NOT NULL,"
    " days_by_system_json TEXT NOT NULL, fact_count INTEGER NOT NULL,"
    " facts_sha256 TEXT NOT NULL, tau_evidence_json TEXT NOT NULL,"
    " schema TEXT NOT NULL, content_sha256 TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS station_model_weight_params_asof_idx"
    " ON station_model_weight_params(station_id, computed_at_utc)",
    "CREATE TABLE IF NOT EXISTS station_model_weight_veto ("
    " veto_id TEXT PRIMARY KEY, computed_at_utc TEXT NOT NULL, rule_version TEXT NOT NULL,"
    " vetoed INTEGER NOT NULL, evidence_json TEXT NOT NULL,"
    " schema TEXT NOT NULL, content_sha256 TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS station_model_weight_veto_asof_idx"
    " ON station_model_weight_veto(computed_at_utc)",
)


def _truth_floor() -> float:
    # Imported here: materialization imports modules that import this one.
    from .materialization import PROBABILITY_MODEL_ZERO_FLOOR  # noqa: PLC0415

    return float(PROBABILITY_MODEL_ZERO_FLOOR)


class WeightFact:
    """One representative decision of one settled station-day."""

    __slots__ = ("station", "event_date", "horizon_bin", "revision", "at", "known",
                 "pool", "truth_probability", "seat", "served", "rule_version", "state")

    def __init__(self, record: Mapping[str, Any]) -> None:
        self.station = str(record["station_id"])
        self.event_date = str(record["event_date"])
        self.horizon_bin = int(record["horizon_bin"])
        self.revision = int(record["truth_revision"])
        self.at = _stamp(record["decision_at_utc"])
        self.known = _stamp(record["truth_known_at_utc"])
        self.pool = tuple(sorted(json.loads(record["systems_json"])))
        self.truth_probability = {
            str(k): float(v) for k, v in json.loads(record["truth_probability_json"]).items()
        }
        self.seat = {str(k): float(v) for k, v in json.loads(record["seat_weights_json"]).items()}
        self.served = {str(k): float(v) for k, v in json.loads(record["served_weights_json"]).items()}
        self.rule_version = str(record["fusion_rule_version"])
        self.state = str(record["fusion_state"])


def knowable_facts(facts: Sequence[WeightFact], at: datetime, *, strict: bool = True) -> list[WeightFact]:
    """Per (station, day, bin), the newest truth revision known before ``at``."""

    best: dict[tuple[str, str, int], WeightFact] = {}
    for fact in facts:
        if (fact.known >= at) if strict else (fact.known > at):
            continue
        slot = (fact.station, fact.event_date, fact.horizon_bin)
        held = best.get(slot)
        if held is None or fact.revision > held.revision:
            best[slot] = fact
    return sorted(best.values(), key=lambda f: (f.station, f.event_date, f.horizon_bin))


def fit_theta(facts: Sequence[WeightFact], systems: Sequence[str]) -> dict[str, float]:
    """Softmax logits of the station's log-score-optimal mixture.

    ``systems`` is the active registry in sorted order; the logits are
    centred over it (the research fit, ``score.py eb_fit``).  A system never
    scored here keeps a logit the centring alone moves; its shrinkage weight
    is zero, so the logit never serves.
    """

    order = sorted(str(s) for s in systems)
    index = {s: i for i, s in enumerate(order)}
    rows = [f for f in facts if any(s in index for s in f.pool)]
    theta = np.zeros(len(order))
    if not rows:
        return {s: 0.0 for s in order}
    floor = _truth_floor()
    mask = np.zeros((len(rows), len(order)))
    prob = np.zeros((len(rows), len(order)))
    for row, fact in enumerate(rows):
        for system in fact.pool:
            if system in index:
                mask[row, index[system]] = 1.0
                prob[row, index[system]] = max(fact.truth_probability.get(system, 0.0), floor)
    for _ in range(FIT_ITERATIONS):
        e = np.exp(theta - theta.max()) * mask
        q = e / e.sum(axis=1, keepdims=True)
        pooled = (q * prob).sum(axis=1, keepdims=True)
        grad = (q * prob / pooled - q).mean(axis=0)
        theta = theta + FIT_STEP * grad
        theta = theta - theta.mean()
        if float(np.abs(grad).max()) < FIT_TOLERANCE:
            break
    return {s: float(theta[index[s]]) for s in order}


def days_by_system(facts: Sequence[WeightFact]) -> dict[str, int]:
    """Station-days on which each system was scored."""

    days: dict[str, set[str]] = defaultdict(set)
    for fact in facts:
        for system in fact.pool:
            days[system].add(fact.event_date)
    return {s: len(v) for s, v in sorted(days.items())}


def mixture_weights(
    seat: Mapping[str, float],
    theta: Mapping[str, float],
    days: Mapping[str, int],
    tau: float,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Served weights over the seat's systems, and the parts that made them."""

    total = sum(float(v) for v in seat.values())
    prior = {s: float(v) / total for s, v in seat.items()}
    top = max(float(theta.get(s, 0.0)) for s in prior)
    raw = {s: math.exp(float(theta.get(s, 0.0)) - top) for s in prior}
    raw_total = sum(raw.values())
    fit = {s: v / raw_total for s, v in raw.items()}
    alpha = {s: int(days.get(s, 0)) / (int(days.get(s, 0)) + float(tau)) for s in prior}
    mixed = {s: (1.0 - alpha[s]) * prior[s] + alpha[s] * fit[s] for s in prior}
    mixed_total = sum(mixed.values())
    weights = {s: v / mixed_total for s, v in mixed.items()}
    return weights, {
        "station_fit_weights": dict(sorted(fit.items())),
        "alpha_by_system": dict(sorted(alpha.items())),
        "days_by_system": {s: int(days.get(s, 0)) for s in sorted(prior)},
    }


def pooled_log_score(fact: WeightFact, weights: Mapping[str, float]) -> float:
    """The fit's own score: each system's truth probability floored before
    pooling (as ``fit_theta`` scores it), then the pool floored again."""

    floor = _truth_floor()
    total = sum(float(weights[s]) for s in fact.pool)
    pooled = sum(
        float(weights[s]) / total * max(fact.truth_probability.get(s, 0.0), floor)
        for s in fact.pool
    )
    return -math.log(max(pooled, floor))


def choose_tau(
    facts: Sequence[WeightFact], systems: Sequence[str], as_of: datetime
) -> tuple[float, dict[str, Any]]:
    """TAU_GRID's best on the fleet's event days d-22..d-2 (d = as_of's date),
    each decision weighted by its own station's fit as of that decision."""

    today = as_of.date()
    lo, hi = TAU_WINDOW_DAYS
    window = [
        f for f in knowable_facts(facts, as_of, strict=False)
        if lo <= (today - date.fromisoformat(f.event_date)).days <= hi
    ]
    by_station: dict[str, list[WeightFact]] = defaultdict(list)
    for fact in facts:
        by_station[fact.station].append(fact)
    cache: dict[tuple[str, int], tuple[dict[str, float], dict[str, int]]] = {}
    losses = {tau: 0.0 for tau in TAU_GRID}
    for fact in window:
        prefix = knowable_facts(by_station[fact.station], fact.at)
        key = (fact.station, len(prefix))
        if key not in cache:
            cache[key] = (fit_theta(prefix, systems), days_by_system(prefix))
        theta, days = cache[key]
        seat = {s: fact.seat.get(s, 0.0) for s in fact.pool}
        if sum(seat.values()) <= 0:
            seat = {s: 1.0 for s in fact.pool}
        for tau in TAU_GRID:
            weights, _ = mixture_weights(seat, theta, days, tau)
            losses[tau] += pooled_log_score(fact, weights)
    if not window:
        return TAU_GRID[0], {"window_decisions": 0, "mean_log_score_by_tau": {}}
    means = {tau: value / len(window) for tau, value in losses.items()}
    best = min(TAU_GRID, key=lambda tau: (means[tau], tau))
    return best, {
        "window_decisions": len(window),
        "window_event_days": [(today - timedelta(days=hi)).isoformat(), (today - timedelta(days=lo)).isoformat()],
        "mean_log_score_by_tau": {str(tau): round(v, 9) for tau, v in means.items()},
    }


def veto_evidence(facts: Sequence[WeightFact], as_of: datetime) -> dict[str, Any]:
    """Served against the seat on the decisions this rule served."""

    served = [
        f for f in knowable_facts(facts, as_of, strict=False)
        if f.rule_version == FUSION_BLEND_RULE_VERSION and f.state == "APPLIED"
        and set(f.pool) <= set(f.seat) and set(f.pool) <= set(f.served)
    ]
    per_day: dict[tuple[str, str], list[float]] = defaultdict(list)
    for fact in served:
        per_day[(fact.station, fact.event_date)].append(
            pooled_log_score(fact, fact.served) - pooled_log_score(fact, fact.seat)
        )
    cells = np.array([float(np.mean(v)) for v in per_day.values()])
    days = len({day for _station, day in per_day})
    out: dict[str, Any] = {
        "rule": "served_minus_seat_pooled_logscore_station_day_bootstrap_v1",
        "station_days": int(len(cells)), "event_days": days,
        "min_days": VETO_MIN_DAYS, "min_cells": VETO_MIN_CELLS,
    }
    if len(cells) == 0:
        out.update(mean=None, ci=None, vetoed=False, enough_evidence=False)
        return out
    draws = np.random.default_rng(VETO_SEED).integers(0, len(cells), size=(VETO_DRAWS, len(cells)))
    boot = cells[draws].mean(axis=1)
    tail = 100.0 * (1.0 - VETO_CONFIDENCE) / 2.0
    low, high = (float(x) for x in np.percentile(boot, [tail, 100.0 - tail]))
    enough = days >= VETO_MIN_DAYS and len(cells) >= VETO_MIN_CELLS
    out.update(mean=float(cells.mean()), ci=[low, high], enough_evidence=enough,
               vetoed=bool(enough and low > 0.0))
    return out


_CACHE_LOCK = threading.Lock()
_TABLE_CACHE: dict[str, tuple[tuple[int, ...], dict[str, Any]]] = {}


def _generation(target: Path) -> tuple[int, ...]:
    stat = target.stat()
    generation: tuple[int, ...] = (stat.st_mtime_ns, stat.st_size)
    wal = target.with_name(target.name + "-wal")
    if wal.exists():
        wal_stat = wal.stat()
        generation = generation + (wal_stat.st_mtime_ns, wal_stat.st_size)
    return generation


def load_serving_tables(path: Path | str | None = None) -> dict[str, Any]:
    """Params per station (ascending by computation) and the veto rows,
    parsed once per file generation."""

    target = Path(path or FUSION_BLEND_TRAINING_DB)
    generation = _generation(target)
    key = str(target)
    with _CACHE_LOCK:
        cached = _TABLE_CACHE.get(key)
        if cached is not None and cached[0] == generation:
            return cached[1]
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        params: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in conn.execute(
            "SELECT * FROM station_model_weight_params WHERE rule_version=?"
            " ORDER BY station_id, computed_at_utc", (FUSION_BLEND_RULE_VERSION,)
        ):
            params[str(row["station_id"])].append({
                "params_id": str(row["params_id"]),
                "computed_at": _stamp(row["computed_at_utc"]),
                "computed_at_utc": str(row["computed_at_utc"]),
                "tau": float(row["tau"]),
                "theta": json.loads(row["theta_json"]),
                "days": json.loads(row["days_by_system_json"]),
            })
        vetoes = [
            {"veto_id": str(row["veto_id"]), "computed_at": _stamp(row["computed_at_utc"]),
             "vetoed": bool(row["vetoed"])}
            for row in conn.execute(
                "SELECT * FROM station_model_weight_veto WHERE rule_version=?"
                " ORDER BY computed_at_utc", (FUSION_BLEND_RULE_VERSION,)
            )
        ]
    finally:
        conn.close()
    tables = {"params": dict(params), "vetoes": vetoes}
    with _CACHE_LOCK:
        _TABLE_CACHE[key] = (generation, tables)
    return tables


def _as_of(rows: Sequence[Mapping[str, Any]], at: datetime) -> Mapping[str, Any] | None:
    held = None
    for row in rows:
        if row["computed_at"] <= at:
            held = row
        else:
            break
    return held


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
    SHADOW, which computes the served weights and records them as
    ``shadow_weights`` beside ``shadow_state``.  ``event_date``,
    ``timezone_name`` and ``lead_by_system`` are recorded context: the station
    fit is per station, not per lead.
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
        "seat_weights": dict(sorted(seat.items())),
    }

    def _seat(state: str, **extra: Any) -> tuple[dict[str, float], dict[str, Any]]:
        receipt.update(state=state, served_weights=dict(sorted(seat.items())), **extra)
        return dict(seat), receipt

    if active_mode == FUSION_BLEND_MODE_OFF:
        return _seat("OFF")
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
        tables = load_serving_tables(training_db)
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        return _seat("SEAT_TRAINING_UNAVAILABLE", error=type(error).__name__)
    veto = _as_of(tables["vetoes"], decision_at)
    if veto is not None:
        receipt["veto_id"] = veto["veto_id"]
        if veto["vetoed"]:
            return _seat("SEAT_VETOED")
    params = _as_of(tables["params"].get(str(station_id), []), decision_at)
    if params is None:
        return _seat("SEAT_NO_STATION_PARAMS")
    receipt.update(
        params_id=params["params_id"],
        params_computed_at_utc=params["computed_at_utc"],
        tau=params["tau"],
    )
    try:
        mixed, detail = mixture_weights(weighted, params["theta"], params["days"], params["tau"])
        receipt.update(detail)
        if not any(detail["days_by_system"].values()):
            return _seat("SEAT_NO_TRAINING_DAYS")
        served = feasible(
            mixed, {s: family_by_system[s] for s in weighted}, family_caps,
        )
    except (ValueError, KeyError, ZeroDivisionError, FloatingPointError, OverflowError) as error:
        return _seat("SEAT_FIT_FAILED", error=str(error)[:160])
    if min(served.values()) < FUSION_BLEND_MIN_WEIGHT:
        return _seat("SEAT_BLEND_VOTED_OUT_A_MEMBER", blended_weights=dict(sorted(mixed.items())))
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
