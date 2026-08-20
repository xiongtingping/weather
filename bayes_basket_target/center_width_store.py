"""Fit, gate and serve the per-stage centre/width scalars.

Same shape as ``band_reliability_store``: an active row per stage says what
is served, a journal row per fit says what was known the day it was chosen,
and nothing is activated that does not beat the incumbent on a held-out
forward window.  The two layers correct different things and are measured
separately -- this one moves the distribution on the temperature axis, the
band table rescales the claim that distribution produces -- so they are
fitted independently and applied in that order.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .center_width_calibration import (
    MAX_ABS_SHIFT_C,
    MAX_WIDTH_LAMBDA,
    MIN_WIDTH_LAMBDA,
    apply_center_width_calibration,
    bucket_centers_from_intervals,
)
from .contracts import ContractError, content_sha256

CENTER_WIDTH_STAGES = ("P0", "P0-M", "P1")
# Stages whose serving path actually reads this table.  P0's distribution is
# produced by the native-ensemble compiler against an OOS-gated artifact that
# pins the band table version its holdout was scored against; hanging an
# un-pinned correction after it would serve math the gate never saw, which is
# the serve/train split the spread cap already taught us.  P0 is fitted and
# journalled anyway -- the measurement is worth having, and the day the
# pinning is built the evidence is already there -- but it is not activated,
# because an active row is a claim that something is being served.
CENTER_WIDTH_SERVED_STAGES = ("P0", "P0-M", "P1")
# P0 joined once the pin existed.  Its distribution is compiled against an
# OOS-gated artifact, so the correction had to become part of the gated math
# rather than something hung after it: the holdout below scores it, the
# artifact records the table version scored, and serving reproduces that
# exact version or fails closed.  Built while the P0 table was still empty,
# so the plumbing landed at identity and changed no served number.
CENTER_WIDTH_METHOD_TAG = "center_width_scalars_v1"
# The grids the fit searches.  Deliberately coarse: the measured optimum sits
# on a broad, flat basin (lambda 0.80-0.85 across every slice tried), and a
# finer grid would only be fitting the sampling noise of a two-week window.
SHIFT_GRID_C = tuple(round(-0.8 + 0.05 * step, 2) for step in range(33))
LAMBDA_GRID = tuple(round(0.55 + 0.025 * step, 3) for step in range(19))
# A width that leans on the day's brightness was fitted here as a shadow and
# has been withdrawn: radiation moves the centre, not the width, and four
# independent measurements agree.  Signed residual correlates +0.237 with the
# radiation level and absolute residual +0.027; the quadratic term a "both
# extremes are easy" story needs is +0.018; splitting the scope by radiation
# tercile costs 0.163 bits; and the ensemble's own *disagreement* about
# radiation -- the second moment, pulled per member from GRIB to answer
# exactly this -- correlates -0.087 with the size of the residual.  The
# mechanism explains it: the models forecast radiation well and mistranslate
# it to screen temperature through a coupling they all share, so members
# disagreeing about radiation says nothing about how wrong that shared
# coupling will be.  Do not re-add it without a different covariate.
# Below this a stage has not seen enough settled days to distinguish a real
# correction from a fortnight's weather, and fits to identity instead.
MIN_QUALIFYING_DAYS = 8
MIN_QUALIFYING_CELLS = 120
# The fraction of the history held out of the fit and used to admit it.
FORWARD_WINDOW_FRACTION = 0.35
# A candidate must beat the incumbent by more than this on the forward
# window.  Anything smaller is inside the noise of a window this size, and
# activating on it would churn the served artifact every refit.
MIN_FORWARD_NLL_GAIN = 0.005
# Open-ended edge buckets have no far side; this is the width the fit gives
# them, and serving must use the same number or the correction lands
# somewhere the measurement never looked.
EDGE_WIDTH_C = 2.0

_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_center_width_table ("
    " center_width_sha256 TEXT PRIMARY KEY"
    "  CHECK(length(center_width_sha256)=64),"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
    " delta_c REAL NOT NULL,"
    " width_lambda REAL NOT NULL,"
    " evidence_json TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " serving_day_count INTEGER NOT NULL,"
    " activated INTEGER NOT NULL CHECK(activated IN (0,1)),"
    " activated_at_utc TEXT"
    ")"
)
_JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_center_width_fit_journal ("
    " fit_id TEXT PRIMARY KEY,"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
    " fitted_at_utc TEXT NOT NULL,"
    " center_width_sha256 TEXT NOT NULL"
    "  CHECK(length(center_width_sha256)=64),"
    " delta_c REAL NOT NULL,"
    " width_lambda REAL NOT NULL,"
    " serving_day_count INTEGER NOT NULL,"
    " serving_cell_count INTEGER NOT NULL,"
    " forward_nll_gain REAL NOT NULL,"
    " outcome TEXT NOT NULL CHECK(outcome IN"
    "  ('ACTIVATED','UNCHANGED','ACCUMULATING','REFUSED_FORWARD','NOT_WIRED')),"
    " evidence_json TEXT NOT NULL"
    ")"
)
_JOURNAL_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_center_width_journal_stage_time"
    " ON calibration_center_width_fit_journal(stage, fitted_at_utc)"
)
# The per-station width layer.  The stage lambda centres the fleet's width
# error; measured 2026-08-19 over 522 settled station-days the per-station z
# standard deviation still runs 0.26 to 1.21 around it, and a leave-one-day-
# out replay of a per-station contraction gained 0.10 nats forward
# (1.815 -> 1.718 at shrink n0=5, a clean single minimum).  One row per
# station under a whole-table sha, so the P0 artifact can pin the exact set
# its holdout was scored with, the same way it pins the stage scalars.
_STATION_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_center_width_station ("
    " station_width_sha256 TEXT NOT NULL"
    "  CHECK(length(station_width_sha256)=64),"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
    " station_id TEXT NOT NULL,"
    " width_scale REAL NOT NULL,"
    " raw_z_sd REAL,"
    " sample_n INTEGER NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " activated INTEGER NOT NULL CHECK(activated IN (0,1)),"
    " activated_at_utc TEXT,"
    " PRIMARY KEY(station_width_sha256, station_id)"
    ")"
)
_STATION_JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_center_width_station_journal ("
    " fit_id TEXT PRIMARY KEY,"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
    " fitted_at_utc TEXT NOT NULL,"
    " station_width_sha256 TEXT NOT NULL"
    "  CHECK(length(station_width_sha256)=64),"
    " station_count INTEGER NOT NULL,"
    " serving_day_count INTEGER NOT NULL,"
    " serving_cell_count INTEGER NOT NULL,"
    " forward_nll_gain REAL NOT NULL,"
    " outcome TEXT NOT NULL CHECK(outcome IN"
    "  ('ACTIVATED','UNCHANGED','ACCUMULATING','REFUSED_FORWARD','NOT_WIRED')),"
    " evidence_json TEXT NOT NULL"
    ")"
)


def ensure_center_width_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)
    conn.execute(_JOURNAL_DDL)
    conn.execute(_STATION_TABLE_DDL)
    conn.execute(_STATION_JOURNAL_DDL)
    # ``CREATE TABLE IF NOT EXISTS`` will not widen the CHECK constraint on a
    # journal that already exists, so a new outcome value fails at insert with
    # a constraint error rather than being recorded.  Rebuild in place when
    # the live constraint is missing one this code can emit; the journal is
    # append-only evidence, so it is copied across rather than dropped.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='calibration_center_width_fit_journal'"
    ).fetchone()
    live = str((row or [""])[0] or "")
    missing = [
        outcome
        for outcome in ("ACTIVATED", "UNCHANGED", "ACCUMULATING",
                        "REFUSED_FORWARD", "NOT_WIRED")
        if f"'{outcome}'" not in live
    ]
    if live and missing:
        conn.execute(
            "ALTER TABLE calibration_center_width_fit_journal"
            " RENAME TO calibration_center_width_fit_journal_superseded"
        )
        conn.execute(_JOURNAL_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO calibration_center_width_fit_journal"
            " SELECT * FROM calibration_center_width_fit_journal_superseded"
        )
        conn.execute(
            "DROP TABLE calibration_center_width_fit_journal_superseded"
        )
        conn.commit()
    conn.execute(_JOURNAL_INDEX)


def _check_stage(stage: str) -> str:
    value = str(stage)
    if value not in CENTER_WIDTH_STAGES:
        raise ContractError("center_width_stage_invalid")
    return value


def center_width_sha256(
    *, stage: str, delta_c: float, width_lambda: float
) -> str:
    return content_sha256({
        "schema": "center_width_table_v1",
        "stage": _check_stage(stage),
        "delta_c": round(float(delta_c), 6),
        "width_lambda": round(float(width_lambda), 6),
    })


def pinned_center_width_sha(combination_method: str) -> str | None:
    """The scalars version an artifact says its holdout was scored against."""

    for part in str(combination_method).split("::"):
        if part.startswith("center_width_sha256="):
            return part.split("=", 1)[1]
    return None


def load_center_width(
    conn: sqlite3.Connection,
    *,
    stage: str,
    table_sha256: str | None = None,
) -> tuple[float, float, str] | None:
    """The stage's active scalars, or the pinned version an artifact names.

    A pinned sha that is missing fails closed, for the same reason the band
    table does: an artifact whose scored math cannot be reproduced must not
    be served an approximation of it.

    This is a read.  It creates nothing -- a missing table means no
    correction has ever been fitted, which is exactly the None case.  The
    serving path calls this from inside the resident's write transaction,
    and a lazy CREATE plus its commit would end that transaction early.
    """

    stage_id = _check_stage(stage)
    try:
        if table_sha256 is None:
            row = conn.execute(
                "SELECT delta_c, width_lambda, center_width_sha256"
                " FROM calibration_center_width_table"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT delta_c, width_lambda, center_width_sha256"
                " FROM calibration_center_width_table"
                " WHERE center_width_sha256=?",
                (str(table_sha256),),
            ).fetchone()
    except sqlite3.OperationalError:
        # No table yet: nothing has been fitted, so nothing is served.  A
        # pinned request still fails closed below rather than silently
        # serving the uncorrected distribution.
        if table_sha256 is not None:
            raise ContractError("center_width_pinned_table_missing") from None
        return None
    if row is None:
        if table_sha256 is not None:
            raise ContractError("center_width_pinned_table_missing")
        return None
    return float(row[0]), float(row[1]), str(row[2])


STATION_WIDTH_METHOD_TAG = "station_width_scale_v1"
# The shrinkage prior on the per-station z standard deviation, in
# pseudo-samples of 1.0.  Swept leave-one-day-out over 522 station-days:
# forward NLL has a clean single minimum at 5 (1.718, four falls to it and
# three rises after), degrading below 3 where an eleven-sample raw z-sd
# starts fitting its own noise.  The correction self-strengthens: at eleven
# samples a station serves about two thirds of its measured contraction, at
# thirty about six sevenths.
STATION_WIDTH_SHRINK_N0 = 5.0
STATION_WIDTH_MIN_SAMPLES = 3
MIN_STATION_WIDTH_SCALE = 0.5
MAX_STATION_WIDTH_SCALE = 1.5


def station_width_sha256(
    *, stage: str, scales: Mapping[str, float]
) -> str:
    return content_sha256({
        "schema": "station_width_table_v1",
        "stage": _check_stage(stage),
        "scales": {
            str(station): round(float(scale), 6)
            for station, scale in sorted(scales.items())
        },
    })


def pinned_station_width_sha(combination_method: str) -> str | None:
    """The station-width version an artifact says its holdout was scored
    against."""

    for part in str(combination_method).split("::"):
        if part.startswith("station_width_sha256="):
            return part.split("=", 1)[1]
    return None


def load_station_width(
    conn: sqlite3.Connection,
    *,
    stage: str,
    table_sha256: str | None = None,
) -> tuple[dict[str, float], str] | None:
    """The stage's active per-station width scales, or a pinned version.

    Same contract as ``load_center_width``: a pinned sha that is missing
    fails closed, an unpinned read of a layer that has fitted nothing is the
    ordinary None case, and nothing is created on the read path.
    """

    stage_id = _check_stage(stage)
    try:
        if table_sha256 is None:
            rows = conn.execute(
                "SELECT station_id, width_scale, station_width_sha256"
                " FROM calibration_center_width_station"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT station_id, width_scale, station_width_sha256"
                " FROM calibration_center_width_station"
                " WHERE station_width_sha256=?",
                (str(table_sha256),),
            ).fetchall()
    except sqlite3.OperationalError:
        if table_sha256 is not None:
            raise ContractError(
                "station_width_pinned_table_missing"
            ) from None
        return None
    if not rows:
        if table_sha256 is not None:
            raise ContractError("station_width_pinned_table_missing")
        return None
    return (
        {str(row[0]): float(row[1]) for row in rows},
        str(rows[0][2]),
    )


def _station_cell_z(
    cell: Mapping[str, Any], truth_c: float
) -> float | None:
    """The cell's standardised error against its own served distribution."""

    probabilities = cell["probabilities"]
    centers = dict(zip(cell["ordered_keys"], cell["key_centers_c"]))
    mean = sum(
        float(value) * centers[str(key)]
        for key, value in probabilities.items()
    )
    variance = sum(
        float(value) * (centers[str(key)] - mean) ** 2
        for key, value in probabilities.items()
    )
    if variance <= 0.0025:  # sd <= 0.05C is a degenerate distribution
        return None
    return (float(truth_c) - mean) / math.sqrt(variance)


def fit_station_width(
    conn: sqlite3.Connection,
    *,
    stage: str,
    min_decision_at_utc: str | None = None,
) -> dict[str, Any]:
    """Fit one stage's per-station width scales, gated on a forward window.

    The cells are served envelopes, so a measured residual is *relative to
    whatever was active while they were served*.  The candidate therefore
    composes onto the incumbent rather than replacing it: once serving is
    calibrated the residual fits to one, the composition is a fixed point,
    and the gate refuses the no-op.  Replacement semantics would instead
    fit the incumbent's own correction as a new residual and un-apply it.
    """

    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    truth = _truth(conn)
    by_day = serving_cells_by_day(
        conn, truth, stage=stage_id,
        min_decision_at_utc=min_decision_at_utc,
    )
    days = sorted(by_day)
    cells = [cell for day in days for cell in by_day[day]]
    incumbent = load_station_width(conn, stage=stage_id)
    incumbent_scales = {} if incumbent is None else dict(incumbent[0])
    evidence: dict[str, Any] = {
        "schema": "station_width_evidence_v1",
        "stage": stage_id,
        "serving_day_count": len(days),
        "serving_cell_count": len(cells),
        "shrink_n0": STATION_WIDTH_SHRINK_N0,
        "incumbent_sha256": None if incumbent is None else incumbent[1],
        "min_decision_at_utc": min_decision_at_utc,
    }
    accumulating = (
        len(days) < MIN_QUALIFYING_DAYS or len(cells) < MIN_QUALIFYING_CELLS
    )
    if accumulating:
        evidence["basis"] = "ACCUMULATING"
        return {
            "stage": stage_id,
            "scales": incumbent_scales,
            "station_count": len(incumbent_scales),
            "forward_nll_gain": 0.0,
            "accumulating": True,
            "serving_day_count": len(days),
            "serving_cell_count": len(cells),
            "evidence": evidence,
        }
    cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
    train_days, forward_days = days[:cut], days[cut:] or days[-1:]
    z_by_station: dict[str, list[float]] = {}
    for day in train_days:
        for cell in by_day[day]:
            actual = truth.get((cell["station_id"], cell["event_date"]))
            if actual is None:
                continue
            z = _station_cell_z(cell, actual)
            if z is not None:
                z_by_station.setdefault(cell["station_id"], []).append(z)
    residual: dict[str, float] = {}
    raw_by_station: dict[str, Any] = {}
    for station, zs in z_by_station.items():
        if len(zs) < STATION_WIDTH_MIN_SAMPLES:
            continue
        mean = sum(zs) / len(zs)
        raw = math.sqrt(sum((z - mean) ** 2 for z in zs) / len(zs))
        shrunk = (len(zs) * raw + STATION_WIDTH_SHRINK_N0) / (
            len(zs) + STATION_WIDTH_SHRINK_N0
        )
        residual[station] = min(
            MAX_STATION_WIDTH_SCALE,
            max(MIN_STATION_WIDTH_SCALE, shrunk),
        )
        raw_by_station[station] = {"raw_z_sd": raw, "sample_n": len(zs)}
    forward = [cell for day in forward_days for cell in by_day[day]]

    def _forward_nll(scales: Mapping[str, float]) -> float:
        if not forward:
            return 0.0
        total = 0.0
        for cell in forward:
            scale = float(scales.get(cell["station_id"], 1.0))
            if scale == 1.0:
                total += _cell_nll(cell, delta_c=0.0, width_lambda=1.0)
                continue
            adjusted, _ = apply_center_width_calibration(
                cell["probabilities"],
                delta_c=0.0,
                width_lambda=1.0,
                ordered_keys=cell["ordered_keys"],
                key_centers_c=cell["key_centers_c"],
                excluded_buckets=cell["excluded_buckets"],
                station_width_scale=scale,
            )
            value = float(adjusted.get(cell["settled_bucket"], 0.0))
            total += -math.log(max(value, 1e-9))
        return total / len(forward)

    forward_base = _forward_nll({})
    forward_candidate = _forward_nll(residual)
    gain = forward_base - forward_candidate
    composed = {
        station: min(
            MAX_STATION_WIDTH_SCALE,
            max(
                MIN_STATION_WIDTH_SCALE,
                incumbent_scales.get(station, 1.0) * scale,
            ),
        )
        for station, scale in residual.items()
    }
    # Stations the incumbent corrects but this window could not measure keep
    # their correction: absence of evidence is not evidence of identity.
    for station, scale in incumbent_scales.items():
        composed.setdefault(station, scale)
    evidence.update({
        "basis": "FORWARD_WINDOW",
        "train_day_count": len(train_days),
        "forward_day_count": len(forward_days),
        "forward_cell_count": len(forward),
        "forward_nll_identity": forward_base,
        "forward_nll_residual": forward_candidate,
        "forward_nll_gain": gain,
        "min_forward_nll_gain": MIN_FORWARD_NLL_GAIN,
        "residual_station_count": len(residual),
        "per_station": raw_by_station,
    })
    admitted = gain > MIN_FORWARD_NLL_GAIN
    return {
        "stage": stage_id,
        "scales": composed if admitted else incumbent_scales,
        "candidate_scales": composed,
        "residual_scales": residual,
        "station_count": len(composed if admitted else incumbent_scales),
        "forward_nll_gain": gain,
        "admitted": admitted,
        "accumulating": False,
        "serving_day_count": len(days),
        "serving_cell_count": len(cells),
        "evidence": evidence,
    }


def persist_and_activate_station_width(
    conn: sqlite3.Connection,
    *,
    stage: str,
    scales: Mapping[str, float],
    per_station: Mapping[str, Mapping[str, Any]],
    fitted_at_utc: str | None = None,
) -> dict[str, Any]:
    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    digest = station_width_sha256(stage=stage_id, scales=scales)
    active = load_station_width(conn, stage=stage_id)
    if active is not None and active[1] == digest:
        return {"state": "UNCHANGED", "station_width_sha256": digest}
    now = fitted_at_utc or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    conn.execute(
        "UPDATE calibration_center_width_station SET activated=0"
        " WHERE stage=? AND activated=1",
        (stage_id,),
    )
    for station, scale in sorted(scales.items()):
        detail = per_station.get(station, {})
        conn.execute(
            "INSERT OR REPLACE INTO calibration_center_width_station ("
            " station_width_sha256, stage, station_id, width_scale,"
            " raw_z_sd, sample_n, fitted_at_utc, activated, activated_at_utc"
            ") VALUES (?,?,?,?,?,?,?,1,?)",
            (
                digest, stage_id, str(station), float(scale),
                detail.get("raw_z_sd"), int(detail.get("sample_n") or 0),
                now, now,
            ),
        )
    conn.commit()
    return {"state": "ACTIVATED", "station_width_sha256": digest}


def journal_station_width_fit(
    conn: sqlite3.Connection,
    *,
    stage: str,
    fitted: Mapping[str, Any],
    outcome: str,
) -> str:
    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    digest = station_width_sha256(
        stage=stage_id, scales=fitted["scales"]
    )
    fit_id = "swfit-" + content_sha256({
        "stage": stage_id,
        "fitted_at_utc": now,
        "station_width_sha256": digest,
    })[:24]
    conn.execute(
        "INSERT OR REPLACE INTO calibration_center_width_station_journal ("
        " fit_id, stage, fitted_at_utc, station_width_sha256,"
        " station_count, serving_day_count, serving_cell_count,"
        " forward_nll_gain, outcome, evidence_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            fit_id, stage_id, now, digest,
            int(fitted["station_count"]),
            int(fitted["serving_day_count"]),
            int(fitted["serving_cell_count"]),
            float(fitted["forward_nll_gain"]), str(outcome),
            json.dumps(
                dict(fitted["evidence"]), ensure_ascii=False, sort_keys=True
            ),
        ),
    )
    conn.commit()
    return fit_id


def _truth(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for station, day, value in conn.execute(
        "SELECT station_id, local_date, official_tmax_c"
        " FROM calibration_station_daily_high_truth t"
        " WHERE revision_no = ("
        "   SELECT MAX(revision_no)"
        "   FROM calibration_station_daily_high_truth x"
        "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
    ):
        out[(str(station), str(day))] = float(value)
    return out


def serving_cells_by_day(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
    *,
    stage: str,
    min_decision_at_utc: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """One scoreable cell per station-day-stage-hourblock.

    Sampling one decision per three-hour block rather than every decision:
    a station-day emits dozens of re-decisions off the same evidence, and
    counting each as an observation would let the busiest scopes set the
    scalars for everyone.

    ``min_decision_at_utc`` bounds the evidence below.  These scalars measure
    how wrong the *served* distribution is, so every decision taken before a
    change to the serving arithmetic is evidence about a distribution nobody
    serves any more.  Pooling across such a change makes the layer re-derive
    whatever it was compensating for: after the width fix of 2026-08-18 an
    unbounded fit reproduced the old 0.825 exactly, because weeks of pre-fix
    decisions outvoted the handful of post-fix ones.

    The bound is on the decision, not the event date, and that distinction is
    not academic: a P0 decision for an event two days out is taken two days
    early, so event_date >= X still admits decisions from well before X.
    Measured on this table, event date 2026-08-19 carries decisions reaching
    back to 2026-08-17 -- an event-date bound would have let two days of
    retired arithmetic vote on the very fit meant to exclude it.
    """

    stage_id = _check_stage(stage)
    query = (
        "SELECT station_id, event_date, decision_at_utc,"
        " envelope_json, settlement_timezone"
        " FROM basket_probability_bridge_history"
        " WHERE stage=?"
    )
    parameters: list[Any] = [stage_id]
    if min_decision_at_utc:
        query += " AND decision_at_utc>=?"
        parameters.append(str(min_decision_at_utc))
    grouped: dict[tuple[str, str, int], list[tuple[float, dict[str, Any]]]] = {}
    for station, day, decided, envelope_json, tz_name in conn.execute(
        query, parameters
    ):
        actual = truth.get((str(station), str(day)))
        if actual is None or not tz_name:
            continue
        try:
            envelope = json.loads(envelope_json)
            lineage = envelope.get("lineage") or {}
            probabilities = envelope.get("probabilities") or {}
            ordinals = envelope.get("bucket_ordinals") or {}
            intervals = json.loads(
                str(lineage.get("bucket_intervals_c_json") or "{}")
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not probabilities or set(intervals) != set(probabilities):
            continue
        keys = sorted(probabilities, key=lambda key: ordinals.get(key, 0))
        try:
            centers = bucket_centers_from_intervals(
                intervals, ordered_keys=keys, edge_width_c=EDGE_WIDTH_C
            )
        except ValueError:
            continue
        landed = None
        for key in keys:
            bounds = intervals[key]
            lower, upper = bounds[0], bounds[1]
            if (lower is None or actual >= float(lower)) and (
                upper is None or actual < float(upper)
            ):
                landed = key
                break
        if landed is None:
            continue
        excluded_raw = str(lineage.get("hard_excluded_buckets") or "")
        excluded = tuple(
            name
            for name in excluded_raw.split(",")
            if name.strip() and name != "NONE" and name in probabilities
        )
        try:
            zone = ZoneInfo(str(tz_name))
            decided_at = datetime.fromisoformat(
                str(decided).replace("Z", "+00:00")
            ).astimezone(zone)
            midnight = datetime.fromisoformat(
                f"{day}T00:00:00"
            ).replace(tzinfo=zone)
            hours = (decided_at - midnight).total_seconds() / 3600.0
        except (TypeError, ValueError, KeyError):
            continue
        block = int(hours // 3)
        cell = {
            "station_id": str(station),
            "event_date": str(day),
            "probabilities": {
                str(key): float(value) for key, value in probabilities.items()
            },
            "ordered_keys": keys,
            "key_centers_c": centers,
            "settled_bucket": landed,
            "excluded_buckets": excluded,
        }
        grouped.setdefault((str(station), str(day), block), []).append(
            (hours, cell)
        )
    by_day: dict[str, list[dict[str, Any]]] = {}
    for (_station, day, _block), entries in grouped.items():
        entries.sort(key=lambda item: item[0])
        by_day.setdefault(day, []).append(entries[len(entries) // 2][1])
    return by_day


def _cell_nll(
    cell: Mapping[str, Any], *, delta_c: float, width_lambda: float
) -> float:
    if delta_c == 0.0 and width_lambda == 1.0:
        adjusted = cell["probabilities"]
    else:
        adjusted, _ = apply_center_width_calibration(
            cell["probabilities"],
            delta_c=delta_c,
            width_lambda=width_lambda,
            ordered_keys=cell["ordered_keys"],
            key_centers_c=cell["key_centers_c"],
            excluded_buckets=cell["excluded_buckets"],
        )
    total = sum(adjusted.values())
    if total <= 0.0:
        return -math.log(1e-6)
    return -math.log(max(adjusted[cell["settled_bucket"]] / total, 1e-6))


def _mean_nll(
    cells: Sequence[Mapping[str, Any]], *, delta_c: float, width_lambda: float
) -> float:
    if not cells:
        return 0.0
    return sum(
        _cell_nll(cell, delta_c=delta_c, width_lambda=width_lambda)
        for cell in cells
    ) / len(cells)


def _search(cells: Sequence[Mapping[str, Any]]) -> tuple[float, float, float]:
    best: tuple[float, float, float] | None = None
    for delta in SHIFT_GRID_C:
        if abs(delta) > MAX_ABS_SHIFT_C:
            continue
        for lam in LAMBDA_GRID:
            if not MIN_WIDTH_LAMBDA <= lam <= MAX_WIDTH_LAMBDA:
                continue
            value = _mean_nll(cells, delta_c=delta, width_lambda=lam)
            if best is None or value < best[2]:
                best = (delta, lam, value)
    if best is None:
        return 0.0, 1.0, 0.0
    return best


def fit_center_width(
    conn: sqlite3.Connection,
    *,
    stage: str,
    min_decision_at_utc: str | None = None,
) -> dict[str, Any]:
    """Fit one stage's scalars, admitted on a held-out forward window.

    The fit searches the earlier fraction of the history and is scored on the
    later one against whatever is being served now.  A stage with too little
    settled history fits to identity and says so rather than erroring: it
    promotes itself the day the evidence clears the hurdle.
    """

    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    truth = _truth(conn)
    by_day = serving_cells_by_day(
        conn, truth, stage=stage_id,
        min_decision_at_utc=min_decision_at_utc,
    )
    days = sorted(by_day)
    cells = [cell for day in days for cell in by_day[day]]
    active = load_center_width(conn, stage=stage_id)
    base = (0.0, 1.0) if active is None else (active[0], active[1])
    accumulating = (
        len(days) < MIN_QUALIFYING_DAYS or len(cells) < MIN_QUALIFYING_CELLS
    )
    evidence: dict[str, Any] = {
        "schema": "center_width_evidence_v1",
        "stage": stage_id,
        "serving_day_count": len(days),
        "serving_cell_count": len(cells),
        "incumbent_delta_c": base[0],
        "incumbent_width_lambda": base[1],
        "edge_width_c": EDGE_WIDTH_C,
        "min_qualifying_days": MIN_QUALIFYING_DAYS,
        "min_qualifying_cells": MIN_QUALIFYING_CELLS,
        "min_decision_at_utc": min_decision_at_utc,
    }
    if accumulating:
        evidence["basis"] = "ACCUMULATING"
        return {
            "stage": stage_id,
            "delta_c": base[0],
            "width_lambda": base[1],
            "forward_nll_gain": 0.0,
            "accumulating": True,
            "serving_day_count": len(days),
            "serving_cell_count": len(cells),
            "evidence": evidence,
        }
    cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
    train_days, forward_days = days[:cut], days[cut:] or days[-1:]
    train = [cell for day in train_days for cell in by_day[day]]
    forward = [cell for day in forward_days for cell in by_day[day]]
    delta, lam, train_nll = _search(train)
    forward_base = _mean_nll(
        forward, delta_c=base[0], width_lambda=base[1]
    )
    forward_candidate = _mean_nll(
        forward, delta_c=delta, width_lambda=lam
    )
    gain = forward_base - forward_candidate
    evidence.update({
        "basis": "FORWARD_WINDOW",
        "train_day_count": len(train_days),
        "forward_day_count": len(forward_days),
        "train_cell_count": len(train),
        "forward_cell_count": len(forward),
        "train_nll": train_nll,
        "forward_nll_incumbent": forward_base,
        "forward_nll_candidate": forward_candidate,
        "forward_nll_gain": gain,
        "min_forward_nll_gain": MIN_FORWARD_NLL_GAIN,
        "identity_nll_all": _mean_nll(cells, delta_c=0.0, width_lambda=1.0),
        "candidate_nll_all": _mean_nll(cells, delta_c=delta, width_lambda=lam),
    })
    admitted = gain > MIN_FORWARD_NLL_GAIN
    return {
        "stage": stage_id,
        "delta_c": delta if admitted else base[0],
        "width_lambda": lam if admitted else base[1],
        "candidate_delta_c": delta,
        "candidate_width_lambda": lam,
        "forward_nll_gain": gain,
        "admitted": admitted,
        "accumulating": False,
        "serving_day_count": len(days),
        "serving_cell_count": len(cells),
        "evidence": evidence,
    }


def persist_and_activate_center_width(
    conn: sqlite3.Connection,
    *,
    stage: str,
    delta_c: float,
    width_lambda: float,
    evidence: Mapping[str, Any],
    serving_day_count: int,
    fitted_at_utc: str | None = None,
) -> dict[str, Any]:
    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    digest = center_width_sha256(
        stage=stage_id, delta_c=delta_c, width_lambda=width_lambda
    )
    active = load_center_width(conn, stage=stage_id)
    if active is not None and active[2] == digest:
        return {"state": "UNCHANGED", "center_width_sha256": digest}
    now = fitted_at_utc or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    conn.execute(
        "INSERT INTO calibration_center_width_table ("
        " center_width_sha256, stage, delta_c, width_lambda, evidence_json,"
        " fitted_at_utc, serving_day_count, activated, activated_at_utc"
        ") VALUES (?,?,?,?,?,?,?,0,NULL)"
        " ON CONFLICT(center_width_sha256) DO UPDATE SET"
        "  evidence_json=excluded.evidence_json,"
        "  fitted_at_utc=excluded.fitted_at_utc,"
        "  serving_day_count=excluded.serving_day_count",
        (
            digest, stage_id, float(delta_c), float(width_lambda),
            json.dumps(dict(evidence), ensure_ascii=False, sort_keys=True),
            now, int(serving_day_count),
        ),
    )
    conn.execute(
        "UPDATE calibration_center_width_table SET activated=0,"
        " activated_at_utc=NULL WHERE stage=? AND activated=1",
        (stage_id,),
    )
    conn.execute(
        "UPDATE calibration_center_width_table SET activated=1,"
        " activated_at_utc=? WHERE center_width_sha256=?",
        (now, digest),
    )
    conn.commit()
    return {
        "state": "ACTIVATED",
        "center_width_sha256": digest,
        "activated_at_utc": now,
    }


def journal_center_width_fit(
    conn: sqlite3.Connection,
    *,
    stage: str,
    fitted: Mapping[str, Any],
    outcome: str,
) -> str:
    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    digest = center_width_sha256(
        stage=stage_id,
        delta_c=fitted["delta_c"],
        width_lambda=fitted["width_lambda"],
    )
    fit_id = "cwfit-" + content_sha256({
        "stage": stage_id,
        "fitted_at_utc": now,
        "center_width_sha256": digest,
    })[:24]
    conn.execute(
        "INSERT OR REPLACE INTO calibration_center_width_fit_journal ("
        " fit_id, stage, fitted_at_utc, center_width_sha256, delta_c,"
        " width_lambda, serving_day_count, serving_cell_count,"
        " forward_nll_gain, outcome, evidence_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            fit_id, stage_id, now, digest,
            float(fitted["delta_c"]), float(fitted["width_lambda"]),
            int(fitted["serving_day_count"]),
            int(fitted["serving_cell_count"]),
            float(fitted["forward_nll_gain"]), str(outcome),
            json.dumps(
                dict(fitted["evidence"]), ensure_ascii=False, sort_keys=True
            ),
        ),
    )
    conn.commit()
    return fit_id


def refit_all_center_width(
    conn: sqlite3.Connection,
    *,
    commit: bool = False,
    min_decision_at_utc: str | None = None,
) -> dict[str, Any]:
    """Fit every stage, journal every fit, activate the admitted ones."""

    results: dict[str, Any] = {}
    for stage in CENTER_WIDTH_STAGES:
        fitted = fit_center_width(
            conn, stage=stage,
            min_decision_at_utc=min_decision_at_utc,
        )
        active = load_center_width(conn, stage=stage)
        entry: dict[str, Any] = {
            "stage": stage,
            "delta_c": fitted["delta_c"],
            "width_lambda": fitted["width_lambda"],
            "candidate_delta_c": fitted.get("candidate_delta_c"),
            "candidate_width_lambda": fitted.get("candidate_width_lambda"),
            "forward_nll_gain": fitted["forward_nll_gain"],
            "accumulating": fitted["accumulating"],
            "admitted": fitted.get("admitted", False),
            "served_stage": stage in CENTER_WIDTH_SERVED_STAGES,
            "active_sha256": None if active is None else active[2],
            "serving_day_count": fitted["serving_day_count"],
            "serving_cell_count": fitted["serving_cell_count"],
            "min_decision_at_utc": min_decision_at_utc,
        }
        if commit:
            if stage not in CENTER_WIDTH_SERVED_STAGES:
                outcome = "NOT_WIRED"
            elif fitted["accumulating"]:
                outcome = "ACCUMULATING"
            elif not fitted.get("admitted", False):
                outcome = "REFUSED_FORWARD"
            else:
                committed = persist_and_activate_center_width(
                    conn,
                    stage=stage,
                    delta_c=fitted["delta_c"],
                    width_lambda=fitted["width_lambda"],
                    evidence=fitted["evidence"],
                    serving_day_count=fitted["serving_day_count"],
                )
                entry["commit"] = committed
                outcome = str(committed["state"])
            entry["outcome"] = outcome
            entry["fit_id"] = journal_center_width_fit(
                conn, stage=stage, fitted=fitted, outcome=outcome
            )
        # The per-station width scales ride the same entry point, the same
        # evidence anchor and the same admission discipline as the stage
        # scalars they compose with.
        station_fitted = fit_station_width(
            conn, stage=stage,
            min_decision_at_utc=min_decision_at_utc,
        )
        station_active = load_station_width(conn, stage=stage)
        station_entry: dict[str, Any] = {
            "station_count": station_fitted["station_count"],
            "forward_nll_gain": station_fitted["forward_nll_gain"],
            "accumulating": station_fitted["accumulating"],
            "admitted": station_fitted.get("admitted", False),
            "active_sha256": (
                None if station_active is None else station_active[1]
            ),
        }
        if commit:
            if stage not in CENTER_WIDTH_SERVED_STAGES:
                station_outcome = "NOT_WIRED"
            elif station_fitted["accumulating"]:
                station_outcome = "ACCUMULATING"
            elif not station_fitted.get("admitted", False):
                station_outcome = "REFUSED_FORWARD"
            else:
                station_committed = persist_and_activate_station_width(
                    conn,
                    stage=stage,
                    scales=station_fitted["scales"],
                    per_station=(
                        station_fitted["evidence"].get("per_station") or {}
                    ),
                )
                station_entry["commit"] = station_committed
                station_outcome = str(station_committed["state"])
            station_entry["outcome"] = station_outcome
            station_entry["fit_id"] = journal_station_width_fit(
                conn, stage=stage, fitted=station_fitted,
                outcome=station_outcome,
            )
        entry["station_width"] = station_entry
        results[stage] = entry
    return results
