"""Charter 267 (三): the listing-time forecast F, computed before listing.

User ruling 2026-10-11 「挂牌前把f算好」.  The benchmark cluster's first trade on a
station-day comes minutes after Polymarket lists it (two days ahead, ~04:56Z
for the Asia/Europe/Africa batch), before the market has a mode, and its YES
sits at the bucket of a calibrated centre of the deterministic global models:
the raw median + a fleet-wide warm shift (the models' mean cold bias, +0.83 C
measured) + ~30% of the station's own correction (benchmark research rounds
32-35: forward exact 47% against 36% for a station-only correction).  Our own
P0 is not there at listing (first P0 a median 10 min to 3 h later, round 38)
and its mode is the weaker bucket for this play, so F is built separately,
from native deterministic runs only (``deterministic_forecast_family.
F_SYSTEM_IDS``: AIFS Single, IFS HRES, GFS, AIGFS, GDPS), for every roster
station and every event day from today to D+2, and kept ready:

  raw    = median over systems of each system's newest knowable run's
           local-day maximum (``feature_at``: covers the afternoon)
  c      = fleet mean of (truth - raw) over the trailing ``FLEET_DAYS`` days,
           every past day read as of its own listing instant (D-2 05:00Z);
           the research value ``RESEARCH_FLEET_C`` until ``MIN_FLEET_SAMPLES``
           station-days over ``MIN_FLEET_DAYS`` days exist
  corr_s = median(raw_m + b_sm) - raw, b_sm the station x system mean of
           (truth - raw_m) over the station's trailing days (as of d-2, >= 3)
  F_base = raw + c + LAMBDA x corr_s
  F      = F_base + the truth recalibration (补记五: ``fit_recalibration``, least
           absolute error of truth - F_base on the station correction, the
           members' skew and the change from the newest settled observation,
           every training day read as of its own listing instant)
  F_v3   (补记六, beside F, read by no lane yet) the same chain on STATION values:
           each member's grid value + lapse rate x (cell height - station
           height) from ``listing_forecast_f_terrain``, the GFS member from its
           13 km flux grid when in hand; c and the biases re-estimated on those
           values; station share ``V3_LAMBDA`` 0.9.  ``v3.f_c`` keeps the
           cluster-like warm lean; ``v3.f_recal_c`` is re-centred on the truth.

补记十 (user 2026-10-18 「比现行 F 准，那就替换 F」): ``f_c`` is now research
round 75's chain (``listing_forecast_f_mos``: station values, shrunk station
bias, shrunk inverse-MAE weights) on the same history, as of this pass's
clock; the chain above is still computed and written as ``f_legacy_c``.  The
keep-F pick is fitted and served around the chain's centre.

Read-only on the SSOT; writes one runtime file (``F_PATH``), never the SSOT.
F is an execution-layer number: it is not a probability, never enters P0,
P0-M, P1, a certificate or any formal authority.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from . import listing_forecast_f_terrain as terrain
from .canonical_contract import FORMAL_TRUTH_LANES_SQL
from .deterministic_forecast_family import F_SYSTEM_IDS, F_V3_SYSTEM_IDS, feature_at, global_roster
from .regional_point_lane import SYSTEM_IDS as _LANE_SYSTEM_IDS

ROOT = Path(__file__).resolve().parents[1]
F_PATH = Path(os.environ.get("PM_LISTING_FORECAST_F_PATH") or ROOT / "data" / "runtime" / "listing_forecast_f_v1.json")
SCHEMA = "listing_forecast_f_v1"
# 补记十 (user 2026-10-18 「比现行 F 准，那就替换 F」): F's centre is research round
# 75's chain -- station values, each system's station bias shrunk to its fleet
# bias, shrunk inverse-MAE weights (``listing_forecast_f_mos``); MAE 0.038 C
# [-0.057, -0.021] below the v3 centre in a strict walk-forward, both halves.
# The v3 centre is still computed and written as ``f_legacy_c``.
RULE = "r75_station_value_shrunk_bias_shrunk_inverse_mae_v4"
LEGACY_RULE = "deterministic_median_fleet_shift_station_share_truth_recal_v3"
# 补记四 (user 「改」 10-12): 0.3 -> 0.5.  Our values are nearest-land-cell
# grid values, not point values adjusted to the station's elevation, so a
# larger share of each station's own bias is needed: on the 440 research
# station-days, agreement with the cluster's first YES +7.3 points
# [+0.8, +13.7] (+7.1 / +7.3 in each half of the dates), hits unchanged.
LAMBDA = 0.5
RESEARCH_FLEET_C = 0.8
FLEET_DAYS = 30
STATION_DAYS = 60
MIN_FLEET_SAMPLES = 200
MIN_FLEET_DAYS = 14
MIN_STATION_SYSTEM_DAYS = 3
MIN_SYSTEMS = 3
LISTING_HOUR_UTC = 5
# 补记五 (user 「更准」 10-12): F's conditional recalibration against the truth,
# F = F_base + a + sum_j beta_j z_j over three conditions F was found biased on
# (benchmark research round 43): the station correction (under-applied), the
# corrected members' skew, mean - median (ignored), and the change from the
# newest settled observation to F (overshot).  Fitted by least absolute error
# on every roster station-day of the trailing ``RECAL_TRAIN_DAYS``, each read as
# of its own listing instant.  Round 43b, strictly forward over 834 station-days
# (09-20..10-10): MAE -0.063 C [-0.091, -0.034], hits on the market ladder
# +2.0 points [-1.1, +5.2], bias -0.45 -> -0.14 C.
RECAL_FEATURES = ("station_corr_c", "members_skew_c", "obs_to_f_change_c")
RECAL_TRAIN_DAYS = 60
RECAL_MIN_ROWS = 500
RECAL_MIN_DAYS = 14
RECAL_RIDGE = 1e-3
RECAL_MAX_ITER = 200
RECAL_IRLS_FLOOR = 1e-4
OBS_BACK_DAYS = (3, 4, 5)
# A station-day's truth counts as knowable this long after its local day ends.
SETTLE_HOURS = 3
# 补记六 (user 「加」 10-12): F_v3, the station-value F, written beside F and read
# by no lane until its forward check.  Each member's grid value is brought to
# the station's elevation at the standard lapse rate (``listing_forecast_f_
# terrain``), the GFS member from GFS's own ~13 km flux grid (hourly maxima)
# when that run is in hand; the fleet shift, the station biases and the
# recalibration are re-estimated on the station values; the station share is
# 0.9.  Benchmark research rounds 54-56: the cluster's centre follows station
# values one-for-one; this F reproduces its bucket 54.6% vs F's 45.8% (lambda
# chosen leave-one-day-out, both halves) and, re-centred, is as accurate as F
# (MAE -0.011 [-0.027, +0.005], hits +0.1 point over 717 station-days).
V3_RULE = "deterministic_station_values_lapse_sflux_v3"
V3_LAMBDA = 0.9
GFS_SYSTEM_ID = _LANE_SYSTEM_IDS["GFS"]
GFS_SFLUX_SYSTEM_ID = _LANE_SYSTEM_IDS["GFS_SFLUX"]
_HISTORY_CACHE_V3: dict[tuple[str, str], dict[str, float] | None] = {}
# Past days' listing-time maxima change only when the run table grows (a
# backfill can add a run that was knowable then); the cache is dropped
# whenever the table's newest rowid moves.
_HISTORY_CACHE: dict[tuple[str, str], dict[str, float] | None] = {}
_HISTORY_GENERATION: list[int | None] = [None]
# 补记七 (user 「上线」 10-16): the keep-F pick -- F's bucket, the one below or
# the one above.  The deterministic members' structure (skew, spread, warmest
# and coldest minus F, skill-weighted members minus F) and the median run
# change carry side information beyond F's position in its bucket (benchmark
# research round 65: walk-forward side logloss -0.018 [-0.035, -0.003] on the
# shoulder rows, BH-pass, both halves).  Over N(F, sigma) on the station-day's
# ladder, F's bucket keeps its probability and the two sides are reweighted by
# exp(kappa x (logit P_full - logit P_pos)); the pick is the most likely bucket
# (round 66 post-hoc: off F on 24% of station-days, there 35.0% vs F 27.8%;
# fleet single-bucket hit 33.3% -> 35.1%).  Both logits are refitted every
# refresh on the trailing ``RECAL_TRAIN_DAYS`` of settled station-days that
# left F's bucket, each read as of its own listing instant.
# User 10-16 「照样上线：生产F文件写pick、看板确定性预报显示选桶，标注未验证、不接车道」:
# the production replay of this very code (round 67, strictly as of each listing
# instant, 09-22..10-14) found no gain -- pick 33.5% vs F 33.3% (+0.2 [-1.7,
# +2.3]), the second half worse -- so it is written and shown, unverified, and
# no lane reads it.  The clean re-read is round 66's protocol on event days
# >= 10-15 (~10-30).
PICK_AUTHORITY = "DISPLAY_ONLY_UNVERIFIED"
PICK_RULE = "keep_f_member_tilt_v1"
PICK_KAPPA = 1.0
PICK_FEATURES = (
    "pos", "skew", "spread", "max_minus_f", "min_minus_f", "skill_weighted_minus_f", "run_change_median",
)
PICK_MIN_DAYS = 7
PICK_L2 = 1.0
PICK_ITER = 800
PICK_LR = 0.3
PICK_FLOOR = 1e-3
PICK_SKILL_DAYS = 30
PICK_SKILL_MIN = 5
PICK_SKILL_MAE_FLOOR_C = 0.3
_HISTORY_PREV_CACHE: dict[tuple[str, str], dict[str, float]] = {}
_LADDER_CACHE: dict[tuple[str, str], list[tuple[str, float | None, float | None]] | None] = {}


def listing_instant(event_date: date) -> str:
    """The batch listing instant F is for: two days ahead at ~05:00Z."""

    stamp = datetime.combine(event_date - timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
    return (stamp + timedelta(hours=LISTING_HOUR_UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")


def system_maxima(
    conn: sqlite3.Connection, *, station_id: str, event_date: date, as_of_utc: str, settlement_timezone: str,
    systems: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Each F system's newest knowable, afternoon-covering run at ``as_of_utc``."""

    features = feature_at(
        conn, station_id=station_id, event_date=event_date.isoformat(), as_of_utc=as_of_utc,
        settlement_timezone=settlement_timezone, systems=sorted(systems if systems is not None else F_SYSTEM_IDS),
    )
    return {
        system: {
            "tmax_c": round(float(feature["day_max_c"]), 3),
            "run_init_utc": str(feature["run_init_utc"]),
            "knowable_at_utc": str(feature["knowable_at_utc"]),
        }
        for system, feature in features.items()
    }


def formal_truths(conn: sqlite3.Connection, *, since: date, until: date) -> dict[tuple[str, str], float]:
    """Settled maxima, both formal lanes, latest revision (settlement source on a tie)."""

    best: dict[tuple[str, str], tuple[int, int, float]] = {}
    for station, day, value, lane, revision in conn.execute(
        "SELECT station_id, local_date, official_tmax_c, source_lane, revision_no"
        " FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND local_date BETWEEN ? AND ?"
        " AND official_tmax_c IS NOT NULL",
        (since.isoformat(), until.isoformat()),
    ):
        key = (str(station).upper(), str(day))
        rank = (int(revision or 0), 1 if lane == "SETTLEMENT_SOURCE" else 0, float(value))
        if key not in best or rank[:2] > best[key][:2]:
            best[key] = rank
    return {key: value[2] for key, value in best.items()}


def _history_point(
    conn: sqlite3.Connection, *, station_id: str, event_date: date, zone: str,
) -> dict[str, float] | None:
    key = (station_id, event_date.isoformat())
    if key not in _HISTORY_CACHE:
        maxima = system_maxima(
            conn, station_id=station_id, event_date=event_date,
            as_of_utc=listing_instant(event_date), settlement_timezone=zone,
        )
        _HISTORY_CACHE[key] = {system: entry["tmax_c"] for system, entry in maxima.items()} or None
    return _HISTORY_CACHE[key]


def station_values(
    maxima: Mapping[str, Mapping[str, Any]], *, station_id: str, table: Mapping[str, Any],
) -> dict[str, float]:
    """补记六: F's members as station values -- each grid value plus its lapse-rate offset.

    The GFS member is the flux-grid run (``GFS_SFLUX``) when in hand, else the
    0.25 degree run; a member the terrain table has no cell for is left out
    (fail closed), never used unadjusted.
    """

    out: dict[str, float] = {}
    for system in sorted(F_SYSTEM_IDS):
        source = system
        if system == GFS_SYSTEM_ID and GFS_SFLUX_SYSTEM_ID in maxima:
            source = GFS_SFLUX_SYSTEM_ID
        if source not in maxima:
            continue
        offset = terrain.offset_c(table, system_id=source, station_id=station_id)
        if offset is None:
            continue
        out[system] = float(maxima[source]["tmax_c"]) + offset
    return out


def _history_point_v3(
    conn: sqlite3.Connection, *, station_id: str, event_date: date, zone: str, table: Mapping[str, Any],
) -> dict[str, float] | None:
    key = (station_id, event_date.isoformat())
    if key not in _HISTORY_CACHE_V3:
        maxima = system_maxima(
            conn, station_id=station_id, event_date=event_date,
            as_of_utc=listing_instant(event_date), settlement_timezone=zone, systems=sorted(F_V3_SYSTEM_IDS),
        )
        _HISTORY_CACHE_V3[key] = station_values(maxima, station_id=station_id, table=table) or None
    return _HISTORY_CACHE_V3[key]


def _settled_at(day: date, zone: str) -> datetime:
    """When a station-day's truth is knowable: the end of its local day + ``SETTLE_HOURS``."""

    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo(zone))
    return end.astimezone(timezone.utc) + timedelta(hours=SETTLE_HOURS)


def _listing_dt(event_date: date) -> datetime:
    return datetime.fromisoformat(listing_instant(event_date).replace("Z", "+00:00"))


def _base_f(
    maxima: Mapping[str, float], rows: Sequence[tuple[date, dict[str, float], float, datetime]], *,
    event_date: date, fleet_c: float, knowable_by: datetime | None, lam: float = LAMBDA,
) -> dict[str, Any]:
    """F before recalibration: raw median + c + LAMBDA x the station correction.

    Each member's bias is the mean of (truth - member) over the station's days in
    [event - STATION_DAYS, event - 2].  ``knowable_by`` keeps only truths settled by
    that instant (a past day read as of its own listing); live it is None and every
    settled truth counts, as before the recalibration.
    """

    raw = statistics.median(maxima.values())
    cut, since = event_date - timedelta(days=2), event_date - timedelta(days=STATION_DAYS)
    biases: dict[str, float] = {}
    for system, value in maxima.items():
        errors = [
            truth - point[system] for past, point, truth, settled in rows
            if system in point and since <= past <= cut and (knowable_by is None or settled <= knowable_by)
        ]
        biases[system] = statistics.mean(errors) if len(errors) >= MIN_STATION_SYSTEM_DAYS else 0.0
    corrected = [maxima[system] + biases[system] for system in maxima]
    corr = statistics.median(corrected) - raw
    return {
        "raw": raw, "corr": corr, "biases": biases, "f": raw + fleet_c + lam * corr,
        "skew": statistics.mean(corrected) - statistics.median(corrected),
    }


def _fleet_shift(
    history: Mapping[str, Sequence[tuple[date, dict[str, float], float, datetime]]], *,
    since: date, knowable_by: datetime | None,
) -> tuple[float, str, int, int]:
    """c, its source, samples and days: the fleet mean of (truth - raw median) since ``since``."""

    errors = [
        (past, truth - statistics.median(point.values()))
        for rows in history.values() for past, point, truth, settled in rows
        if past >= since and len(point) >= MIN_SYSTEMS and (knowable_by is None or settled <= knowable_by)
    ]
    days = len({past for past, _err in errors})
    if len(errors) >= MIN_FLEET_SAMPLES and days >= MIN_FLEET_DAYS:
        return statistics.mean(err for _past, err in errors), "native_history", len(errors), days
    return RESEARCH_FLEET_C, "research_fallback", len(errors), days


def _obs_before_listing(station_truths: Mapping[date, tuple[float, datetime]], event_date: date) -> float | None:
    """The newest of the truths at event-3..-5 already settled at the event's listing instant."""

    listing = _listing_dt(event_date)
    for back in OBS_BACK_DAYS:
        hit = station_truths.get(event_date - timedelta(days=back))
        if hit is not None and hit[1] <= listing:
            return hit[0]
    return None


def _recal_features(base: Mapping[str, Any], obs: float | None) -> tuple[float, float, float]:
    """``RECAL_FEATURES``, in order (no settled observation: a zero change, as in the research)."""

    return (base["corr"], base["skew"], 0.0 if obs is None else base["f"] - obs)


def fit_recalibration(rows: Sequence[tuple[date, Sequence[float], float]]) -> dict[str, Any] | None:
    """Least absolute error fit of (truth - F) on the standardised ``RECAL_FEATURES``.

    Iteratively reweighted least squares (numpy only), the research objective
    mean |residual| + RECAL_RIDGE x |beta|^2 on standardised features (round 43b's
    L1 robust arm).  None below ``RECAL_MIN_ROWS`` rows or ``RECAL_MIN_DAYS`` days.
    """

    days = sorted({day for day, _x, _y in rows})
    if len(rows) < RECAL_MIN_ROWS or len(days) < RECAL_MIN_DAYS:
        return None
    X = np.array([x for _day, x, _y in rows], dtype=float)
    y = np.array([value for _day, _x, value in rows], dtype=float)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    # A constant feature standardises to zero in training; the ridge holds its beta at 0.
    sd = np.where(sd > 1e-9, sd, 1.0)
    A = np.c_[(X - mu) / sd, np.ones(len(X))]
    n, k = A.shape
    penalty = np.diag([2.0 * RECAL_RIDGE] * (k - 1) + [0.0])
    beta = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(RECAL_MAX_ITER):
        weights = 1.0 / np.maximum(np.abs(y - A @ beta), RECAL_IRLS_FLOOR)
        weighted = A * weights[:, None]
        step = np.linalg.solve(A.T @ weighted / n + penalty, weighted.T @ y / n)
        done = float(np.max(np.abs(step - beta))) < 1e-9
        beta = step
        if done:
            break
    if not np.all(np.isfinite(beta)):
        return None
    fitted = A @ beta
    return {
        "state": "FITTED",
        "features": list(RECAL_FEATURES),
        "mean": [round(float(v), 6) for v in mu],
        "sd": [round(float(v), 6) for v in sd],
        "beta_per_sd": [round(float(v), 6) for v in beta[:-1]],
        "intercept_c": round(float(beta[-1]), 6),
        "rows": n, "days": len(days), "train_from": days[0].isoformat(), "train_to": days[-1].isoformat(),
        "in_sample_mae_before_c": round(float(np.mean(np.abs(y))), 4),
        "in_sample_mae_after_c": round(float(np.mean(np.abs(y - fitted))), 4),
    }


def recal_correction(model: Mapping[str, Any], features: Sequence[float]) -> float:
    """The correction the fitted model adds to F for one station-day's features."""

    z = (np.array(features, dtype=float) - np.array(model["mean"])) / np.array(model["sd"])
    return float(z @ np.array(model["beta_per_sd"]) + model["intercept_c"])


def previous_run_maxima(
    conn: sqlite3.Connection, *, station_id: str, event_date: date, as_of_utc: str, settlement_timezone: str,
) -> dict[str, float]:
    """Each F system's run before its newest knowable afternoon-covering run (the same reader, one run back)."""

    from .regional_point_lane import runs_for  # noqa: PLC0415

    rows = runs_for(conn, station_id=station_id, event_date=event_date.isoformat(), as_of_utc=as_of_utc)
    newest = feature_at(
        conn, station_id=station_id, event_date=event_date.isoformat(), as_of_utc=as_of_utc,
        settlement_timezone=settlement_timezone, systems=sorted(F_SYSTEM_IDS), rows=rows,
    )
    rest = [
        row for row in rows
        if not (row["ensemble_system_id"] in newest and row["run_init_utc"] == newest[row["ensemble_system_id"]]["run_init_utc"])
    ]
    before = feature_at(
        conn, station_id=station_id, event_date=event_date.isoformat(), as_of_utc=as_of_utc,
        settlement_timezone=settlement_timezone, systems=sorted(F_SYSTEM_IDS), rows=rest,
    )
    return {system: float(value["day_max_c"]) for system, value in before.items()}


def _history_prev(conn: sqlite3.Connection, *, station_id: str, event_date: date, zone: str) -> dict[str, float]:
    key = (station_id, event_date.isoformat())
    if key not in _HISTORY_PREV_CACHE:
        _HISTORY_PREV_CACHE[key] = previous_run_maxima(
            conn, station_id=station_id, event_date=event_date, as_of_utc=listing_instant(event_date),
            settlement_timezone=zone,
        )
    return _HISTORY_PREV_CACHE[key]


def station_day_rungs(
    conn: sqlite3.Connection, *, station_id: str, event_date: date, prefer_registry: bool,
) -> list[tuple[str, float | None, float | None]] | None:
    """The station-day's ladder in C, lowest first: the registry's buckets (a listed
    day, before any decision) or the ladder its newest committed decision was priced
    against (``station_day_ladder``, kept back to the oldest decision)."""

    from .market_registry import scope_ladder_rows  # noqa: PLC0415
    from .p0_producer import bucket_intervals_from_registry_rows  # noqa: PLC0415
    from .station_day_ladder import station_day_ladder  # noqa: PLC0415

    def registry() -> list[tuple[str, float | None, float | None]] | None:
        # The newest snapshot's rows, not every snapshot's: all of them at once
        # are the ladder hundreds of times over, which the parser refused for
        # every station-day, so the registry branch never read (2026-10-18).
        rows = scope_ladder_rows(conn, station_id=station_id, event_date=event_date.isoformat())
        if not rows:
            return None
        try:
            intervals = bucket_intervals_from_registry_rows(rows)
        except Exception:  # noqa: BLE001 -- an invalid contract is no ladder (fail closed)
            return None
        return sorted(((label, lo, hi) for label, (lo, hi) in intervals.items()),
                      key=lambda rung: float("-inf") if rung[1] is None else rung[1])

    def decided() -> list[tuple[str, float | None, float | None]] | None:
        hit = station_day_ladder(conn, station_id=station_id, event_date=event_date.isoformat())
        return [(bucket, lower, upper) for bucket, _ordinal, lower, upper in hit[2]] if hit and hit[2] else None

    try:
        return (registry() or decided()) if prefer_registry else (decided() or registry())
    except sqlite3.Error:  # a database without the market tables has no ladder (fail closed; F itself unaffected)
        return None


def _rung_edges(rungs: Sequence[tuple[str, float | None, float | None]]) -> tuple[list[tuple[float, float]], float] | None:
    """Closed edges (open ends one interior width out) and the interior width."""

    widths = [hi - lo for _label, lo, hi in rungs if lo is not None and hi is not None]
    if len(rungs) < 3 or not widths:
        return None
    width = statistics.median(widths)
    edges = []
    for _label, lo, hi in rungs:
        lo2 = hi - width if lo is None else lo
        hi2 = lo + width if hi is None else hi
        edges.append((float(lo2), float(hi2)))
    return edges, width


def _rung_of(edges: Sequence[tuple[float, float]], value: float) -> int:
    """The rung holding ``value``, lower edge inclusive, the open ends catching the rest."""

    if value < edges[0][1]:
        return 0
    for index, (lower, upper) in enumerate(edges):
        if lower <= value < upper:
            return index
    return len(edges) - 1


def pick_features(
    *, f_c: float, edges: Sequence[tuple[float, float]], width: float, maxima: Mapping[str, float],
    biases: Mapping[str, float], previous: Mapping[str, float], errors: Mapping[str, Sequence[float]],
) -> dict[str, float]:
    """``PICK_FEATURES`` for one station-day (benchmark research round 65's definitions, in rungs)."""

    fb = _rung_of(edges, f_c)
    centre = (edges[fb][0] + edges[fb][1]) / 2.0
    corrected = {system: value + biases.get(system, 0.0) for system, value in maxima.items()}
    values = list(corrected.values())
    weights = {}
    for system in corrected:
        recent = list(errors.get(system) or ())[-PICK_SKILL_DAYS:]
        if len(list(errors.get(system) or ())) >= PICK_SKILL_MIN and recent:
            weights[system] = 1.0 / max(PICK_SKILL_MAE_FLOOR_C, statistics.mean(abs(v) for v in recent))
    skill = (
        (sum(weights[s] * corrected[s] for s in weights) / sum(weights.values()) - f_c) / width
        if len(weights) >= 2 else 0.0
    )
    changes = [maxima[s] - previous[s] for s in maxima if s in previous]
    return {
        "pos": (f_c - centre) / width,
        "skew": (statistics.mean(values) - statistics.median(values)) / width,
        "spread": statistics.pstdev(values) / width,
        "max_minus_f": (max(values) - f_c) / width,
        "min_minus_f": (min(values) - f_c) / width,
        "skill_weighted_minus_f": skill,
        "run_change_median": statistics.median(changes) / width if changes else 0.0,
    }


def _fit_side_logit(X: np.ndarray, warm: np.ndarray) -> dict[str, Any]:
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd > 1e-9, sd, 1.0)
    Z = np.c_[(X - mu) / sd, np.ones(len(X))]
    w = np.zeros(Z.shape[1])
    for _ in range(PICK_ITER):
        p = 1.0 / (1.0 + np.exp(-Z @ w))
        grad = Z.T @ (p - warm) / len(X)
        grad[:-1] += PICK_L2 * w[:-1] / len(X)
        w -= PICK_LR * grad
    return {"mean": mu.tolist(), "sd": sd.tolist(), "weights": w[:-1].tolist(), "intercept": float(w[-1])}


def _side_logit(fit: Mapping[str, Any], x: Sequence[float]) -> float:
    z = (np.array(x, dtype=float) - np.array(fit["mean"])) / np.array(fit["sd"])
    return float(z @ np.array(fit["weights"]) + fit["intercept"])


def _member_errors(
    rows: Sequence[tuple[date, dict[str, float], float, datetime]], *, cut: date, knowable_by: datetime,
) -> dict[str, list[float]]:
    """Each system's (truth - listing-time maximum) on the station's settled days up to ``cut``, oldest first."""

    out: dict[str, list[float]] = {}
    for past, point, truth, settled in rows:
        if past <= cut and settled <= knowable_by:
            for system, value in point.items():
                out.setdefault(system, []).append(truth - value)
    return out


def _live_pick(
    conn: sqlite3.Connection, fit: Mapping[str, Any] | None, *, station: str, zone: str, event_date: date,
    as_of: str, now: datetime, f_c: float, maxima: Mapping[str, float], biases: Mapping[str, float],
    rows: Sequence[tuple[date, dict[str, float], float, datetime]],
) -> dict[str, Any]:
    if fit is None:
        return {"state": "INSUFFICIENT_HISTORY"}
    rungs = station_day_rungs(conn, station_id=station, event_date=event_date, prefer_registry=True)
    shaped = _rung_edges(rungs or [])
    if shaped is None:
        return {"state": "NO_LADDER"}
    edges, width = shaped
    features = pick_features(
        f_c=f_c, edges=edges, width=width, maxima=maxima, biases=biases,
        previous=previous_run_maxima(conn, station_id=station, event_date=event_date, as_of_utc=as_of, settlement_timezone=zone),
        errors=_member_errors(rows, cut=now.date() - timedelta(days=2), knowable_by=now),
    )
    return keep_f_pick(fit, f_c=f_c, rungs=rungs or [], features=features)


def fit_pick(rows: Sequence[tuple[date, Mapping[str, float], int, float]]) -> dict[str, Any] | None:
    """The two side logits and sigma from training rows ``(day, features, side, truth_centre - F)``.

    Side: +1 the truth's rung above F's, -1 below, 0 on it (kept for sigma, not for the logits).
    """

    off = [(feat, side) for _day, feat, side, _err in rows if side != 0]
    days = {day for day, _f, side, _e in rows if side != 0}
    if len(days) < PICK_MIN_DAYS or len(off) < 30:
        return None
    warm = np.array([side > 0 for _feat, side in off], dtype=float)
    full = np.array([[feat[name] for name in PICK_FEATURES] for feat, _side in off], dtype=float)
    return {
        "state": "FITTED", "rule": PICK_RULE, "authority": PICK_AUTHORITY, "kappa": PICK_KAPPA, "features": list(PICK_FEATURES),
        "pos": _fit_side_logit(full[:, :1], warm), "full": _fit_side_logit(full, warm),
        "sigma_c": float(np.sqrt(np.mean([err * err for _d, _f, _s, err in rows]))),
        "rows": len(rows), "off_rows": len(off), "days": len({day for day, *_rest in rows}),
        "train_from": min(day for day, *_rest in rows).isoformat(), "train_to": max(day for day, *_rest in rows).isoformat(),
    }


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def keep_f_pick(
    fit: Mapping[str, Any], *, f_c: float, rungs: Sequence[tuple[str, float | None, float | None]],
    features: Mapping[str, float],
) -> dict[str, Any]:
    """N(F, sigma) on the ladder, F's rung held, sides tilted by kappa x the logit increment; the top rung."""

    shaped = _rung_edges(rungs)
    if shaped is None:
        return {"state": "NO_LADDER"}
    edges, _width = shaped
    fb = _rung_of(edges, f_c)
    sigma = float(fit["sigma_c"])
    cdf = [0.0] + [_ncdf((edges[i][1] - f_c) / sigma) for i in range(len(edges) - 1)] + [1.0]
    p = np.maximum(np.diff(np.array(cdf)), PICK_FLOOR)
    p = p / p.sum()
    x = [features[name] for name in PICK_FEATURES]
    z_full, z_pos = _side_logit(fit["full"], x), _side_logit(fit["pos"], x[:1])
    increment = z_full - z_pos
    above, below = float(p[fb + 1:].sum()), float(p[:fb].sum())
    q = p.copy()
    if above > 0.0 and below > 0.0:
        odds = above / below * math.exp(float(fit["kappa"]) * increment)
        total = above + below
        new_above = total * odds / (1.0 + odds)
        q[fb + 1:] *= new_above / above
        q[:fb] *= (total - new_above) / below
        q = np.maximum(q, PICK_FLOOR)
        q = q / q.sum()
    top = int(np.argmax(q))
    offset = top - fb
    if abs(offset) > 1:  # a side keeps its own shape, so its top is the shoulder; never seen
        return {"state": "PICK_BEYOND_SHOULDER"}
    return {
        "state": "READY", "rule": fit["rule"], "authority": PICK_AUTHORITY, "offset": offset,
        "bucket": rungs[top][0], "f_bucket": rungs[fb][0],
        "p_warm": round(1.0 / (1.0 + math.exp(-z_full)), 4), "p_warm_position": round(1.0 / (1.0 + math.exp(-z_pos)), 4),
        "tilt_logit": round(increment, 4), "sigma_c": round(sigma, 3),
        "probabilities": {
            rungs[i][0]: round(float(q[i]), 4) for i in (fb - 1, fb, fb + 1) if 0 <= i < len(rungs)
        },
        "features": {name: round(float(value), 4) for name, value in features.items()},
    }


def compute(
    conn: sqlite3.Connection, *, now: datetime | None = None,
    roster: Sequence[tuple[str, float, float, str]] | None = None,
) -> dict[str, Any]:
    """F for every roster station x event day today..D+2 (UTC calendar)."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    as_of = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stations = list(roster if roster is not None else global_roster(conn))
    today = now.date()
    generation = conn.execute("SELECT max(rowid) FROM calibration_regional_point_run").fetchone()[0]
    if generation != _HISTORY_GENERATION[0]:
        _HISTORY_CACHE.clear()
        _HISTORY_CACHE_V3.clear()
        _HISTORY_PREV_CACHE.clear()
        _HISTORY_GENERATION[0] = generation
    table = terrain.read()
    # The oldest training day needs its own STATION_DAYS of bias history.
    history_from = today - timedelta(days=RECAL_TRAIN_DAYS + max(FLEET_DAYS, STATION_DAYS) + 4)
    truths = formal_truths(conn, since=history_from, until=today)
    zones = {station: zone for station, _lat, _lon, zone in stations}
    station_truths: dict[str, dict[date, tuple[float, datetime]]] = {station: {} for station in zones}
    for (st, day), truth in truths.items():
        if st in zones:
            past = date.fromisoformat(day)
            station_truths[st][past] = (truth, _settled_at(past, zones[st]))
    # History: each past settled day's listing-time maxima, per station.
    history: dict[str, list[tuple[date, dict[str, float], float, datetime]]] = {}
    for station, zone in zones.items():
        rows = []
        for past, (truth, settled) in sorted(station_truths[station].items()):
            point = _history_point(conn, station_id=station, event_date=past, zone=zone)
            if point:
                rows.append((past, point, truth, settled))
        history[station] = rows
    fleet_c, fleet_source, fleet_samples, fleet_days = _fleet_shift(
        history, since=today - timedelta(days=FLEET_DAYS), knowable_by=None,
    )
    # 补记五: each settled day in the training window, F as it stood at that day's
    # own listing instant (members, truths and c knowable then), against its truth.
    train: list[tuple[date, tuple[float, float, float], float]] = []
    past_fleet: dict[date, float] = {}
    pick_inputs: list[tuple[str, date, dict[str, float], float, dict[str, Any], tuple[float, float, float], datetime]] = []
    for station, rows in history.items():
        for past, point, truth, _settled in rows:
            if past < today - timedelta(days=RECAL_TRAIN_DAYS) or len(point) < MIN_SYSTEMS:
                continue
            listing = _listing_dt(past)
            if past not in past_fleet:
                past_fleet[past] = _fleet_shift(
                    history, since=past - timedelta(days=2 + FLEET_DAYS), knowable_by=listing,
                )[0]
            base = _base_f(point, rows, event_date=past, fleet_c=past_fleet[past], knowable_by=listing)
            obs = _obs_before_listing(station_truths[station], past)
            train.append((past, _recal_features(base, obs), truth - base["f"]))
            pick_inputs.append((station, past, point, truth, base, _recal_features(base, obs), listing))
    model = fit_recalibration(train)
    # 补记十: F's centre is round 75's chain (``listing_forecast_f_mos``) on the
    # same history -- each past station-day's members at its own listing
    # instant, brought to station values.  No terrain table, no chain: F fails
    # closed (the legacy centre is kept beside it, never served as F).
    from . import listing_forecast_f_mos as mos  # noqa: PLC0415

    chain = None
    chain_xst: dict[tuple[str, date], dict[str, float]] = {}
    if table is not None:
        chain_rows = []
        for station, rows in history.items():
            for past, point, truth, settled in rows:
                if past < today - timedelta(days=mos.HISTORY_DAYS):
                    continue
                xst = mos.station_values({s: {"tmax_c": v} for s, v in point.items()}, station=station, table=table)
                chain_xst[(station, past)] = xst
                chain_rows.append({"station": station, "day": past, "truth": truth, "settled": settled,
                                   "listing": _listing_dt(past), "xst": xst})
        chain = mos.Chain(chain_rows)
    # 补记七: the keep-F pick's training rows -- each settled day's F (as recalibrated
    # now) on the ladder that day was priced against, its members as of its listing.
    pick_rows: list[tuple[date, dict[str, float], int, float]] = []
    for station, past, point, truth, base, features, listing in pick_inputs:
        key = (station, past.isoformat())
        if key not in _LADDER_CACHE:
            _LADDER_CACHE[key] = station_day_rungs(conn, station_id=station, event_date=past, prefer_registry=False)
        shaped = _rung_edges(_LADDER_CACHE[key] or [])
        if shaped is None:
            continue
        edges, width = shaped
        # 补记十: the pick is fitted around the F it is served beside -- the
        # chain's centre as of that day's listing instant.
        past_centre = None if chain is None else chain.centre(
            station=station, day=past, xst=chain_xst.get((station, past), {}),
        )
        if past_centre is None:
            continue
        f_past = past_centre["mos_c"]
        fb, ty = _rung_of(edges, f_past), _rung_of(edges, truth)
        feats = pick_features(
            f_c=f_past, edges=edges, width=width, maxima=point, biases=base["biases"],
            previous=_history_prev(conn, station_id=station, event_date=past, zone=zones[station]),
            errors=_member_errors(history[station], cut=listing.date() - timedelta(days=2), knowable_by=listing),
        )
        pick_rows.append((past, feats, (ty > fb) - (ty < fb), (edges[ty][0] + edges[ty][1]) / 2.0 - f_past))
    pick_model = fit_pick(pick_rows)
    # 补记六: the same chain on station values, when the terrain table is in hand.
    history3: dict[str, list[tuple[date, dict[str, float], float, datetime]]] = {}
    fleet3 = model3 = None
    train3: list[tuple[date, tuple[float, float, float], float]] = []
    if table is not None:
        for station, zone in zones.items():
            rows3 = []
            for past, (truth, settled) in sorted(station_truths[station].items()):
                point = _history_point_v3(conn, station_id=station, event_date=past, zone=zone, table=table)
                if point:
                    rows3.append((past, point, truth, settled))
            history3[station] = rows3
        fleet3 = _fleet_shift(history3, since=today - timedelta(days=FLEET_DAYS), knowable_by=None)
        past_fleet3: dict[date, float] = {}
        for station, rows3 in history3.items():
            for past, point, truth, _settled in rows3:
                if past < today - timedelta(days=RECAL_TRAIN_DAYS) or len(point) < MIN_SYSTEMS:
                    continue
                listing = _listing_dt(past)
                if past not in past_fleet3:
                    past_fleet3[past] = _fleet_shift(
                        history3, since=past - timedelta(days=2 + FLEET_DAYS), knowable_by=listing,
                    )[0]
                base3 = _base_f(point, rows3, event_date=past, fleet_c=past_fleet3[past], knowable_by=listing, lam=V3_LAMBDA)
                obs = _obs_before_listing(station_truths[station], past)
                train3.append((past, _recal_features(base3, obs), truth - base3["f"]))
        model3 = fit_recalibration(train3)
    out: dict[str, Any] = {}
    for station, zone in zones.items():
        for offset in (0, 1, 2):
            event_date = today + timedelta(days=offset)
            maxima_all = system_maxima(
                conn, station_id=station, event_date=event_date, as_of_utc=as_of, settlement_timezone=zone,
                systems=sorted(F_V3_SYSTEM_IDS),
            )
            # F's own members: everything but the flux-grid GFS, which only F_v3 reads.
            maxima = {system: value for system, value in maxima_all.items() if system != GFS_SFLUX_SYSTEM_ID}
            entry: dict[str, Any] = {"systems": maxima, "n_systems": len(maxima)}
            if table is not None:
                entry["v3"] = _v3_entry(
                    maxima_all, station=station, event_date=event_date, table=table,
                    rows=history3.get(station, []), fleet_c=fleet3[0], model=model3,
                    obs=_obs_before_listing(station_truths.get(station, {}), event_date),
                )
            if len(maxima) < MIN_SYSTEMS:
                entry["state"] = "TOO_FEW_SYSTEMS"
                out[f"{station}|{event_date.isoformat()}"] = entry
                continue
            base = _base_f(
                {system: value["tmax_c"] for system, value in maxima.items()}, history.get(station, []),
                event_date=event_date, fleet_c=fleet_c, knowable_by=None,
            )
            # Each member's own correction, for readers that look past the median.
            for system, value in maxima.items():
                value["bias_c"] = round(base["biases"][system], 3)
            features = _recal_features(base, _obs_before_listing(station_truths.get(station, {}), event_date))
            correction = recal_correction(model, features) if model else 0.0
            centre = None if chain is None else chain.centre(
                station=station, day=event_date,
                xst=mos.station_values(maxima, station=station, table=table), cutoff=now,
            )
            entry.update({
                "state": "READY" if centre is not None else (
                    "TERRAIN_TABLE_MISSING" if table is None else "TOO_FEW_STATION_VALUES"
                ),
                "raw_median_c": round(base["raw"], 3),
                "fleet_c": round(fleet_c, 3),
                "station_corr_c": round(base["corr"], 3),
                "lambda": LAMBDA,
                "f_base_c": round(base["f"], 3),
                "recal_features": {name: round(value, 3) for name, value in zip(RECAL_FEATURES, features)},
                "recal_c": round(correction, 3) if model else None,
                # 补记十: the pre-chain centre, kept for comparison; never F.
                "f_legacy_c": round(base["f"] + correction, 3),
                "f_c": None if centre is None else centre["mos_c"],
                "listing_instant_utc": listing_instant(event_date),
            })
            if centre is None:
                out[f"{station}|{event_date.isoformat()}"] = entry
                continue
            entry["centre"] = {k: centre[k] for k in ("l1_c", "l2_c", "members_l1", "members_l2")}
            entry["pick"] = _live_pick(
                conn, pick_model, station=station, zone=zone, event_date=event_date, as_of=as_of, now=now,
                f_c=centre["mos_c"], maxima={system: value["tmax_c"] for system, value in maxima.items()},
                biases=base["biases"], rows=history.get(station, []),
            )
            out[f"{station}|{event_date.isoformat()}"] = entry
    v3: dict[str, Any] = {"rule": V3_RULE, "lambda": V3_LAMBDA, "systems": sorted(F_V3_SYSTEM_IDS)}
    if table is None:
        v3["state"] = "TERRAIN_TABLE_MISSING"
    else:
        v3.update({
            "state": "READY",
            "terrain_built_at_utc": table.get("built_at_utc"),
            "fleet": {"c": round(fleet3[0], 3), "source": fleet3[1], "samples": fleet3[2], "days": fleet3[3]},
            "recalibration": model3 or {
                "state": "INSUFFICIENT_HISTORY", "rows": len(train3), "days": len({day for day, _x, _y in train3}),
            },
        })
    return {
        "schema": SCHEMA, "rule": RULE, "legacy_rule": LEGACY_RULE, "computed_at_utc": as_of,
        "systems": sorted(F_SYSTEM_IDS),
        "fleet": {"c": round(fleet_c, 3), "source": fleet_source, "samples": fleet_samples, "days": fleet_days},
        "recalibration": model or {
            "state": "INSUFFICIENT_HISTORY", "rows": len(train), "days": len({day for day, _x, _y in train}),
        },
        "v3": v3,
        "pick": pick_model or {"state": "INSUFFICIENT_HISTORY", "rule": PICK_RULE, "rows": len(pick_rows)},
        "station_days": out,
        "authority": "EXECUTION_LAYER_ONLY",
        "calls_order_gateway": False,
    }


def _v3_entry(
    maxima_all: Mapping[str, Mapping[str, Any]], *, station: str, event_date: date, table: Mapping[str, Any],
    rows: Sequence[tuple[date, dict[str, float], float, datetime]], fleet_c: float,
    model: Mapping[str, Any] | None, obs: float | None,
) -> dict[str, Any]:
    """补记六: one station-day's F_v3 -- station values, lambda 0.9, its own recalibration."""

    values = station_values(maxima_all, station_id=station, table=table)
    entry: dict[str, Any] = {
        "members": {system: round(value, 3) for system, value in values.items()},
        "gfs_source": GFS_SFLUX_SYSTEM_ID if GFS_SFLUX_SYSTEM_ID in maxima_all else (GFS_SYSTEM_ID if GFS_SYSTEM_ID in maxima_all else None),
    }
    if len(values) < MIN_SYSTEMS:
        entry["state"] = "TOO_FEW_SYSTEMS"
        return entry
    base = _base_f(values, rows, event_date=event_date, fleet_c=fleet_c, knowable_by=None, lam=V3_LAMBDA)
    features = _recal_features(base, obs)
    correction = recal_correction(model, features) if model else 0.0
    # ``f_c`` is the cluster-like number the research scored (station values,
    # fleet shift and station share, no recalibration: the cluster's own warm
    # lean is in it); ``f_recal_c`` is the same brought back to the truth's
    # centre (as accurate as F).  The forward read compares both.
    entry.update({
        "state": "READY",
        "raw_median_c": round(base["raw"], 3),
        "station_corr_c": round(base["corr"], 3),
        "f_c": round(base["f"], 3),
        "recal_c": round(correction, 3) if model else None,
        "f_recal_c": round(base["f"] + correction, 3) if model else None,
    })
    return entry


def write(payload: Mapping[str, Any], path: Path = F_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    os.replace(tmp, path)


def refresh(db_path: str | Path) -> dict[str, Any]:
    """The resident's ten-minute job: compute from the SSOT ``mode=ro``, write the file."""

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        conn.execute("PRAGMA query_only=1")
        payload = compute(conn)
    finally:
        conn.close()
    write(payload)
    ready = sum(1 for v in payload["station_days"].values() if v.get("state") == "READY")
    ready_v3 = sum(1 for v in payload["station_days"].values() if (v.get("v3") or {}).get("state") == "READY")
    return {"state": "ok", "ready": ready, "station_days": len(payload["station_days"]),
            "fleet_c": payload["fleet"]["c"], "fleet_source": payload["fleet"]["source"],
            "recalibration": payload["recalibration"]["state"],
            "v3": payload.get("v3", {}).get("state"), "ready_v3": ready_v3,
            "pick": payload.get("pick", {}).get("state"),
            "ready_pick": sum(1 for v in payload["station_days"].values() if (v.get("pick") or {}).get("state") == "READY")}


def read(path: Path = F_PATH) -> dict[str, Any] | None:
    """The file as written, or None (missing/unreadable)."""

    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def bucket_for(f_c: float, ladder: Sequence[Mapping[str, Any]]) -> str | None:
    """The ladder bucket (``lower_c``/``upper_c``, None = open) containing F."""

    for bucket in ladder:
        lower, upper = bucket.get("lower_c"), bucket.get("upper_c")
        if (lower is None or f_c >= float(lower)) and (upper is None or f_c < float(upper)):
            return str(bucket.get("label") or bucket.get("outcome") or "")
    return None


__all__ = [
    "F_PATH", "LAMBDA", "PICK_AUTHORITY", "PICK_FEATURES", "PICK_KAPPA", "PICK_RULE", "RECAL_FEATURES", "RESEARCH_FLEET_C", "RULE", "SCHEMA", "V3_LAMBDA", "V3_RULE", "bucket_for", "compute",
    "station_values",
    "fit_pick", "fit_recalibration", "formal_truths", "keep_f_pick", "pick_features", "previous_run_maxima", "station_day_rungs", "listing_instant", "read", "recal_correction", "refresh",
    "system_maxima", "write",
]
