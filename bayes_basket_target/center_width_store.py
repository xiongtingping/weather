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
import os
import math
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from zoneinfo import ZoneInfo

# One write-lock acquisition, one budget: the store shares its database --
# and therefore its single write lock -- with the band reliability store,
# whose module holds the measured numbers (resident holds bursts of 3.6-9.1s,
# up to 45s) and the bounded retry that answers them.
from .band_reliability_store import _invert_band_receipt, _begin_immediate
from .center_width_calibration import (
    WIDTH_TIER_BOUNDARIES_RUNGS,
    WIDTH_TIER_NAMES,
    distribution_sigma_rungs_v1,
    width_tier_index_v1,
    MAX_ABS_SHIFT_C,
    MAX_SIGMA_FLOOR_LIFT,
    MAX_WIDTH_LAMBDA,
    MIN_WIDTH_LAMBDA,
    apply_center_width_calibration,
    bucket_centers_from_intervals,
    sigma_floor_lift_v1,
)
from . import serving_arithmetic
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
# The fit reads served distributions, so whatever is being served is already
# inside every cell.  Everything the grid measures is therefore a *residual*
# on top of the incumbent, and identity means "keep serving today's width".
# Serving applies one absolute pair to the uncorrected distribution, so the
# residual has to be composed onto the incumbent before it is stored.  The
# two spaces were conflated until 2026-09-10: the grid's best residual on
# P0's own cells was 1.000 -- today's width is right -- and storing that as
# the absolute would have widened every served P0 distribution by 33%, undoing
# the narrowing that landed 2026-09-04 and took sigma/RMSE from 1.36 to 1.16.
RESIDUAL_IDENTITY = (0.0, 1.0)
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
# The admission gate walks the origin instead of cutting once.  A single
# trailing window makes the verdict a property of which days happen to land
# in it, and this layer's own gain changes sign by day: measured 2026-09-10
# on P0's served cells, narrowing a further 10% bought 0.032 nat on 09-03 and
# 09-04 and cost 0.027 on 09-09, and the two days the lane's window happened
# to hold were the two costly ones.  On the same cells the training half's
# best lambda was 1.05 and the forward half's was 0.85 -- opposite
# directions, one coin toss apart.  Under a rolling origin each held-out day
# is scored by a fit that saw only the days before it, so the estimate
# averages several independent fit-and-test events rather than one window's
# luck.  A fold needs a few days to fit at all, and the folds are capped so
# the nightly cost stays bounded as history grows.
ROLLING_ORIGIN_MIN_FOLD_TRAIN_DAYS = 3
ROLLING_ORIGIN_MAX_FOLDS = 5
# A candidate must beat the incumbent by more than this on the forward
# window.  Anything smaller is inside the noise of a window this size, and
# activating on it would churn the served artifact every refit.
MIN_FORWARD_NLL_GAIN = 0.005
# Open-ended edge buckets have no far side; this is the width the fit gives
# them, and serving must use the same number or the correction lands
# somewhere the measurement never looked.
EDGE_WIDTH_C = 2.0
# The TAF-conditional lane (user ruling, 2026-08-25, charter 57).  The
# pooled scalar was measured to be an average over opposite regimes: the
# P0-M residual runs +0.90C when the TAF's TX sits above the model centre,
# -0.35C when below, +0.18C when they agree -- one number cannot serve all
# three.  Each direction is its own cell, fitted from its own days under
# the layer's own hurdles and admitted against the pooled candidate on its
# own forward slice, so nothing here is a constant: a cell with too little
# history serves the pooled scalars and promotes itself the day its
# evidence clears.  ``none`` is the 28 stations whose TAF carries no TX --
# a regime of its own, not a missing value.
TAF_CELL_NAMES = ("warm", "agree", "cold", "none")
TAF_GAP_THRESHOLD_C = 0.5
# The width floor, in rungs of the station's own vocabulary (charter 53(b):
# a rule counted in rungs is synonymous across units and is not converted).
# 1.44 is where the measurement puts the break -- below it the served claim's
# standardised error has standard deviation 0.95, above it 0.67 -- and it is
# one number doing two jobs, the tier boundary and the floor itself, because
# a distribution is disbelieved exactly as far as it falls short.
SIGMA_FLOOR_RUNGS = 1.44
# Whether an admitted floor is actually served.  ``shadow`` fits it, scores
# it and journals the verdict while the stage keeps serving the pooled pair;
# ``serve`` lets the verdict reach the activated row.  Default shadow, by the
# user's plan of 2026-09-05: the lane's own forward gate is a probability
# argument, and this repository has been taught more than once that a
# probability argument is not a book argument.  Promotion is the paper
# ledger's to grant, and this switch is what it grants.
SIGMA_FLOOR_MODE = (
    os.environ.get("PM_CENTER_WIDTH_SIGMA_FLOOR", "").strip().lower()
    or "shadow"
)
if SIGMA_FLOOR_MODE not in {"shadow", "serve"}:
    raise RuntimeError(
        f"unknown center-width sigma floor mode: {SIGMA_FLOOR_MODE}"
    )
# Width tiers (user ruling, 2026-09-05: "上新设计").  A lambda per tier of the
# served sigma (boundaries in ``center_width_calibration``), fitted on an
# objective that scores the mean+-1 window the seat rule reads as well as the
# bucket -- the flat-day core under-claim this lane exists for moves the
# bucket NLL by thousandths while moving window containment from 1.7 to 1.0,
# so a bucket-only fit cannot see what it is asked to fix.  Same promotion
# contract as the floor: ``shadow`` fits, scores and journals; ``serve`` lets
# an admitted tier table reach the activated row.  A tier candidate must beat
# the pooled candidate on the combined forward objective by the layer's
# hurdle without regressing the bucket NLL by more than a noise margin.
WIDTH_TIER_MODE = (
    os.environ.get("PM_CENTER_WIDTH_TIERS", "").strip().lower() or "shadow"
)
if WIDTH_TIER_MODE not in {"shadow", "serve"}:
    raise RuntimeError(f"unknown center-width tier mode: {WIDTH_TIER_MODE}")
WIDTH_TIER_LAMBDA_GRID = tuple(round(0.50 + 0.025 * step, 3) for step in range(21))
WINDOW_TERM_WEIGHT = float(
    os.environ.get("PM_CENTER_WIDTH_WINDOW_TERM_WEIGHT", "") or 1.0
)
if not WINDOW_TERM_WEIGHT >= 0.0:
    raise RuntimeError(f"invalid centre-width window term weight: {WINDOW_TERM_WEIGHT}")
MAX_BUCKET_NLL_REGRESSION = 0.002


def taf_direction_for_gap(gap_c: float | None) -> str:
    """Which conditional cell a TAF-vs-centre gap belongs to.

    One definition shared by the fit and the serve: a threshold that
    drifted between them would train one regime and serve another.
    """

    if gap_c is None:
        return "none"
    if float(gap_c) > TAF_GAP_THRESHOLD_C:
        return "warm"
    if float(gap_c) < -TAF_GAP_THRESHOLD_C:
        return "cold"
    return "agree"


def _annotate_taf_directions(
    conn: sqlite3.Connection, cells: Sequence[Mapping[str, Any]]
) -> None:
    """Stamp each cell with the TAF direction its decision could have seen.

    The forecast is the latest TX-carrying revision issued at or before the
    cell's decision -- the same as-of rule the P0-M confirmation itself
    uses, so the fit classifies days exactly the way serving will.
    """

    cache: dict[tuple[str, str, str], float | None] = {}
    for cell in cells:
        center = cell.get("model_center_c")
        decided = str(cell.get("decision_at_utc") or "")
        key = (str(cell["station_id"]), str(cell["event_date"]), decided)
        if key not in cache:
            row = conn.execute(
                "SELECT tx_c FROM calibration_forward_taf_capture"
                " WHERE station_id=? AND event_date=? AND tx_available=1"
                " AND issued_at_utc<=?"
                " ORDER BY issued_at_utc DESC LIMIT 1",
                key,
            ).fetchone()
            cache[key] = (
                None if row is None or row[0] is None else float(row[0])
            )
        tx = cache[key]
        gap = (
            None if tx is None or center is None else tx - float(center)
        )
        cell["taf_direction"] = taf_direction_for_gap(gap)  # type: ignore[index]

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
    "  ('ACTIVATED','UNCHANGED','ACCUMULATING','REFUSED_FORWARD','NOT_WIRED',"
    "   'INCUMBENT_VETOED')),"
    " evidence_json TEXT NOT NULL"
    ")"
)
_STATION_JOURNAL_OUTCOMES = (
    "ACTIVATED", "UNCHANGED", "ACCUMULATING", "REFUSED_FORWARD", "NOT_WIRED",
    "INCUMBENT_VETOED",
)


def ensure_center_width_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)
    conn.execute(_JOURNAL_DDL)
    conn.execute(_STATION_TABLE_DDL)
    conn.execute(_STATION_JOURNAL_DDL)
    # The TAF-conditional cells (charter 57) ride on the same row as the
    # pooled scalars they refine; nullable so every pre-lane row keeps
    # reading as pooled-only.
    try:
        conn.execute(
            "ALTER TABLE calibration_center_width_table"
            " ADD COLUMN taf_cells_json TEXT"
        )
    except sqlite3.OperationalError:
        pass
    # The width floor rides on the same row as the scalars it qualifies.
    # Nullable, and read as zero, so every row written before the floor
    # existed keeps describing exactly the arithmetic it described.
    try:
        conn.execute(
            "ALTER TABLE calibration_center_width_table"
            " ADD COLUMN sigma_floor_rungs REAL"
        )
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(
            "ALTER TABLE calibration_center_width_table"
            " ADD COLUMN width_tiers_json TEXT"
        )
    except sqlite3.OperationalError:
        pass
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
    # Same widening for the station journal: the veto outcome was added
    # after the live table was created.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='calibration_center_width_station_journal'"
    ).fetchone()
    live = str((row or [""])[0] or "")
    if live and any(
        f"'{outcome}'" not in live for outcome in _STATION_JOURNAL_OUTCOMES
    ):
        conn.execute(
            "ALTER TABLE calibration_center_width_station_journal"
            " RENAME TO calibration_center_width_station_journal_superseded"
        )
        conn.execute(_STATION_JOURNAL_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO calibration_center_width_station_journal"
            " SELECT * FROM calibration_center_width_station_journal_superseded"
        )
        conn.execute(
            "DROP TABLE calibration_center_width_station_journal_superseded"
        )
        conn.commit()


def _check_stage(stage: str) -> str:
    value = str(stage)
    if value not in CENTER_WIDTH_STAGES:
        raise ContractError("center_width_stage_invalid")
    return value


def center_width_sha256(
    *, stage: str, delta_c: float, width_lambda: float,
    taf_cells: Mapping[str, Sequence[float]] | None = None,
    sigma_floor_rungs: float = 0.0,
    width_tiers: Mapping[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "schema": "center_width_table_v1",
        "stage": _check_stage(stage),
        "delta_c": round(float(delta_c), 6),
        "width_lambda": round(float(width_lambda), 6),
    }
    # Conditional so a table with no cells keeps its historical digest --
    # the pinned-version contract must keep resolving rows written before
    # the lane existed.
    if taf_cells:
        payload["taf_cells"] = {
            str(name): [
                round(float(pair[0]), 6), round(float(pair[1]), 6),
            ]
            for name, pair in sorted(taf_cells.items())
        }
    # Conditional for the same reason, and it is the floor that makes the
    # scalars mean what they mean: 0.725 under a floor is a different
    # arithmetic from 0.725 without one, and an artifact pinning the digest
    # must be served the pair it was scored with.
    if sigma_floor_rungs:
        payload["sigma_floor_rungs"] = round(float(sigma_floor_rungs), 6)
    if width_tiers:
        payload["width_tiers"] = {
            "boundaries_rungs": [
                round(float(b), 6) for b in width_tiers["boundaries_rungs"]
            ],
            "lambdas": [round(float(v), 6) for v in width_tiers["lambdas"]],
        }
    return content_sha256(payload)


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


def load_center_width_floor(
    conn: sqlite3.Connection,
    *,
    stage: str,
    table_sha256: str | None = None,
) -> float:
    """The width floor the active -- or pinned -- scalars were fitted under.

    Zero is the honest answer for every row written before the floor existed
    and for every stage whose floor did not clear the forward gate, and zero
    makes ``apply_center_width_calibration`` an identity in this factor.  A
    separate reader rather than a wider return from ``load_center_width``:
    that tuple is unpacked at a dozen call sites, and widening it would make
    a serving change out of a lookup.
    """

    stage_id = _check_stage(stage)
    try:
        if table_sha256 is None:
            row = conn.execute(
                "SELECT sigma_floor_rungs"
                " FROM calibration_center_width_table"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT sigma_floor_rungs"
                " FROM calibration_center_width_table"
                " WHERE center_width_sha256=?",
                (str(table_sha256),),
            ).fetchone()
    except sqlite3.OperationalError:
        return 0.0
    if row is None or row[0] is None:
        return 0.0
    return float(row[0])


def load_center_width_tiers(
    conn: sqlite3.Connection,
    *,
    stage: str,
    table_sha256: str | None = None,
) -> dict[str, Any] | None:
    """The active (or pinned) table's width tiers, if any -- None means the
    table serves one lambda, which is every row written before the tier
    lane existed and every fit whose tiers were not admitted to serve."""

    stage_id = _check_stage(stage)
    try:
        if table_sha256 is None:
            row = conn.execute(
                "SELECT width_tiers_json"
                " FROM calibration_center_width_table"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT width_tiers_json"
                " FROM calibration_center_width_table"
                " WHERE center_width_sha256=?",
                (str(table_sha256),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row[0]:
        return None
    try:
        tiers = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    if not isinstance(tiers, Mapping) or not tiers.get("lambdas"):
        return None
    return {
        "boundaries_rungs": [float(b) for b in tiers.get("boundaries_rungs") or WIDTH_TIER_BOUNDARIES_RUNGS],
        "lambdas": [float(v) for v in tiers["lambdas"]],
    }


def load_center_width_cells(
    conn: sqlite3.Connection,
    *,
    stage: str,
    table_sha256: str | None = None,
) -> dict[str, tuple[float, float]] | None:
    """The active (or pinned) table's TAF-conditional cells, if any.

    Same resolution and fail-closed rules as ``load_center_width``; None
    means the table serves pooled-only, which is every table written
    before the lane existed and every fit whose cells are still
    accumulating.
    """

    stage_id = _check_stage(stage)
    try:
        if table_sha256 is None:
            row = conn.execute(
                "SELECT taf_cells_json"
                " FROM calibration_center_width_table"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT taf_cells_json"
                " FROM calibration_center_width_table"
                " WHERE center_width_sha256=?",
                (str(table_sha256),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row[0]:
        return None
    try:
        cells = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    return {
        str(name): (float(pair[0]), float(pair[1]))
        for name, pair in cells.items()
        if isinstance(pair, Sequence) and len(pair) >= 2
    } or None


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
# An active table is held while the lane accumulates, and held again when a
# candidate is refused.  Holding is only safe if the incumbent is still
# earning its place: the P0-M table fitted 2026-09-06 on pre-anchor cells
# cost +0.12 nat on every cell served after it (+0.16/+0.17 on 09-08/09)
# and nothing could retire it, because the only path to identity was a fit
# that never runs while accumulating.  So before anything else the lane
# scores the incumbent on the cells served AFTER its own activation -- out
# of sample by construction -- against the same cells with the incumbent
# undone.  Losing by more than the admission hurdle retires the table.  The
# window opens at three days / sixty cells, half the qualifying floor: the
# question "is this table hurting" needs less evidence than "which table
# is best", and a day earlier here was one fewer day served too narrow.
STATION_WIDTH_VETO_MIN_DAYS = 3
STATION_WIDTH_VETO_MIN_CELLS = 60


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


def _active_station_width_activated_at(
    conn: sqlite3.Connection, *, stage: str
) -> str | None:
    try:
        row = conn.execute(
            "SELECT MAX(activated_at_utc) FROM calibration_center_width_station"
            " WHERE activated=1 AND stage=?",
            (stage,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None or row[0] is None else str(row[0])


def _undo_station_scale_nll(
    cell: Mapping[str, Any], *, scale: float
) -> float:
    """The cell's NLL with the station contraction it was served under
    widened back out, as far as the kernel's own lambda bound allows.

    A full undo of a 0.5 scale is a lambda of 2.0, and the kernel's linear
    split at exactly two rungs lands every bucket's mass on every other
    bucket -- a comb with zeros in it, not a distribution anything is
    scored against.  So the probe widens by ``min(1/scale, 1.5)``: if even
    that beats the served cell, the contraction was too much, which is the
    only question the veto asks."""

    total = 1.0 / float(scale)
    lam = min(MAX_WIDTH_LAMBDA, max(MIN_WIDTH_LAMBDA, total))
    adjusted, _ = apply_center_width_calibration(
        cell["probabilities"],
        delta_c=0.0,
        width_lambda=lam,
        ordered_keys=cell["ordered_keys"],
        key_centers_c=cell["key_centers_c"],
        excluded_buckets=cell["excluded_buckets"],
    )
    total_mass = sum(adjusted.values())
    if total_mass <= 0.0:
        return -math.log(1e-6)
    return -math.log(max(adjusted[cell["settled_bucket"]] / total_mass, 1e-6))


def _station_width_veto(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    scales: Mapping[str, float],
    activated_at_utc: str | None,
) -> dict[str, Any]:
    """Score the active table on the cells served after it went live: kept
    (served as-is) against undone.  ``vetoed`` when undoing wins by more
    than the admission hurdle on at least the veto window."""

    since = activated_at_utc or ""
    kept_total = undone_total = 0.0
    count = 0
    per_day: dict[str, dict[str, float]] = {}
    for day in sorted(by_day):
        day_kept = day_undone = 0.0
        day_count = 0
        for cell in by_day[day]:
            if str(cell.get("decision_at_utc") or "") < since:
                continue
            scale = float(scales.get(cell["station_id"], 1.0))
            if scale == 1.0:
                continue
            kept = _cell_nll(cell, delta_c=0.0, width_lambda=1.0)
            undone = _undo_station_scale_nll(cell, scale=scale)
            day_kept += kept
            day_undone += undone
            day_count += 1
        if day_count:
            per_day[day] = {
                "cells": day_count,
                "kept_minus_undone": (day_kept - day_undone) / day_count,
            }
            kept_total += day_kept
            undone_total += day_undone
            count += day_count
    enough = (
        len(per_day) >= STATION_WIDTH_VETO_MIN_DAYS
        and count >= STATION_WIDTH_VETO_MIN_CELLS
    )
    gain = 0.0 if count == 0 else (kept_total - undone_total) / count
    return {
        "activated_at_utc": activated_at_utc,
        "day_count": len(per_day),
        "cell_count": count,
        "nll_kept": None if count == 0 else kept_total / count,
        "nll_undone": None if count == 0 else undone_total / count,
        "undo_gain": gain,
        "hurdle": MIN_FORWARD_NLL_GAIN,
        "enough_evidence": enough,
        "vetoed": bool(enough and gain > MIN_FORWARD_NLL_GAIN),
        "per_day": per_day,
    }


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
    min_decision_at_utc = _evidence_floor(
        conn, stage=stage_id, caller_bound=min_decision_at_utc
    )
    truth = _memoised(("truth",), lambda: _truth(conn))
    by_day = _memoised(
        ("serving_cells", stage_id, min_decision_at_utc),
        lambda: serving_cells_by_day(
            conn, truth, stage=stage_id,
            min_decision_at_utc=min_decision_at_utc,
        ),
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
    if incumbent_scales:
        veto = _station_width_veto(
            by_day,
            scales=incumbent_scales,
            activated_at_utc=_active_station_width_activated_at(
                conn, stage=stage_id
            ),
        )
        evidence["incumbent_veto"] = veto
        if veto["vetoed"]:
            evidence["basis"] = "INCUMBENT_VETOED"
            return {
                "stage": stage_id,
                "scales": {},
                "station_count": 0,
                "forward_nll_gain": -float(veto["undo_gain"]),
                "accumulating": accumulating,
                "vetoed": True,
                "serving_day_count": len(days),
                "serving_cell_count": len(cells),
                "evidence": evidence,
            }
    if accumulating:
        evidence["basis"] = "ACCUMULATING"
        return {
            "stage": stage_id,
            "scales": incumbent_scales,
            "station_count": len(incumbent_scales),
            "forward_nll_gain": 0.0,
            "accumulating": True,
            "vetoed": False,
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
        "vetoed": False,
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
    if not scales and active is None:
        return {"state": "UNCHANGED", "station_width_sha256": digest}
    now = fitted_at_utc or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    _begin_immediate(conn)
    try:
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
                " raw_z_sd, sample_n, fitted_at_utc, activated,"
                " activated_at_utc"
                ") VALUES (?,?,?,?,?,?,?,1,?)",
                (
                    digest, stage_id, str(station), float(scale),
                    detail.get("raw_z_sd"),
                    int(detail.get("sample_n") or 0),
                    now, now,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "state": "DEACTIVATED" if not scales else "ACTIVATED",
        "station_width_sha256": digest,
    }


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
    _begin_immediate(conn)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO"
            " calibration_center_width_station_journal ("
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
                    dict(fitted["evidence"]),
                    ensure_ascii=False, sort_keys=True,
                ),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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



def _pre_band_claim(
    probabilities: Mapping[str, float], lineage: Mapping[str, Any]
) -> dict[str, float]:
    """The claim as it stood before the band table touched it.

    A row whose lineage carries no applied band receipt is already pre-band
    and is returned unchanged, which is every row until that layer activates
    for the stage.
    """

    raw = lineage.get("band_reliability_json")
    if not raw:
        return {str(k): float(v) for k, v in probabilities.items()}
    try:
        receipt = json.loads(str(raw))
    except (TypeError, ValueError):
        return {str(k): float(v) for k, v in probabilities.items()}
    if not isinstance(receipt, Mapping) or receipt.get("state") != "APPLIED":
        return {str(k): float(v) for k, v in probabilities.items()}
    try:
        inverted = _invert_band_receipt(
            [(float(v), str(k)) for k, v in probabilities.items()], receipt
        )
    except (ContractError, TypeError, ValueError):
        # An unreadable receipt means this row cannot be placed on the axis
        # the fit needs; the served claim would be the wrong quantity, so the
        # caller drops it by getting an empty mapping back.
        return {}
    return {key: value for value, key in inverted}


def serving_cells_by_day(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
    *,
    stage: str,
    min_decision_at_utc: str | None = None,
    pre_band: bool = True,
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
        " settlement_timezone, checkpoint_id, envelope_content_sha256,"
        " envelope_json"
        " FROM basket_probability_bridge_history"
        " WHERE stage=?"
    )
    parameters: list[Any] = [stage_id]
    if min_decision_at_utc:
        query += " AND decision_at_utc>=?"
        parameters.append(str(min_decision_at_utc))
    # Two passes, because the sampling rule reads the row and the cell reads
    # the body, and only the row decides which cell survives.
    #
    # One pass built every cell and then kept the median of each three-hour
    # block.  Since 2026-08-26 a history row carries no inline body -- all
    # 158,784 P0 rows have ``envelope_json`` empty -- so building a cell is a
    # single-row query into ``basket_probability_envelope``, a rebuild, and a
    # full re-hash against the sha the history row carries.  Profiled
    # 2026-09-09 that ran 154,723 times inside one render and cost 115.7s of
    # the 146.5s this function spends, which is 77% of the ``selection``
    # section -- the largest section on the page and one the cache is
    # forbidden to hold, so it is paid every thirty minutes.
    #
    # The block a decision belongs to is a function of decision_at_utc, the
    # event date and the timezone: all three are columns on the row.  So the
    # first pass groups on those alone, and only the sampled row is rebuilt.
    # ``envelope_json`` rides along because rows written before 2026-08-26
    # carry their body there and rebuilding is not an option for them; on the
    # lane this was measured against it is NULL on every row, and the cost
    # being removed was never that column -- it was the per-row query,
    # rebuild and re-hash behind it.
    candidates: dict[tuple[str, str, int], list[tuple[float, tuple]]] = {}
    for (
        station, day, decided, tz_name, checkpoint, sha, stored
    ) in conn.execute(query, parameters):
        actual = truth.get((str(station), str(day)))
        if actual is None or not tz_name:
            continue
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
        candidates.setdefault((str(station), str(day), int(hours // 3)), [])\
            .append((
                hours,
                (str(station), str(day), str(decided), str(checkpoint),
                 str(sha), str(stored) if stored else None, float(actual)),
            ))
    by_day: dict[str, list[dict[str, Any]]] = {}
    for (_station, day, _block), entries in candidates.items():
        entries.sort(key=lambda item: item[0])
        # The old code took the median of the rows whose CELL was built; this
        # takes the median row and walks outward until one yields a cell.
        # They agree whenever every row in the block yields one, which is the
        # ordinary case -- a row that does not is a body that fails its own
        # sha or an event that landed in no bucket.  Walking outward keeps
        # the sample near the middle of the block rather than dropping the
        # block, which is what returning only the median row would do.
        for index in _outward_from_middle(len(entries)):
            cell = _serving_cell(
                conn, entries[index][1], pre_band=pre_band
            )
            if cell is not None:
                by_day.setdefault(day, []).append(cell)
                break
    return by_day


def _outward_from_middle(count: int) -> list[int]:
    """Indices from the middle outward: 2 3 1 4 0 for five.

    Ties go to the LOWER index, matching ``entries[len(entries) // 2]`` on an
    even-length list, so a block whose rows all yield cells picks exactly the
    row the single-pass version picked.
    """

    if count <= 0:
        return []
    middle = count // 2
    order = [middle]
    for step in range(1, count):
        for index in (middle - step, middle + step):
            if 0 <= index < count and index not in order:
                order.append(index)
        if len(order) >= count:
            break
    return order


def _serving_cell(
    conn: sqlite3.Connection,
    row: tuple,
    *,
    pre_band: bool,
) -> dict[str, Any] | None:
    """One scoreable cell, or None if this row cannot produce one.

    Every rejection here was a ``continue`` in the single-pass loop and keeps
    the same reason; the difference is only that it is now reached for the
    sampled row rather than for all 154,723 of them.
    """

    from .probability_bridge import history_envelope_payload

    station, day, decided, checkpoint, sha, stored, actual = row
    try:
        envelope = history_envelope_payload(
            conn,
            checkpoint_id=checkpoint,
            envelope_content_sha256=sha,
            stored=stored,
        )
        lineage = envelope.get("lineage") or {}
        probabilities = envelope.get("probabilities") or {}
        ordinals = envelope.get("bucket_ordinals") or {}
        intervals = json.loads(
            str(lineage.get("bucket_intervals_c_json") or "{}")
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not probabilities or set(intervals) != set(probabilities):
        return None
    if pre_band:
        probabilities = _pre_band_claim(probabilities, lineage)
    keys = sorted(probabilities, key=lambda key: ordinals.get(key, 0))
    try:
        centers = bucket_centers_from_intervals(
            intervals, ordered_keys=keys, edge_width_c=EDGE_WIDTH_C
        )
    except ValueError:
        return None
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
        return None
    excluded_raw = str(lineage.get("hard_excluded_buckets") or "")
    excluded = tuple(
        name
        for name in excluded_raw.split(",")
        if name.strip() and name != "NONE" and name in probabilities
    )
    return {
        "station_id": station,
        "event_date": day,
        "decision_at_utc": decided,
        "model_center_c": lineage.get("model_center_c"),
        "probabilities": {
            str(key): float(value) for key, value in probabilities.items()
        },
        "ordered_keys": keys,
        "key_centers_c": centers,
        "settled_bucket": landed,
        "excluded_buckets": excluded,
    }


def _cell_nll(
    cell: Mapping[str, Any],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
) -> float:
    if delta_c == 0.0 and width_lambda == 1.0 and not sigma_floor_rungs:
        adjusted = cell["probabilities"]
    else:
        adjusted, _ = apply_center_width_calibration(
            cell["probabilities"],
            delta_c=delta_c,
            width_lambda=width_lambda,
            ordered_keys=cell["ordered_keys"],
            key_centers_c=cell["key_centers_c"],
            excluded_buckets=cell["excluded_buckets"],
            sigma_floor_rungs=sigma_floor_rungs,
        )
    total = sum(adjusted.values())
    if total <= 0.0:
        return -math.log(1e-6)
    return -math.log(max(adjusted[cell["settled_bucket"]] / total, 1e-6))


def _mean_nll(
    cells: Sequence[Mapping[str, Any]],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
) -> float:
    if not cells:
        return 0.0
    return sum(
        _cell_nll(
            cell,
            delta_c=delta_c,
            width_lambda=width_lambda,
            sigma_floor_rungs=sigma_floor_rungs,
        )
        for cell in cells
    ) / len(cells)


def _compose_absolute(
    incumbent: tuple[float, float],
    residual: tuple[float, float],
) -> tuple[float, float]:
    """The absolute pair that serves what ``residual`` did to served cells.

    Serving is ``x -> mu + d + lam * (x - mu)`` about the input's own mean.
    Applying (d1, lam1) then (d2, lam2) leaves the mean at mu + d1 + d2 and
    every deviation scaled by lam1 * lam2, so the composition is exact on a
    density and monotone on the histogram the layer actually transforms --
    which is all the grid needs, and the same standing the docstring of
    ``apply_center_width_calibration`` already claims for its own arithmetic.

    Clamped to the serving bounds: a composition outside them is a pair the
    server would refuse, and storing it would fail closed at serve time
    rather than here.
    """

    delta = float(incumbent[0]) + float(residual[0])
    lam = float(incumbent[1]) * float(residual[1])
    delta = max(-MAX_ABS_SHIFT_C, min(MAX_ABS_SHIFT_C, delta))
    lam = max(MIN_WIDTH_LAMBDA, min(MAX_WIDTH_LAMBDA, lam))
    return (round(delta, 4), round(lam, 4))


def _search(
    cells: Sequence[Mapping[str, Any]],
    *,
    sigma_floor_rungs: float = 0.0,
) -> tuple[float, float, float]:
    """The grid's best pair, optionally searched under a width floor.

    The floor is not a third grid axis.  It is fixed by the measurement that
    proposed it, and the pair is refitted underneath it, so what the search
    returns is the best scalars *given* the floor -- which is the arithmetic
    the arm would serve, scored the way it would serve it.
    """

    best: tuple[float, float, float] | None = None
    for delta in SHIFT_GRID_C:
        if abs(delta) > MAX_ABS_SHIFT_C:
            continue
        for lam in LAMBDA_GRID:
            if not MIN_WIDTH_LAMBDA <= lam <= MAX_WIDTH_LAMBDA:
                continue
            value = _mean_nll(
                cells,
                delta_c=delta,
                width_lambda=lam,
                sigma_floor_rungs=sigma_floor_rungs,
            )
            if best is None or value < best[2]:
                best = (delta, lam, value)
    if best is None:
        return 0.0, 1.0, 0.0
    return best



def _cell_tier(cell: Mapping[str, Any]) -> int:
    """The tier the cell's distribution is served in -- read off the input
    the same way ``apply_center_width_calibration`` reads it."""

    return width_tier_index_v1(distribution_sigma_rungs_v1(
        cell["probabilities"],
        ordered_keys=cell["ordered_keys"],
        key_centers_c=cell["key_centers_c"],
    ))


def _cell_scores(
    cell: Mapping[str, Any],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
    width_tier_lambdas: Sequence[float] | None = None,
) -> tuple[float, float, float, int]:
    """(bucket NLL, window NLL, window claim, window hit) for one cell.

    The window is the mean bucket +-1 of the *transformed* distribution,
    which is the shape the seat rule (charter 79) reads; its claim is what
    the positive-EV gate compares with price, so its binary NLL is the
    term the flat-day under-claim shows up in.
    """

    if (
        delta_c == 0.0 and width_lambda == 1.0 and not sigma_floor_rungs
        and not width_tier_lambdas
    ):
        adjusted = dict(cell["probabilities"])
    else:
        adjusted, _ = apply_center_width_calibration(
            cell["probabilities"],
            delta_c=delta_c,
            width_lambda=width_lambda,
            ordered_keys=cell["ordered_keys"],
            key_centers_c=cell["key_centers_c"],
            excluded_buckets=cell["excluded_buckets"],
            sigma_floor_rungs=sigma_floor_rungs,
            width_tier_lambdas=width_tier_lambdas,
        )
    total = sum(adjusted.values())
    if total <= 0.0:
        return -math.log(1e-6), -math.log(1e-6), 0.0, 0
    keys = list(cell["ordered_keys"])
    centers = list(cell["key_centers_c"])
    mean_c = sum(
        centers[i] * float(adjusted.get(k, 0.0)) for i, k in enumerate(keys)
    ) / total
    mean_index = min(
        range(len(keys)), key=lambda i: (abs(centers[i] - mean_c), i)
    )
    window = keys[max(0, mean_index - 1):mean_index + 2]
    claim = sum(float(adjusted.get(k, 0.0)) for k in window) / total
    hit = 1 if cell["settled_bucket"] in window else 0
    q = min(max(claim, 1e-6), 1.0 - 1e-6)
    window_nll = -math.log(q) if hit else -math.log(1.0 - q)
    bucket_nll = -math.log(max(adjusted[cell["settled_bucket"]] / total, 1e-6))
    return bucket_nll, window_nll, claim, hit


def _mean_scores(
    cells: Sequence[Mapping[str, Any]],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
    width_tier_lambdas: Sequence[float] | None = None,
) -> dict[str, float]:
    """Mean bucket NLL, mean window NLL, the combined objective and the
    window containment ratio (hits over claims) over ``cells``."""

    if not cells:
        return {"bucket": 0.0, "window": 0.0, "objective": 0.0, "containment": 0.0, "cells": 0}
    bucket = window = claims = 0.0
    hits = 0
    for cell in cells:
        b, w, c, h = _cell_scores(
            cell, delta_c=delta_c, width_lambda=width_lambda,
            sigma_floor_rungs=sigma_floor_rungs,
            width_tier_lambdas=width_tier_lambdas,
        )
        bucket += b
        window += w
        claims += c
        hits += h
    n = len(cells)
    return {
        "bucket": bucket / n,
        "window": window / n,
        "objective": (bucket + WINDOW_TERM_WEIGHT * window) / n,
        "containment": (hits / claims) if claims > 0 else 0.0,
        "cells": n,
    }


def _search_tiers(
    cells: Sequence[Mapping[str, Any]],
    *,
    sigma_floor_rungs: float = 0.0,
    pooled_lambda: float = 1.0,
) -> tuple[float, tuple[float, ...], float]:
    """The best shared shift and one lambda per width tier.

    Tiers partition the cells, so for a fixed shift each tier's lambda is
    chosen on its own cells alone and the objective adds up; the shift is
    then the one whose sum is smallest.  A tier with no cells keeps the
    pooled lambda.  Scored on the combined objective (bucket + window).
    """

    by_tier: dict[int, list[Mapping[str, Any]]] = {}
    for cell in cells:
        by_tier.setdefault(_cell_tier(cell), []).append(cell)
    tier_count = len(WIDTH_TIER_BOUNDARIES_RUNGS) + 1
    best: tuple[float, tuple[float, ...], float] | None = None
    for delta in SHIFT_GRID_C:
        if abs(delta) > MAX_ABS_SHIFT_C:
            continue
        lambdas: list[float] = []
        total = 0.0
        for tier in range(tier_count):
            members = by_tier.get(tier, [])
            if not members:
                lambdas.append(float(pooled_lambda))
                continue
            tier_best: tuple[float, float] | None = None
            for lam in WIDTH_TIER_LAMBDA_GRID:
                if not MIN_WIDTH_LAMBDA <= lam <= MAX_WIDTH_LAMBDA:
                    continue
                scores = _mean_scores(
                    members, delta_c=delta, width_lambda=lam,
                    sigma_floor_rungs=sigma_floor_rungs,
                )
                value = scores["objective"] * len(members)
                if tier_best is None or value < tier_best[1]:
                    tier_best = (lam, value)
            lambdas.append(tier_best[0])
            total += tier_best[1]
        objective = total / max(1, len(cells))
        if best is None or objective < best[2]:
            best = (delta, tuple(lambdas), objective)
    if best is None:
        return 0.0, tuple([float(pooled_lambda)] * tier_count), 0.0
    return best


def _width_tier_lane(
    *,
    forward: Sequence[Mapping[str, Any]],
    incumbent: tuple[float, float],
    incumbent_floor: float,
    incumbent_tiers: Mapping[str, Any] | None,
    pooled: tuple[float, float],
    pooled_floor: float,
    tiered: tuple[float, tuple[float, ...]],
) -> dict[str, Any]:
    """Whether the tiers earned their place, and the numbers that say so.

    Admitted where the tiered candidate beats the pooled candidate on the
    combined forward objective by the layer's hurdle without regressing the
    bucket NLL by more than a noise margin -- the window term is what the
    lane is for, but it may not buy it by making the bucket read worse.
    """

    inc = _mean_scores(
        forward, delta_c=incumbent[0], width_lambda=incumbent[1],
        sigma_floor_rungs=incumbent_floor,
        width_tier_lambdas=(incumbent_tiers or {}).get("lambdas") or None,
    )
    pool = _mean_scores(
        forward, delta_c=pooled[0], width_lambda=pooled[1],
        sigma_floor_rungs=pooled_floor,
    )
    tier = _mean_scores(
        forward, delta_c=tiered[0], width_lambda=pooled[1],
        sigma_floor_rungs=pooled_floor, width_tier_lambdas=tiered[1],
    )
    by_tier: dict[int, list[Mapping[str, Any]]] = {}
    for cell in forward:
        by_tier.setdefault(_cell_tier(cell), []).append(cell)
    per_tier = []
    for index in range(len(WIDTH_TIER_BOUNDARIES_RUNGS) + 1):
        members = by_tier.get(index, [])
        entry: dict[str, Any] = {
            "tier": WIDTH_TIER_NAMES[index],
            "forward_cells": len(members),
            "candidate_lambda": tiered[1][index],
        }
        if members:
            p = _mean_scores(
                members, delta_c=pooled[0], width_lambda=pooled[1],
                sigma_floor_rungs=pooled_floor,
            )
            t = _mean_scores(
                members, delta_c=tiered[0], width_lambda=pooled[1],
                sigma_floor_rungs=pooled_floor, width_tier_lambdas=tiered[1],
            )
            entry.update({
                "containment_pooled": p["containment"],
                "containment_tiered": t["containment"],
                "bucket_nll_pooled": p["bucket"],
                "bucket_nll_tiered": t["bucket"],
            })
        per_tier.append(entry)
    gain_objective = pool["objective"] - tier["objective"]
    bucket_regression = tier["bucket"] - pool["bucket"]
    measured = (
        gain_objective > MIN_FORWARD_NLL_GAIN
        and bucket_regression <= MAX_BUCKET_NLL_REGRESSION
    )
    admitted = measured and WIDTH_TIER_MODE == "serve"
    return {
        "schema": "center_width_tier_lane_v1",
        "mode": WIDTH_TIER_MODE,
        "boundaries_rungs": list(WIDTH_TIER_BOUNDARIES_RUNGS),
        "tier_names": list(WIDTH_TIER_NAMES),
        "window_term_weight": WINDOW_TERM_WEIGHT,
        "candidate_delta_c": tiered[0],
        "candidate_lambdas": list(tiered[1]),
        "pooled_delta_c": pooled[0],
        "pooled_width_lambda": pooled[1],
        "forward_incumbent": inc,
        "forward_pooled_candidate": pool,
        "forward_tier_candidate": tier,
        "forward_objective_gain_vs_pooled": gain_objective,
        "forward_objective_gain_vs_incumbent": inc["objective"] - tier["objective"],
        "forward_bucket_nll_regression_vs_pooled": bucket_regression,
        "forward_window_nll_gain_vs_pooled": pool["window"] - tier["window"],
        "min_forward_nll_gain": MIN_FORWARD_NLL_GAIN,
        "max_bucket_nll_regression": MAX_BUCKET_NLL_REGRESSION,
        "per_tier": per_tier,
        "measured_admitted": measured,
        "admitted": admitted,
        "state": (
            "ADMITTED" if admitted
            else "SHADOW_ADMITTED" if measured
            else "REFUSED_FORWARD"
        ),
    }


def served_arithmetic_anchor(
    conn: sqlite3.Connection, *, stage: str, known_floor: str | None = None
) -> str | None:
    """When the arithmetic this stage serves today first started serving.

    These scalars measure how wrong the *served* distribution is, so decisions
    taken before the arithmetic changed describe a distribution nobody serves
    any more -- left unbounded the fit re-derives whatever it had been
    compensating for.  The bound used to be a date maintained by hand in the
    daily script, which is a second place to remember and was measured to be
    wrong the moment it was needed: it was still pointing at the 2026-08-18
    width fix on the day the ceiling landed.

    ``posterior_version`` already rides on every envelope and already moves
    when the arithmetic moves, so the boundary can be read rather than
    remembered: the earliest decision still carrying the version being served
    now.  A stage with nothing committed returns None, which the caller reads
    as "no bound", the same as before.

    Inferring it that way makes a bump expensive, and the expense is why the
    bumps stop being made: a version bumped today first appears today, so the
    bump costs the lane every day it had accumulated.  Charter 122 wrote the
    trap down and paid a hand-set flag in the sweep rather than the evidence.
    So a bump may instead SAY when its arithmetic began -- see
    ``serving_arithmetic.STAGE_VERSION_EFFECTIVE_FROM`` -- and the declaration
    is preferred over the inference for exactly the versions that carry one.
    """

    # A store that cannot answer the question yields no bound rather than an
    # error: replays and fixtures carry a narrower table than production, and
    # the caller's own bound is the pre-existing behaviour to fall back to.
    # posterior_version rides on the envelope row itself, keyed by the
    # checkpoint the history row names; history rows no longer carry a
    # body of their own to json_extract from.
    try:
        current = conn.execute(
            "SELECT e.posterior_version"
            " FROM basket_probability_bridge_history h"
            " JOIN basket_probability_envelope e"
            "   ON e.envelope_id = h.checkpoint_id"
            " WHERE h.stage=? AND h.state='COMMITTED'"
            " ORDER BY h.decision_at_utc DESC LIMIT 1",
            (_check_stage(stage),),
        ).fetchone()
        if current is None:
            return None
        version = str(current[0] or "")
        if not version:
            return None
        declared = serving_arithmetic.version_effective_from(
            _check_stage(stage), version
        )
        if declared:
            # Said, not inferred.  Nothing below runs: the seek and the scan
            # both exist only to reconstruct a moment the bump has now stated.
            return declared
        if known_floor:
            # The caller already holds a bound and takes the maximum, so this
            # anchor only matters if it lands strictly above it.  One
            # descending seek answers that: if any decision at or below the
            # bound already carries the served version, the minimum is at or
            # below it too and cannot move the maximum.  The scan below walks
            # every row of the stage older than the version's first
            # appearance -- 147,870 of them for P1 -- so skipping it is the
            # difference between 27.0s and 1.5ms, for the same answer.
            settled_below = conn.execute(
                "SELECT 1"
                " FROM basket_probability_bridge_history h"
                " JOIN basket_probability_envelope e"
                "   ON e.envelope_id = h.checkpoint_id"
                " WHERE h.stage=? AND h.state='COMMITTED'"
                " AND h.decision_at_utc<=? AND e.posterior_version=?"
                " ORDER BY h.decision_at_utc DESC LIMIT 1",
                (_check_stage(stage), str(known_floor), version),
            ).fetchone()
            if settled_below is not None:
                return None
        row = conn.execute(
            "SELECT MIN(h.decision_at_utc)"
            " FROM basket_probability_bridge_history h"
            " JOIN basket_probability_envelope e"
            "   ON e.envelope_id = h.checkpoint_id"
            " WHERE h.stage=? AND h.state='COMMITTED'"
            " AND e.posterior_version=?",
            (_check_stage(stage), version),
        ).fetchone()
    except (sqlite3.OperationalError, TypeError, ValueError):
        return None
    return str(row[0]) if row and row[0] else None



# One refit round asks the same three loading questions six to nine times.
# The stage's evidence floor, the truth table and the serving cells are each
# resolved once per lane, and the lanes pass identical arguments -- measured
# 2026-08-26: 608s of a 885s round went to ``served_arithmetic_anchor`` alone,
# 202s of it for a value ``record`` discards, and 252s more to re-scanning the
# same 287k history rows.  The memo lives for exactly one
# ``refit_all_center_width`` call because ``serving_arithmetic.record`` stamps
# before the fit begins: within a round the floor is fixed, across rounds it
# is not.  Direct callers of the two fit functions get no memo and behave
# exactly as before.
_ROUND_MEMO: dict[tuple[Any, ...], Any] | None = None


@contextmanager
def _round_memo() -> "Iterator[None]":
    """Memoise one refit round's loading questions, then forget them."""

    global _ROUND_MEMO
    outer = _ROUND_MEMO
    _ROUND_MEMO = {} if outer is None else outer
    try:
        yield
    finally:
        _ROUND_MEMO = outer


def _memoised(key: tuple[Any, ...], produce: "Callable[[], Any]") -> Any:
    memo = _ROUND_MEMO
    if memo is None:
        return produce()
    if key not in memo:
        memo[key] = produce()
    return memo[key]


def _evidence_floor(
    conn: sqlite3.Connection, *, stage: str, caller_bound: str | None
) -> str | None:
    """The latest of the boundaries that say what is actually being served.

    Two bounds, not three (user ruling, 2026-08-27).  ``posterior_version``
    rides on every envelope and moves when the served distribution moves, so
    it is the honest one; the caller's bound covers what happened before any
    of this existed.  The AST fingerprint used to be a third, and it is now
    a watchdog instead -- see ``serving_arithmetic.watchdog``.

    Why it was demoted: it hashes the serving functions, so it moves for a
    commit that only records a receipt or opens an inert replay door exactly
    as it does for a real change of arithmetic.  The original note here said
    a boundary "can only ever be too recent, and too recent merely costs days
    of evidence".  Measured 2026-08-27, that is false: three P0 generations
    landed within four hours, and P1 had never once held a fingerprint for
    the eight days the layer needs -- longest quiet window since 2026-08-23
    was 1.5 days.  Too recent did not cost days of evidence, it cost all of
    it, permanently, and a layer that never activates never corrects the
    error it was built to correct.

    The failure the AST caught is real and stays caught: it now raises a
    discrepancy that names the stage and the moment, rather than silently
    spending the evidence.  What clears it is bumping the version, which is
    the thing that was skipped in the first place.
    """

    def _resolve() -> str | None:
        floor = caller_bound
        served = served_arithmetic_anchor(conn, stage=stage, known_floor=floor)
        if served and (floor is None or str(served) > str(floor)):
            floor = served
        return floor

    # ``caller_bound`` is in the key: two lanes given different bounds are
    # asking different questions and must not share an answer.
    return _memoised(("evidence_floor", stage, caller_bound), _resolve)


def _forward_lift_census(
    forward: Sequence[Mapping[str, Any]], *, floor_rungs: float
) -> dict[str, Any]:
    """How much of the forward window the floor actually reaches.

    A lane that lifts nothing and a lane that lifts everything can post the
    same NLL gain for very different reasons, and only this says which.
    """

    lifts: list[float] = []
    for cell in forward:
        try:
            lift, _sigma, _rung = sigma_floor_lift_v1(
                cell["probabilities"],
                ordered_keys=cell["ordered_keys"],
                key_centers_c=cell["key_centers_c"],
                floor_rungs=floor_rungs,
            )
        except ValueError:
            continue
        lifts.append(lift)
    lifted = sum(1 for lift in lifts if lift > 1.0)
    return {
        "forward_cell_count": len(forward),
        "forward_lifted_count": lifted,
        "forward_lifted_share": lifted / len(lifts) if lifts else 0.0,
        "forward_mean_lift": sum(lifts) / len(lifts) if lifts else 1.0,
    }


def _rolling_origin_admission(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    days: Sequence[str],
    *,
    sigma_floor_rungs: float = 0.0,
) -> dict[str, Any]:
    """Walk the origin: fit on the days before each held-out day, score on it.

    The gain returned is the cell-weighted mean over folds of what the search
    bought against identity -- identity being what the stage serves today,
    since the cells already carry it.  Cell-weighted rather than fold-weighted
    so a thin day cannot outvote a full one.

    What this validates is the search *procedure*, not one pair: each fold
    picks its own pair from its own prefix.  That is what licenses the served
    pair being refitted on every day afterwards, which is where the newest
    evidence is.
    """

    ordered = list(days)
    first = max(
        ROLLING_ORIGIN_MIN_FOLD_TRAIN_DAYS,
        len(ordered) - ROLLING_ORIGIN_MAX_FOLDS,
    )
    folds: list[dict[str, Any]] = []
    total_gain = 0.0
    total_cells = 0
    for index in range(first, len(ordered)):
        train = [
            cell for day in ordered[:index] for cell in by_day[day]
        ]
        test = list(by_day[ordered[index]])
        if not train or not test:
            continue
        delta, lam, _train_nll = _search(
            train, sigma_floor_rungs=sigma_floor_rungs
        )
        identity_nll = _mean_nll(
            test,
            delta_c=RESIDUAL_IDENTITY[0],
            width_lambda=RESIDUAL_IDENTITY[1],
            sigma_floor_rungs=0.0,
        )
        candidate_nll = _mean_nll(
            test,
            delta_c=delta,
            width_lambda=lam,
            sigma_floor_rungs=sigma_floor_rungs,
        )
        gain = identity_nll - candidate_nll
        folds.append({
            "scored_day": ordered[index],
            "train_day_count": index,
            "train_end_day": ordered[index - 1],
            "cell_count": len(test),
            "delta_c": delta,
            "width_lambda": lam,
            "identity_nll": identity_nll,
            "candidate_nll": candidate_nll,
            "gain": gain,
        })
        total_gain += gain * len(test)
        total_cells += len(test)
    return {
        "schema": "center_width_rolling_origin_v1",
        "min_fold_train_days": ROLLING_ORIGIN_MIN_FOLD_TRAIN_DAYS,
        "max_folds": ROLLING_ORIGIN_MAX_FOLDS,
        "fold_count": len(folds),
        "scored_cell_count": total_cells,
        "gain": (total_gain / total_cells) if total_cells else 0.0,
        "folds_positive": sum(1 for fold in folds if fold["gain"] > 0.0),
        # A mean is not enough, and the first live run says why: on P0's ten
        # days to 2026-09-09 the folds picked four different pairs, two of
        # five gained, and the mean came out at +0.0066 -- over the hurdle --
        # because one hard day (09-09, identity NLL 2.02) swung +0.0987 and
        # carried three losing folds.  Admitting there would have swapped one
        # window's luck for one fold's.  So a majority of folds must gain as
        # well, which no single day can supply.
        "majority_positive": (
            sum(1 for fold in folds if fold["gain"] > 0.0) * 2 > len(folds)
            if folds else False
        ),
        # Whether the folds agreed on where to go, which a single number
        # cannot say: a mean gain assembled from opposite picks is a
        # procedure nobody should promote on.  Reported, not gated -- the
        # majority rule above already refuses the case this diagnoses, and a
        # second hurdle on it would be two names for one judgement.
        "distinct_picks": len({
            (fold["delta_c"], fold["width_lambda"]) for fold in folds
        }),
        "folds": folds,
    }


def _sigma_floor_lane(
    *,
    forward: Sequence[Mapping[str, Any]],
    pooled: tuple[float, float],
    floored: tuple[float, float],
    pooled_nll: float,
    floored_nll: float,
    incumbent_nll: float,
    incumbent_floor_rungs: float,
) -> dict[str, Any]:
    """Whether the floor earned its place, and the numbers that say so.

    Admitted only where it beats the *pooled* candidate on the held-out
    forward window by the layer's own hurdle.  Beating the incumbent is not
    enough on its own: the floored fit carries a refitted lambda with it, and
    a lane that only wins by dragging that refit along has not shown that the
    floor is what did the work.  The pair as a whole then faces the incumbent
    at the usual gate, one rung up.
    """

    gain_pooled = pooled_nll - floored_nll
    measured = gain_pooled > MIN_FORWARD_NLL_GAIN
    # The verdict is recorded whatever the mode; only serving is switched.
    # A shadow that stopped measuring would have nothing to promote on.
    admitted = measured and SIGMA_FLOOR_MODE == "serve"
    out: dict[str, Any] = {
        "schema": "center_width_sigma_floor_lane_v1",
        "mode": SIGMA_FLOOR_MODE,
        "measured_admitted": measured,
        "sigma_floor_rungs": SIGMA_FLOOR_RUNGS,
        "max_lift": MAX_SIGMA_FLOOR_LIFT,
        "incumbent_floor_rungs": float(incumbent_floor_rungs),
        "pooled_delta_c": pooled[0],
        "pooled_width_lambda": pooled[1],
        "candidate_delta_c": floored[0],
        "candidate_width_lambda": floored[1],
        "forward_nll_incumbent": incumbent_nll,
        "forward_nll_pooled_candidate": pooled_nll,
        "forward_nll_floor_candidate": floored_nll,
        "forward_nll_gain_vs_pooled": gain_pooled,
        "forward_nll_gain_vs_incumbent": incumbent_nll - floored_nll,
        "min_forward_nll_gain": MIN_FORWARD_NLL_GAIN,
        "admitted": admitted,
        "state": (
            "ADMITTED" if admitted
            else "SHADOW_ADMITTED" if measured
            else "REFUSED_FORWARD"
        ),
    }
    out.update(_forward_lift_census(forward, floor_rungs=SIGMA_FLOOR_RUNGS))
    return out


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
    min_decision_at_utc = _evidence_floor(
        conn, stage=stage_id, caller_bound=min_decision_at_utc
    )
    truth = _memoised(("truth",), lambda: _truth(conn))
    by_day = _memoised(
        ("serving_cells", stage_id, min_decision_at_utc),
        lambda: serving_cells_by_day(
            conn, truth, stage=stage_id,
            min_decision_at_utc=min_decision_at_utc,
        ),
    )
    days = sorted(by_day)
    cells = [cell for day in days for cell in by_day[day]]
    active = load_center_width(conn, stage=stage_id)
    base = (0.0, 1.0) if active is None else (active[0], active[1])
    base_floor = (
        0.0 if active is None else load_center_width_floor(conn, stage=stage_id)
    )
    base_tiers = (
        None if active is None else load_center_width_tiers(conn, stage=stage_id)
    )
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
        # Travels with every fit, refused or not: if the arithmetic moved
        # after the version that names it, these days may span two shapes
        # and the reader has to know that before trusting the scalars.
        "serving_arithmetic_watchdog": _memoised(
            ("watchdog", stage_id),
            lambda: serving_arithmetic.watchdog(conn, stage=stage_id),
        ),
    }
    if accumulating:
        evidence["basis"] = "ACCUMULATING"
        evidence["sigma_floor"] = {
            "schema": "center_width_sigma_floor_lane_v1",
            "sigma_floor_rungs": SIGMA_FLOOR_RUNGS,
            "max_lift": MAX_SIGMA_FLOOR_LIFT,
            "incumbent_floor_rungs": base_floor,
            "admitted": False,
            "state": "ACCUMULATING",
        }
        evidence["width_tiers"] = {
            "schema": "center_width_tier_lane_v1",
            "mode": WIDTH_TIER_MODE,
            "admitted": False,
            "state": "ACCUMULATING",
        }
        return {
            "stage": stage_id,
            "delta_c": base[0],
            "width_lambda": base[1],
            "sigma_floor_rungs": base_floor,
            "width_tiers": base_tiers,
            "forward_nll_gain": 0.0,
            "accumulating": True,
            "serving_day_count": len(days),
            "serving_cell_count": len(cells),
            "evidence": evidence,
        }
    # A scalar pair composes onto a scalar pair.  A floor and a tier table do
    # not: the floor is read off the input distribution and the tier a cell
    # falls in is read off its served sigma, so both mean something different
    # once the incumbent has already narrowed that cell.  Neither has ever been
    # admitted at any stage, so this refuses nothing today; it fails closed the
    # day one is, instead of storing a composition nobody defined.
    if base_floor or base_tiers:
        evidence["basis"] = "COMPOSITION_UNDEFINED_UNDER_FLOOR_OR_TIERS"
        return {
            "stage": stage_id,
            "delta_c": base[0],
            "width_lambda": base[1],
            "sigma_floor_rungs": base_floor,
            "width_tiers": base_tiers,
            "forward_nll_gain": 0.0,
            "accumulating": False,
            "admitted": False,
            "serving_day_count": len(days),
            "serving_cell_count": len(cells),
            "evidence": evidence,
        }
    cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
    train_days, forward_days = days[:cut], days[cut:] or days[-1:]
    train = [cell for day in train_days for cell in by_day[day]]
    forward = [cell for day in forward_days for cell in by_day[day]]
    # Which pair is stored does not change here: it is still the one the
    # training prefix picks.  Only the gate moves, and the two sub-lanes keep
    # the single window -- each compares one candidate against another on the
    # same days, so which days those are matters far less than it does for a
    # candidate facing identity.
    pooled_delta, pooled_lam, pooled_train_nll = _search(train)
    floor_delta, floor_lam, floor_train_nll = _search(
        train, sigma_floor_rungs=SIGMA_FLOOR_RUNGS
    )
    # The incumbent is identity here, and that is not a simplification: the
    # cells are read from what was served, so the incumbent pair is already
    # inside them.  Applying it again scored a stage against its own scalars
    # squared -- 0.75 read as 0.5625 -- which made the incumbent look 0.0296
    # nat worse than the distribution production actually served, and handed
    # that margin to every candidate as free gain.  Measured on P0's own
    # cells for 2026-09-03..09-09 on 2026-09-10.
    forward_base = _mean_nll(
        forward,
        delta_c=RESIDUAL_IDENTITY[0],
        width_lambda=RESIDUAL_IDENTITY[1],
        sigma_floor_rungs=0.0,
    )
    forward_pooled = _mean_nll(
        forward, delta_c=pooled_delta, width_lambda=pooled_lam
    )
    forward_floored = _mean_nll(
        forward,
        delta_c=floor_delta,
        width_lambda=floor_lam,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    sigma_floor = _sigma_floor_lane(
        forward=forward,
        pooled=(pooled_delta, pooled_lam),
        floored=(floor_delta, floor_lam),
        pooled_nll=forward_pooled,
        floored_nll=forward_floored,
        incumbent_nll=forward_base,
        incumbent_floor_rungs=base_floor,
    )
    evidence["sigma_floor"] = sigma_floor
    if sigma_floor["admitted"]:
        delta, lam = floor_delta, floor_lam
        train_nll, forward_candidate = floor_train_nll, forward_floored
        candidate_floor = SIGMA_FLOOR_RUNGS
    else:
        delta, lam = pooled_delta, pooled_lam
        train_nll, forward_candidate = pooled_train_nll, forward_pooled
        candidate_floor = 0.0
    # Width tiers: fitted under the floor the pair will serve, judged against
    # the pooled/floored candidate on the combined forward objective.
    tier_delta, tier_lambdas, _tier_train = _search_tiers(
        train, sigma_floor_rungs=candidate_floor, pooled_lambda=lam,
    )
    width_tier_lane = _width_tier_lane(
        forward=forward,
        incumbent=RESIDUAL_IDENTITY,
        incumbent_floor=0.0,
        incumbent_tiers=None,
        pooled=(delta, lam),
        pooled_floor=candidate_floor,
        tiered=(tier_delta, tier_lambdas),
    )
    evidence["width_tiers"] = width_tier_lane
    candidate_tiers: dict[str, Any] | None = None
    if width_tier_lane["admitted"]:
        candidate_tiers = {
            "boundaries_rungs": list(WIDTH_TIER_BOUNDARIES_RUNGS),
            "lambdas": list(tier_lambdas),
        }
        delta = tier_delta
        forward_candidate = width_tier_lane["forward_tier_candidate"]["bucket"]
    # The admission number.  A sub-lane that took the pair off the plain
    # scalar search keeps its own single-window verdict below; the question
    # here is whether the search beats what is served at all, and that is the
    # one the single window was deciding by coin toss.
    rolling = _rolling_origin_admission(
        by_day, days, sigma_floor_rungs=candidate_floor
    )
    evidence["rolling_origin"] = rolling
    single_window_gain = forward_base - forward_candidate
    gain = float(rolling["gain"])
    evidence.update({
        "basis": "ROLLING_ORIGIN",
        "train_day_count": len(train_days),
        "forward_day_count": len(forward_days),
        "train_cell_count": len(train),
        "forward_cell_count": len(forward),
        "train_nll": train_nll,
        "candidate_sigma_floor_rungs": candidate_floor,
        "incumbent_sigma_floor_rungs": base_floor,
        "forward_nll_incumbent": forward_base,
        "forward_nll_candidate": forward_candidate,
        "forward_nll_gain": gain,
        # What the single trailing window would have said, kept because the
        # two disagreeing is the reading a reviewer wants, not a detail.
        "single_window_nll_gain": single_window_gain,
        "min_forward_nll_gain": MIN_FORWARD_NLL_GAIN,
        "identity_nll_all": _mean_nll(cells, delta_c=0.0, width_lambda=1.0),
        "candidate_nll_all": _mean_nll(
            cells,
            delta_c=delta,
            width_lambda=lam,
            sigma_floor_rungs=candidate_floor,
        ),
    })
    # Both halves of the rolling verdict: the mean clears the hurdle and a
    # majority of folds gained.  The second is not a refinement -- without it
    # one hard day can carry three losing folds over the line, which is the
    # first live run's reading exactly.
    admitted = (
        gain > MIN_FORWARD_NLL_GAIN
        and bool(rolling["majority_positive"])
    )
    if candidate_tiers is not None:
        # The tier lane's own gate already held the bucket NLL to within
        # a noise margin of the pooled candidate; what it must beat at this
        # rung is the incumbent on the objective it was fitted on.  Its own
        # window is the single one, so the fold majority does not speak to
        # it; the pair it rides on has already passed the gate above.
        admitted = (
            width_tier_lane["forward_objective_gain_vs_incumbent"]
            > MIN_FORWARD_NLL_GAIN
        )
    # The TAF-conditional lane (charter 57): each direction fits its own
    # scalars from its own slice of the same train/forward split, and is
    # admitted only where it beats the POOLED candidate on its own forward
    # cells -- the pooled pair is what would serve that day otherwise, so
    # beating identity is not enough.  A cell short of the layer's own
    # hurdles stays out and its days keep serving the pooled scalars;
    # nothing is a constant, and the report shows each cell accumulate
    # until it promotes itself.
    _annotate_taf_directions(conn, cells)
    # Residual space, because these two are applied to cells: what serves if
    # the candidate is refused is the incumbent, and the incumbent is already
    # in the cell, so its residual is identity rather than its own pair.
    served_delta = delta if admitted else RESIDUAL_IDENTITY[0]
    served_lambda = lam if admitted else RESIDUAL_IDENTITY[1]
    # The cells refine the pair that would serve, so they are fitted and
    # scored under the floor that would serve with it.  A cell searched
    # without the floor and then served under one would be a scalar chosen
    # against arithmetic it never saw.
    served_floor = candidate_floor if admitted else base_floor
    taf_cells: dict[str, list[float]] = {}
    taf_evidence: dict[str, Any] = {}
    for name in TAF_CELL_NAMES:
        group_train = [
            c for c in train if c.get("taf_direction") == name
        ]
        group_forward = [
            c for c in forward if c.get("taf_direction") == name
        ]
        group_days = {
            c["event_date"] for c in group_train + group_forward
        }
        entry: dict[str, Any] = {
            "day_count": len(group_days),
            "train_cell_count": len(group_train),
            "forward_cell_count": len(group_forward),
        }
        taf_evidence[name] = entry
        if (
            len(group_days) < MIN_QUALIFYING_DAYS
            or len(group_train) + len(group_forward) < MIN_QUALIFYING_CELLS
            or not group_train or not group_forward
        ):
            entry["state"] = "ACCUMULATING"
            continue
        cell_delta, cell_lambda, _ = _search(
            group_train, sigma_floor_rungs=served_floor
        )
        pooled_nll = _mean_nll(
            group_forward, delta_c=served_delta, width_lambda=served_lambda,
            sigma_floor_rungs=served_floor,
        )
        cell_nll = _mean_nll(
            group_forward, delta_c=cell_delta, width_lambda=cell_lambda,
            sigma_floor_rungs=served_floor,
        )
        cell_gain = pooled_nll - cell_nll
        entry.update({
            "candidate_delta_c": cell_delta,
            "candidate_width_lambda": cell_lambda,
            "forward_nll_gain_vs_pooled": cell_gain,
        })
        if cell_gain > MIN_FORWARD_NLL_GAIN:
            entry["state"] = "ADMITTED"
            taf_cells[name] = [cell_delta, cell_lambda]
        else:
            entry["state"] = "REFUSED_FORWARD"
    evidence["taf_cells"] = taf_evidence
    # Leaving residual space.  Everything above measured a correction on top
    # of what is served; everything below stores a pair the server applies to
    # the uncorrected distribution, so the two are composed exactly here and
    # nowhere else.  The evidence carries both numbers because a reader who
    # sees only one cannot tell which space it is in -- which is the confusion
    # this boundary exists to end.
    absolute_delta, absolute_lambda = _compose_absolute(base, (delta, lam))
    evidence.update({
        "residual_delta_c": delta,
        "residual_width_lambda": lam,
        "absolute_delta_c": absolute_delta,
        "absolute_width_lambda": absolute_lambda,
    })
    taf_cells = {
        name: list(_compose_absolute(base, (pair[0], pair[1])))
        for name, pair in taf_cells.items()
    }
    return {
        "stage": stage_id,
        "delta_c": absolute_delta if admitted else base[0],
        "width_lambda": absolute_lambda if admitted else base[1],
        # The floor travels with the pair it qualifies.  A refused candidate
        # leaves the incumbent's floor in place for the same reason it leaves
        # the incumbent's scalars: nothing was shown to be better.
        "sigma_floor_rungs": candidate_floor if admitted else base_floor,
        "width_tiers": candidate_tiers if admitted else base_tiers,
        "candidate_width_tiers": {
            "boundaries_rungs": list(WIDTH_TIER_BOUNDARIES_RUNGS),
            "lambdas": list(tier_lambdas),
        },
        # Absolute, because the reader compares these with ``width_lambda``
        # above and with what the table serves, and both of those are
        # absolute.  The residual the grid actually returned is in the
        # evidence under ``residual_width_lambda``.
        "candidate_delta_c": absolute_delta,
        "candidate_width_lambda": absolute_lambda,
        "candidate_sigma_floor_rungs": candidate_floor,
        "forward_nll_gain": gain,
        "admitted": admitted,
        "accumulating": False,
        "serving_day_count": len(days),
        "serving_cell_count": len(cells),
        "taf_cells": taf_cells,
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
    taf_cells: Mapping[str, Sequence[float]] | None = None,
    sigma_floor_rungs: float = 0.0,
    width_tiers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    digest = center_width_sha256(
        stage=stage_id, delta_c=delta_c, width_lambda=width_lambda,
        taf_cells=taf_cells, sigma_floor_rungs=sigma_floor_rungs,
        width_tiers=width_tiers,
    )
    tiers_json = (
        json.dumps(
            {
                "boundaries_rungs": [float(b) for b in width_tiers["boundaries_rungs"]],
                "lambdas": [float(v) for v in width_tiers["lambdas"]],
            },
            sort_keys=True,
        )
        if width_tiers else None
    )
    active = load_center_width(conn, stage=stage_id)
    if active is not None and active[2] == digest:
        return {"state": "UNCHANGED", "center_width_sha256": digest}
    now = fitted_at_utc or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    cells_json = (
        json.dumps(
            {
                str(name): [float(pair[0]), float(pair[1])]
                for name, pair in sorted(taf_cells.items())
            },
            ensure_ascii=False, sort_keys=True,
        )
        if taf_cells else None
    )
    # Explicit acquisition rather than the implicit transaction the first
    # INSERT would open: the retry budget is the point, not the isolation.
    _begin_immediate(conn)
    try:
        conn.execute(
            "INSERT INTO calibration_center_width_table ("
            " center_width_sha256, stage, delta_c, width_lambda,"
            " evidence_json,"
            " fitted_at_utc, serving_day_count, activated, activated_at_utc,"
            " taf_cells_json, sigma_floor_rungs, width_tiers_json"
            ") VALUES (?,?,?,?,?,?,?,0,NULL,?,?,?)"
            " ON CONFLICT(center_width_sha256) DO UPDATE SET"
            "  evidence_json=excluded.evidence_json,"
            "  fitted_at_utc=excluded.fitted_at_utc,"
            "  serving_day_count=excluded.serving_day_count,"
            "  taf_cells_json=excluded.taf_cells_json,"
            "  sigma_floor_rungs=excluded.sigma_floor_rungs,"
            "  width_tiers_json=excluded.width_tiers_json",
            (
                digest, stage_id, float(delta_c), float(width_lambda),
                json.dumps(
                    dict(evidence), ensure_ascii=False, sort_keys=True
                ),
                now, int(serving_day_count), cells_json,
                float(sigma_floor_rungs) or None, tiers_json,
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
    except Exception:
        conn.rollback()
        raise
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
    # The activated row hashes the TAF cells and the width floor too, so a
    # journal digest that omits either can never match the row it is
    # evidence for.
    digest = center_width_sha256(
        stage=stage_id,
        delta_c=fitted["delta_c"],
        width_lambda=fitted["width_lambda"],
        taf_cells=fitted.get("taf_cells") or None,
        sigma_floor_rungs=float(fitted.get("sigma_floor_rungs") or 0.0),
        width_tiers=fitted.get("width_tiers") or None,
    )
    fit_id = "cwfit-" + content_sha256({
        "stage": stage_id,
        "fitted_at_utc": now,
        "center_width_sha256": digest,
    })[:24]
    _begin_immediate(conn)
    try:
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
                    dict(fitted["evidence"]),
                    ensure_ascii=False, sort_keys=True,
                ),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return fit_id


def refit_all_center_width(
    conn: sqlite3.Connection,
    *,
    commit: bool = False,
    min_decision_at_utc: str | None = None,
    stages: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Fit every stage, journal every fit, activate the admitted ones.

    ``stages`` narrows the pass to the ones named, so a caller holding a
    different evidence bound per stage can run them one at a time.  A serving
    change usually touches one compile path, and a stage whose arithmetic did
    not move should keep the days it has already earned -- the window needs
    eight of them and fixes have been landing faster than that.

    Isolation matches the band store's (2026-08-20 lesson: one stage
    losing the write-lock race to the resident took all three stages'
    fits and journal rows with it, though their evidence had nothing to
    do with the lock).  Two grains of it here, because a stage does two
    units of work: a stage that throws records ``error`` in its own entry
    and the loop continues, and within a stage the scalar lane and the
    per-station lane are isolated from each other -- they are two fits on
    two tables that merely share an anchor, so the scalar lane failing
    must not cost the station lane its fit, nor the reverse.  After any
    failure the connection is rolled back so a transaction left open
    cannot fail the next unit's lock take for an unrelated reason.

    A partial refit is still a failed refit; the CLI exits non-zero when
    any entry carries ``error``, so the cron reports ✗ without discarding
    the lanes that did land.
    """

    results: dict[str, Any] = {}
    # An explicit ``stages`` is the caller's own order and is honoured as
    # given; only the default sweep order is rotated.
    wanted = (
        tuple(stages) if stages
        else stage_order_by_staleness(conn, CENTER_WIDTH_STAGES)
    )
    with _round_memo():
        _refit_stages(conn, wanted, results, commit, min_decision_at_utc)
    return results


def stage_order_by_staleness(
    conn: sqlite3.Connection, stages: "Sequence[str]"
) -> tuple[str, ...]:
    """Least-recently fitted first, ties in the canonical order.

    A round that is cut short always cut the same stage.  The tuple was read
    from the head every time and nothing remembered where the previous round
    stopped, so P1 -- last in it and the most expensive -- was what never got
    fitted: measured 2026-08-26, sixteen journal rows for P0 and eleven for
    P1, whose newest was a full day behind the other two.  The resident's
    hosted-sweep scheduler had the identical fault and the identical fix.

    Staleness comes from the journals because they are the only record that
    survives the process.  A stage is as stale as its *most* neglected lane,
    and a lane with no row at all has never run, which sorts first: the two
    lanes are separately isolated, so a stage can carry one and not the other,
    and SQL's ``MIN`` would drop the missing one and rank that stage by the
    lane that did run -- the exact inversion of what is wanted.

    Known boundary: a stage that raises writes no row, so its timestamp stays
    frozen and it keeps the head.  While it fails fast that is merely a bad
    stage pinned where the old order pinned P1, and no worse; a slow failure
    would spend budget before the others run.  The error lives only in the
    returned entry, never in a table, so no ordering read from the database
    can see it.
    """

    def _last_fit(stage: str) -> str:
        newest = ""
        for table in (
            "calibration_center_width_fit_journal",
            "calibration_center_width_station_journal",
        ):
            try:
                row = conn.execute(
                    f"SELECT MAX(fitted_at_utc) FROM {table} WHERE stage=?",
                    (stage,),
                ).fetchone()
            except sqlite3.Error:
                # No journal is the same answer as no fit: run it first.
                return ""
            value = "" if row is None or row[0] is None else str(row[0])
            if not value:
                return ""
            newest = value if not newest else min(newest, value)
        return newest

    canonical = {stage: index for index, stage in enumerate(CENTER_WIDTH_STAGES)}
    return tuple(
        sorted(
            stages,
            key=lambda stage: (_last_fit(stage), canonical.get(stage, 0)),
        )
    )


def _refit_stages(
    conn: sqlite3.Connection,
    wanted: "Sequence[str]",
    results: dict[str, Any],
    commit: bool,
    min_decision_at_utc: str | None,
) -> None:
    for stage in wanted:
        if stage not in CENTER_WIDTH_STAGES:
            raise ContractError("center_width_stage_invalid")
        entry: dict[str, Any] = {"stage": stage}
        results[stage] = entry
        try:
            fitted = fit_center_width(
                conn, stage=stage,
                min_decision_at_utc=min_decision_at_utc,
            )
            active = load_center_width(conn, stage=stage)
            entry.update({
                "delta_c": fitted["delta_c"],
                "width_lambda": fitted["width_lambda"],
                "candidate_delta_c": fitted.get("candidate_delta_c"),
                "candidate_width_lambda": fitted.get(
                    "candidate_width_lambda"
                ),
                "forward_nll_gain": fitted["forward_nll_gain"],
                "accumulating": fitted["accumulating"],
                "admitted": fitted.get("admitted", False),
                "served_stage": stage in CENTER_WIDTH_SERVED_STAGES,
                "active_sha256": None if active is None else active[2],
                "serving_day_count": fitted["serving_day_count"],
                "serving_cell_count": fitted["serving_cell_count"],
                "min_decision_at_utc": min_decision_at_utc,
                "taf_cells": fitted.get("taf_cells") or {},
                "taf_cell_states": {
                    name: str(detail.get("state") or "")
                    for name, detail in (
                        fitted["evidence"].get("taf_cells") or {}
                    ).items()
                },
                # The floor rides on the report so the daily run shows it
                # admitted, refused or accumulating without anyone reading
                # the journal's evidence blob by hand.
                "sigma_floor_rungs": fitted.get("sigma_floor_rungs") or 0.0,
                "sigma_floor": fitted["evidence"].get("sigma_floor") or {},
                "width_tiers": fitted.get("width_tiers"),
                "candidate_width_tiers": fitted.get("candidate_width_tiers"),
                "width_tier_lane": fitted["evidence"].get("width_tiers") or {},
            })
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
                        taf_cells=fitted.get("taf_cells") or None,
                        sigma_floor_rungs=float(
                            fitted.get("sigma_floor_rungs") or 0.0
                        ),
                        width_tiers=fitted.get("width_tiers") or None,
                    )
                    entry["commit"] = committed
                    outcome = str(committed["state"])
                entry["outcome"] = outcome
                entry["fit_id"] = journal_center_width_fit(
                    conn, stage=stage, fitted=fitted, outcome=outcome
                )
        except Exception as exc:  # noqa: BLE001 -- isolation is the point
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            entry["error"] = f"{type(exc).__name__}: {exc}"
        # The per-station width scales ride the same entry point, the same
        # evidence anchor and the same admission discipline as the stage
        # scalars they compose with.
        station_entry: dict[str, Any] = {}
        entry["station_width"] = station_entry
        try:
            station_fitted = fit_station_width(
                conn, stage=stage,
                min_decision_at_utc=min_decision_at_utc,
            )
            station_active = load_station_width(conn, stage=stage)
            station_entry.update({
                "station_count": station_fitted["station_count"],
                "forward_nll_gain": station_fitted["forward_nll_gain"],
                "accumulating": station_fitted["accumulating"],
                "vetoed": station_fitted.get("vetoed", False),
                "admitted": station_fitted.get("admitted", False),
                "active_sha256": (
                    None if station_active is None else station_active[1]
                ),
            })
            if commit:
                if stage not in CENTER_WIDTH_SERVED_STAGES:
                    station_outcome = "NOT_WIRED"
                elif station_fitted.get("vetoed", False):
                    station_committed = persist_and_activate_station_width(
                        conn, stage=stage, scales={}, per_station={},
                    )
                    station_entry["commit"] = station_committed
                    station_outcome = "INCUMBENT_VETOED"
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
                            station_fitted["evidence"].get("per_station")
                            or {}
                        ),
                    )
                    station_entry["commit"] = station_committed
                    station_outcome = str(station_committed["state"])
                station_entry["outcome"] = station_outcome
                station_entry["fit_id"] = journal_station_width_fit(
                    conn, stage=stage, fitted=station_fitted,
                    outcome=station_outcome,
                )
        except Exception as exc:  # noqa: BLE001 -- isolation is the point
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            station_entry["error"] = f"{type(exc).__name__}: {exc}"

