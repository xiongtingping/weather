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

import functools
import json
import os
import math
import sqlite3
import statistics
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
from .band_reliability_store import (
    _begin_immediate,
    _invert_band_receipt,
    utc_instant,
)
from .center_width_calibration import (
    WIDTH_TIER_BOUNDARIES_RUNGS,
    WIDTH_TIER_NAMES,
    distribution_sigma_rungs_v1,
    width_tier_index_v1,
    MAX_ABS_SHIFT_C,
    MAX_SIGMA_FLOOR_LIFT,
    MAX_WIDTH_LAMBDA,
    MIN_WIDTH_LAMBDA,
    OPEN_EDGE_TAIL_RULE,
    SERVED_OPEN_EDGE_OFFSET_C,
    apply_center_width_calibration,
    center_width_adjusted_grid,
    bucket_rung_width_c,
    served_bucket_axis,
    sigma_floor_lift_v1,
    THIN_FAR_MAX_SEATED_SYSTEMS,
    THIN_FAR_MIN_HOURS_TO_SETTLEMENT,
    THIN_FAR_SLICE_NAME,
    TAIL_MIX_SCALE,
    TAIL_MIX_WEIGHT_GRID,
    apply_peak_ceiling,
    apply_tail_mix,
    thin_far_slice_applies,
    MAX_MODE_SHOULDER_SHARE,
    MODE_SHOULDER_BINS,
    MODE_SHOULDER_SCHEMA,
    MODE_SHOULDER_SHARE_GRID,
    MODE_SHOULDER_WARM_SHARE_RULE,
    apply_mode_shoulder,
    mode_shoulder_bin_for,
)
from . import serving_arithmetic
from .contracts import ContractError, content_sha256
from .settlement_time import hours_to_settlement_if_known

CENTER_WIDTH_STAGES = ("P0", "P0-M", "P1")
# Stages whose serving path actually reads this table.  P0's distribution is
# produced by the native-ensemble compiler against an OOS-gated artifact that
# pins the band table version its holdout was scored against; hanging an
# un-pinned correction after it would serve math the gate never saw, which is
# the serve/train split the spread cap already taught us.  P0 is fitted and
# journalled anyway -- the measurement is worth having, and the day the
# pinning is built the evidence is already there -- but it is not activated,
# because an active row is a claim that something is being served.
# Charter 150 (2026-09-19): P0 only.  P0-M and P1 are one likelihood update
# each on P0's calibrated root and carry no centre/width correction of their
# own; the refit retires whatever they still had and fits nothing for them.
CENTER_WIDTH_SERVED_STAGES = ("P0",)
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
#
# The lambda grid reaches past one (charter 138).  It used to stop there,
# which was the era's own reasoning -- the basin above sat well below one, so
# 1.000 read as a generous ceiling.  It is not a ceiling, it is a vocabulary:
# a grid that stops at one can say "narrow" and "hold" and cannot say
# "widen", whatever the evidence says.  Measured 2026-09-16, P0-M's window
# containment ran 0.969/0.966/0.942 across three windows -- claiming the
# mean+-1 window more often than it settles there, i.e. served too narrow --
# and the residual that fixes it is 1.05 to 1.10, so every fit pinned at the
# ceiling and reported "unchanged".  1.5 is the kernel's bound and is a
# different statement (a correction this layer is not entitled to make, which
# must be reported rather than served), so the grid stops short of it.
SHIFT_GRID_C = tuple(round(-0.8 + 0.05 * step, 2) for step in range(33))
LAMBDA_GRID = tuple(round(0.55 + 0.025 * step, 3) for step in range(29))
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
# Charter 228 (user 2026-09-25, 「继续」 on judging this lane by charters 211
# and 224).  Every verdict the lane reaches -- admission, the veto, the floor
# and tier sub-lanes, the station lane -- changes what P0 serves, so it needs
# what any model change needs: the bucket Brier and the LogScore both
# significantly lower.  Both are scored on the vector serving serves, i.e.
# after ``materialization.floor_probabilities`` (charter 224); the interval
# resamples whole station-days, because the cells of one station-day are one
# weather.  This replaces a hurdle of 0.005 nat on one score, read at a 1e-6
# floor serving never applies -- a truth on a near-zero tail cost 13.8 nats
# where serving is charged 6.9, and 57 such cells carried 71% of the long
# window's "widen" gain measured on 2026-09-16.
# Charter 240 (user ruling 2026-10-01): the model's target is P(Y=k|X), and
# the three proper scores of the bucket distribution are ranked, not equal
# vetoes.  The LogScore is the primary judge and must be significantly lower;
# the ladder CRPS checks the distribution's structure once bucket distance
# counts and must not be significantly higher; the bucket Brier is the
# guardrail against buying a few cells at the cost of the whole and must not
# be significantly higher either.  Charter 228's rule (both Brier and
# LogScore significantly lower) asked the guardrail to prove an improvement
# it has half the power to see.
GATE_RULE = "logscore_primary_crps_brier_guardrail_station_day_bootstrap_v1"
GATE_BOOTSTRAP_DRAWS = 2000
GATE_BOOTSTRAP_SEED = 20260925
GATE_CONFIDENCE = 0.95
# The pooled table is held while the lane accumulates and held again when a
# candidate is refused, and the evidence clock restarts on every serving
# arithmetic change -- so the moment most likely to invalidate a pair is the
# same moment that guarantees nobody looks at it for eight days.  The station
# lane answered this in 2026-09-10 with a veto; this is that gate at the
# pooled grain (charter 138).  It is a different question from admission, and
# cheaper one: installing a table needs evidence, while going back to
# identity needs none -- identity is applying no correction, the default, not
# a claim to be proved.  So the window is half the qualifying floor, on the
# station lane's reasoning: a day earlier here is one fewer day served wrong.
CENTER_WIDTH_VETO_MIN_DAYS = 3
CENTER_WIDTH_VETO_MIN_CELLS = 60
# Charter 239 (user ruling 2026-10-01, 「按照logscore单独判」): the thin-pool
# far-lead slice -- decisions seating at most THIN_FAR_MAX_SEATED_SYSTEMS
# systems more than THIN_FAR_MIN_HOURS_TO_SETTLEMENT hours before settlement
# -- is judged on the LogScore alone.  Measured on the ladder the bucket
# Brier needs about twice the LogScore's sample to see a 15% width error
# (300 vs 600 independent station-days; with 45 stations sharing a day's
# weather the effective count is far smaller), and twice in a row (09-27
# first-decision slice -0.0174 [-0.0288, -0.0078]; 10-01 -0.034 [-0.072,
# -0.005]) the LogScore has called the slice's widening while the Brier sat
# flat.  Admission, the fold majority and the veto on this slice all read
# this rule; since charter 240 the slice and everything pooled read one verdict.
# Charter 240 subsumes charter 239's LogScore-only gate: the slice is judged
# by the stage's one verdict, whose primary score is the LogScore and whose
# CRPS and Brier are guardrails, never required to improve.
SLICE_MIN_QUALIFYING_DAYS = 8
SLICE_MIN_QUALIFYING_CELLS = 60
SLICE_MIN_FOLD_TRAIN_CELLS = 30
# Charter 248 amendment (user ruling 2026-10-03 「放宽重测」): the slice lane
# walks every available fold, not the stage's last ``ROLLING_ORIGIN_MAX_FOLDS``.
# The slice is thin -- about 27 station-days a day -- so five held-out days
# held 136 station-days and an interval half-width of 0.033 nat, three times
# the gain it had to resolve (first live run 2026-10-02: -0.0095 [-0.044,
# +0.022]); the research arm needed ~460 station-days to resolve -0.018.  The
# stage lane's five days hold 5,000+ cells and keep their cap.  None means
# every fold from ``ROLLING_ORIGIN_MIN_FOLD_TRAIN_DAYS`` on; the gate is the
# same ranked verdict and fold majority.
SLICE_ROLLING_ORIGIN_MAX_FOLDS: int | None = None
# Open-ended edge buckets have no far side; this is the width
# ``materialization`` gives them on the P0-M TAF likelihood's axis, which its
# own artifact was fitted on.  It is no longer the width layer's convention:
# until 2026-09-21 the fit here placed an open centre a whole degree past its
# bound while serving placed it half a degree past, so the scalars were
# chosen on a mean, a sigma and a transport nobody served.  Since charter 190
# the fit reads ``served_bucket_axis`` -- the function serving calls.
EDGE_WIDTH_C = 2.0
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
# served sigma (boundaries in ``center_width_calibration``).  Same promotion
# contract as the floor: ``shadow`` fits, scores and journals; ``serve`` lets
# an admitted tier table reach the activated row.  Charter 228: fitted on the
# bucket LogScore alone and admitted like everything else here.  It used to
# add a binary score of the seat rule's mean+-1 window claim to the
# objective, which is not a score of the bucket distribution (charter 211);
# the window's containment is still reported per tier, as a diagnostic.
WIDTH_TIER_MODE = (
    os.environ.get("PM_CENTER_WIDTH_TIERS", "").strip().lower() or "shadow"
)
if WIDTH_TIER_MODE not in {"shadow", "serve"}:
    raise RuntimeError(f"unknown center-width tier mode: {WIDTH_TIER_MODE}")
# Reaches past one for the same reason as the pooled grid (charter 138): a
# tier whose distributions are served too narrow has to be able to say so.
WIDTH_TIER_LAMBDA_GRID = tuple(round(0.50 + 0.025 * step, 3) for step in range(31))


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
    "  ('ACTIVATED','UNCHANGED','ACCUMULATING','REFUSED_FORWARD','NOT_WIRED',"
    "   'INCUMBENT_VETOED')),"
    " evidence_json TEXT NOT NULL"
    ")"
)
_JOURNAL_OUTCOMES = (
    "ACTIVATED", "UNCHANGED", "ACCUMULATING", "REFUSED_FORWARD", "NOT_WIRED",
    "INCUMBENT_VETOED",
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
    # A table version the lane retired by veto, as opposed to one merely
    # superseded by a better table.  Artifacts pin the version their holdout
    # was scored against and serve exactly that version; a retired version
    # is the one case where the pin must not be honoured, because the veto
    # is the settled truth saying that width was wrong.  Nullable: every row
    # written before this existed reads as never retired.
    try:
        conn.execute(
            "ALTER TABLE calibration_center_width_station"
            " ADD COLUMN retired_at_utc TEXT"
        )
    except sqlite3.OperationalError:
        pass
    # Same stamp for the pooled table (charter 138).  Deactivating stops the
    # unpinned readers; only the stamp stops the pinned ones, and without it
    # a vetoed pair keeps serving every artifact minted before the veto --
    # which on 2026-09-10 was the whole difference between retiring the
    # width tables and actually ceasing to serve them.
    try:
        conn.execute(
            "ALTER TABLE calibration_center_width_table"
            " ADD COLUMN retired_at_utc TEXT"
        )
    except sqlite3.OperationalError:
        pass
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
    # Charter 239: the thin-pool far-lead slice rides on the same row as the
    # pair it refines; nullable, and read as "no slice", so every row written
    # before it keeps describing exactly the arithmetic it described.
    try:
        conn.execute(
            "ALTER TABLE calibration_center_width_table"
            " ADD COLUMN width_slices_json TEXT"
        )
    except sqlite3.OperationalError:
        pass
    # Charter 271: the mode-to-shoulder share table rides on the same row;
    # nullable, and read as "no share", so every row written before it keeps
    # describing exactly the arithmetic it described.
    try:
        conn.execute(
            "ALTER TABLE calibration_center_width_table"
            " ADD COLUMN mode_shoulder_json TEXT"
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
        outcome for outcome in _JOURNAL_OUTCOMES if f"'{outcome}'" not in live
    ]
    if live and missing:
        # Degrade on a read-only connection rather than failing the caller.
        # Every dry run, probe and dashboard opens this store read-only and
        # calls the schema on the way in; a rebuild that raises there would
        # take all of them out between a deploy and the next writable sweep,
        # for a migration none of them needs -- they read the journal, they
        # do not insert the new outcome into it.
        try:
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
        except sqlite3.OperationalError:
            conn.rollback()
    try:
        conn.execute(_JOURNAL_INDEX)
    except sqlite3.OperationalError:
        pass
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
        try:
            conn.execute(
                "ALTER TABLE calibration_center_width_station_journal"
                " RENAME TO"
                " calibration_center_width_station_journal_superseded"
            )
            conn.execute(_STATION_JOURNAL_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO"
                " calibration_center_width_station_journal"
                " SELECT * FROM"
                " calibration_center_width_station_journal_superseded"
            )
            conn.execute(
                "DROP TABLE calibration_center_width_station_journal_superseded"
            )
            conn.commit()
        except sqlite3.OperationalError:
            conn.rollback()


def _check_stage(stage: str) -> str:
    value = str(stage)
    if value not in CENTER_WIDTH_STAGES:
        raise ContractError("center_width_stage_invalid")
    return value


def center_width_sha256(
    *, stage: str, delta_c: float, width_lambda: float,
    sigma_floor_rungs: float = 0.0,
    width_tiers: Mapping[str, Any] | None = None,
    width_slices: Mapping[str, Mapping[str, Any]] | None = None,
    mode_shoulder: Mapping[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "schema": "center_width_table_v1",
        "stage": _check_stage(stage),
        "delta_c": round(float(delta_c), 6),
        "width_lambda": round(float(width_lambda), 6),
    }
    # Conditional so a table without a floor keeps its historical digest --
    # the pinned-version contract must keep resolving rows written before
    # the floor existed -- and it is the floor that makes the
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
    # Charter 239 / 248: a row carrying a slice is a different arithmetic
    # from the same pair without one, so the slice is in the digest an
    # artifact pins.  The slice is a tail mix (248); the digest names its
    # weight and scale.
    if width_slices:
        payload["width_slices"] = {
            str(name): {
                "max_seated_systems": int(entry["max_seated_systems"]),
                "min_hours_to_settlement": round(
                    float(entry["min_hours_to_settlement"]), 6
                ),
                "tail_mix_weight": round(float(entry["tail_mix_weight"]), 6),
                "tail_mix_scale": round(float(entry["tail_mix_scale"]), 6),
            }
            for name, entry in sorted(width_slices.items())
        }
    # Charter 271: a row carrying a share is a different arithmetic from the
    # same pair without one; conditional so every older row keeps its digest.
    canonical_shoulder = _canonical_mode_shoulder(mode_shoulder)
    if canonical_shoulder:
        payload["mode_shoulder"] = canonical_shoulder
    return content_sha256(payload)


def _canonical_mode_shoulder(
    table: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Charter 271: the share table as stored and hashed, or None.

    Only bins with a positive share are kept -- a zero share is identity and
    must not make an otherwise identical row a different sha.
    """

    if not table or not isinstance(table, Mapping):
        return None
    bins = table.get("bins")
    if not isinstance(bins, Mapping):
        return None
    known = {name for name, _lower, _upper in MODE_SHOULDER_BINS}
    kept: dict[str, Any] = {}
    for name, entry in sorted(bins.items()):
        if name not in known or not isinstance(entry, Mapping):
            raise ContractError("center_width_mode_shoulder_bin_invalid")
        share = round(float(entry.get("share", 0.0)), 6)
        if not math.isfinite(share) or not (0.0 <= share <= MAX_MODE_SHOULDER_SHARE):
            raise ContractError("center_width_mode_shoulder_share_out_of_range")
        if share > 0.0:
            kept[str(name)] = {"share": share}
    if not kept:
        return None
    return {
        "schema": MODE_SHOULDER_SCHEMA,
        "warm_share_rule": MODE_SHOULDER_WARM_SHARE_RULE,
        "bins": kept,
    }


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
    as_of_utc: str | None = None,
) -> tuple[float, float, str] | None:
    """The stage's active scalars, or the pinned version an artifact names.

    A pinned sha that is missing fails closed, for the same reason the band
    table does: an artifact whose scored math cannot be reproduced must not
    be served an approximation of it.  A pinned sha the lane RETIRED is the
    one case that does not fail closed and does not serve either: the pin
    exists so serving reproduces the width the gate scored, and the veto is
    the settled truth saying that width was wrong, so it reads as identity
    with the sha kept for the receipt (charter 138, the station lane's rule
    at the pooled grain).

    ``as_of_utc`` reads that retirement at a moment: serving passes nothing
    and stops at the veto, a replay passes the decision's time.

    This is a read.  It creates nothing -- a missing table means no
    correction has ever been fitted, which is exactly the None case.  The
    serving path calls this from inside the resident's write transaction,
    and a lazy CREATE plus its commit would end that transaction early.
    """

    stage_id = _check_stage(stage)
    try:
        if table_sha256 is None:
            row = conn.execute(
                "SELECT delta_c, width_lambda, center_width_sha256, NULL"
                " FROM calibration_center_width_table"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchone()
        else:
            try:
                row = conn.execute(
                    "SELECT delta_c, width_lambda, center_width_sha256,"
                    " retired_at_utc"
                    " FROM calibration_center_width_table"
                    " WHERE center_width_sha256=?",
                    (str(table_sha256),),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "retired_at_utc" not in str(exc):
                    raise
                # A store the migration has not reached yet: the resident
                # serves without running the schema, so between a deploy and
                # the next sweep the column is absent.  That is not a missing
                # table -- read it exactly as before the column existed,
                # never retired, rather than failing closed on every artifact
                # that pins a version.
                row = conn.execute(
                    "SELECT delta_c, width_lambda, center_width_sha256, NULL"
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
    if table_sha256 is not None and row[3] and (
        as_of_utc is None or utc_instant(row[3]) <= utc_instant(as_of_utc)
    ):
        return 0.0, 1.0, str(row[2])
    return float(row[0]), float(row[1]), str(row[2])


def _pinned_center_width_retired(
    conn: sqlite3.Connection,
    *,
    table_sha256: str,
    as_of_utc: str | None = None,
) -> bool:
    """Whether the pinned version had been retired by ``as_of_utc``.

    Retirement retires the whole row's arithmetic, not just its scalars: a
    pin that reads identity for the pair while still serving that row's floor
    or tier table would serve a composition nobody ever scored.  Absent
    column (deploy before the sweep) reads as never retired, the same way
    the scalar loader reads it.
    """

    try:
        row = conn.execute(
            "SELECT retired_at_utc FROM calibration_center_width_table"
            " WHERE center_width_sha256=?",
            (str(table_sha256),),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None or not row[0]:
        return False
    return as_of_utc is None or utc_instant(row[0]) <= utc_instant(as_of_utc)


def load_center_width_floor(
    conn: sqlite3.Connection,
    *,
    stage: str,
    table_sha256: str | None = None,
    as_of_utc: str | None = None,
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
    if table_sha256 is not None and _pinned_center_width_retired(
        conn, table_sha256=table_sha256, as_of_utc=as_of_utc
    ):
        return 0.0
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
    as_of_utc: str | None = None,
) -> dict[str, Any] | None:
    """The active (or pinned) table's width tiers, if any -- None means the
    table serves one lambda, which is every row written before the tier
    lane existed and every fit whose tiers were not admitted to serve."""

    stage_id = _check_stage(stage)
    if table_sha256 is not None and _pinned_center_width_retired(
        conn, table_sha256=table_sha256, as_of_utc=as_of_utc
    ):
        return None
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


def load_center_width_slices(
    conn: sqlite3.Connection,
    *,
    stage: str,
    table_sha256: str | None = None,
    as_of_utc: str | None = None,
) -> dict[str, dict[str, Any]] | None:
    """The active (or pinned) table's width slices (charter 239), if any --
    None means the row serves no slice, which is every row written before
    the slice lane existed and every fit whose slice was not admitted."""

    stage_id = _check_stage(stage)
    if table_sha256 is not None and _pinned_center_width_retired(
        conn, table_sha256=table_sha256, as_of_utc=as_of_utc
    ):
        return None
    try:
        if table_sha256 is None:
            row = conn.execute(
                "SELECT width_slices_json"
                " FROM calibration_center_width_table"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT width_slices_json"
                " FROM calibration_center_width_table"
                " WHERE center_width_sha256=?",
                (str(table_sha256),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row[0]:
        return None
    try:
        slices = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    if not isinstance(slices, Mapping):
        return None
    out: dict[str, dict[str, Any]] = {}
    for name, entry in slices.items():
        # Charter 248: a slice is a tail mix.  An entry of the retired 239
        # form (``width_lambda``) reads as no slice; the lane's next run
        # rewrites the row.
        if not isinstance(entry, Mapping) or entry.get("tail_mix_weight") is None:
            continue
        out[str(name)] = {
            "max_seated_systems": int(
                entry.get("max_seated_systems", THIN_FAR_MAX_SEATED_SYSTEMS)
            ),
            "min_hours_to_settlement": float(
                entry.get(
                    "min_hours_to_settlement", THIN_FAR_MIN_HOURS_TO_SETTLEMENT
                )
            ),
            "tail_mix_weight": float(entry["tail_mix_weight"]),
            "tail_mix_scale": float(entry.get("tail_mix_scale", TAIL_MIX_SCALE)),
        }
    return out or None


def load_center_width_mode_shoulder(
    conn: sqlite3.Connection,
    *,
    stage: str,
    table_sha256: str | None = None,
    as_of_utc: str | None = None,
) -> dict[str, Any] | None:
    """The active (or pinned) table's mode-to-shoulder shares (charter 271),
    if any -- None means the row serves no share, which is every row written
    before the lane existed and every fit whose share was not admitted."""

    stage_id = _check_stage(stage)
    if table_sha256 is not None and _pinned_center_width_retired(
        conn, table_sha256=table_sha256, as_of_utc=as_of_utc
    ):
        return None
    try:
        if table_sha256 is None:
            row = conn.execute(
                "SELECT mode_shoulder_json"
                " FROM calibration_center_width_table"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT mode_shoulder_json"
                " FROM calibration_center_width_table"
                " WHERE center_width_sha256=?",
                (str(table_sha256),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or not row[0]:
        return None
    try:
        table = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    try:
        return _canonical_mode_shoulder(table)
    except ContractError:
        return None


STATION_WIDTH_METHOD_TAG = "station_width_scale_v2"
# Three rules decide what this layer is allowed to say, and all three were
# wrong in v1 (measured 2026-09-11, walk-forward on the store's own cells
# and the serving kernel; v1 scored -0.078 nat/cell on P0, -0.124 on P0-M,
# -0.036 on P1 -- a layer that was losing on every stage it served).
#
# 1. THE SAMPLE UNIT IS THE STATION-DAY.  A station-day emits about nine
#    cells (one per three-hour block off the same evidence), and v1 counted
#    cells: seven station-days read as sixty-odd samples, weight 0.93, and
#    the shrinkage it was named for never happened.  That is how the
#    2026-09-06 table pinned thirty-two stations to the 0.5 floor on what
#    its own evidence recorded as sample_n=62 and was really seven days.
#    Counting station-days instead, an average fit clips one station rather
#    than eleven.
#
# 2. THE TARGET IS THE FLEET, NOT ONE.  Shrinking toward 1.0 asks each
#    station "are you wider than calibrated", and in a fleet that is wide
#    everywhere the answer is yes everywhere -- so this layer swallowed the
#    pooled miscalibration that the pooled lambda lane exists to correct,
#    and the two composed: 0.75 x 0.5 = 0.375 served, sd(z) 1.40 and 1.85 on
#    2026-09-08/09.  Shrinking toward the fleet's own median sd(z) leaves
#    this layer carrying only "how much wider than the fleet is this
#    station", which is the only question a per-station layer may answer.
#    A fleet that is uniformly wide now yields all-identity here and the
#    pooled lane corrects it once.
#
# 3. ONE DAY MUST NOT SET A STATION'S WIDTH.  KLAX 2026-08-23 served a
#    point mass (sigma 0.09C, truth 3.2C away): one cell at z=-35 lifted the
#    station's raw z-sd to 6.2 and would have pinned it to the ceiling for
#    the life of the table.  One such cell in 945 station-days, so the fix
#    is a clip, not a rule about degenerate distributions.
STATION_WIDTH_SHRINK_KAPPA_STATION_DAYS = 10.0
# Reported beside the fitted value, never gating: the lane's own answer to
# "what would a stronger or weaker prior have served", on the same forward
# window, so the next ruling reads evidence instead of a swept memory.
STATION_WIDTH_KAPPA_PROBES = (5.0, 20.0)
STATION_WIDTH_Z_CLIP = 4.0
STATION_WIDTH_MIN_STATION_DAYS = 3
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
# undone.  Undoing better under the ranked verdict (charter 240)
# retires the table.  The window opens at three days / sixty cells, half the
# qualifying floor: the question "is this table hurting" needs less evidence
# than "which table is best", and a day earlier here was one fewer day
# served too narrow.
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
    as_of_utc: str | None = None,
) -> tuple[dict[str, float], str] | None:
    """The stage's active per-station width scales, or a pinned version.

    Same contract as ``load_center_width``: a pinned sha that is missing
    fails closed, an unpinned read of a layer that has fitted nothing is the
    ordinary None case, and nothing is created on the read path.

    ``as_of_utc`` reads a pinned version's retirement at a moment, the same
    parameter the band loader takes (review 2026-09-13, R6): serving passes
    nothing and stops at the veto, a replay passes the decision's time.
    """

    stage_id = _check_stage(stage)
    try:
        if table_sha256 is None:
            rows = conn.execute(
                "SELECT station_id, width_scale, station_width_sha256, NULL"
                " FROM calibration_center_width_station"
                " WHERE activated=1 AND stage=?",
                (stage_id,),
            ).fetchall()
        else:
            try:
                rows = conn.execute(
                    "SELECT station_id, width_scale, station_width_sha256,"
                    " retired_at_utc"
                    " FROM calibration_center_width_station"
                    " WHERE station_width_sha256=?",
                    (str(table_sha256),),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "retired_at_utc" not in str(exc):
                    raise
                # A store the migration has not reached yet: the resident
                # serves without running the schema, so between a deploy
                # and the next sweep the column is absent.  That is not a
                # missing table -- read it exactly as before the column
                # existed, never retired, rather than failing closed on
                # every artifact that pins a version.
                rows = [
                    (*row, None) for row in conn.execute(
                        "SELECT station_id, width_scale, station_width_sha256"
                        " FROM calibration_center_width_station"
                        " WHERE station_width_sha256=?",
                        (str(table_sha256),),
                    ).fetchall()
                ]
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
    if table_sha256 is not None and all(
        row[3] and (
            as_of_utc is None
            or utc_instant(row[3]) <= utc_instant(as_of_utc)
        )
        for row in rows
    ):
        # Pinned to a version the lane retired by veto.  The pin exists so
        # that serving reproduces the width the gate scored; the veto is
        # the settled truth saying that width was wrong, and every artifact
        # minted before the retirement pins it -- honouring the pin would
        # keep a retired table in service for as long as the oldest artifact
        # lives.  Identity, with the sha kept so the receipt can say why.
        return ({}, str(rows[0][2]))
    return (
        {str(row[0]): float(row[1]) for row in rows},
        str(rows[0][2]),
    )


def _active_center_width_activated_at(
    conn: sqlite3.Connection, *, stage: str
) -> str | None:
    try:
        row = conn.execute(
            "SELECT MAX(activated_at_utc) FROM calibration_center_width_table"
            " WHERE activated=1 AND stage=?",
            (stage,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None or row[0] is None else str(row[0])


# How far the recovered input may fail to reproduce the row that was served
# before the recovery is called a description of something else and the cell
# is not judged (total variation on the bucket vector).
MAX_RECOVERY_RESIDUAL = 0.01
# A forward map this ill-conditioned has lost the input rather than moved it.
MAX_RECOVERY_CONDITION = 1.0e6


def _reserved_under_current_width(
    receipt: Mapping[str, Any] | None,
    *,
    keys: Sequence[str],
    centers: Sequence[float],
    open_edge_bounds: Any,
    excluded: Sequence[str],
) -> dict[str, float] | None:
    """An old sample as today's width function would serve it (charter 202).

    User ruling 2026-09-23: 「证据时钟不要清零，用旧的样本」.  A lane that
    reads a row served under an older width function would otherwise score
    that function, not today's: the first dry refit after the ruling vetoed
    the incumbent pair on 2026-09-19/20 alone (+0.136 and +0.122 nat per
    cell), rows served before charter 190 in which the transport had emptied
    the open cold bucket the settled high then landed in -- on the two days
    served under the tail rule the pair won (-0.058, -0.082).  So a row that
    recorded the width layer's input is re-served here from that input, with
    the pair it was served with, through today's function and today's axis.
    A row served by today's code comes back as it was served.  A row with no
    recorded input, an unapplied layer or a tier lambda keeps its served
    vector: there is nothing exact to re-serve it from.
    """

    if not isinstance(receipt, Mapping):
        return None
    state = str(receipt.get("state") or "")
    # An identity pair has nothing exact to re-serve (charter 202) -- unless
    # the row was then mixed by the tail slice (charter 248), in which case
    # the recorded input under the identity pair is exactly the mix's input.
    tail_mixed = False
    try:
        tail_mixed = float(receipt.get("width_slice_tail_weight") or 0.0) > 0.0
    except (TypeError, ValueError):
        tail_mixed = False
    # Charter 253: likewise a row the far-peak ceiling then capped -- the
    # recorded input under the identity pair is exactly the ceiling's input.
    capped = receipt.get("peak_ceiling") is not None
    # Charter 271: likewise a row the mode-to-shoulder share then moved.
    shouldered = receipt.get("mode_shoulder_share") is not None
    if state != "APPLIED" and not (
        state == "IDENTITY" and (tail_mixed or capped or shouldered)
    ):
        return None
    if receipt.get("width_tier_lambda") is not None:
        return None
    recorded = receipt.get("input_probability")
    if not isinstance(recorded, Mapping) or set(recorded) != set(keys):
        return None
    applied_lambda = float(receipt.get("width_lambda") or 1.0)
    # Charter 248: a row served under the retired 239 slice (a lambda that
    # replaced the stage's) is re-served under the stage lambda it stood in
    # for, so the lane fits on the vector every slice cell is served now.  A
    # row served under the tail mix re-serves to the mix's input: the
    # recorded input under the recorded pair is exactly that.
    legacy_slice = receipt.get("width_slice_lambda")
    stage_lambda = receipt.get("stage_width_lambda")
    if legacy_slice is not None and stage_lambda is not None:
        try:
            if float(legacy_slice) > 0.0:
                applied_lambda = applied_lambda * float(stage_lambda) / float(legacy_slice)
        except (TypeError, ValueError):
            return None
        if not MIN_WIDTH_LAMBDA <= applied_lambda <= MAX_WIDTH_LAMBDA:
            return None
    try:
        served, _ = apply_center_width_calibration(
            {str(key): float(value) for key, value in recorded.items()},
            delta_c=float(receipt.get("delta_c") or 0.0),
            width_lambda=applied_lambda,
            ordered_keys=list(keys),
            key_centers_c=list(centers),
            excluded_buckets=tuple(excluded),
            open_edge_bounds_c=open_edge_bounds,
            sigma_floor_rungs=float(receipt.get("sigma_floor_rungs") or 0.0),
        )
    except (ContractError, TypeError, ValueError, KeyError):
        return None
    total = sum(served.values())
    if total <= 0.0:
        return None
    return {str(key): float(value) / total for key, value in served.items()}


def _receipt_of(lineage: Mapping[str, Any]) -> dict[str, Any] | None:
    """The row's own centre/width receipt, or None if it carries none."""

    raw = lineage.get("center_width_json")
    if not raw:
        return None
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _center_width_forward_matrix(
    centers: Sequence[float], *, mean_c: float, lam: float, delta: float
):
    """The kernel's move written as a matrix: column j is where input bucket
    j's mass lands.  Same clipping and the same linear split, so it is the
    transform itself rather than a model of it.

    The PRE-charter-190 transform, deliberately: this solves for the input
    of rows that recorded none, and every such row predates charter 138's
    receipt key and so was served with open edge buckets moved as points.
    A row served under the tail rule always records its input (the
    recovery below refuses one that does not), so this matrix is never
    asked to describe it.  (User ruling 2026-09-21, charter 190.)"""

    import numpy as np

    size = len(centers)
    matrix = np.zeros((size, size))
    for column, origin in enumerate(centers):
        position = mean_c + (float(origin) - mean_c) * lam + delta
        if position <= centers[0]:
            matrix[0, column] = 1.0
            continue
        if position >= centers[-1]:
            matrix[size - 1, column] = 1.0
            continue
        for row in range(size - 1):
            lower, upper = float(centers[row]), float(centers[row + 1])
            if lower <= position <= upper:
                span = upper - lower
                share = (position - lower) / span if span > 0 else 0.0
                matrix[row, column] += 1.0 - share
                matrix[row + 1, column] += share
                break
    return matrix


def _nnls(matrix, target, *, tol: float = 1e-10):
    """Lawson-Hanson non-negative least squares.

    Carried here rather than imported: numpy is already a dependency of this
    package and scipy is not, and the problem is eleven by eleven.
    """

    import numpy as np

    columns = matrix.shape[1]
    chosen = np.zeros(columns, dtype=bool)
    solution = np.zeros(columns)
    gradient = matrix.T @ (target - matrix @ solution)
    budget = 3 * columns
    outer = 0
    while (~chosen).any() and gradient[~chosen].max() > tol:
        outer += 1
        if outer > budget:
            break
        chosen[int(np.argmax(np.where(chosen, -np.inf, gradient)))] = True
        trial = np.zeros(columns)
        trial[chosen] = np.linalg.lstsq(
            matrix[:, chosen], target, rcond=None
        )[0]
        inner = 0
        while chosen.any() and trial[chosen].min() <= tol:
            inner += 1
            if inner > budget:
                break
            blocking = chosen & (trial <= tol)
            denominator = solution[blocking] - trial[blocking]
            with np.errstate(divide="ignore", invalid="ignore"):
                ratios = np.where(
                    denominator != 0.0, solution[blocking] / denominator, 0.0
                )
            step = float(np.min(ratios)) if ratios.size else 0.0
            solution = solution + step * (trial - solution)
            chosen &= np.abs(solution) > tol
            trial = np.zeros(columns)
            if chosen.any():
                trial[chosen] = np.linalg.lstsq(
                    matrix[:, chosen], target, rcond=None
                )[0]
        solution = np.where(chosen, trial, 0.0)
        gradient = matrix.T @ (target - matrix @ solution)
    return np.clip(solution, 0.0, None)


def _identity_counterfactual(
    cell: Mapping[str, Any], *, delta_c: float, width_lambda: float
) -> tuple[dict[str, float] | None, str]:
    """What this row would have been served with the pooled pair left off.

    NOT the pair run backwards.  The move is a linear map on the bucket
    vector and running it a second time is not its inverse: lambda and then
    1/lambda smears mass across neighbours every pass, so an undone arm comes
    out both wider than the input ever was and blurrier than it ever was --
    measured 2026-09-16 over the live window at 14% too wide for P0 and 23%
    for P0-M, plus 0.045 to 0.063 nat of pure rebinning, every bit of it
    pointing the same way, towards keeping the incumbent.

    So: take the input the row records, or solve the kernel's own map for it,
    and re-apply whatever the pooled pair was NOT (the station scale, the
    floor lift, a regime shift).  A row that cannot answer is not judged, and
    says which question it could not answer -- a contraction is
    rank-deficient, and at lambda 0.75 on an eleven-bucket axis the map has
    rank nine, so for those rows nothing short of the recorded input will do.
    """

    import numpy as np

    receipt = cell.get("center_width_receipt")
    if not receipt:
        return None, "no_receipt"
    if str(receipt.get("state") or "") not in ("APPLIED", "IDENTITY"):
        # Served untouched (the hard bound emptied the transform): this row
        # says nothing about whether the pair earns its place.
        return None, "not_applied"
    if receipt.get("width_tier_lambda") is not None:
        # A tier lambda replaces the pooled one rather than composing with
        # it, so "the pooled pair left off" is not a factor of what was
        # applied.  No tier table has ever been served; this fails closed
        # the day one is.
        return None, "tiered"
    keys = list(cell["ordered_keys"])
    applied_lambda = float(receipt.get("width_lambda") or 1.0)
    applied_delta = float(receipt.get("delta_c") or 0.0)
    if applied_lambda <= 0.0:
        return None, "no_receipt"
    recorded = receipt.get("input_probability")
    if isinstance(recorded, Mapping) and set(recorded) == set(keys):
        recovered = np.array([float(recorded[key]) for key in keys])
        basis = "recorded_input"
    elif receipt.get("open_edge_rule"):
        # Served under charter 190's tail rule, which the recovery matrix
        # does not describe.  Every such receipt records its input, so this
        # is a malformed row, not one to solve for.
        return None, "not_recoverable"
    else:
        mean_c = receipt.get("distribution_mean_c")
        if mean_c is None:
            return None, "no_receipt"
        centers = [float(value) for value in cell["key_centers_c"]]
        matrix = _center_width_forward_matrix(
            centers, mean_c=float(mean_c),
            lam=applied_lambda, delta=applied_delta,
        )
        if (
            np.linalg.matrix_rank(matrix, tol=1e-9) < len(centers)
            or not np.isfinite(np.linalg.cond(matrix))
            or np.linalg.cond(matrix) > MAX_RECOVERY_CONDITION
        ):
            return None, "not_recoverable"
        served = np.array([
            float(cell["probabilities"][key]) for key in keys
        ])
        total_served = served.sum()
        if total_served <= 0.0:
            return None, "no_receipt"
        served = served / total_served
        recovered = _nnls(matrix, served)
        if recovered.sum() <= 0.0:
            return None, "not_recoverable"
        recovered = recovered / recovered.sum()
        if float(np.abs(matrix @ recovered - served).sum()) > (
            MAX_RECOVERY_RESIDUAL
        ):
            return None, "residual"
        settled = keys.index(cell["settled_bucket"])
        if recovered[settled] < 1e-4 <= served[settled]:
            # The solve drove the settled bucket to nothing while the row
            # served it real mass.  -log of that is 13.8 nat and would set
            # the mean by itself; it is an artefact of an ill-posed solve,
            # not a reading.
            return None, "degenerate"
        basis = "recovered"
    probability = {key: float(value) for key, value in zip(keys, recovered)}
    # Whatever the pooled pair was not.  Identity in both factors leaves the
    # recovered input untouched -- the kernel's identity lands every bucket
    # exactly on its own centre, so it costs no rebinning either.
    residual_lambda = applied_lambda / float(width_lambda or 1.0)
    residual_delta = applied_delta - float(delta_c)
    if abs(residual_lambda - 1.0) > 1e-9 or abs(residual_delta) > 1e-9:
        if not (
            MIN_WIDTH_LAMBDA <= residual_lambda <= MAX_WIDTH_LAMBDA
            and abs(residual_delta) <= MAX_ABS_SHIFT_C
        ):
            return None, "residual"
        probability, _ = apply_center_width_calibration(
            probability,
            delta_c=residual_delta,
            width_lambda=residual_lambda,
            ordered_keys=keys,
            key_centers_c=cell["key_centers_c"],
            excluded_buckets=cell["excluded_buckets"],
            open_edge_bounds_c=cell.get("open_edge_bounds_c"),
        )
    total = sum(probability.values())
    if total <= 0.0:
        return None, "residual"
    return {key: value / total for key, value in probability.items()}, basis


def _center_width_veto(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    delta_c: float,
    width_lambda: float,
    activated_at_utc: str | None,
) -> dict[str, Any]:
    """Score the active pair on the cells served after it went live: kept
    (served as-is) against the same rows with the pair left off.  ``vetoed``
    when leaving it off is better under the ranked verdict
    (charter 240) on at least the veto window (charter 138).

    Rows that cannot say what identity would have served are not judged and
    are counted by reason.  Declining is the safe direction here -- it holds
    the incumbent -- and it is the honest one: a reconstruction of an input
    the transform destroyed is a guess wearing a number's clothes.
    """

    since = activated_at_utc or ""
    kept_scores: list[tuple[float, float]] = []
    undone_scores: list[tuple[float, float]] = []
    clusters: list[tuple[Any, str]] = []
    per_day: dict[str, dict[str, float]] = {}
    skipped: dict[str, int] = {}
    bases: dict[str, int] = {}
    for day in sorted(by_day):
        day_kept: list[tuple[float, float]] = []
        day_undone: list[tuple[float, float]] = []
        for cell in by_day[day]:
            if str(cell.get("decision_at_utc") or "") < since:
                continue
            counterfactual, basis = _identity_counterfactual(
                cell, delta_c=delta_c, width_lambda=width_lambda
            )
            if counterfactual is None:
                skipped[basis] = skipped.get(basis, 0) + 1
                continue
            bases[basis] = bases.get(basis, 0) + 1
            # The served pair is already inside the cell, so serving it as-is
            # is the identity residual -- applying the pair again here would
            # score the incumbent against its own scalars squared.
            day_kept.append(
                _cell_proper_scores(cell, delta_c=0.0, width_lambda=1.0)
            )
            day_undone.append(_served_scores(
                counterfactual,
                ordered_keys=cell["ordered_keys"],
                settled_bucket=cell["settled_bucket"],
                excluded_buckets=cell["excluded_buckets"],
                key_centers_c=cell["key_centers_c"],
            ))
            clusters.append((cell.get("station_id"), day))
        if day_kept:
            per_day[day] = {
                "cells": len(day_kept),
                "kept_minus_undone": (
                    sum(s[0] for s in day_kept) - sum(s[0] for s in day_undone)
                ) / len(day_kept),
                "kept_minus_undone_brier": (
                    sum(s[1] for s in day_kept) - sum(s[1] for s in day_undone)
                ) / len(day_kept),
            }
            kept_scores.extend(day_kept)
            undone_scores.extend(day_undone)
    count = len(kept_scores)
    enough = (
        len(per_day) >= CENTER_WIDTH_VETO_MIN_DAYS
        and count >= CENTER_WIDTH_VETO_MIN_CELLS
    )
    gate = _proper_score_gate(kept_scores, undone_scores, clusters)

    def _mean(scores: Sequence[tuple[float, float]], index: int) -> float | None:
        return None if not scores else sum(s[index] for s in scores) / len(scores)

    return {
        "schema": "center_width_incumbent_veto_v2",
        "activated_at_utc": activated_at_utc,
        "incumbent_delta_c": float(delta_c),
        "incumbent_width_lambda": float(width_lambda),
        "day_count": len(per_day),
        "cell_count": count,
        # Never a silent cap: a window that mostly could not be judged must
        # not read as a window that was judged and held.
        "counterfactual_basis": bases,
        "skipped": skipped,
        "skipped_count": sum(skipped.values()),
        "nll_kept": _mean(kept_scores, 0),
        "nll_undone": _mean(undone_scores, 0),
        "brier_kept": _mean(kept_scores, 1),
        "brier_undone": _mean(undone_scores, 1),
        # What undoing buys, per cell: positive means the pair is costing.
        "undo_gain": 0.0 if count == 0 else -float(gate["d_logscore"]),
        "undo_brier_gain": 0.0 if count == 0 else -float(gate["d_brier"]),
        "gate": gate,
        "min_days": CENTER_WIDTH_VETO_MIN_DAYS,
        "min_cells": CENTER_WIDTH_VETO_MIN_CELLS,
        "enough_evidence": enough,
        "vetoed": bool(enough and gate["lower"]),
        "per_day": per_day,
    }


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


def _undo_station_scale_scores(
    cell: Mapping[str, Any], *, scale: float
) -> tuple[float, float]:
    """The cell's (LogScore, bucket Brier) with the station contraction it
    was served under widened back out, as far as the kernel's own lambda
    bound allows.

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
    return _served_scores(
        adjusted,
        ordered_keys=cell["ordered_keys"],
        settled_bucket=cell["settled_bucket"],
        excluded_buckets=cell["excluded_buckets"],
        key_centers_c=cell["key_centers_c"],
    )


def _station_width_veto(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    scales: Mapping[str, float],
    activated_at_utc: str | None,
) -> dict[str, Any]:
    """Score the active table on the cells served after it went live: kept
    (served as-is) against undone.  ``vetoed`` when undoing is
    better under the ranked verdict (charter 240) on at least
    the veto window."""

    since = activated_at_utc or ""
    kept_scores: list[tuple[float, float]] = []
    undone_scores: list[tuple[float, float]] = []
    clusters: list[tuple[Any, str]] = []
    per_day: dict[str, dict[str, float]] = {}
    for day in sorted(by_day):
        day_kept: list[tuple[float, float]] = []
        day_undone: list[tuple[float, float]] = []
        for cell in by_day[day]:
            if str(cell.get("decision_at_utc") or "") < since:
                continue
            scale = float(scales.get(cell["station_id"], 1.0))
            if scale == 1.0:
                continue
            day_kept.append(
                _cell_proper_scores(cell, delta_c=0.0, width_lambda=1.0)
            )
            day_undone.append(_undo_station_scale_scores(cell, scale=scale))
            clusters.append((cell.get("station_id"), day))
        if day_kept:
            per_day[day] = {
                "cells": len(day_kept),
                "kept_minus_undone": (
                    sum(s[0] for s in day_kept) - sum(s[0] for s in day_undone)
                ) / len(day_kept),
            }
            kept_scores.extend(day_kept)
            undone_scores.extend(day_undone)
    count = len(kept_scores)
    enough = (
        len(per_day) >= STATION_WIDTH_VETO_MIN_DAYS
        and count >= STATION_WIDTH_VETO_MIN_CELLS
    )
    gate = _proper_score_gate(kept_scores, undone_scores, clusters)
    return {
        "activated_at_utc": activated_at_utc,
        "day_count": len(per_day),
        "cell_count": count,
        "nll_kept": (
            None if count == 0 else sum(s[0] for s in kept_scores) / count
        ),
        "nll_undone": (
            None if count == 0 else sum(s[0] for s in undone_scores) / count
        ),
        "undo_gain": 0.0 if count == 0 else -float(gate["d_logscore"]),
        "undo_brier_gain": 0.0 if count == 0 else -float(gate["d_brier"]),
        "gate": gate,
        "enough_evidence": enough,
        "vetoed": bool(enough and gate["lower"]),
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
        "schema": "station_width_evidence_v2",
        "stage": stage_id,
        "serving_day_count": len(days),
        "serving_cell_count": len(cells),
        "shrink_kappa_station_days": (
            STATION_WIDTH_SHRINK_KAPPA_STATION_DAYS
        ),
        "z_clip": STATION_WIDTH_Z_CLIP,
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
    station_days_by_station: dict[str, set[str]] = {}
    clipped_cells = 0
    for day in train_days:
        for cell in by_day[day]:
            actual = truth.get((cell["station_id"], cell["event_date"]))
            if actual is None:
                continue
            z = _station_cell_z(cell, actual)
            if z is None:
                continue
            if abs(z) > STATION_WIDTH_Z_CLIP:
                z = math.copysign(STATION_WIDTH_Z_CLIP, z)
                clipped_cells += 1
            z_by_station.setdefault(cell["station_id"], []).append(z)
            station_days_by_station.setdefault(
                cell["station_id"], set()
            ).add(str(cell["event_date"]))
    raw_by_station: dict[str, Any] = {}
    for station, zs in z_by_station.items():
        station_days = len(station_days_by_station.get(station, ()))
        if station_days < STATION_WIDTH_MIN_STATION_DAYS:
            continue
        mean = sum(zs) / len(zs)
        raw = math.sqrt(sum((z - mean) ** 2 for z in zs) / len(zs))
        raw_by_station[station] = {
            "raw_z_sd": raw,
            "station_day_n": station_days,
            "cell_n": len(zs),
        }
    # The fleet's own width, as the median of the per-station raw sd(z).
    # Median rather than the pooled sd: the pooled number is dominated by
    # whichever stations are widest, which is precisely the quantity this
    # layer must not inherit.  With no measurable fleet the target is 1.0,
    # which reproduces the v1 question for a lane that has nothing to
    # compare against.
    fleet_sd = (
        statistics.median(
            entry["raw_z_sd"] for entry in raw_by_station.values()
        )
        if raw_by_station
        else 1.0
    )

    def _scales_at(kappa: float) -> dict[str, float]:
        """Each station's width relative to the fleet, shrunk by station-days.

        The deviation is carried in log space so that "twice the fleet" and
        "half the fleet" shrink symmetrically, and a station that matches the
        fleet returns exactly 1.0 at every kappa.
        """

        out: dict[str, float] = {}
        if fleet_sd <= 0.0:
            return out
        for station, entry in raw_by_station.items():
            station_days = float(entry["station_day_n"])
            weight = station_days / (station_days + float(kappa))
            deviation = math.log(
                max(float(entry["raw_z_sd"]), 1e-6) / fleet_sd
            )
            out[station] = min(
                MAX_STATION_WIDTH_SCALE,
                max(
                    MIN_STATION_WIDTH_SCALE,
                    math.exp(weight * deviation),
                ),
            )
        return out

    residual = _scales_at(STATION_WIDTH_SHRINK_KAPPA_STATION_DAYS)
    for station, entry in raw_by_station.items():
        entry["fleet_relative"] = (
            float(entry["raw_z_sd"]) / fleet_sd if fleet_sd > 0 else None
        )
        entry["residual_scale"] = residual.get(station)
    forward = [cell for day in forward_days for cell in by_day[day]]
    forward_clusters = [
        (cell.get("station_id"), day)
        for day in forward_days for cell in by_day[day]
    ]

    def _forward_scores(
        scales: Mapping[str, float],
    ) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for cell in forward:
            scale = float(scales.get(cell["station_id"], 1.0))
            if scale == 1.0:
                out.append(
                    _cell_proper_scores(cell, delta_c=0.0, width_lambda=1.0)
                )
                continue
            adjusted, _ = apply_center_width_calibration(
                cell["probabilities"],
                delta_c=0.0,
                width_lambda=1.0,
                ordered_keys=cell["ordered_keys"],
                key_centers_c=cell["key_centers_c"],
                excluded_buckets=cell["excluded_buckets"],
                open_edge_bounds_c=cell.get("open_edge_bounds_c"),
                station_width_scale=scale,
            )
            out.append(_served_scores(
                adjusted,
                ordered_keys=cell["ordered_keys"],
                settled_bucket=cell["settled_bucket"],
                excluded_buckets=cell["excluded_buckets"],
                key_centers_c=cell["key_centers_c"],
            ))
        return out

    def _forward_nll(scales: Mapping[str, float]) -> float:
        scores = _forward_scores(scales)
        return sum(s[0] for s in scores) / len(scores) if scores else 0.0

    base_scores = _forward_scores({})
    residual_scores = _forward_scores(residual)
    gate = _proper_score_gate(base_scores, residual_scores, forward_clusters)
    forward_base = (
        sum(s[0] for s in base_scores) / len(base_scores) if base_scores else 0.0
    )
    forward_candidate = (
        sum(s[0] for s in residual_scores) / len(residual_scores)
        if residual_scores else 0.0
    )
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
    # What a stronger or weaker prior would have served on this same forward
    # window.  Reported, never gating: the fitted kappa is the one the ruling
    # named, and a lane that silently picked the best probe would be choosing
    # its own hyper-parameter on the window it is judged by.
    kappa_probes = {
        str(kappa): _forward_nll({}) - _forward_nll(_scales_at(kappa))
        for kappa in STATION_WIDTH_KAPPA_PROBES
    }
    evidence.update({
        "basis": "FORWARD_WINDOW",
        "train_day_count": len(train_days),
        "train_station_day_count": sum(
            len(entry) for entry in station_days_by_station.values()
        ),
        "forward_day_count": len(forward_days),
        "forward_cell_count": len(forward),
        "forward_nll_identity": forward_base,
        "forward_nll_residual": forward_candidate,
        "forward_nll_gain": gain,
        "gate_rule": GATE_RULE,
        "gate": gate,
        "residual_station_count": len(residual),
        "fleet_median_z_sd": fleet_sd,
        "clipped_cell_count": clipped_cells,
        "kappa_probe_gain": kappa_probes,
        "per_station": raw_by_station,
    })
    admitted = bool(gate["lower"])
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
        if scales:
            conn.execute(
                "UPDATE calibration_center_width_station SET activated=0"
                " WHERE stage=? AND activated=1",
                (stage_id,),
            )
        else:
            # Retired, not superseded: the pinned reads serve identity from
            # here on.  Every earlier version of the stage goes with it --
            # the veto is a verdict on the contraction as served, and an
            # older version still pinned by a live artifact (the 09:32
            # fit of 2026-09-06 under the 15:48 one that was vetoed) carried
            # the same contraction from the same evidence.
            conn.execute(
                "UPDATE calibration_center_width_station"
                " SET activated=0, retired_at_utc=?"
                " WHERE stage=? AND retired_at_utc IS NULL",
                (now, stage_id),
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
    # Charter 260: a P0-M decision taken at or after the station-day's first
    # P1 is not a belief anyone serves (P1 stands in for it), so it is not
    # evidence about the P0-M distribution either -- on the served record
    # those rows ran 0.57C warm with a dead upside and would pull the stage's
    # pair toward a correction no live P0-M reader needs.
    from .stage_authority import first_p1_decisions, p0m_superseded  # noqa: PLC0415

    first_p1 = (
        first_p1_decisions(conn, min_decision_at_utc=min_decision_at_utc)
        if stage_id == "P0-M" else {}
    )
    candidates: dict[tuple[str, str, int], list[tuple[float, tuple]]] = {}
    for (
        station, day, decided, tz_name, checkpoint, sha, stored
    ) in conn.execute(query, parameters):
        actual = truth.get((str(station), str(day)))
        if actual is None or not tz_name:
            continue
        if first_p1 and p0m_superseded(stage_id, str(station), str(day), str(decided), first_p1):
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
                 str(sha), str(stored) if stored else None, float(actual),
                 str(tz_name)),
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
    from .serving_payload_cache import record_pass  # noqa: PLC0415

    record_pass(
        "center_width.serving_cells_by_day", stage=stage_id, pre_band=pre_band,
        min_decision_at_utc=min_decision_at_utc, conn=conn,
    )
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

    # Charter 238 (「现在做」): a frozen day's payload comes from the cache,
    # so the envelopes behind this lane's evidence can leave the SSOT.
    from .serving_payload_cache import cached_history_envelope_payload

    station, day, decided, checkpoint, sha, stored, actual = row[:7]
    tz_name = str(row[7]) if len(row) > 7 and row[7] else None
    try:
        envelope = cached_history_envelope_payload(
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
        # Charter 190: the axis serving hands the layer -- key centres and
        # the open ends -- from the one function both call.
        centers, open_edge_bounds = served_bucket_axis(intervals, keys)
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
    receipt = _receipt_of(lineage)
    reserved = False
    if pre_band:
        today = _reserved_under_current_width(
            receipt, keys=keys, centers=centers,
            open_edge_bounds=open_edge_bounds, excluded=excluded,
        )
        if today is not None:
            probabilities, reserved = today, True
    # Charter 239: which slice the decision was in, read off the row itself
    # -- the seated count from the weights the pool served, the horizon from
    # the decision instant on the station's own settlement clock.  Absent
    # where the row cannot say; the slice predicate then fails closed.
    seated_system_count: int | None = None
    try:
        served_weights = json.loads(
            str(lineage.get("system_weights_json") or "")
        )
        if isinstance(served_weights, Mapping):
            seated_system_count = sum(
                1 for value in served_weights.values() if float(value) > 0.0
            )
    except (TypeError, ValueError, json.JSONDecodeError):
        seated_system_count = None
    hours_to_settlement = hours_to_settlement_if_known(
        str(day), as_of_utc=str(decided), settlement_timezone=tz_name,
    )
    return {
        "station_id": station,
        "event_date": day,
        "decision_at_utc": decided,
        "seated_system_count": seated_system_count,
        "hours_to_settlement": hours_to_settlement,
        "model_center_c": lineage.get("model_center_c"),
        # What this row actually had applied, from the row itself: the veto
        # scores the width this decision was served under, not the width the
        # stage is serving now (charter 138).
        "center_width_receipt": receipt,
        # Charter 202: the old sample re-served under today's width function
        # (see ``_reserved_under_current_width``).
        "reserved_under_current_width": reserved,
        # What the served row itself says the forecaster's TX was -- the TAF
        # cell a day belongs to is read from here, not re-derived.
        "taf_observed_tx_c": lineage.get("taf_observed_tx_c"),
        "probabilities": {
            str(key): float(value) for key, value in probabilities.items()
        },
        "ordered_keys": keys,
        "key_centers_c": centers,
        "open_edge_bounds_c": open_edge_bounds,
        "settled_bucket": landed,
        "excluded_buckets": excluded,
    }


def _served_scores(
    probabilities: Mapping[str, float],
    *,
    ordered_keys: Sequence[str],
    settled_bucket: str,
    excluded_buckets: Sequence[str] = (),
    key_centers_c: Sequence[float] | None = None,
) -> tuple[float, float, float]:
    """(LogScore, bucket Brier, ladder CRPS) of one vector as serving would
    serve it.

    The CRPS (charter 240's structural score) is the ranked probability score
    on the ladder weighted by the distance between adjacent key centres --
    the same arithmetic the OOS gate records (``native_ensemble_oos``).
    Without centres it is NaN, which no gate passes.

    Charter 228: normalised on the cell's own buckets, then the served zero
    floor (``materialization.floor_probabilities``, charter 224) -- called,
    not restated, as that function asks of every fitter -- then scored.
    """

    # Imported here: materialization imports this module at load.
    from .materialization import floor_probabilities  # noqa: PLC0415

    keys = list(ordered_keys)
    values = [float(probabilities.get(key, 0.0)) for key in keys]
    total = sum(values)
    floored, _lifted = floor_probabilities(
        {
            key: (value / total if total > 0.0 else 0.0)
            for key, value in zip(keys, values)
        },
        tuple(excluded_buckets),
    )
    brier = sum(
        (float(floored[key]) - (1.0 if key == settled_bucket else 0.0)) ** 2
        for key in keys
    )
    crps = float("nan")
    if key_centers_c is not None and len(key_centers_c) == len(keys):
        hit = keys.index(settled_bucket)
        cumulative = 0.0
        crps = 0.0
        for index in range(len(keys) - 1):
            cumulative += float(floored[keys[index]])
            observed = 1.0 if index >= hit else 0.0
            crps += (cumulative - observed) ** 2 * (
                float(key_centers_c[index + 1]) - float(key_centers_c[index])
            )
    return -math.log(max(float(floored[settled_bucket]), 1e-12)), brier, crps


def _served_row_logscore(
    row: Sequence[float], *, settled: int, excluded: Sequence[bool], floor: float
) -> float:
    """``_served_scores``'s LogScore over one grid row, no dictionary per pair.

    The same operations in the same order -- normalise, lift every
    non-excluded rung below the floor, divide by the lifted sum -- so every
    value equals the scalar path's exactly; the grid tests assert it with
    ``==``.  The searches only ever read the LogScore, the one training rule,
    so the Brier is left to the scalar path the gates read.
    """

    total = sum(row)
    values = [value / total for value in row] if total > 0.0 else [0.0] * len(row)
    if any(
        value < floor for value, out in zip(values, excluded) if not out
    ):
        lifted = [
            value if out else max(value, floor)
            for value, out in zip(values, excluded)
        ]
        return -math.log(max(lifted[settled] / sum(lifted), 1e-12))
    return -math.log(max(values[settled], 1e-12))


def _adjusted_cell(
    cell: Mapping[str, Any],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
    width_tier_lambdas: Sequence[float] | None = None,
) -> Mapping[str, float]:
    """The cell's vector under a residual pair; identity reads it as is."""

    if (
        delta_c == 0.0 and width_lambda == 1.0 and not sigma_floor_rungs
        and not width_tier_lambdas
    ):
        return cell["probabilities"]
    adjusted, _ = apply_center_width_calibration(
        cell["probabilities"],
        delta_c=delta_c,
        width_lambda=width_lambda,
        ordered_keys=cell["ordered_keys"],
        key_centers_c=cell["key_centers_c"],
        excluded_buckets=cell["excluded_buckets"],
        open_edge_bounds_c=cell.get("open_edge_bounds_c"),
        sigma_floor_rungs=sigma_floor_rungs,
        width_tier_lambdas=width_tier_lambdas,
    )
    return adjusted


def _cell_proper_scores(
    cell: Mapping[str, Any],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
    width_tier_lambdas: Sequence[float] | None = None,
) -> tuple[float, float]:
    """(LogScore, bucket Brier) of one cell under a residual pair (charter 228)."""

    return _served_scores(
        _adjusted_cell(
            cell, delta_c=delta_c, width_lambda=width_lambda,
            sigma_floor_rungs=sigma_floor_rungs,
            width_tier_lambdas=width_tier_lambdas,
        ),
        ordered_keys=cell["ordered_keys"],
        settled_bucket=cell["settled_bucket"],
        excluded_buckets=cell["excluded_buckets"],
        key_centers_c=cell["key_centers_c"],
    )


def _cell_nll(
    cell: Mapping[str, Any],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
) -> float:
    """The cell's LogScore under a residual pair, on the floored vector."""

    return _cell_proper_scores(
        cell, delta_c=delta_c, width_lambda=width_lambda,
        sigma_floor_rungs=sigma_floor_rungs,
    )[0]


def _proper_score_gate(
    base: Sequence[tuple[float, ...]],
    candidate: Sequence[tuple[float, ...]],
    clusters: Sequence[Any],
) -> dict[str, Any]:
    """Charter 240: is ``candidate`` better than ``base`` under the ranked
    verdict?

    One entry per cell, (LogScore, bucket Brier, ladder CRPS) on each side;
    ``clusters`` names each cell's station-day.  The change is cell-weighted,
    as every mean in this lane is, and its intervals resample whole
    station-days (``GATE_BOOTSTRAP_DRAWS`` draws, fixed seed, so a refit is
    reproducible).  ``lower`` needs:

    * primary -- the LogScore's upper bound below zero;
    * structure -- the CRPS's lower bound not above zero (not stably worse);
    * guardrail -- the Brier's lower bound not above zero (not significantly
      worse).

    A CRPS that cannot be computed (NaN) fails the structure check: a verdict
    that cannot see the distribution's structure does not get to skip it.
    """

    import numpy as np

    count = len(base)
    out: dict[str, Any] = {
        "rule": GATE_RULE,
        "cells": count,
        "station_days": len(set(clusters)),
        "confidence": GATE_CONFIDENCE,
        "bootstrap_draws": GATE_BOOTSTRAP_DRAWS,
        "d_logscore": None,
        "d_brier": None,
        "d_crps": None,
        "ci_logscore": None,
        "ci_brier": None,
        "ci_crps": None,
        "primary_lower": False,
        "crps_not_worse": False,
        "brier_not_worse": False,
        "lower": False,
    }
    if count == 0 or len(candidate) != count or len(clusters) != count:
        return out
    ids: dict[Any, int] = {}
    index = np.array([ids.setdefault(key, len(ids)) for key in clusters])

    def _column(position: int) -> Any:
        return np.array([
            (float(c[position]) - float(b[position]))
            if len(b) > position and len(c) > position else float("nan")
            for b, c in zip(base, candidate)
        ])

    d_log, d_brier, d_crps = _column(0), _column(1), _column(2)
    size = len(ids)
    cells = np.bincount(index, minlength=size).astype(float)
    draws = np.random.default_rng(GATE_BOOTSTRAP_SEED).integers(
        0, size, size=(GATE_BOOTSTRAP_DRAWS, size)
    )
    weight = cells[draws].sum(axis=1)
    tail = 100.0 * (1.0 - GATE_CONFIDENCE) / 2.0

    def _interval(values: Any) -> list[float] | None:
        if not np.all(np.isfinite(values)):
            return None
        sums = np.bincount(index, weights=values, minlength=size)
        low, high = np.percentile(
            sums[draws].sum(axis=1) / weight, [tail, 100.0 - tail]
        )
        return [float(low), float(high)]

    ci_log, ci_brier, ci_crps = _interval(d_log), _interval(d_brier), _interval(d_crps)
    primary = bool(ci_log is not None and ci_log[1] < 0.0)
    crps_ok = bool(ci_crps is not None and ci_crps[0] <= 0.0)
    brier_ok = bool(ci_brier is not None and ci_brier[0] <= 0.0)
    out.update({
        "d_logscore": float(d_log.sum() / count),
        "d_brier": float(d_brier.sum() / count),
        "d_crps": (
            float(d_crps.sum() / count) if np.all(np.isfinite(d_crps)) else None
        ),
        "ci_logscore": ci_log,
        "ci_brier": ci_brier,
        "ci_crps": ci_crps,
        "primary_lower": primary,
        "crps_not_worse": crps_ok,
        "brier_not_worse": brier_ok,
        "lower": bool(primary and crps_ok and brier_ok),
    })
    return out


# Charter 202 follow-up (2026-09-23): the lane's grids, scored once per cell.
# Once the evidence stopped resetting at every bump the lane held ten days of
# cells, and scoring the pooled grid, the tier grid and every rolling-origin
# prefix one pair at a time through the scalar transform needed hours -- the
# daily refit is killed at 900 s.  Every pair any search here asks for is on
# this grid, so a cell's whole row is computed once
# (``center_width_adjusted_grid``, pinned bit for bit to the scalar function)
# and kept for the round; the searches then add the same numbers in the same
# order they always did -- ``sum`` where they used ``sum``, ``+=`` where they
# used ``+=`` -- so every argmin, every NLL and every receipt is unchanged.
def _in_memo_scope(function):
    """Run ``function`` inside a grid memo scope (nested in the round's when
    one is open), so a search scores each cell's grid row once however many
    pairs it asks for.  The body and its source are the function's own."""

    @functools.wraps(function)
    def scoped(*args: Any, **kwargs: Any) -> Any:
        with _round_memo():
            return function(*args, **kwargs)

    return scoped


_GRID_LAMBDAS = tuple(sorted(
    {lam for lam in LAMBDA_GRID + WIDTH_TIER_LAMBDA_GRID
     if MIN_WIDTH_LAMBDA <= lam <= MAX_WIDTH_LAMBDA}
))
_GRID_PAIRS = tuple(
    (delta, lam)
    for delta in SHIFT_GRID_C if abs(delta) <= MAX_ABS_SHIFT_C
    for lam in _GRID_LAMBDAS
)
_GRID_INDEX = {pair: position for position, pair in enumerate(_GRID_PAIRS)}


def _grid_rows(cell: Mapping[str, Any], sigma_floor_rungs: float) -> Any:
    """The LogScore of one cell at every grid pair, kept for the round.  Each
    entry equals ``_cell_nll`` at that pair exactly."""

    import array

    # Imported here: materialization imports this module at load.
    from .materialization import PROBABILITY_MODEL_ZERO_FLOOR  # noqa: PLC0415

    memo = _ROUND_MEMO
    key = ("width_grid", id(cell), float(sigma_floor_rungs))
    if memo is not None:
        held = memo.get(key)
        if held is not None:
            return held[0]
    adjusted, refused = center_width_adjusted_grid(
        cell["probabilities"],
        ordered_keys=cell["ordered_keys"],
        key_centers_c=cell["key_centers_c"],
        shifts=[pair[0] for pair in _GRID_PAIRS],
        lambdas=[pair[1] for pair in _GRID_PAIRS],
        excluded_buckets=cell["excluded_buckets"],
        open_edge_bounds_c=cell.get("open_edge_bounds_c"),
        sigma_floor_rungs=sigma_floor_rungs,
    )
    keys = list(cell["ordered_keys"])
    settled = keys.index(cell["settled_bucket"])
    out = set(cell["excluded_buckets"])
    excluded = [key in out for key in keys]
    log_row = array.array("d")
    refused_rows = refused.tolist()
    for position, row in enumerate(adjusted.tolist()):
        delta, lam = _GRID_PAIRS[position]
        if refused_rows[position] or (
            delta == 0.0 and lam == 1.0 and not sigma_floor_rungs
        ):
            # The scalar paths themselves: identity reads the cell as is,
            # and a refused row is served in the caller's key order.
            log_row.append(_cell_nll(
                cell, delta_c=delta, width_lambda=lam,
                sigma_floor_rungs=sigma_floor_rungs,
            ))
            continue
        log_row.append(_served_row_logscore(
            row, settled=settled, excluded=excluded,
            floor=PROBABILITY_MODEL_ZERO_FLOOR,
        ))
    if memo is not None:
        memo[key] = (log_row, cell)
    return log_row


def _mean_nll(
    cells: Sequence[Mapping[str, Any]],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
) -> float:
    """Mean LogScore over the cells, on the floored vector (charter 228)."""

    if not cells:
        return 0.0
    # Only inside a search's memo scope: a single pair asked outside one is
    # one scalar call per cell, cheaper than a whole row nobody keeps.
    position = (
        _GRID_INDEX.get((delta_c, width_lambda))
        if _ROUND_MEMO is not None else None
    )
    if position is not None:
        return sum(
            _grid_rows(cell, sigma_floor_rungs)[position] for cell in cells
        ) / len(cells)
    return sum(
        _cell_nll(
            cell,
            delta_c=delta_c,
            width_lambda=width_lambda,
            sigma_floor_rungs=sigma_floor_rungs,
        )
        for cell in cells
    ) / len(cells)


def _cell_score_list(
    cells: Sequence[Mapping[str, Any]],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
    width_tier_lambdas: Sequence[float] | None = None,
) -> list[tuple[float, float]]:
    """Each cell's (LogScore, bucket Brier) under a residual pair -- what the
    gates read.  A handful of pairs per fit, so always the scalar path."""

    return [
        _cell_proper_scores(
            cell, delta_c=delta_c, width_lambda=width_lambda,
            sigma_floor_rungs=sigma_floor_rungs,
            width_tier_lambdas=width_tier_lambdas,
        )
        for cell in cells
    ]


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


@_in_memo_scope
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



def _cell_in_thin_far_slice(cell: Mapping[str, Any]) -> bool:
    """Charter 239: the fit reads the slice off the cell the way serving
    reads it off the decision -- the one predicate, the row's own count and
    horizon."""

    return thin_far_slice_applies(
        seated_system_count=cell.get("seated_system_count"),
        hours_to_settlement=cell.get("hours_to_settlement"),
    )


def _split_slice_cells(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, list[Mapping[str, Any]]], dict[str, list[Mapping[str, Any]]]]:
    """(pooled cells, slice cells) by day.  Every day keeps a (possibly
    empty) entry on the pooled side so the day order the folds walk is the
    stage's, not the slice's."""

    pooled: dict[str, list[Mapping[str, Any]]] = {}
    sliced: dict[str, list[Mapping[str, Any]]] = {}
    for day in sorted(by_day):
        pooled[day] = []
        for cell in by_day[day]:
            if _cell_in_thin_far_slice(cell):
                sliced.setdefault(day, []).append(cell)
            else:
                pooled[day].append(cell)
    return pooled, sliced


def _cell_tail_scores(
    cell: Mapping[str, Any],
    *,
    delta_c: float,
    width_lambda: float,
    tail_mix_weight: float,
    sigma_floor_rungs: float = 0.0,
) -> tuple[float, float, float]:
    """(LogScore, bucket Brier, ladder CRPS) of one slice cell under a
    residual pair and then the slice's tail mix (charter 248)."""

    adjusted = _adjusted_cell(
        cell, delta_c=delta_c, width_lambda=width_lambda,
        sigma_floor_rungs=sigma_floor_rungs,
    )
    if float(tail_mix_weight) > 0.0:
        adjusted, _ = apply_tail_mix(
            adjusted,
            weight=float(tail_mix_weight),
            scale=TAIL_MIX_SCALE,
            ordered_keys=cell["ordered_keys"],
            key_centers_c=cell["key_centers_c"],
            excluded_buckets=cell["excluded_buckets"],
            open_edge_bounds_c=cell.get("open_edge_bounds_c"),
        )
    return _served_scores(
        adjusted,
        ordered_keys=cell["ordered_keys"],
        settled_bucket=cell["settled_bucket"],
        excluded_buckets=cell["excluded_buckets"],
        key_centers_c=cell["key_centers_c"],
    )


def _search_slice_tail(
    cells: Sequence[Mapping[str, Any]],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
) -> tuple[float, float]:
    """The slice's best tail-mix weight on ``TAIL_MIX_WEIGHT_GRID`` at a
    fixed residual pair, on the LogScore of the floored vector (the training
    rule of 211/228).  Returns (weight, mean LogScore); zero is no slice."""

    if not cells:
        return 0.0, 0.0
    best: tuple[float, float] | None = None
    for weight in TAIL_MIX_WEIGHT_GRID:
        value = sum(
            _cell_tail_scores(
                cell, delta_c=delta_c, width_lambda=width_lambda,
                tail_mix_weight=weight, sigma_floor_rungs=sigma_floor_rungs,
            )[0]
            for cell in cells
        ) / len(cells)
        if best is None or value < best[1]:
            best = (float(weight), value)
    return best if best is not None else (0.0, 0.0)


def _slice_entry(tail_mix_weight: float) -> dict[str, Any]:
    return {
        "max_seated_systems": THIN_FAR_MAX_SEATED_SYSTEMS,
        "min_hours_to_settlement": THIN_FAR_MIN_HOURS_TO_SETTLEMENT,
        "tail_mix_weight": round(float(tail_mix_weight), 4),
        "tail_mix_scale": TAIL_MIX_SCALE,
    }


@_in_memo_scope
def _thin_far_slice_lane(
    *,
    pooled_by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    slice_by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    days: Sequence[str],
    cut: int,
    pooled_admitted: bool,
    served_residual: tuple[float, float],
    sigma_floor_rungs: float,
    incumbent_slices: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Charter 248: one tail-mix weight for the thin-pool far-lead slice,
    on top of the stage pair, admitted under the ranked verdict.

    Residual space, like everything the fit measures: a slice cell carries
    the vector it was served before any slice (``_reserved_under_current_width``
    re-serves it from the recorded input), the pooled candidate's residual
    pair is applied to it when that pair is admitted this round, and the
    slice's candidate is the tail mix on that -- exactly what serving will
    do to a slice decision.  The weight is searched on the fixed grid at the
    fixed scale; kappa is not a fitted number.

    Admission walks the origin the way ``_rolling_origin_admission`` does:
    each fold refits the pooled pair on its prefix (when admitted this
    round) and the weight on the prefix's slice cells, and scores the
    held-out day's slice cells with and without the mix.  The held-out cells
    pass the stage's ranked gate and a majority of folds gain on the
    LogScore.  The stored weight is the one fitted on the training prefix.
    """

    ordered = list(days)
    slice_days = [day for day in ordered if slice_by_day.get(day)]
    slice_cells = [cell for day in slice_days for cell in slice_by_day[day]]
    out: dict[str, Any] = {
        "schema": "center_width_thin_far_slice_lane_v2",
        "slice": THIN_FAR_SLICE_NAME,
        "transform": "tail_mix",
        "tail_mix_scale": TAIL_MIX_SCALE,
        "tail_mix_weight_grid": list(TAIL_MIX_WEIGHT_GRID),
        "max_seated_systems": THIN_FAR_MAX_SEATED_SYSTEMS,
        "min_hours_to_settlement": THIN_FAR_MIN_HOURS_TO_SETTLEMENT,
        "gate_rule": GATE_RULE,
        "slice_day_count": len(slice_days),
        "slice_cell_count": len(slice_cells),
        "min_qualifying_days": SLICE_MIN_QUALIFYING_DAYS,
        "min_qualifying_cells": SLICE_MIN_QUALIFYING_CELLS,
        "max_folds": SLICE_ROLLING_ORIGIN_MAX_FOLDS,
        "incumbent_slices": (
            dict(incumbent_slices) if incumbent_slices else None
        ),
        "served_residual": list(served_residual),
        "pooled_admitted": bool(pooled_admitted),
    }
    if (
        len(slice_days) < SLICE_MIN_QUALIFYING_DAYS
        or len(slice_cells) < SLICE_MIN_QUALIFYING_CELLS
    ):
        out.update({"admitted": False, "state": "ACCUMULATING"})
        return out
    first = (
        ROLLING_ORIGIN_MIN_FOLD_TRAIN_DAYS
        if SLICE_ROLLING_ORIGIN_MAX_FOLDS is None
        else max(
            ROLLING_ORIGIN_MIN_FOLD_TRAIN_DAYS,
            len(ordered) - SLICE_ROLLING_ORIGIN_MAX_FOLDS,
        )
    )
    folds: list[dict[str, Any]] = []
    held_other: list[tuple[float, float, float]] = []
    held_slice: list[tuple[float, float, float]] = []
    held_clusters: list[tuple[Any, str]] = []
    for index in range(first, len(ordered)):
        test = list(slice_by_day.get(ordered[index], []))
        train_slice = [
            cell for day in ordered[:index] for cell in slice_by_day.get(day, [])
        ]
        if not test or len(train_slice) < SLICE_MIN_FOLD_TRAIN_CELLS:
            continue
        if pooled_admitted:
            train_pooled = [
                cell for day in ordered[:index]
                for cell in pooled_by_day.get(day, [])
            ]
            other_delta, other_lambda, _nll = (
                _search(train_pooled, sigma_floor_rungs=sigma_floor_rungs)
                if train_pooled else RESIDUAL_IDENTITY + (0.0,)
            )
        else:
            other_delta, other_lambda = RESIDUAL_IDENTITY
        weight, _train = _search_slice_tail(
            train_slice, delta_c=other_delta, width_lambda=other_lambda,
            sigma_floor_rungs=sigma_floor_rungs,
        )
        other_scores = [
            _cell_tail_scores(
                cell, delta_c=other_delta, width_lambda=other_lambda,
                tail_mix_weight=0.0, sigma_floor_rungs=sigma_floor_rungs,
            )
            for cell in test
        ]
        slice_scores = [
            _cell_tail_scores(
                cell, delta_c=other_delta, width_lambda=other_lambda,
                tail_mix_weight=weight, sigma_floor_rungs=sigma_floor_rungs,
            )
            for cell in test
        ]
        other_nll = sum(x[0] for x in other_scores) / len(test)
        slice_nll = sum(x[0] for x in slice_scores) / len(test)
        folds.append({
            "scored_day": ordered[index],
            "train_slice_cell_count": len(train_slice),
            "cell_count": len(test),
            "other_delta_c": other_delta,
            "other_width_lambda": other_lambda,
            "slice_tail_mix_weight": weight,
            "other_nll": other_nll,
            "slice_nll": slice_nll,
            "gain": other_nll - slice_nll,
            "brier_gain": (
                sum(x[1] for x in other_scores) - sum(x[1] for x in slice_scores)
            ) / len(test),
        })
        held_other.extend(other_scores)
        held_slice.extend(slice_scores)
        held_clusters.extend(
            (cell.get("station_id"), ordered[index]) for cell in test
        )
    gate = _proper_score_gate(held_other, held_slice, held_clusters)
    positive = sum(1 for fold in folds if fold["gain"] > 0.0)
    majority = positive * 2 > len(folds) if folds else False
    train_prefix = [
        cell for day in ordered[:cut] for cell in slice_by_day.get(day, [])
    ]
    candidate_weight, candidate_train_nll = _search_slice_tail(
        train_prefix, delta_c=served_residual[0], width_lambda=served_residual[1],
        sigma_floor_rungs=sigma_floor_rungs,
    ) if train_prefix else (0.0, 0.0)
    measured = bool(gate["lower"]) and bool(majority) and bool(folds)
    admitted = measured and candidate_weight > 0.0
    out.update({
        "fold_count": len(folds),
        "folds_positive": positive,
        "majority_positive": majority,
        "gate": gate,
        "scored_cell_count": sum(fold["cell_count"] for fold in folds),
        "gain": (
            sum(fold["gain"] * fold["cell_count"] for fold in folds)
            / max(1, sum(fold["cell_count"] for fold in folds))
        ) if folds else 0.0,
        "brier_gain": (
            sum(fold["brier_gain"] * fold["cell_count"] for fold in folds)
            / max(1, sum(fold["cell_count"] for fold in folds))
        ) if folds else 0.0,
        "train_prefix_slice_cell_count": len(train_prefix),
        "candidate_tail_mix_weight": float(candidate_weight),
        "candidate_train_nll": float(candidate_train_nll),
        "measured_admitted": measured,
        "admitted": admitted,
        "state": (
            "ADMITTED" if admitted
            else "UNCHANGED" if measured
            else "REFUSED_FORWARD"
        ),
        "folds": folds,
    })
    return out


def _thin_far_slice_veto(
    slice_by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    slices: Mapping[str, Mapping[str, Any]],
    activated_at_utc: str | None,
) -> dict[str, Any]:
    """Charter 248: should the active tail slice come off?  Each slice cell
    served since activation is scored as served -- its pre-mix vector
    (re-served from the recorded input) mixed again with the receipt's own
    weight and scale -- against the same vector without the mix.  Vetoed
    when the undo is significantly better under the ranked verdict on at
    least the veto window (charter 138)."""

    since = activated_at_utc or ""
    entry = (slices or {}).get(THIN_FAR_SLICE_NAME) or {}
    kept: list[tuple[float, float, float]] = []
    undone: list[tuple[float, float, float]] = []
    clusters: list[tuple[Any, str]] = []
    skipped: dict[str, int] = {}
    judged_days: set[str] = set()
    for day in sorted(slice_by_day):
        for cell in slice_by_day[day]:
            if str(cell.get("decision_at_utc") or "") < since:
                continue
            receipt = cell.get("center_width_receipt") or {}
            weight = receipt.get("width_slice_tail_weight")
            if weight is None or float(weight) <= 0.0:
                skipped["not_sliced"] = skipped.get("not_sliced", 0) + 1
                continue
            if not cell.get("reserved_under_current_width"):
                # The cell's vector is the served one, mix included, and the
                # mix is not its own inverse: nothing exact to undo from.
                skipped["no_recorded_input"] = skipped.get("no_recorded_input", 0) + 1
                continue
            scale = float(receipt.get("width_slice_tail_scale") or TAIL_MIX_SCALE)
            try:
                mixed, _ = apply_tail_mix(
                    cell["probabilities"],
                    weight=float(weight),
                    scale=scale,
                    ordered_keys=cell["ordered_keys"],
                    key_centers_c=cell["key_centers_c"],
                    excluded_buckets=cell["excluded_buckets"],
                    open_edge_bounds_c=cell.get("open_edge_bounds_c"),
                )
            except ValueError:
                skipped["malformed"] = skipped.get("malformed", 0) + 1
                continue
            kept.append(_served_scores(
                mixed,
                ordered_keys=cell["ordered_keys"],
                settled_bucket=cell["settled_bucket"],
                excluded_buckets=cell["excluded_buckets"],
                key_centers_c=cell["key_centers_c"],
            ))
            undone.append(_cell_proper_scores(cell, delta_c=0.0, width_lambda=1.0))
            clusters.append((cell.get("station_id"), day))
            judged_days.add(day)
    gate = _proper_score_gate(kept, undone, clusters)
    enough = (
        len(judged_days) >= CENTER_WIDTH_VETO_MIN_DAYS
        and len(kept) >= CENTER_WIDTH_VETO_MIN_CELLS
    )
    return {
        "schema": "center_width_thin_far_slice_veto_v2",
        "activated_at_utc": activated_at_utc,
        "incumbent_slice": dict(entry),
        "day_count": len(judged_days),
        "cell_count": len(kept),
        "skipped": skipped,
        "undo_gain": 0.0 if not kept else -float(gate["d_logscore"]),
        "gate": gate,
        "enough_evidence": enough,
        "vetoed": bool(enough and gate["lower"]),
    }


# ---------------------------------------------------------------------------
# Charter 271: the mode-to-shoulder share, one number per horizon bin.

MODE_SHOULDER_MIN_QUALIFYING_DAYS = 8
MODE_SHOULDER_MIN_QUALIFYING_CELLS = 60
MODE_SHOULDER_MIN_FOLD_TRAIN_CELLS = 30


def _cell_shoulder_eligible(cell: Mapping[str, Any]) -> bool:
    """A cell the lane can score a share on.

    A re-served cell (charter 202) came back before the share, so any share
    can be laid on it.  A cell served with a share and NOT re-served still
    carries that share inside its vector; laying another on it would score
    two shares, so it is left out.
    """

    receipt = cell.get("center_width_receipt") or {}
    served_share = receipt.get("mode_shoulder_share")
    if served_share is not None and float(served_share) > 0.0:
        return bool(cell.get("reserved_under_current_width"))
    return True


def _cell_pre_shoulder(
    cell: Mapping[str, Any],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float,
) -> dict[str, float]:
    """The vector serving hands the share: the residual pair, then the
    slice's tail mix where the row was mixed.  A cell that was not re-served
    already carries its mix in the vector."""

    adjusted = dict(_adjusted_cell(
        cell, delta_c=delta_c, width_lambda=width_lambda,
        sigma_floor_rungs=sigma_floor_rungs,
    ))
    receipt = cell.get("center_width_receipt") or {}
    weight = receipt.get("width_slice_tail_weight")
    if cell.get("reserved_under_current_width") and weight is not None and float(weight) > 0.0:
        adjusted, _ = apply_tail_mix(
            adjusted,
            weight=float(weight),
            scale=float(receipt.get("width_slice_tail_scale") or TAIL_MIX_SCALE),
            ordered_keys=cell["ordered_keys"],
            key_centers_c=cell["key_centers_c"],
            excluded_buckets=cell["excluded_buckets"],
            open_edge_bounds_c=cell.get("open_edge_bounds_c"),
        )
    return adjusted


def _cell_shoulder_scores(
    cell: Mapping[str, Any],
    pre: Mapping[str, float],
    share: float,
) -> tuple[float, float, float]:
    """(LogScore, Brier, CRPS) of ``pre`` under ``share`` and then the
    far-peak ceiling the row was served under -- serving's order."""

    shaped = dict(pre)
    if float(share) > 0.0:
        shaped, _ = apply_mode_shoulder(
            shaped, share=float(share),
            ordered_keys=cell["ordered_keys"],
            excluded_buckets=cell["excluded_buckets"],
        )
    receipt = cell.get("center_width_receipt") or {}
    ceiling = receipt.get("peak_ceiling")
    if ceiling is not None:
        shaped, _ = apply_peak_ceiling(
            shaped, ceiling=float(ceiling),
            ordered_keys=cell["ordered_keys"],
            excluded_buckets=cell["excluded_buckets"],
        )
    return _served_scores(
        shaped,
        ordered_keys=cell["ordered_keys"],
        settled_bucket=cell["settled_bucket"],
        excluded_buckets=cell["excluded_buckets"],
        key_centers_c=cell["key_centers_c"],
    )


def _search_mode_shoulder(
    cells: Sequence[tuple[Mapping[str, Any], Mapping[str, float]]],
) -> tuple[float, float]:
    """Best share on ``MODE_SHOULDER_SHARE_GRID`` by mean LogScore (the
    training rule of 211/228).  Returns (share, mean LogScore)."""

    if not cells:
        return 0.0, 0.0
    best: tuple[float, float] | None = None
    for share in MODE_SHOULDER_SHARE_GRID:
        value = sum(
            _cell_shoulder_scores(cell, pre, share)[0] for cell, pre in cells
        ) / len(cells)
        if best is None or value < best[1]:
            best = (float(share), value)
    return best if best is not None else (0.0, 0.0)


@_in_memo_scope
def _mode_shoulder_lane(
    *,
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
    days: Sequence[str],
    served_residual: tuple[float, float],
    sigma_floor_rungs: float,
    incumbent: Mapping[str, Any] | None,
    activated_at_utc: str | None,
) -> dict[str, Any]:
    """Charter 271: fit, admit and veto one share per horizon bin.

    Residual space like every lane here: each cell carries what it was
    served before the share (charter 202 re-serve), the pooled residual that
    would serve this round is applied to it, the row's tail mix and ceiling
    are applied around the share the way serving applies them.  Per bin the
    origin walks forward: each fold fits the share on the bin's earlier
    cells and scores the held-out day with and without it; admitted when
    the held-out cells pass the stage's ranked gate (charter 240) and a
    majority of folds gain.  The stored share is fitted on every bin cell --
    the walk validated the procedure, as the pooled lane's walk does.  An
    incumbent share is vetoed when its undo is significantly better on the
    cells served under it since activation (charter 138).
    """

    ordered = list(days)
    incumbent_bins = dict((incumbent or {}).get("bins") or {})
    out: dict[str, Any] = {
        "schema": MODE_SHOULDER_SCHEMA + "_lane",
        "warm_share_rule": MODE_SHOULDER_WARM_SHARE_RULE,
        "share_grid": list(MODE_SHOULDER_SHARE_GRID),
        "gate_rule": GATE_RULE,
        "served_residual": list(served_residual),
        "incumbent": dict(incumbent) if incumbent else None,
        "bins": {},
    }
    served_bins: dict[str, Any] = {}
    candidate_bins: dict[str, Any] = {}
    for name, _lower, _upper in MODE_SHOULDER_BINS:
        cells_by_day: dict[str, list[tuple[Mapping[str, Any], dict[str, float]]]] = {}
        for day in ordered:
            for cell in by_day.get(day, []):
                if mode_shoulder_bin_for(cell.get("hours_to_settlement")) != name:
                    continue
                if not _cell_shoulder_eligible(cell):
                    continue
                try:
                    pre = _cell_pre_shoulder(
                        cell, delta_c=served_residual[0],
                        width_lambda=served_residual[1],
                        sigma_floor_rungs=sigma_floor_rungs,
                    )
                except (ContractError, ValueError):
                    continue
                cells_by_day.setdefault(day, []).append((cell, pre))
        bin_days = [day for day in ordered if cells_by_day.get(day)]
        bin_cells = [item for day in bin_days for item in cells_by_day[day]]
        entry: dict[str, Any] = {
            "day_count": len(bin_days),
            "cell_count": len(bin_cells),
            "incumbent_share": (incumbent_bins.get(name) or {}).get("share"),
        }
        incumbent_share = float((incumbent_bins.get(name) or {}).get("share") or 0.0)
        # The veto first: an active share is only safe while it still earns.
        vetoed = False
        if incumbent_share > 0.0:
            kept, undone, clusters = [], [], []
            for cell, pre in bin_cells:
                receipt = cell.get("center_width_receipt") or {}
                if str(cell.get("decision_at_utc") or "") < str(activated_at_utc or ""):
                    continue
                if receipt.get("mode_shoulder_share") is None:
                    continue
                kept.append(_cell_shoulder_scores(cell, pre, float(receipt["mode_shoulder_share"])))
                undone.append(_cell_shoulder_scores(cell, pre, 0.0))
                clusters.append((cell.get("station_id"), cell.get("event_date")))
            veto_gate = _proper_score_gate(kept, undone, clusters) if kept else None
            enough = (
                len({c[1] for c in clusters}) >= CENTER_WIDTH_VETO_MIN_DAYS
                and len(kept) >= CENTER_WIDTH_VETO_MIN_CELLS
            )
            vetoed = bool(enough and veto_gate and veto_gate["lower"])
            entry["veto"] = {
                "cell_count": len(kept), "enough_evidence": enough,
                "gate": veto_gate, "vetoed": vetoed,
            }
        if (
            len(bin_days) < MODE_SHOULDER_MIN_QUALIFYING_DAYS
            or len(bin_cells) < MODE_SHOULDER_MIN_QUALIFYING_CELLS
        ):
            entry.update({"state": "ACCUMULATING", "admitted": False})
            if incumbent_share > 0.0 and not vetoed:
                served_bins[name] = {"share": incumbent_share}
            out["bins"][name] = entry
            continue
        first = max(
            ROLLING_ORIGIN_MIN_FOLD_TRAIN_DAYS,
            len(bin_days) - ROLLING_ORIGIN_MAX_FOLDS * 2,
        )
        folds: list[dict[str, Any]] = []
        held_base: list[tuple[float, float, float]] = []
        held_cand: list[tuple[float, float, float]] = []
        held_clusters: list[tuple[Any, str]] = []
        for index in range(first, len(bin_days)):
            train = [item for day in bin_days[:index] for item in cells_by_day[day]]
            test = cells_by_day[bin_days[index]]
            if len(train) < MODE_SHOULDER_MIN_FOLD_TRAIN_CELLS or not test:
                continue
            share, _train_nll = _search_mode_shoulder(train)
            base_scores = [_cell_shoulder_scores(c, pre, 0.0) for c, pre in test]
            cand_scores = [_cell_shoulder_scores(c, pre, share) for c, pre in test]
            gain = (
                sum(x[0] for x in base_scores) - sum(x[0] for x in cand_scores)
            ) / len(test)
            folds.append({
                "scored_day": bin_days[index], "train_cell_count": len(train),
                "cell_count": len(test), "share": share, "gain": gain,
            })
            held_base.extend(base_scores)
            held_cand.extend(cand_scores)
            held_clusters.extend(
                (c.get("station_id"), bin_days[index]) for c, _pre in test
            )
        gate = _proper_score_gate(held_base, held_cand, held_clusters) if held_base else None
        positive = sum(1 for fold in folds if fold["gain"] > 0.0)
        majority = positive * 2 > len(folds) if folds else False
        candidate_share, candidate_nll = _search_mode_shoulder(bin_cells)
        measured = bool(gate and gate["lower"]) and majority and bool(folds)
        admitted = measured and candidate_share > 0.0
        entry.update({
            "fold_count": len(folds), "folds_positive": positive,
            "majority_positive": majority, "gate": gate,
            "candidate_share": candidate_share,
            "candidate_train_nll": candidate_nll,
            "admitted": admitted,
            "state": (
                "ADMITTED" if admitted
                else "UNCHANGED" if measured
                else "REFUSED_FORWARD"
            ),
            "folds": folds,
        })
        if candidate_share > 0.0:
            candidate_bins[name] = {"share": candidate_share}
        if admitted:
            served_bins[name] = {"share": candidate_share}
        elif incumbent_share > 0.0 and not vetoed:
            served_bins[name] = {"share": incumbent_share}
        out["bins"][name] = entry
    out["served_table"] = (
        _canonical_mode_shoulder({"bins": served_bins}) if served_bins else None
    )
    out["candidate_table"] = (
        _canonical_mode_shoulder({"bins": candidate_bins}) if candidate_bins else None
    )
    return out


def _f_far_tail_reading(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Charter 261: is the F-station far tail mix earning its place?  A
    reading journalled with every fit, like the ceiling's; the rule is fixed
    in code and comes off by its switch.

    Each cell served under the ``f_far`` slice is scored as served -- its
    pre-mix vector (re-served from the recorded input) mixed at the receipt's
    weight and capped at the receipt's ceiling where one stands -- against
    the same vector capped but unmixed, under the stage's ranked verdict.
    Beside it: the mass said and landed two or more rungs off the mode.
    """

    from .center_width_calibration import F_FAR_TAIL_SLICE_NAME  # noqa: PLC0415

    kept: list[tuple[float, float, float]] = []
    undone: list[tuple[float, float, float]] = []
    clusters: list[tuple[Any, str]] = []
    skipped: dict[str, int] = {}
    judged_days: set[str] = set()
    tail_said_mixed: list[float] = []
    tail_said_plain: list[float] = []
    tail_landed: list[float] = []
    for day in sorted(by_day):
        for cell in by_day[day]:
            receipt = cell.get("center_width_receipt") or {}
            if receipt.get("width_slice") != F_FAR_TAIL_SLICE_NAME:
                continue
            weight = float(receipt.get("width_slice_tail_weight") or 0.0)
            if weight <= 0.0:
                continue
            if not cell.get("reserved_under_current_width"):
                skipped["no_recorded_input"] = skipped.get("no_recorded_input", 0) + 1
                continue
            keys = list(cell["ordered_keys"])
            try:
                plain: Mapping[str, float] = cell["probabilities"]
                mixed, _ = apply_tail_mix(
                    plain,
                    weight=weight,
                    scale=float(receipt.get("width_slice_tail_scale") or TAIL_MIX_SCALE),
                    ordered_keys=keys,
                    key_centers_c=cell["key_centers_c"],
                    excluded_buckets=cell["excluded_buckets"],
                    open_edge_bounds_c=cell.get("open_edge_bounds_c"),
                )
                ceiling = receipt.get("peak_ceiling")
                if ceiling is not None:
                    plain, _ = apply_peak_ceiling(
                        plain, ceiling=float(ceiling), ordered_keys=keys,
                        excluded_buckets=cell["excluded_buckets"],
                    )
                    mixed, _ = apply_peak_ceiling(
                        mixed, ceiling=float(ceiling), ordered_keys=keys,
                        excluded_buckets=cell["excluded_buckets"],
                    )
            except (TypeError, ValueError):
                skipped["malformed"] = skipped.get("malformed", 0) + 1
                continue
            score = dict(
                ordered_keys=keys,
                settled_bucket=cell["settled_bucket"],
                excluded_buckets=cell["excluded_buckets"],
                key_centers_c=cell["key_centers_c"],
            )
            kept.append(_served_scores(mixed, **score))
            undone.append(_served_scores(plain, **score))
            clusters.append((cell.get("station_id"), day))
            judged_days.add(day)
            top = max(range(len(keys)), key=lambda index: (float(plain[keys[index]]), -index))
            far = [index for index in range(len(keys)) if abs(index - top) >= 2]
            tail_said_plain.append(sum(float(plain[keys[index]]) for index in far))
            tail_said_mixed.append(sum(float(mixed[keys[index]]) for index in far))
            hit = keys.index(str(cell["settled_bucket"])) if str(cell["settled_bucket"]) in keys else top
            tail_landed.append(1.0 if abs(hit - top) >= 2 else 0.0)
    gate = _proper_score_gate(undone, kept, clusters)
    count = len(kept)
    return {
        "schema": "center_width_f_far_tail_reading_v1",
        "day_count": len(judged_days),
        "cell_count": count,
        "skipped": skipped,
        # The mixed vector against the unmixed one: negative is the mix's gain.
        "gate_mixed_vs_unmixed": gate,
        "tail_said_unmixed": (sum(tail_said_plain) / count) if count else None,
        "tail_said_mixed": (sum(tail_said_mixed) / count) if count else None,
        "tail_landed": (sum(tail_landed) / count) if count else None,
    }


def _far_peak_ceiling_reading(
    by_day: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Charter 253: is the far-peak ceiling earning its place?  A reading,
    journalled with every fit; the rule itself is fixed in code and comes off
    by its switch, not by this lane.

    Each cell served under a ceiling is scored as served -- its pre-ceiling
    vector (re-served from the recorded input, the tail mix put back where
    the receipt names one) capped again at the receipt's own ceiling --
    against the same vector uncapped, under the stage's ranked verdict.
    Beside it, on the cells whose top bucket claimed 0.40 or more before the
    ceiling: what the top claimed, what it claims capped, and how often it
    landed.
    """

    kept: list[tuple[float, float, float]] = []
    undone: list[tuple[float, float, float]] = []
    clusters: list[tuple[Any, str]] = []
    skipped: dict[str, int] = {}
    judged_days: set[str] = set()
    tops_before: list[float] = []
    tops_after: list[float] = []
    top_hits: list[float] = []
    for day in sorted(by_day):
        for cell in by_day[day]:
            receipt = cell.get("center_width_receipt") or {}
            ceiling = receipt.get("peak_ceiling")
            if ceiling is None:
                continue
            if not cell.get("reserved_under_current_width"):
                skipped["no_recorded_input"] = skipped.get("no_recorded_input", 0) + 1
                continue
            vector: Mapping[str, float] = cell["probabilities"]
            try:
                weight = float(receipt.get("width_slice_tail_weight") or 0.0)
                if weight > 0.0:
                    vector, _ = apply_tail_mix(
                        vector,
                        weight=weight,
                        scale=float(receipt.get("width_slice_tail_scale") or TAIL_MIX_SCALE),
                        ordered_keys=cell["ordered_keys"],
                        key_centers_c=cell["key_centers_c"],
                        excluded_buckets=cell["excluded_buckets"],
                        open_edge_bounds_c=cell.get("open_edge_bounds_c"),
                    )
                capped, cap_receipt = apply_peak_ceiling(
                    vector,
                    ceiling=float(ceiling),
                    ordered_keys=cell["ordered_keys"],
                    excluded_buckets=cell["excluded_buckets"],
                )
            except (TypeError, ValueError):
                skipped["malformed"] = skipped.get("malformed", 0) + 1
                continue
            score = dict(
                ordered_keys=cell["ordered_keys"],
                settled_bucket=cell["settled_bucket"],
                excluded_buckets=cell["excluded_buckets"],
                key_centers_c=cell["key_centers_c"],
            )
            kept.append(_served_scores(capped, **score))
            undone.append(_served_scores(vector, **score))
            clusters.append((cell.get("station_id"), day))
            judged_days.add(day)
            before = float(cap_receipt["top_probability_before"])
            if before >= 0.40:
                top = str(cap_receipt["top_bucket"])
                tops_before.append(before)
                tops_after.append(float(capped[top]))
                top_hits.append(1.0 if top == cell["settled_bucket"] else 0.0)
    gate = _proper_score_gate(undone, kept, clusters)
    count = len(tops_before)
    return {
        "schema": "center_width_far_peak_ceiling_reading_v1",
        "day_count": len(judged_days),
        "cell_count": len(kept),
        "skipped": skipped,
        # The capped vector against the uncapped one: negative is the
        # ceiling's gain.
        "gate_capped_vs_uncapped": gate,
        "top_cell_count": count,
        "top_claim_uncapped": (sum(tops_before) / count) if count else None,
        "top_claim_capped": (sum(tops_after) / count) if count else None,
        "top_hit_rate": (sum(top_hits) / count) if count else None,
    }


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
    """(LogScore, bucket Brier, window claim, window hit) for one cell.

    The two scores are charter 228's, on the floored vector.  The window is
    the mean bucket +-1 of the *transformed* distribution, the shape the
    seat rule (charter 79) reads; its claim and hit feed the containment
    diagnostic only.
    """

    adjusted = dict(_adjusted_cell(
        cell, delta_c=delta_c, width_lambda=width_lambda,
        sigma_floor_rungs=sigma_floor_rungs,
        width_tier_lambdas=width_tier_lambdas,
    ))
    logscore, brier, _crps = _served_scores(
        adjusted,
        ordered_keys=cell["ordered_keys"],
        settled_bucket=cell["settled_bucket"],
        excluded_buckets=cell["excluded_buckets"],
        key_centers_c=cell["key_centers_c"],
    )
    total = sum(adjusted.values())
    if total <= 0.0:
        return logscore, brier, 0.0, 0
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
    return logscore, brier, claim, hit


def _mean_scores(
    cells: Sequence[Mapping[str, Any]],
    *,
    delta_c: float,
    width_lambda: float,
    sigma_floor_rungs: float = 0.0,
    width_tier_lambdas: Sequence[float] | None = None,
) -> dict[str, float]:
    """Mean LogScore, mean bucket Brier and the window containment ratio
    (hits over claims, a diagnostic) over ``cells``."""

    if not cells:
        return {"logscore": 0.0, "brier": 0.0, "containment": 0.0, "cells": 0}
    logscore = brier = claims = 0.0
    hits = 0
    for cell in cells:
        s, b, c, h = _cell_scores(
            cell, delta_c=delta_c, width_lambda=width_lambda,
            sigma_floor_rungs=sigma_floor_rungs,
            width_tier_lambdas=width_tier_lambdas,
        )
        logscore += s
        brier += b
        claims += c
        hits += h
    n = len(cells)
    return {
        "logscore": logscore / n,
        "brier": brier / n,
        "containment": (hits / claims) if claims > 0 else 0.0,
        "cells": n,
    }


@_in_memo_scope
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
    pooled lambda.  Scored on the LogScore of the floored vector, the one
    training rule (charters 211 and 228).
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
                # ``_mean_scores``'s LogScore, added with its own ``+=`` in
                # its own order, off the round's grid rows.
                position = _GRID_INDEX[(delta, lam)]
                logscore = 0.0
                for member in members:
                    logscore += _grid_rows(member, sigma_floor_rungs)[position]
                count = len(members)
                value = (logscore / count) * len(members)
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
    forward_clusters: Sequence[Any],
    incumbent: tuple[float, float],
    incumbent_floor: float,
    incumbent_tiers: Mapping[str, Any] | None,
    pooled: tuple[float, float],
    pooled_floor: float,
    tiered: tuple[float, tuple[float, ...]],
) -> dict[str, Any]:
    """Whether the tiers earned their place, and the numbers that say so.

    Admitted where the tiered candidate is significantly better than the
    pooled candidate on the ranked verdict over the forward window
    (charter 240); ``gate_vs_incumbent`` is what the pair then has to clear
    at the stage's own gate.
    """

    incumbent_lambdas = (incumbent_tiers or {}).get("lambdas") or None
    inc = _mean_scores(
        forward, delta_c=incumbent[0], width_lambda=incumbent[1],
        sigma_floor_rungs=incumbent_floor,
        width_tier_lambdas=incumbent_lambdas,
    )
    pool = _mean_scores(
        forward, delta_c=pooled[0], width_lambda=pooled[1],
        sigma_floor_rungs=pooled_floor,
    )
    tier = _mean_scores(
        forward, delta_c=tiered[0], width_lambda=pooled[1],
        sigma_floor_rungs=pooled_floor, width_tier_lambdas=tiered[1],
    )
    tier_scores = _cell_score_list(
        forward, delta_c=tiered[0], width_lambda=pooled[1],
        sigma_floor_rungs=pooled_floor, width_tier_lambdas=tiered[1],
    )
    vs_pooled = _proper_score_gate(
        _cell_score_list(
            forward, delta_c=pooled[0], width_lambda=pooled[1],
            sigma_floor_rungs=pooled_floor,
        ),
        tier_scores, forward_clusters,
    )
    vs_incumbent = _proper_score_gate(
        _cell_score_list(
            forward, delta_c=incumbent[0], width_lambda=incumbent[1],
            sigma_floor_rungs=incumbent_floor,
            width_tier_lambdas=incumbent_lambdas,
        ),
        tier_scores, forward_clusters,
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
                "logscore_pooled": p["logscore"],
                "logscore_tiered": t["logscore"],
                "brier_pooled": p["brier"],
                "brier_tiered": t["brier"],
            })
        per_tier.append(entry)
    measured = bool(vs_pooled["lower"])
    admitted = measured and WIDTH_TIER_MODE == "serve"
    return {
        "schema": "center_width_tier_lane_v2",
        "mode": WIDTH_TIER_MODE,
        "gate_rule": GATE_RULE,
        "boundaries_rungs": list(WIDTH_TIER_BOUNDARIES_RUNGS),
        "tier_names": list(WIDTH_TIER_NAMES),
        "candidate_delta_c": tiered[0],
        "candidate_lambdas": list(tiered[1]),
        "pooled_delta_c": pooled[0],
        "pooled_width_lambda": pooled[1],
        "forward_incumbent": inc,
        "forward_pooled_candidate": pool,
        "forward_tier_candidate": tier,
        "forward_logscore_gain_vs_pooled": pool["logscore"] - tier["logscore"],
        "forward_brier_gain_vs_pooled": pool["brier"] - tier["brier"],
        "forward_logscore_gain_vs_incumbent": inc["logscore"] - tier["logscore"],
        "gate_vs_pooled": vs_pooled,
        "gate_vs_incumbent": vs_incumbent,
        "per_tier": per_tier,
        "measured_admitted": measured,
        "admitted": admitted,
        "state": (
            "ADMITTED" if admitted
            else "SHADOW_ADMITTED" if measured
            else "REFUSED_FORWARD"
        ),
    }


def evidence_anchor(
    conn: sqlite3.Connection, *, stage: str, known_floor: str | None = None
) -> str | None:
    """Where the calibration lanes' evidence for this stage begins (charter 202).

    User ruling 2026-09-23: 「证据时钟不要清零，用旧的样本」.  The served
    version's own start (``served_arithmetic_anchor``) stays what the watchdog
    and the version comparison read; the lanes read the stage's evidence
    epoch instead (``serving_arithmetic.STAGE_EVIDENCE_EPOCH``), so a bump no
    longer spends the days they had accumulated.  A served version that
    declares no start, or began before the epoch, keeps the anchor it had.
    """

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
    except (sqlite3.OperationalError, TypeError, ValueError):
        current = None
    version = str(current[0] or "") if current is not None else ""
    if version:
        carried = serving_arithmetic.evidence_effective_from(
            _check_stage(stage), version
        )
        if carried:
            return carried
    if known_floor:
        return served_arithmetic_anchor(conn, stage=stage, known_floor=known_floor)
    return served_arithmetic_anchor(conn, stage=stage)


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
        # Charter 202: the evidence epoch, not the served version's start.
        served = evidence_anchor(conn, stage=stage, known_floor=floor)
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


@_in_memo_scope
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
    so a thin day cannot outvote a full one.  ``gate`` pools every fold's
    held-out cells and asks charter 240's question of them.

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
    total_brier_gain = 0.0
    total_cells = 0
    held_identity: list[tuple[float, float]] = []
    held_candidate: list[tuple[float, float]] = []
    held_clusters: list[tuple[Any, str]] = []
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
        identity = _cell_score_list(
            test,
            delta_c=RESIDUAL_IDENTITY[0],
            width_lambda=RESIDUAL_IDENTITY[1],
            sigma_floor_rungs=0.0,
        )
        candidate = _cell_score_list(
            test,
            delta_c=delta,
            width_lambda=lam,
            sigma_floor_rungs=sigma_floor_rungs,
        )
        identity_nll = sum(s[0] for s in identity) / len(test)
        candidate_nll = sum(s[0] for s in candidate) / len(test)
        identity_brier = sum(s[1] for s in identity) / len(test)
        candidate_brier = sum(s[1] for s in candidate) / len(test)
        gain = identity_nll - candidate_nll
        brier_gain = identity_brier - candidate_brier
        folds.append({
            "scored_day": ordered[index],
            "train_day_count": index,
            "train_end_day": ordered[index - 1],
            "cell_count": len(test),
            "delta_c": delta,
            "width_lambda": lam,
            "identity_nll": identity_nll,
            "candidate_nll": candidate_nll,
            "identity_brier": identity_brier,
            "candidate_brier": candidate_brier,
            "gain": gain,
            "brier_gain": brier_gain,
        })
        total_gain += gain * len(test)
        total_brier_gain += brier_gain * len(test)
        total_cells += len(test)
        held_identity.extend(identity)
        held_candidate.extend(candidate)
        held_clusters.extend(
            (cell.get("station_id"), ordered[index]) for cell in test
        )
    # A fold gained when it gained on the primary score (charter 240; under
    # charter 228 it had to gain on both).
    positive = sum(1 for fold in folds if fold["gain"] > 0.0)
    return {
        "schema": "center_width_rolling_origin_v2",
        "min_fold_train_days": ROLLING_ORIGIN_MIN_FOLD_TRAIN_DAYS,
        "max_folds": ROLLING_ORIGIN_MAX_FOLDS,
        "fold_count": len(folds),
        "scored_cell_count": total_cells,
        "gain": (total_gain / total_cells) if total_cells else 0.0,
        "brier_gain": (total_brier_gain / total_cells) if total_cells else 0.0,
        "gate": _proper_score_gate(
            held_identity, held_candidate, held_clusters
        ),
        "folds_positive": positive,
        # A mean is not enough, and the first live run says why: on P0's ten
        # days to 2026-09-09 the folds picked four different pairs, two of
        # five gained, and the mean came out at +0.0066 -- over the hurdle
        # of the day -- because one hard day (09-09, identity NLL 2.02) swung
        # +0.0987 and carried three losing folds.  Admitting there would have
        # swapped one window's luck for one fold's.  So a majority of folds
        # must gain as well, which no single day can supply.  The station-day
        # interval does not replace it: one hard day moves all its stations
        # the same way, and the draws count that as many confirmations.
        "majority_positive": positive * 2 > len(folds) if folds else False,
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
    forward_clusters: Sequence[Any],
    pooled: tuple[float, float],
    floored: tuple[float, float],
    pooled_scores: Sequence[tuple[float, float]],
    floored_scores: Sequence[tuple[float, float]],
    incumbent_nll: float,
    incumbent_floor_rungs: float,
) -> dict[str, Any]:
    """Whether the floor earned its place, and the numbers that say so.

    Admitted only where it is significantly better than the *pooled*
    candidate on the ranked verdict over the held-out forward window
    (charter 240).  Beating the incumbent is not enough on its own: the
    floored fit carries a refitted lambda with it, and a lane that only wins
    by dragging that refit along has not shown that the floor is what did
    the work.  The pair as a whole then faces the incumbent at the usual
    gate, one rung up.
    """

    pooled_nll = (
        sum(s[0] for s in pooled_scores) / len(pooled_scores)
        if pooled_scores else 0.0
    )
    floored_nll = (
        sum(s[0] for s in floored_scores) / len(floored_scores)
        if floored_scores else 0.0
    )
    gate = _proper_score_gate(pooled_scores, floored_scores, forward_clusters)
    measured = bool(gate["lower"])
    # The verdict is recorded whatever the mode; only serving is switched.
    # A shadow that stopped measuring would have nothing to promote on.
    admitted = measured and SIGMA_FLOOR_MODE == "serve"
    out: dict[str, Any] = {
        "schema": "center_width_sigma_floor_lane_v2",
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
        "forward_nll_gain_vs_pooled": pooled_nll - floored_nll,
        "forward_nll_gain_vs_incumbent": incumbent_nll - floored_nll,
        "gate_rule": GATE_RULE,
        "gate_vs_pooled": gate,
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
    promotes itself the day the evidence clears the gate (charter 228).
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
    # Charter 239: the active row's slice, and the cells split by it.  While a
    # slice serves, its cells carry the slice's lambda, not the pair's, so the
    # pooled lane (veto, search, admission) reads the pooled cells only and
    # the slice lane reads its own; with no slice active every cell is the
    # pooled lane's, exactly as before.
    base_slices = (
        None if active is None else load_center_width_slices(conn, stage=stage_id)
    )
    # Charter 271: the active row's mode-to-shoulder shares.
    base_shoulder = (
        None if active is None
        else load_center_width_mode_shoulder(conn, stage=stage_id)
    )
    pooled_by_day, slice_by_day = _split_slice_cells(by_day)
    lane_by_day: Mapping[str, Sequence[Mapping[str, Any]]] = (
        pooled_by_day if base_slices else by_day
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
        # Charter 190: the axis the cells are scored on is serving's own.
        "open_edge_rule": OPEN_EDGE_TAIL_RULE,
        "served_open_edge_offset_c": SERVED_OPEN_EDGE_OFFSET_C,
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
    # Before anything else, and before the accumulating gate in particular:
    # holding the incumbent is only safe while it is still earning its place,
    # and the hold is exactly when nobody else is looking (charter 138).
    if active is not None and (base[0] != 0.0 or base[1] != 1.0):
        veto = _center_width_veto(
            lane_by_day,
            delta_c=base[0],
            width_lambda=base[1],
            activated_at_utc=_active_center_width_activated_at(
                conn, stage=stage_id
            ),
        )
        evidence["incumbent_veto"] = veto
        if veto["vetoed"]:
            evidence["basis"] = "INCUMBENT_VETOED"
            return {
                "stage": stage_id,
                "delta_c": 0.0,
                "width_lambda": 1.0,
                "sigma_floor_rungs": 0.0,
                "width_tiers": None,
                "width_slices": None,
                "mode_shoulder": None,
                "forward_nll_gain": -float(veto["undo_gain"]),
                "accumulating": accumulating,
                "vetoed": True,
                "serving_day_count": len(days),
                "serving_cell_count": len(cells),
                "evidence": evidence,
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
            "width_slices": base_slices,
            "mode_shoulder": base_shoulder,
            "forward_nll_gain": 0.0,
            "accumulating": True,
            "vetoed": False,
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
            "width_slices": base_slices,
            "mode_shoulder": base_shoulder,
            "forward_nll_gain": 0.0,
            "accumulating": False,
            "vetoed": False,
            "admitted": False,
            "serving_day_count": len(days),
            "serving_cell_count": len(cells),
            "evidence": evidence,
        }
    cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
    train_days, forward_days = days[:cut], days[cut:] or days[-1:]
    train = [cell for day in train_days for cell in lane_by_day.get(day, [])]
    forward = [cell for day in forward_days for cell in lane_by_day.get(day, [])]
    forward_clusters = [
        (cell.get("station_id"), day)
        for day in forward_days for cell in lane_by_day.get(day, [])
    ]
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
    pooled_scores = _cell_score_list(
        forward, delta_c=pooled_delta, width_lambda=pooled_lam
    )
    floored_scores = _cell_score_list(
        forward,
        delta_c=floor_delta,
        width_lambda=floor_lam,
        sigma_floor_rungs=SIGMA_FLOOR_RUNGS,
    )
    sigma_floor = _sigma_floor_lane(
        forward=forward,
        forward_clusters=forward_clusters,
        pooled=(pooled_delta, pooled_lam),
        floored=(floor_delta, floor_lam),
        pooled_scores=pooled_scores,
        floored_scores=floored_scores,
        incumbent_nll=forward_base,
        incumbent_floor_rungs=base_floor,
    )
    evidence["sigma_floor"] = sigma_floor
    if sigma_floor["admitted"]:
        delta, lam = floor_delta, floor_lam
        train_nll = floor_train_nll
        forward_candidate = sigma_floor["forward_nll_floor_candidate"]
        candidate_floor = SIGMA_FLOOR_RUNGS
    else:
        delta, lam = pooled_delta, pooled_lam
        train_nll = pooled_train_nll
        forward_candidate = sigma_floor["forward_nll_pooled_candidate"]
        candidate_floor = 0.0
    # Width tiers: fitted under the floor the pair will serve, judged against
    # the pooled/floored candidate on the ranked verdict (charter 240).
    tier_delta, tier_lambdas, _tier_train = _search_tiers(
        train, sigma_floor_rungs=candidate_floor, pooled_lambda=lam,
    )
    width_tier_lane = _width_tier_lane(
        forward=forward,
        forward_clusters=forward_clusters,
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
        forward_candidate = width_tier_lane["forward_tier_candidate"]["logscore"]
    # The admission number.  A sub-lane that took the pair off the plain
    # scalar search keeps its own single-window verdict below; the question
    # here is whether the search beats what is served at all, and that is the
    # one the single window was deciding by coin toss.
    rolling = _rolling_origin_admission(
        lane_by_day, days, sigma_floor_rungs=candidate_floor
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
        "forward_brier_gain": float(rolling["brier_gain"]),
        "gate_rule": GATE_RULE,
        # What the single trailing window would have said, kept because the
        # two disagreeing is the reading a reviewer wants, not a detail.
        "single_window_nll_gain": single_window_gain,
        "identity_nll_all": _mean_nll(cells, delta_c=0.0, width_lambda=1.0),
        "candidate_nll_all": _mean_nll(
            cells,
            delta_c=delta,
            width_lambda=lam,
            sigma_floor_rungs=candidate_floor,
        ),
    })
    # Both halves of the rolling verdict: the held-out cells are
    # better under the ranked verdict (charter 240), and a
    # majority of folds gained on both.  The second is not a refinement --
    # without it one hard day can carry three losing folds over the line,
    # which is the first live run's reading exactly.
    admitted = (
        bool(rolling["gate"]["lower"])
        and bool(rolling["majority_positive"])
    )
    if candidate_tiers is not None:
        # The tier lane's own gate already beat the pooled candidate; what
        # it must beat at this rung is the incumbent.  Its own window is the
        # single one, so the fold majority does not speak to it; the pair it
        # rides on has already passed the gate above.
        admitted = bool(width_tier_lane["gate_vs_incumbent"]["lower"])
    # Charter 239 / 248: the thin-pool far-lead slice -- a tail mix on the stage
    # pair since 248, judged under the stage's ranked verdict.
    # The veto first, for the reason the pooled veto runs first: an active
    # slice is only safe while it is still earning its place.  Then the lane,
    # against whatever a slice cell would be served without a slice this
    # round -- the pooled candidate's residual if that is admitted, identity
    # (the incumbent pair, already in the cell) otherwise.
    served_slices: Mapping[str, Mapping[str, Any]] | None = base_slices
    slice_veto: dict[str, Any] | None = None
    if base_slices:
        slice_veto = _thin_far_slice_veto(
            slice_by_day, slices=base_slices,
            activated_at_utc=_active_center_width_activated_at(
                conn, stage=stage_id
            ),
        )
        evidence["width_slice_veto"] = slice_veto
    if slice_veto is not None and slice_veto["vetoed"]:
        served_slices = None
        evidence["width_slices"] = {
            "schema": "center_width_thin_far_slice_lane_v1",
            "slice": THIN_FAR_SLICE_NAME,
            "admitted": False,
            "state": "INCUMBENT_VETOED",
        }
    else:
        slice_lane = _thin_far_slice_lane(
            pooled_by_day=pooled_by_day,
            slice_by_day=slice_by_day,
            days=days,
            cut=cut,
            pooled_admitted=admitted,
            served_residual=(delta, lam) if admitted else RESIDUAL_IDENTITY,
            sigma_floor_rungs=candidate_floor if admitted else base_floor,
            incumbent_slices=base_slices,
        )
        evidence["width_slices"] = slice_lane
        if slice_lane["admitted"]:
            served_slices = {
                THIN_FAR_SLICE_NAME: _slice_entry(
                    slice_lane["candidate_tail_mix_weight"]
                )
            }
    # Charter 271: the mode-to-shoulder share per horizon bin, on top of
    # whatever pair this round serves, judged under the stage's ranked verdict.
    shoulder_lane = _mode_shoulder_lane(
        by_day=by_day,
        days=days,
        served_residual=(delta, lam) if admitted else RESIDUAL_IDENTITY,
        sigma_floor_rungs=candidate_floor if admitted else base_floor,
        incumbent=base_shoulder,
        activated_at_utc=_active_center_width_activated_at(conn, stage=stage_id),
    )
    evidence["mode_shoulder"] = shoulder_lane
    # Charter 253: the far-peak ceiling's nightly reading, on every cell a
    # ceiling was served to.  A record; nothing below reads it.
    if stage_id == "P0":
        evidence["far_peak_ceiling"] = _far_peak_ceiling_reading(by_day)
        evidence["f_far_tail_mix"] = _f_far_tail_reading(by_day)
    # The TAF-conditional lane (charter 57, distance axis): each cell fits
    # its own scalars from its own slice of the same train/forward split --
    # the shift along each day's TAF direction, the width unsigned -- and is
    # admitted only where it beats the POOLED candidate on its own forward
    # cells: the pooled pair is what would serve that day otherwise, so
    # beating identity is not enough.  A cell short of the layer's own
    # hurdles stays out and its days keep serving the pooled scalars;
    # nothing is a constant, and the report shows each cell accumulate
    # until it promotes itself.  Only the stages that serve the cells fit
    # them.
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
    return {
        "stage": stage_id,
        "delta_c": absolute_delta if admitted else base[0],
        "width_lambda": absolute_lambda if admitted else base[1],
        # The floor travels with the pair it qualifies.  A refused candidate
        # leaves the incumbent's floor in place for the same reason it leaves
        # the incumbent's scalars: nothing was shown to be better.
        "sigma_floor_rungs": candidate_floor if admitted else base_floor,
        "width_tiers": candidate_tiers if admitted else base_tiers,
        # Charter 239: the slice that will serve -- admitted this round, kept
        # from the incumbent, or dropped by its veto -- beside the pair.
        "width_slices": dict(served_slices) if served_slices else None,
        # Charter 271: the share table that will serve beside the pair.
        "mode_shoulder": shoulder_lane["served_table"],
        "candidate_mode_shoulder": shoulder_lane["candidate_table"],
        "candidate_width_slices": (
            {THIN_FAR_SLICE_NAME: _slice_entry(
                evidence["width_slices"]["candidate_tail_mix_weight"]
            )}
            if evidence["width_slices"].get("candidate_tail_mix_weight") is not None
            else None
        ),
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
        "vetoed": False,
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
    sigma_floor_rungs: float = 0.0,
    width_tiers: Mapping[str, Any] | None = None,
    width_slices: Mapping[str, Mapping[str, Any]] | None = None,
    mode_shoulder: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    digest = center_width_sha256(
        stage=stage_id, delta_c=delta_c, width_lambda=width_lambda,
        sigma_floor_rungs=sigma_floor_rungs,
        width_tiers=width_tiers,
        width_slices=width_slices,
        mode_shoulder=mode_shoulder,
    )
    canonical_shoulder = _canonical_mode_shoulder(mode_shoulder)
    shoulder_json = (
        json.dumps(canonical_shoulder, sort_keys=True)
        if canonical_shoulder else None
    )
    slices_json = (
        json.dumps(
            {
                str(name): {
                    "max_seated_systems": int(entry["max_seated_systems"]),
                    "min_hours_to_settlement": float(
                        entry["min_hours_to_settlement"]
                    ),
                    "tail_mix_weight": float(entry["tail_mix_weight"]),
                    "tail_mix_scale": float(entry["tail_mix_scale"]),
                }
                for name, entry in sorted(width_slices.items())
            },
            sort_keys=True,
        )
        if width_slices else None
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
    # The TAF-cell lane (charter 57) retired with P0-M's centre/width layer
    # (charter 150); the column stays for the rows written before.
    cells_json = None
    # Explicit acquisition rather than the implicit transaction the first
    # INSERT would open: the retry budget is the point, not the isolation.
    _begin_immediate(conn)
    try:
        conn.execute(
            "INSERT INTO calibration_center_width_table ("
            " center_width_sha256, stage, delta_c, width_lambda,"
            " evidence_json,"
            " fitted_at_utc, serving_day_count, activated, activated_at_utc,"
            " taf_cells_json, sigma_floor_rungs, width_tiers_json,"
            " width_slices_json, mode_shoulder_json"
            ") VALUES (?,?,?,?,?,?,?,0,NULL,?,?,?,?,?)"
            " ON CONFLICT(center_width_sha256) DO UPDATE SET"
            "  evidence_json=excluded.evidence_json,"
            "  fitted_at_utc=excluded.fitted_at_utc,"
            "  serving_day_count=excluded.serving_day_count,"
            "  taf_cells_json=excluded.taf_cells_json,"
            "  sigma_floor_rungs=excluded.sigma_floor_rungs,"
            "  width_tiers_json=excluded.width_tiers_json,"
            "  width_slices_json=excluded.width_slices_json,"
            "  mode_shoulder_json=excluded.mode_shoulder_json",
            (
                digest, stage_id, float(delta_c), float(width_lambda),
                json.dumps(
                    dict(evidence), ensure_ascii=False, sort_keys=True
                ),
                now, int(serving_day_count), cells_json,
                float(sigma_floor_rungs) or None, tiers_json, slices_json,
                shoulder_json,
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


def retire_center_width(
    conn: sqlite3.Connection,
    *,
    stage: str,
    retired_at_utc: str | None = None,
) -> dict[str, Any]:
    """Deactivate this stage's pooled scalars AND stamp them retired.

    Two effects, and the second is the one that matters: deactivating stops
    the unpinned readers, the stamp stops the pinned ones.  Every earlier
    version of the stage is stamped with it, not just the active one -- the
    veto is a verdict on the contraction as served, and an older version
    still pinned by a live artifact carried the same contraction from the
    same evidence (charter 138; the band store's ``retire_band_table`` and
    the station lane's deactivation path answer this the same way).
    """

    stage_id = _check_stage(stage)
    ensure_center_width_schema(conn)
    now = retired_at_utc or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    active = conn.execute(
        "SELECT center_width_sha256 FROM calibration_center_width_table"
        " WHERE activated=1 AND stage=?",
        (stage_id,),
    ).fetchone()
    _begin_immediate(conn)
    try:
        cursor = conn.execute(
            "UPDATE calibration_center_width_table"
            " SET activated=0, activated_at_utc=NULL, retired_at_utc=?"
            " WHERE stage=? AND retired_at_utc IS NULL",
            (now, stage_id),
        )
        retired = int(cursor.rowcount or 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "state": "RETIRED" if retired else "UNCHANGED",
        "center_width_sha256": None if active is None else str(active[0]),
        "retired_at_utc": now,
        "retired_row_count": retired,
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
    # The activated row hashes the width floor and tiers too, so a journal
    # digest that omits either can never match the row it is evidence for.
    digest = center_width_sha256(
        stage=stage_id,
        delta_c=fitted["delta_c"],
        width_lambda=fitted["width_lambda"],
        sigma_floor_rungs=float(fitted.get("sigma_floor_rungs") or 0.0),
        width_tiers=fitted.get("width_tiers") or None,
        width_slices=fitted.get("width_slices") or None,
        mode_shoulder=fitted.get("mode_shoulder") or None,
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
    retired = [stage for stage in wanted if stage not in CENTER_WIDTH_SERVED_STAGES]
    wanted = tuple(stage for stage in wanted if stage in CENTER_WIDTH_SERVED_STAGES)
    for stage in retired:
        results[stage] = {"stage": stage, "served_stage": False}
        if commit:
            try:
                results[stage]["commit"] = retire_center_width(conn, stage=stage)
            except Exception as exc:  # noqa: BLE001
                results[stage]["error"] = f"{type(exc).__name__}: {exc}"
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


def _active_slices_in_retired_form(
    conn: sqlite3.Connection, *, stage: str
) -> bool:
    """Whether the stage's active row names a slice entry the loader no
    longer reads (charter 239's ``width_lambda`` form, retired by 248)."""

    try:
        row = conn.execute(
            "SELECT width_slices_json FROM calibration_center_width_table"
            " WHERE activated=1 AND stage=?",
            (_check_stage(stage),),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None or not row[0]:
        return False
    try:
        slices = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return False
    if not isinstance(slices, Mapping):
        return False
    return any(
        isinstance(entry, Mapping) and entry.get("tail_mix_weight") is None
        for entry in slices.values()
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
                "vetoed": fitted.get("vetoed", False),
                "incumbent_veto": fitted["evidence"].get("incumbent_veto"),
                "admitted": fitted.get("admitted", False),
                "served_stage": stage in CENTER_WIDTH_SERVED_STAGES,
                "active_sha256": None if active is None else active[2],
                "serving_day_count": fitted["serving_day_count"],
                "serving_cell_count": fitted["serving_cell_count"],
                "min_decision_at_utc": min_decision_at_utc,
                # The floor rides on the report so the daily run shows it
                # admitted, refused or accumulating without anyone reading
                # the journal's evidence blob by hand.
                "sigma_floor_rungs": fitted.get("sigma_floor_rungs") or 0.0,
                "sigma_floor": fitted["evidence"].get("sigma_floor") or {},
                "width_tiers": fitted.get("width_tiers"),
                "candidate_width_tiers": fitted.get("candidate_width_tiers"),
                "width_tier_lane": fitted["evidence"].get("width_tiers") or {},
                # Charter 239: the slice beside the pair, and its lane.
                "width_slices": fitted.get("width_slices"),
                "candidate_width_slices": fitted.get("candidate_width_slices"),
                "width_slice_lane": fitted["evidence"].get("width_slices") or {},
                "width_slice_veto": fitted["evidence"].get("width_slice_veto"),
            })
            # Charter 239: a slice admitted or vetoed is a row change even
            # when the pooled pair stands, so the pair is re-persisted with
            # the new slice (a new sha) rather than held under REFUSED.
            active_slices = (
                load_center_width_slices(conn, stage=stage)
                if active is not None else None
            )
            slices_changed = (
                (fitted.get("width_slices") or None) != (active_slices or None)
            )
            # Charter 248: an active row still naming a slice of the retired
            # 239 form reads as no slice but is a different row from one
            # that names none; the first run under 248 rewrites it.
            if active is not None and _active_slices_in_retired_form(
                conn, stage=stage
            ):
                slices_changed = True
            # Charter 271: a share admitted or vetoed is a row change too.
            active_shoulder = (
                load_center_width_mode_shoulder(conn, stage=stage)
                if active is not None else None
            )
            if (fitted.get("mode_shoulder") or None) != (active_shoulder or None):
                slices_changed = True
            entry["mode_shoulder"] = fitted.get("mode_shoulder")
            entry["candidate_mode_shoulder"] = fitted.get("candidate_mode_shoulder")
            entry["mode_shoulder_lane"] = {
                name: {k: v for k, v in lane.items() if k != "folds"}
                for name, lane in (
                    (fitted["evidence"].get("mode_shoulder") or {}).get("bins") or {}
                ).items()
            }
            if commit:
                if stage not in CENTER_WIDTH_SERVED_STAGES:
                    outcome = "NOT_WIRED"
                elif fitted.get("vetoed", False):
                    # Ahead of the accumulating branch, because that branch
                    # is the hold the veto exists to interrupt.
                    entry["commit"] = retire_center_width(conn, stage=stage)
                    outcome = "INCUMBENT_VETOED"
                elif fitted["accumulating"]:
                    outcome = "ACCUMULATING"
                elif not fitted.get("admitted", False) and not slices_changed:
                    outcome = "REFUSED_FORWARD"
                else:
                    committed = persist_and_activate_center_width(
                        conn,
                        stage=stage,
                        delta_c=fitted["delta_c"],
                        width_lambda=fitted["width_lambda"],
                        evidence=fitted["evidence"],
                        serving_day_count=fitted["serving_day_count"],
                        sigma_floor_rungs=float(
                            fitted.get("sigma_floor_rungs") or 0.0
                        ),
                        width_tiers=fitted.get("width_tiers") or None,
                        width_slices=fitted.get("width_slices") or None,
                        mode_shoulder=fitted.get("mode_shoulder") or None,
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

