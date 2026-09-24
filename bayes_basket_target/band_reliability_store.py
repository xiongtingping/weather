"""DB-resident, content-addressed band reliability tables, one per stage.

The band factors used to be code constants gated by human review; the
review step is gone (user ruling, 2026-08-15) and this module is what
replaced it.  The daily calibration cron fits a fresh table per stage from
accumulated truth under the same admission rule the constants were chosen
by, persists it content-addressed, and activates it.  Nothing here loosens
the rule -- automation replaced the reviewer, not the hurdles.

Three stages, fitted independently, because they are three different
distributions and their defects are measurably different:

* ``P0``   the cross-ensemble blend at decision time.
* ``P0-M`` the same root confirmed against the morning's TAF revision.
* ``P1``   the root multiplied by the day's observation likelihood.

P0-M and P1 are Bayesian posteriors *of the P0 root*, so P0's correction
already propagates into them; a stage table therefore fits the residual
its own likelihood step leaves behind, never the whole defect twice.  That
is also why P1 measures better calibrated than P0 and mostly serves
identity today -- which is the correct outcome, not a missing feature.

Every stage is fitted every day from the day the stage has any settled
history at all, long before it can qualify.  A stage that cannot clear the
hurdles serves an identity table and records how far off it is, so the
progress is visible and the promotion happens by itself the day the
evidence arrives.  Nothing about a stage's arrival needs a code change.

The invariant that shapes the design: an artifact must be served with the
exact table its evidence was scored against.  Every candidate scored
against a DB table carries ``band_table_sha256=<sha>``, and serving loads
that pinned version -- never "the latest".  Artifacts carrying the bare
tag predate the store and keep serving the static bootstrap constants in
``cross_consensus``.

Admission rule per cell (fixed; the table changes, the rule does not):

* serving lane (this stage's own settled snapshots): by-day bootstrap
  interval excludes 1.0 AND the chronological half-split keeps the
  miscalibration sign -> serve the interval end nearest one.
* else transfer lane (P0 only, which has a gate geometry to transfer
  from): the gate archive qualifies the cell, its direction agrees with
  the serving lane's point estimate, and adding the cell improves NLL on
  the serving lane's trailing forward window.
* else identity.

Stages without an OOS gate (P0-M, P1) carry an extra hurdle: every
non-identity cell must also improve the trailing forward window.  For P0
that check is what the OOS gate already does per scope; for a stage whose
distribution nothing re-scores, it has to happen here.

Sample growth: post-band serving rows are NOT excluded -- their receipts
record the applied factors and the pre-normalisation mass, so the pre-band
claim is reconstructed exactly and the fitting sample keeps growing.  The
gate lane cannot be inverted (its predictions carry no receipt), so banded
gate rows are excluded; that lane only confirms transfer direction.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import log
from typing import Any

from .contracts import ContractError, content_sha256
from .station_day_ladder import settled_rung, station_day_ladder

BAND_STAGES = ("P0", "P0-M", "P1")
# Charter 150 (2026-09-19): only P0 serves a band table.  P0-M and P1 are
# one likelihood update each on a calibrated root and carry no post-hoc
# correction; the refit retires whatever table they still had active and
# fits nothing for them.  ``BAND_STAGES`` stays the vocabulary so the
# journal and the retired rows keep reading.
BAND_SERVED_STAGES = ("P0",)
# Five-percent cells capped at 50% (user ruling, 2026-08-25, charter 60):
# the fitting partition itself, not just the page that renders it, runs on
# the same 5% grid as the section-six audit table, with everything claimed
# at half or more pooled into one open top cell.  The admission rule per
# cell is untouched -- thinner cells simply take longer to qualify and
# promote themselves as evidence accumulates, which is the store's
# standing contract.  Tables fitted under the old five-cell partition keep
# serving the artifacts pinned to them; nothing is rewritten.
BAND_TABLE_CELLS = tuple(
    (round(step * 0.05, 2), round(step * 0.05 + 0.05, 2))
    for step in range(10)
) + ((0.50, 1.01),)
# Charter 105's axis.  The disease this table treats is over-dispersion --
# too little mass on the centre, too much four buckets out -- and that is a
# statement about POSITION.  Keying the cells on the claim costs two things:
# the key is not invariant under the map (a bucket claiming 0.44 lands in
# [0.40,0.45), gets multiplied, renormalises to 0.46 and belongs to a cell
# whose factor it was never scored under), and it pools rungs that behave
# differently (on 2026-09-05 the [5-10%) cell held VILK one step off centre,
# ZHHH three off and ZHHH five off; they realised 1.151, 0.783 and 0.490).
# Distance from the peak is a fixed point of the map and separates them.
BAND_DISTANCE_CELLS = (
    (0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 99.0),
)
BAND_AXIS_CLAIM = "claim"
BAND_AXIS_CENTRE_DISTANCE = "centre_distance"
BAND_AXES = (BAND_AXIS_CLAIM, BAND_AXIS_CENTRE_DISTANCE)


def cells_for_axis(axis: str) -> tuple[tuple[float, float], ...]:
    """The partition an axis is fitted on.  One place, so a fit and a serve
    cannot disagree about how many cells a table has."""

    return (
        BAND_DISTANCE_CELLS
        if str(axis) == BAND_AXIS_CENTRE_DISTANCE
        else BAND_TABLE_CELLS
    )


# The diagnostics grid is the fitting grid now that the fitting grid is
# fine enough to diagnose with.
FINE_BANDS = BAND_TABLE_CELLS
# A qualified cell still serves within these bounds.  The interval end
# nearest one is modest by construction; the clamp only guards a future
# degenerate fit (a handful of days, a wild interval) from serving an
# extreme factor without anyone in the loop.
FACTOR_FLOOR = 0.5
FACTOR_CEILING = 2.0
# Two-sided width of the by-day bootstrap interval whose near end a
# qualified cell serves.  0.05 is 95%, the rule as shipped.
DEFAULT_INTERVAL_ALPHA = 0.05
FORWARD_WINDOW_FRACTION = 0.4
# Below this a stage is fitted and journalled but serves identity: ten
# days is where the by-day bootstrap stops being a re-description of two
# or three weather regimes.  A stage under it is accumulating, not broken.
MIN_QUALIFYING_DAYS = 10
# A cell may also be a handful of stations repeating themselves.  The day
# bootstrap prices the day-to-day variation of a pooled ratio and is blind
# to that: a factor earned on four stations is a per-station correction
# wearing the fleet's name, and the shadow lane's own numbers say the fleet
# does not agree with itself across stations (distance [2,3) realises 1.272
# warm against 0.876 cold).  Thirty is two thirds of the forty-five-station
# fleet -- enough that the cell describes the fleet rather than a corner of
# it, and low enough that an ordinary cell clears it.
MIN_CELL_STATIONS = 30
# The store has one write lock and the resident basket worker owns it for
# about half of every production cycle, in bursts: probed against the live
# store on 2026-08-21, ``BEGIN IMMEDIATE`` waited 3.6-9.1s (six samples)
# to get in, while 19 of 20 attempts capped at 3s never got in at all.
# The cron already waits a minute per attempt (``PRAGMA busy_timeout``),
# which is 6-17x the measured wait and normally ample -- it has lost the
# race once, on 2026-08-20.  Retrying is the same lever as a longer
# busy_timeout, with two differences worth having: the budget is bounded
# and countable (the attempt count lands in the report, so a run that
# nearly missed reads differently from one that walked in), and each
# retry re-enters SQLite's race from a fresh handler rather than sitting
# out the whole wait in one poll loop that has no fairness guarantee.
WRITE_LOCK_ATTEMPTS = 4
WRITE_LOCK_BACKOFF_SECONDS = 5.0

_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_band_reliability_table ("
    " band_table_sha256 TEXT PRIMARY KEY"
    "  CHECK(length(band_table_sha256)=64),"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
    " factors_json TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " serving_day_count INTEGER NOT NULL,"
    " activated INTEGER NOT NULL CHECK(activated IN (0,1)),"
    " activated_at_utc TEXT,"
    # Set when the lane VETOES this table -- not when a better one replaces
    # it.  The distinction is the whole point: a superseded table keeps
    # serving the artifacts pinned to it, because the pin exists so serving
    # reproduces what the gate scored; a vetoed one is settled truth that
    # the table was wrong, and honouring the pin would keep it in service
    # for as long as the oldest artifact lives (2026-09-24 at the time this
    # was written).  The station-width lane learned this the hard way on
    # 2026-09-10: retiring its tables changed nothing on the serving path
    # for eighteen hours.
    " retired_at_utc TEXT"
    ")"
)
# Every fit, activated or not.  The active table says what is served; this
# says what was known on the day it was chosen -- which cells were close,
# which hurdle each one failed, how much sample each had.  A replay of "why
# did we serve that in mid-August" reads this, not a diff of constants.
_JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_band_reliability_fit_journal ("
    " fit_id TEXT PRIMARY KEY,"
    " stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),"
    " fitted_at_utc TEXT NOT NULL,"
    " band_table_sha256 TEXT NOT NULL CHECK(length(band_table_sha256)=64),"
    " serving_day_count INTEGER NOT NULL,"
    " serving_station_day_count INTEGER NOT NULL,"
    " qualifying_cell_count INTEGER NOT NULL,"
    # FIT_NOT_ACTIVATED: the fit itself succeeded and only the activation
    # failed (in practice: lost the write-lock race to the resident).  The
    # journal's whole purpose is recording what was known on the day it was
    # chosen, and a fit that was made is known whether or not it served.
    " outcome TEXT NOT NULL CHECK(outcome IN"
    "  ('ACTIVATED','UNCHANGED','ACCUMULATING','FIT_NOT_ACTIVATED',"
    "   'INCUMBENT_VETOED')),"
    " factors_json TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL"
    ")"
)
_JOURNAL_STAGE_INDEX = (
    "CREATE INDEX IF NOT EXISTS"
    " idx_band_fit_journal_stage_time"
    " ON calibration_band_reliability_fit_journal(stage, fitted_at_utc)"
)


_JOURNAL_OUTCOMES = (
    "ACTIVATED", "UNCHANGED", "ACCUMULATING", "FIT_NOT_ACTIVATED",
    "INCUMBENT_VETOED",
)


def ensure_band_table_schema(conn: sqlite3.Connection) -> None:
    # Every read path calls this, and some of those connections are opened
    # read-only (dashboards, audits, probes).  A migration such a connection
    # cannot run is not that connection's problem to solve: the next writer
    # runs it for everyone, and until then a reader must keep reading rather
    # than fail on a column it was not going to use.  The journal rebuild
    # below has returned on readonly since it was written; the ALTERs did
    # not, and adding a column here would have crashed every read-only
    # reader of a store the sweep had not reached yet.
    try:
        conn.execute(_TABLE_DDL)
        conn.execute(_JOURNAL_DDL)
        conn.execute(_JOURNAL_STAGE_INDEX)
    except sqlite3.OperationalError as exc:
        if "readonly" in str(exc).lower():
            return
        raise
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(calibration_band_reliability_table)"
        )
    }
    pending = []
    if "stage" not in columns:
        # Pre-stage bootstrap rows were all P0 by construction.
        pending.append(
            "ALTER TABLE calibration_band_reliability_table"
            " ADD COLUMN stage TEXT NOT NULL DEFAULT 'P0'"
        )
    if "retired_at_utc" not in columns:
        pending.append(
            "ALTER TABLE calibration_band_reliability_table"
            " ADD COLUMN retired_at_utc TEXT"
        )
    for statement in pending:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "readonly" in str(exc).lower():
                return
            raise
    if "axis" not in columns:
        # Which quantity the cells are read from (user ruling, 2026-09-07:
        # "band 表换轴").  Rows written before the column existed were all
        # fitted on the claim, so that is the default and no stored table
        # changes meaning.  It travels with the table rather than with the
        # artifact because charter 60 already pins the table by sha: a
        # pinned table that could not say which axis it was scored on would
        # be served through the wrong lookup, which is the one way this
        # change could reach an artifact it was never gated against.
        try:
            conn.execute(
                "ALTER TABLE calibration_band_reliability_table"
                " ADD COLUMN axis TEXT NOT NULL DEFAULT 'claim'"
            )
        except sqlite3.OperationalError as exc:
            if "readonly" in str(exc).lower():
                return
            raise
    # SQLite cannot ALTER a CHECK, so a journal built before an outcome
    # existed is rebuilt in place around the widened one -- by migration,
    # every row crossing, because this table IS the record and dropping it
    # would be the exact loss it exists to prevent.  Read-only connections
    # skip it: they cannot write the new outcome either, and the next
    # writer migrates for everyone.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='calibration_band_reliability_fit_journal'"
    ).fetchone()
    live = str((row or [""])[0] or "")
    if live and any(f"'{name}'" not in live for name in _JOURNAL_OUTCOMES):
        try:
            _begin_immediate(conn)
        except sqlite3.OperationalError as exc:
            if "readonly" in str(exc).lower():
                return
            raise
        try:
            conn.execute(
                "ALTER TABLE calibration_band_reliability_fit_journal"
                " RENAME TO calibration_band_reliability_fit_journal_old"
            )
            conn.execute(_JOURNAL_DDL)
            conn.execute(
                "INSERT INTO calibration_band_reliability_fit_journal"
                " SELECT fit_id, stage, fitted_at_utc, band_table_sha256,"
                "  serving_day_count, serving_station_day_count,"
                "  qualifying_cell_count, outcome, factors_json,"
                "  evidence_json"
                " FROM calibration_band_reliability_fit_journal_old"
            )
            conn.execute(
                "DROP TABLE calibration_band_reliability_fit_journal_old"
            )
            conn.execute(_JOURNAL_STAGE_INDEX)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _check_stage(stage: str) -> str:
    value = str(stage)
    if value not in BAND_STAGES:
        raise ContractError("band_reliability_stage_invalid")
    return value


def _begin_immediate(conn: sqlite3.Connection) -> int:
    """Take the write lock from the resident, returning attempts spent.

    Each attempt already waits out the connection's own ``busy_timeout``;
    this only decides how many such waits we are willing to spend before
    giving up on a writer that is not letting go.  Contention is the only
    thing retried -- any other operational error is a real fault and is
    raised on the first try rather than being sat on for minutes.
    """

    attempt = 0
    while True:
        attempt += 1
        try:
            conn.execute("BEGIN IMMEDIATE")
            return attempt
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            contended = "locked" in message or "busy" in message
            if not contended or attempt >= WRITE_LOCK_ATTEMPTS:
                raise
        time.sleep(WRITE_LOCK_BACKOFF_SECONDS)


def _canonical_factors(
    factors: Sequence[tuple[tuple[float, float], float]],
) -> list[list[Any]]:
    return [
        [[float(lower), float(upper)], float(factor)]
        for (lower, upper), factor in factors
    ]


def band_table_sha256(
    factors: Sequence[tuple[tuple[float, float], float]],
    *,
    stage: str = "P0",
    axis: str = BAND_AXIS_CLAIM,
) -> str:
    """Identity of a table.

    The stage is part of the content so an identity table for P1 can never
    be mistaken for an identity table for P0: they are served on different
    distributions and their evidence is not interchangeable.
    """

    axis_id = str(axis)
    if axis_id not in BAND_AXES:
        raise ValueError(f"unknown band axis: {axis_id}")
    identity: dict[str, Any] = {
        "schema": "band_reliability_table_v1",
        "stage": _check_stage(stage),
        "factors": _canonical_factors(factors),
    }
    # The axis enters the identity only when it is not the shipped one, so
    # every table already on the book keeps the sha the artifacts pinned to
    # it were gated against.  Without this a distance table whose factors
    # happen to match a claim table would collide with it and re-activate
    # the wrong lookup under the right hash.
    if axis_id != BAND_AXIS_CLAIM:
        identity["axis"] = axis_id
    return content_sha256(identity)


def utc_instant(value: str) -> "datetime":
    """An ISO-8601 UTC string as an aware instant.

    The stores write timestamps with and without fractional seconds, so the
    strings do not order as instants: ``...T15:27:09.831Z`` sorts before
    ``...T15:27:09Z`` because ``.`` precedes ``Z``.  Every as-of comparison
    goes through here.
    """

    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _pinned_identity_holds(
    sha: str,
    factors: Sequence[tuple[tuple[float, float], float]],
    *,
    stage: str,
    axis: str,
) -> bool:
    """Whether the stored bytes are the table the sha names.

    Every table since the stage column hashes through ``band_table_sha256``.
    The P0 bootstrap rows written before it hashed the same factors without
    a stage; they were P0 by construction and are claim-axis, so that one
    older identity is accepted for exactly that stage and axis and no other.
    Measured 2026-09-13: 119 of 120 stored tables recompute under the current
    identity, the 120th (9963e3fe, fitted 2026-08-15) under the pre-stage one.
    """

    if band_table_sha256(factors, stage=stage, axis=axis) == sha:
        return True
    return (
        stage == "P0"
        and axis == BAND_AXIS_CLAIM
        and content_sha256({
            "schema": "band_reliability_table_v1",
            "factors": _canonical_factors(factors),
        }) == sha
    )


def _factors_are_legal(
    factors: Sequence[tuple[tuple[float, float], float]],
) -> bool:
    return all(
        math.isfinite(lower) and math.isfinite(upper) and lower < upper
        and math.isfinite(factor) and factor > 0.0
        for (lower, upper), factor in factors
    )


def load_band_table(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    table_sha256: str | None = None,
    as_of_utc: str | None = None,
) -> tuple[tuple[tuple[tuple[float, float], float], ...], str] | None:
    """The stage's active table, or the pinned version an artifact names.

    A pinned sha that is missing fails closed: the artifact's scored math
    cannot be reproduced, so it must not be served an approximation.

    A pinned sha is also held to what it claims to be (model-layer review
    2026-09-13, R6): the row must belong to the caller's stage, its factors
    must be legal, and its bytes must hash to the sha.  The lookup used to
    trust the string, so a P1 table loaded cleanly as P0.

    ``as_of_utc`` answers the retirement question at a moment instead of now.
    Serving passes nothing and stops using a vetoed table the moment the veto
    lands, which is the design.  A replay of a past decision passes that
    decision's time, so the same pin reads the same factors whatever day the
    replay runs on.  It governs only a pinned read; which table was ACTIVE at
    a moment is ``band_table_as_of``'s question.

    Two elements, as it has always returned: this function has nine callers
    and widening it broke four of them the first time it was tried.  A
    serving lane that must know which lookup to apply the table through
    calls ``load_band_table_with_axis`` instead.
    """

    ensure_band_table_schema(conn)
    if table_sha256 is None:
        row = conn.execute(
            "SELECT band_table_sha256, factors_json, COALESCE(axis, ?)"
            " FROM calibration_band_reliability_table"
            " WHERE activated=1 AND stage=?",
            (BAND_AXIS_CLAIM, _check_stage(stage)),
        ).fetchone()
        if row is None:
            return None
    else:
        try:
            row = conn.execute(
                "SELECT band_table_sha256, factors_json, COALESCE(axis, ?),"
                " retired_at_utc, stage"
                " FROM calibration_band_reliability_table"
                " WHERE band_table_sha256=?",
                (BAND_AXIS_CLAIM, str(table_sha256)),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "retired_at_utc" not in str(exc):
                raise
            # A store the migration has not reached: the resident serves
            # without running the schema, so between a deploy and the next
            # sweep the column is absent.  That is not a missing table --
            # read it exactly as before the column existed, never retired,
            # rather than failing closed on every artifact that pins one.
            # The station-width lane shut nine decisions out this way on
            # 2026-09-10 before the same guard was added there.
            legacy = conn.execute(
                "SELECT band_table_sha256, factors_json, COALESCE(axis, ?),"
                " stage"
                " FROM calibration_band_reliability_table"
                " WHERE band_table_sha256=?",
                (BAND_AXIS_CLAIM, str(table_sha256)),
            ).fetchone()
            row = (*legacy[:3], None, legacy[3]) if legacy else None
        if row is None:
            raise ContractError("band_reliability_pinned_table_missing")
        requested_stage = _check_stage(stage)
        if str(row[4]) != requested_stage:
            raise ContractError("band_reliability_pinned_table_stage_mismatch")
        pinned_factors = tuple(
            ((float(cell[0][0]), float(cell[0][1])), float(cell[1]))
            for cell in json.loads(row[1])
        )
        if not _factors_are_legal(pinned_factors):
            raise ContractError("band_reliability_pinned_table_factors_invalid")
        if not _pinned_identity_holds(
            str(table_sha256), pinned_factors,
            stage=requested_stage, axis=str(row[2]),
        ):
            raise ContractError("band_reliability_pinned_table_content_mismatch")
        if row[3] and (
            as_of_utc is None
            or utc_instant(row[3]) <= utc_instant(as_of_utc)
        ):
            # Pinned to a version the lane VETOED.  The pin exists so that
            # serving reproduces the table the gate scored; the veto is the
            # settled finding that the table was wrong.  Honouring the pin
            # would keep a retired table in service for the life of the
            # oldest artifact, so this serves identity and keeps the sha so
            # the receipt can say which version was retired out from under
            # it.  A table merely SUPERSEDED by a better one carries no
            # retirement stamp and is served exactly as before.
            return (), str(row[0])
    factors = tuple(
        ((float(cell[0][0]), float(cell[0][1])), float(cell[1]))
        for cell in json.loads(row[1])
    )
    return factors, str(row[0])


def load_band_table_with_axis(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    table_sha256: str | None = None,
    as_of_utc: str | None = None,
) -> tuple[
    tuple[tuple[tuple[float, float], float], ...], str, str
] | None:
    """``load_band_table`` plus the axis its cells are keyed on.

    The axis rides out WITH the table because it is a property of the table
    and not of whoever is serving it (charter 105, extending charter 60's
    pinning).  A lane that reads the factors and decides the lookup for
    itself can apply a distance-fitted table through the claim lookup, which
    is the one way this change reaches an artifact it was never gated
    against.  ``claim`` for every row written before the column existed, so
    no stored table changes meaning.
    """

    loaded = load_band_table(
        conn, stage=stage, table_sha256=table_sha256, as_of_utc=as_of_utc
    )
    if loaded is None:
        return None
    factors, sha = loaded
    return factors, sha, band_table_axis(conn, table_sha256=sha)


def band_table_axis(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    table_sha256: str | None = None,
) -> str:
    """Which quantity a table's cells are read from.

    ``claim`` for everything fitted before 2026-09-07 and for a database
    that has never carried the column; ``centre_distance`` for a table
    fitted on the bucket's distance from the distribution's own peak.  A
    caller that cannot answer this must not apply the table: the two
    lookups take the same shape and would both run.
    """

    ensure_band_table_schema(conn)
    if table_sha256 is None:
        # Activated first; then the stage's newest row, retired or not.  The
        # axis is a property of the TABLE, so a vetoed stage has no activated
        # row to read it from -- and the fallback below is ``claim``, which
        # would silently refit P0 on the axis it left on 2026-09-07.  Asked
        # 2026-09-15 with P0's table retired at 18:26Z: this returned claim
        # while the retired row still said centre_distance.  A stage with no
        # rows at all is still claim: that is the axis every table was fitted
        # on before the column existed.
        row = conn.execute(
            "SELECT axis FROM calibration_band_reliability_table"
            " WHERE stage=? ORDER BY activated DESC, fitted_at_utc DESC"
            " LIMIT 1",
            (_check_stage(stage),),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT axis FROM calibration_band_reliability_table"
            " WHERE band_table_sha256=?",
            (str(table_sha256),),
        ).fetchone()
    return str((row or ["claim"])[0] or "claim")


def persist_and_activate_band_table(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    factors: Sequence[tuple[tuple[float, float], float]],
    evidence: Mapping[str, Any],
    fitted_at_utc: str | None = None,
    serving_day_count: int,
    axis: str = BAND_AXIS_CLAIM,
) -> dict[str, Any]:
    """Insert the fitted table and make it this stage's active one.

    Idempotent by content: refitting the same factors re-activates the
    existing row rather than growing the table.
    """

    ensure_band_table_schema(conn)
    stage_id = _check_stage(stage)
    axis_id = str(axis)
    if axis_id not in BAND_AXES:
        raise ValueError(f"unknown band axis: {axis_id}")
    sha = band_table_sha256(factors, stage=stage_id, axis=axis_id)
    now = fitted_at_utc or (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    active = conn.execute(
        "SELECT band_table_sha256 FROM calibration_band_reliability_table"
        " WHERE activated=1 AND stage=?",
        (stage_id,),
    ).fetchone()
    if active is not None and str(active[0]) == sha:
        return {
            "stage": stage_id,
            "band_table_sha256": sha,
            "axis": axis_id,
            "state": "UNCHANGED",
        }
    attempts = _begin_immediate(conn)
    try:
        conn.execute(
            "UPDATE calibration_band_reliability_table SET activated=0"
            " WHERE activated=1 AND stage=?",
            (stage_id,),
        )
        conn.execute(
            "INSERT INTO calibration_band_reliability_table"
            " (band_table_sha256, stage, factors_json, evidence_json,"
            "  fitted_at_utc, serving_day_count, activated,"
            "  activated_at_utc, axis)"
            " VALUES (?,?,?,?,?,?,1,?,?)"
            " ON CONFLICT(band_table_sha256) DO UPDATE SET"
            "  activated=1, activated_at_utc=excluded.activated_at_utc",
            (
                sha,
                stage_id,
                json.dumps(
                    _canonical_factors(factors), sort_keys=True
                ),
                json.dumps(dict(evidence), sort_keys=True),
                now,
                int(serving_day_count),
                now,
                axis_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "stage": stage_id,
        "band_table_sha256": sha,
        "axis": axis_id,
        "state": "ACTIVATED",
        "previous": None if active is None else str(active[0]),
        "write_lock_attempts": attempts,
    }


def retire_band_table(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    retired_at_utc: str | None = None,
) -> dict[str, Any]:
    """Deactivate this stage's table AND stamp it retired.

    Two effects, and the second is the one that matters: deactivating stops
    the unpinned readers, while the stamp stops the pinned ones.  Without
    it a vetoed table keeps serving every artifact minted before the veto,
    which on 2026-09-10 was the difference between retiring the width tables
    and actually ceasing to serve them.
    """

    ensure_band_table_schema(conn)
    stage_id = _check_stage(stage)
    now = retired_at_utc or (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    active = conn.execute(
        "SELECT band_table_sha256 FROM calibration_band_reliability_table"
        " WHERE activated=1 AND stage=?",
        (stage_id,),
    ).fetchone()
    if active is None:
        return {"stage": stage_id, "state": "UNCHANGED"}
    attempts = _begin_immediate(conn)
    try:
        conn.execute(
            "UPDATE calibration_band_reliability_table"
            " SET activated=0,"
            "  retired_at_utc=COALESCE(retired_at_utc, ?)"
            " WHERE activated=1 AND stage=?",
            (now, stage_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "stage": stage_id,
        "state": "DEACTIVATED",
        "band_table_sha256": str(active[0]),
        "retired_at_utc": now,
        "write_lock_attempts": attempts,
    }


def journal_band_fit(
    conn: sqlite3.Connection,
    *,
    stage: str,
    fitted: Mapping[str, Any],
    outcome: str,
    fitted_at_utc: str | None = None,
    axis: str | None = None,
) -> str:
    """Record one fit -- what was measured, what it produced, what happened.

    The axis is part of the table's identity for every axis but the shipped
    one, so a journal that omits it files a distance fit under the sha a
    claim fit with the same factors would carry -- a sha that names no row
    in the served table at all.  Measured 2026-09-07..09: every P0 entry
    since the axis moved pointed at nothing, and the dashboard's
    "is this journal row about the serving table?" join silently answered
    no on every one of them, freezing P0's evidence panel on whatever the
    last factor CHANGE saw.  Default ``None`` reads the axis the stage is
    actually serving rather than assuming claim, so a caller that does not
    know the axis still files under the right one.
    """

    ensure_band_table_schema(conn)
    stage_id = _check_stage(stage)
    now = fitted_at_utc or (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    factors = tuple(fitted["factors"])
    axis_id = (
        str(axis) if axis is not None
        else str(fitted.get("axis") or band_table_axis(conn, stage=stage_id))
    )
    if axis_id not in BAND_AXES:
        raise ValueError(f"unknown band axis: {axis_id}")
    sha = band_table_sha256(factors, stage=stage_id, axis=axis_id)
    evidence = dict(fitted["evidence"])
    fit_id = "band-fit-" + content_sha256({
        "stage": stage_id,
        "fitted_at_utc": now,
        "band_table_sha256": sha,
    })[:24]
    # The insert would take the write lock itself, but only with the one
    # busy_timeout wait the connection was opened with; going through the
    # explicit acquisition spends the same budget on the journal as on the
    # activation.  A fit that was made and then lost its trail is the one
    # outcome this table exists to prevent.
    _begin_immediate(conn)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO calibration_band_reliability_fit_journal"
            " (fit_id, stage, fitted_at_utc, band_table_sha256,"
            "  serving_day_count, serving_station_day_count,"
            "  qualifying_cell_count, outcome, factors_json, evidence_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                fit_id,
                stage_id,
                now,
                sha,
                int(fitted["serving_day_count"]),
                int(evidence.get("serving_station_day_count") or 0),
                sum(
                    1 for _band, factor in factors if float(factor) != 1.0
                ),
                str(outcome),
                json.dumps(_canonical_factors(factors), sort_keys=True),
                json.dumps(evidence, sort_keys=True),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return fit_id


def pinned_band_table_sha(combination_method: str) -> str | None:
    for part in str(combination_method).split("::"):
        if part.startswith("band_table_sha256="):
            return part.split("=", 1)[1]
    return None


def _truth(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """The settled high per station-day, at its latest revision.

    The revision filter is not cosmetic: 285 station-days carry two rows,
    revision 0 stating the METAR body group's whole degree and revision 1 the
    tenths from the remark group (KATL 2026-07-14 reads 29.0 then 28.9), and
    which of the two a band fit sees decides which bucket it scores against
    on a day whose truth sits near an edge.  Unfiltered, this took whichever
    row the scan returned last -- today that happens to be the newest, and
    nothing guarantees it stays so.  The ledger's settlement query has always
    taken MAX(revision_no); this now asks the same question.
    """

    return {
        (str(station), str(day)): float(value)
        for station, day, value in conn.execute(
            "SELECT t.station_id, t.local_date, t.official_tmax_c"
            " FROM calibration_station_daily_high_truth AS t"
            " WHERE t.revision_no=("
            "  SELECT MAX(x.revision_no)"
            "  FROM calibration_station_daily_high_truth AS x"
            "  WHERE x.station_id=t.station_id AND x.local_date=t.local_date"
            "  AND x.source_lane=t.source_lane"
            "  AND x.settlement_definition=t.settlement_definition)"
        )
    }


def _invert_band_receipt(
    records: list[tuple[float, str]],
    receipt: Mapping[str, Any],
) -> list[tuple[float, str]]:
    """Reconstruct pre-band claims from a served row's receipt.

    Serving computed ``served_i = raw_i * f_i / total`` and recorded both
    ``f_i`` (applied_factors, identity omitted) and ``total``
    (pre_normalisation_mass), so the inversion is exact, not estimated.
    """

    total = float(receipt.get("pre_normalisation_mass") or 0.0)
    applied = {
        str(key): float(value)
        for key, value in (receipt.get("applied_factors") or {}).items()
    }
    if total <= 0.0:
        raise ContractError("band_reliability_receipt_mass_invalid")
    return [
        (probability * total / applied.get(key, 1.0), key)
        for probability, key in records
    ]


def serving_lane_by_station_day(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
    *,
    stage: str = "P0",
    pre_band: bool = True,
    min_decision_at_utc: str | None = None,
) -> dict[tuple[str, str], list[list[tuple[float, int, float]]]]:
    """First decision per station-day for one stage, as pre-band claims.

    ``min_decision_at_utc`` bounds the evidence below, the same bound and for
    the same reason the centre/width lane has carried since 2026-08-27: this
    table measures how wrong the *served* claim is, so a decision taken before
    the serving arithmetic changed describes a distribution nobody serves any
    more.  Unbounded, P0 was fitting 41 days reaching back to 2026-07-28 --
    across the width layer reaching the fleet on 2026-09-04 -- and the two
    cells that only clear their interval on that pooled sample cost 0.85 of a
    peak-hit point on the days after the change while buying 0.0020 nat.
    Measured 2026-09-10 at three walk-forward cuts; a fourteen-day bounded
    window scored at least as well as the unbounded one at every cut.

    The bound is applied to the inner MIN() as well, so the "first decision"
    is the first one this arithmetic made rather than a retired one that
    happens to sort earlier.

    First, not last (user ruling, 2026-08-24).  The last recompute of a
    station-day is written at a median of 23:00 local while the daily high
    has landed by 13.9h -- a claim made after the answer is a description,
    not a forecast, and a table fitted on descriptions certifies them as
    calibrated.  Measured against market-adjudicated truth on 289 closed
    days, the claims the money actually trades on -- the stage's first
    compile, where the seat is priced -- still over-claim 1.26x in
    [0.20,0.35) and 2x above it AFTER this table was applied, while the
    last-snapshot fit was serving 1.07 upward on the same ground.  The
    same clock error is on record twice already: the "model never learns"
    verdict that came from reading end-of-day rows, and the P1 ruling that
    a prophet is judged before the peak, not after.

    Within the first wall-clock decision burst the LAST row is taken: P1
    writes one row per observation step of a burst, and only the final row
    of the burst is the complete prefix (the same contract every other
    reader of the bridge history follows).

    Keyed by (station, event) with each claim carrying its bucket's
    lower bound, ordered coldest first, so a per-station reader can
    reconstruct windows; ``serving_lane_by_day`` derives the fit's
    day-keyed pair view from this and stays the fit's own entry.
    """

    from .probability_bridge import history_envelope_payload

    by_station_day: dict[
        tuple[str, str], list[list[tuple[float, int, float]]]
    ] = defaultdict(list)
    bound = str(min_decision_at_utc) if min_decision_at_utc else None
    inner_bound = " AND decision_at_utc>=?" if bound else ""
    outer_bound = " AND h.decision_at_utc>=?" if bound else ""
    parameters: list[Any] = [_check_stage(stage)]
    if bound:
        parameters.append(bound)
    parameters.append(_check_stage(stage))
    if bound:
        parameters.append(bound)
    rows = conn.execute(
        "SELECT h.station_id, h.event_date, h.history_id,"
        " h.envelope_json, h.checkpoint_id, h.envelope_content_sha256,"
        " MAX(h.rowid)"
        " FROM basket_probability_bridge_history h"
        " JOIN (SELECT station_id, event_date,"
        "         MIN(decision_at_utc) AS first_decision_at"
        "       FROM basket_probability_bridge_history"
        f"       WHERE stage=?{inner_bound} GROUP BY station_id, event_date) f"
        "   ON f.station_id = h.station_id AND f.event_date = h.event_date"
        "  AND h.decision_at_utc = f.first_decision_at"
        f" WHERE h.stage=?{outer_bound}"
        " GROUP BY h.station_id, h.event_date",
        tuple(parameters),
    ).fetchall()
    for station, event, history_id, stored, checkpoint, sha, _last_row in rows:
        actual = truth.get((str(station), str(event)))
        if actual is None:
            continue
        envelope = history_envelope_payload(
            conn,
            checkpoint_id=str(checkpoint),
            envelope_content_sha256=str(sha),
            stored=str(stored) if stored else None,
        )
        receipt = None
        lineage = envelope.get("lineage") or {}
        raw = lineage.get("band_reliability_json")
        parsed = json.loads(raw) if raw else None
        if parsed and parsed.get("state") == "APPLIED":
            receipt = parsed
        buckets = conn.execute(
            "SELECT bucket, lower_c, upper_c, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=?",
            (history_id,),
        ).fetchall()
        if not buckets:
            continue
        keyed = [
            (float(probability), str(bucket))
            for bucket, _lower, _upper, probability in buckets
        ]
        # ``pre_band=False`` keeps the claims exactly as served, factors
        # included -- the residual audit's view; the fit keeps inverting.
        if receipt is not None and pre_band:
            keyed = _invert_band_receipt(keyed, receipt)
        claim_by_bucket = {key: probability for probability, key in keyed}
        entries = []
        for bucket, lower, upper, _probability in buckets:
            lower_c = -1e9 if lower is None else float(lower)
            upper_c = 1e9 if upper is None else float(upper)
            entries.append((
                claim_by_bucket[str(bucket)],
                1 if lower_c <= actual < upper_c else 0,
                lower_c,
            ))
        entries.sort(key=lambda entry: entry[2])
        by_station_day[(str(station), str(event))].append(entries)
    return dict(by_station_day)


def serving_lane_by_day(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
    *,
    stage: str = "P0",
    pre_band: bool = True,
    min_decision_at_utc: str | None = None,
    with_stations: bool = False,
) -> (
    dict[str, list[list[tuple[float, int]]]]
    | tuple[dict[str, list[list[tuple[float, int]]]], dict[str, list[str]]]
):
    """The fit's day-keyed view of ``serving_lane_by_station_day``.

    ``with_stations`` additionally returns which station each row came from,
    index for index, which is what the report needs to resample the station
    dimension and to count a cell's stations.  Off by default: the replay
    lanes and the tests read the day view alone.
    """

    by_day: dict[str, list[list[tuple[float, int]]]] = defaultdict(list)
    stations_by_day: dict[str, list[str]] = defaultdict(list)
    for (station, event), day_rows in serving_lane_by_station_day(
        conn, truth, stage=stage, pre_band=pre_band,
        min_decision_at_utc=min_decision_at_utc,
    ).items():
        for entries in day_rows:
            by_day[str(event)].append([
                (probability, hit) for probability, hit, _lower in entries
            ])
            stations_by_day[str(event)].append(str(station))
    if with_stations:
        return dict(by_day), dict(stations_by_day)
    return dict(by_day)


def gate_lane_by_day(
    conn: sqlite3.Connection,
    truth: Mapping[tuple[str, str], float],
) -> dict[str, list[list[tuple[float, int]]]]:
    """Latest-vintage pre-band OOS predictions, 1C model buckets."""

    by_day: dict[str, list[list[tuple[float, int]]]] = defaultdict(list)
    rows = conn.execute(
        "SELECT p.station_id, p.event_date, p.probabilities_json,"
        " p.bucket_centers_c_json, a.combination_method,"
        " MAX(p.trained_through_event_date || p.decision_at_utc)"
        " FROM calibration_cross_native_ensemble_oos_prediction p"
        " JOIN calibration_cross_native_ensemble_artifact a"
        " ON a.cross_ensemble_artifact_id = p.cross_ensemble_artifact_id"
        " GROUP BY p.station_id, p.event_date, p.lead_bucket"
    ).fetchall()
    for station, event, probabilities_json, centers_json, method, _ in rows:
        actual = truth.get((str(station), str(event)))
        if actual is None:
            continue
        if "band_reliability" in str(method):
            continue
        probabilities = json.loads(probabilities_json)
        centers = json.loads(centers_json)
        ordered = sorted(
            probabilities, key=lambda k: float(centers.get(k, k))
        )
        # Charter 213: a gate scored on the market ladder archives rung
        # labels, whose widths are the ladder's (2F = 1.11C, open ends
        # unbounded) -- the settled rung is read off the station-day's
        # ladder, not off a 1C window around the centre.  Integer-degree
        # rows keep the rule they were archived under.
        if all(_integer_label(key) for key in ordered):
            hits = [
                1 if (float(centers.get(key, key)) - 0.5) <= actual
                < (float(centers.get(key, key)) + 0.5) else 0
                for key in ordered
            ]
        else:
            found = station_day_ladder(
                conn, station_id=str(station), event_date=str(event),
            )
            rung = settled_rung(found[2], actual) if found else None
            if rung is None or rung not in probabilities:
                continue
            hits = [1 if key == rung else 0 for key in ordered]
        by_day[str(event)].append([
            (float(probabilities[key]), hit)
            for key, hit in zip(ordered, hits)
        ])
    return dict(by_day)


def _integer_label(key: str) -> bool:
    try:
        int(str(key))
    except ValueError:
        return False
    return True


def _record_keys(
    records: Sequence[tuple[float, int]], axis: str
) -> list[float]:
    """The quantity each record's cell is read from.

    ``records`` is one station-day's rungs in bucket order -- that ordering
    is the ordinal, which is why the distance axis needs nothing the fit
    does not already carry.  The peak is the argmax, ties breaking on the
    ordinal, exactly as ``cross_consensus._centre_distances`` does it at
    serving time; the two must agree or the table is fitted on one axis and
    applied on another.
    """

    if str(axis) != BAND_AXIS_CENTRE_DISTANCE:
        return [float(probability) for probability, _hit in records]
    if not records:
        return []
    peak = max(
        range(len(records)),
        key=lambda index: (float(records[index][0]), -index),
    )
    return [float(abs(index - peak)) for index in range(len(records))]


def _day_aggregates(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    bands: Sequence[tuple[float, float]],
    axis: str = BAND_AXIS_CLAIM,
) -> dict[str, dict[tuple[float, float], list[float]]]:
    aggregates: dict[str, dict[tuple[float, float], list[float]]] = {}
    for day, rows in by_day.items():
        cells = {band: [0.0, 0.0] for band in bands}
        for records in rows:
            keys = _record_keys(records, axis)
            for (probability, hit), value in zip(records, keys):
                for band in bands:
                    if band[0] <= value < band[1]:
                        # The claimed mass and the realised count are always
                        # the rung's own; only which cell they land in moves.
                        cells[band][0] += probability
                        cells[band][1] += hit
                        break
        aggregates[day] = cells
    return aggregates


def _panel_aggregates(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    stations_by_day: Mapping[str, Sequence[str]],
    bands: Sequence[tuple[float, float]],
    axis: str = BAND_AXIS_CLAIM,
) -> dict[str, dict[str, dict[tuple[float, float], list[float]]]]:
    """Claimed mass and realised hits per day, per station, per cell.

    The day view sums this over stations; keeping the station apart is what
    lets the interval price the station dimension and what lets a cell say
    how many stations it is actually made of.
    """

    panel: dict[str, dict[str, dict[tuple[float, float], list[float]]]] = {}
    for day, rows in by_day.items():
        stations = list(stations_by_day.get(day) or ())
        day_panel = panel.setdefault(str(day), {})
        for index, records in enumerate(rows):
            station = (
                str(stations[index]) if index < len(stations)
                else f"__row{index}"
            )
            cells = day_panel.setdefault(
                station, {band: [0.0, 0.0] for band in bands}
            )
            keys = _record_keys(records, axis)
            for (probability, hit), value in zip(records, keys):
                for band in bands:
                    if band[0] <= value < band[1]:
                        cells[band][0] += probability
                        cells[band][1] += hit
                        break
    return panel


def band_lane_report(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    *,
    bands: Sequence[tuple[float, float]] = FINE_BANDS,
    bootstrap_samples: int = 1000,
    seed: int = 17,
    axis: str = BAND_AXIS_CLAIM,
    interval_alpha: float = DEFAULT_INTERVAL_ALPHA,
    stations_by_day: Mapping[str, Sequence[str]] | None = None,
    min_cell_stations: int = 0,
) -> dict[tuple[float, float], dict[str, Any]]:
    """The per-cell realised-over-claimed ratio and what may be served of it.

    ``stations_by_day`` names the station behind each row of ``by_day``,
    index for index.  Given it, the interval is resampled over BOTH
    clusters -- days, and the station-days within each drawn day -- and the
    cell reports how many stations it is made of and how it survives the
    loss of its largest station and its largest day.  Without it the report
    is exactly the day-only one this lane has always produced, which is what
    the replay lanes and the gate archive still pass.
    """

    days = sorted(by_day)
    if not days:
        return {}
    aggregates = _day_aggregates(by_day, bands, axis)
    panel = (
        _panel_aggregates(by_day, stations_by_day, bands, axis)
        if stations_by_day is not None
        else None
    )

    def ratios(
        sample: Sequence[str],
    ) -> dict[tuple[float, float], float | None]:
        out = {}
        for band in bands:
            claimed = sum(aggregates[d][band][0] for d in sample)
            realised = sum(aggregates[d][band][1] for d in sample)
            out[band] = realised / claimed if claimed > 0 else None
        return out

    point = ratios(days)
    random.seed(seed)
    bootstrap: dict[tuple[float, float], list[float]] = {
        band: [] for band in bands
    }
    for _ in range(bootstrap_samples):
        if panel is None:
            sample = [random.choice(days) for _ in days]
            sampled = ratios(sample)
        else:
            totals = {band: [0.0, 0.0] for band in bands}
            for _day_draw in days:
                day = random.choice(days)
                stations = list(panel[day])
                if not stations:
                    continue
                for _station_draw in stations:
                    cells = panel[day][random.choice(stations)]
                    for band in bands:
                        totals[band][0] += cells[band][0]
                        totals[band][1] += cells[band][1]
            sampled = {
                band: (
                    totals[band][1] / totals[band][0]
                    if totals[band][0] > 0 else None
                )
                for band in bands
            }
        for band in bands:
            if sampled[band] is not None:
                bootstrap[band].append(sampled[band])
    first = ratios(days[: len(days) // 2])
    second = ratios(days[len(days) // 2:])
    report: dict[tuple[float, float], dict[str, Any]] = {}
    for band in bands:
        ordered = sorted(bootstrap[band])
        if not ordered or point[band] is None:
            continue
        # This width IS how hard the table is allowed to pull: the rule
        # serves the interval end nearest one, so the served factor sits a
        # half-interval short of the point estimate, and a cell whose
        # interval straddles one is refused outright.  It is a parameter so
        # a counterfactual can be measured through this code path instead of
        # through a second copy of it; the default is the shipped 95%, and
        # nothing passes anything else unless a ruling says so.
        last = len(ordered) - 1
        low = ordered[min(last, int(0.5 * interval_alpha * len(ordered)))]
        high = ordered[
            min(last, int((1.0 - 0.5 * interval_alpha) * len(ordered)))
        ]
        interval_excludes_one = not (low <= 1.0 <= high)
        sign_stable = (
            first[band] is not None
            and second[band] is not None
            and (first[band] - 1.0) * (second[band] - 1.0) > 0
        )
        robustness = _cell_robustness(panel, band, point[band])
        station_count = robustness["station_count"]
        enough_stations = (
            station_count is None or station_count >= int(min_cell_stations)
        )
        qualified = (
            interval_excludes_one and sign_stable and enough_stations
        )
        conservative = (high if high < 1.0 else low) if qualified else 1.0
        report[band] = {
            "ratio": point[band],
            "interval": [low, high],
            "halves": [first[band], second[band]],
            "interval_excludes_one": interval_excludes_one,
            "sign_stable": sign_stable,
            "qualified": qualified,
            "blocked_by": (
                None
                if qualified
                else (
                    "interval_spans_one"
                    if not interval_excludes_one
                    else "sign_flips_between_halves"
                    if not sign_stable
                    else "too_few_stations"
                )
            ),
            "conservative_factor": min(
                FACTOR_CEILING, max(FACTOR_FLOOR, conservative)
            ),
            **robustness,
        }
    return report


def _cell_robustness(
    panel: (
        Mapping[str, Mapping[str, Mapping[tuple[float, float], list[float]]]]
        | None
    ),
    band: tuple[float, float],
    point_ratio: float | None,
) -> dict[str, Any]:
    """How much of a cell's verdict is one station's or one day's doing.

    Reported, never gating (2026-09-11).  A pooled factor claims the fleet
    misprices this cell; three cheap questions say whether the fleet agrees
    with itself, and the lane records the answers so the ruling that decides
    whether they should gate has evidence rather than a hunch.  Gating them
    on their first day would be choosing a threshold on the same sample that
    suggested it.
    """

    empty: dict[str, Any] = {
        "station_count": None,
        "station_day_count": None,
        "leave_one_station_sign_agreement": None,
        "drop_largest_station_ratio": None,
        "drop_largest_day_ratio": None,
    }
    if panel is None or point_ratio is None:
        return empty
    by_station: dict[str, list[float]] = {}
    by_day_mass: dict[str, list[float]] = {}
    station_days = 0
    for day, stations in panel.items():
        for station, cells in stations.items():
            claimed, hits = cells[band][0], cells[band][1]
            if claimed <= 0:
                continue
            station_days += 1
            entry = by_station.setdefault(station, [0.0, 0.0])
            entry[0] += claimed
            entry[1] += hits
            day_entry = by_day_mass.setdefault(day, [0.0, 0.0])
            day_entry[0] += claimed
            day_entry[1] += hits
    if not by_station:
        return empty
    total_claimed = sum(entry[0] for entry in by_station.values())
    total_hits = sum(entry[1] for entry in by_station.values())

    def _without(entry: Sequence[float]) -> float | None:
        claimed = total_claimed - entry[0]
        return (total_hits - entry[1]) / claimed if claimed > 0 else None

    agree = 0
    counted = 0
    for entry in by_station.values():
        left = _without(entry)
        if left is None:
            continue
        counted += 1
        if (left - 1.0) * (point_ratio - 1.0) > 0:
            agree += 1
    largest_station = max(by_station.values(), key=lambda e: e[0])
    largest_day = max(by_day_mass.values(), key=lambda e: e[0])
    return {
        "station_count": len(by_station),
        "station_day_count": station_days,
        "leave_one_station_sign_agreement": (
            agree / counted if counted else None
        ),
        "drop_largest_station_ratio": _without(largest_station),
        "drop_largest_day_ratio": _without(largest_day),
    }


def forward_window_split(
    days: Sequence[str],
) -> tuple[list[str], list[str]]:
    """The fit days and the scoring days of the forward check, disjoint.

    The trailing ``FORWARD_WINDOW_FRACTION`` of the days is scored; only the
    days before it may fit the factors being scored.  Until 2026-09-13 the
    factors were fitted on every day and the trailing 40% of those same days
    scored them (model-layer review R4: 8 of 8 "forward" days were fit days),
    which is the in-sample number charter 60's forward NLL was meant not to
    be.
    """

    ordered = sorted(str(day) for day in days)
    if not ordered:
        return [], []
    cut = max(1, int(len(ordered) * (1.0 - FORWARD_WINDOW_FRACTION)))
    return ordered[:cut], ordered[cut:]


def _forward_nll_delta(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    *,
    base_factors: Mapping[tuple[float, float], float],
    candidate_factors: Mapping[tuple[float, float], float],
    axis: str = BAND_AXIS_CLAIM,
    score_days: Sequence[str] | None = None,
) -> float:
    """NLL improvement of candidate over base on the trailing window.

    The lookup follows the AXIS, and this is the second copy of that lookup
    in this module -- ``_day_aggregates`` is the other.  Charter 105 moved
    the first and left this one keyed on the claim, so a distance table was
    scored here against distance-shaped bounds using claim values: every
    bucket falls in the first cell (all claims are under 1, and the cells
    run 0-1, 1-2, 2-3, 3-4, 4+), the other four are never reached, and
    changing any one of them moves the total by exactly zero.

    Measured 2026-09-08 on the live sample, every qualified cell of both
    stages that run this check: the deltas read +-0.0000 across the board,
    which is not "did not improve enough" but "was not being measured".
    Keyed correctly, P1 admits five cells of five (+2.18, +1.02, +0.16,
    +0.58, +7.12) and P0-M two (+0.07, +6.12).  P0 never reached this
    because ``requires_forward_check`` exempts it, which is why the defect
    surfaced only on the two stages that do.
    """

    days = sorted(by_day)
    if not days:
        return 0.0
    if score_days is None:
        cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
        window = days[cut:] or days[-1:]
    else:
        window = [day for day in sorted(score_days) if day in by_day]

    def nll(factors: Mapping[tuple[float, float], float]) -> float:
        total = 0.0
        for day in window:
            for records in by_day[day]:
                mass = 0.0
                corrected = []
                keys = _record_keys(records, axis)
                for (probability, hit), value in zip(records, keys):
                    factor = 1.0
                    for band, band_factor in factors.items():
                        if band[0] <= value < band[1]:
                            factor = band_factor
                            break
                    corrected.append((probability * factor, hit))
                    mass += probability * factor
                if mass <= 0:
                    continue
                for probability, hit in corrected:
                    if hit:
                        total += -log(max(probability / mass, 1e-9))
        return total

    return nll(base_factors) - nll(candidate_factors)


# The veto window, in days and station-days of evidence served AFTER the
# incumbent's own activation.  Same shape and same reason as the station
# width lane's (2026-09-10): a table held through ACCUMULATING is held
# unconditionally, and the only path back to identity was a fit that never
# runs while accumulating -- so an incumbent that is hurting can only be
# retired if something asks.  Out of sample by construction, because the
# cells postdate the activation.
BAND_VETO_MIN_DAYS = 3
BAND_VETO_MIN_STATION_DAYS = 60
# Per station-day, the same hurdle the width lane retires on.  A table has
# to be measurably hurting, not merely not helping: not helping is what
# every identity cell already is.
BAND_VETO_MIN_GAIN = 0.005


def _active_band_activated_at(
    conn: sqlite3.Connection, *, stage: str
) -> str | None:
    try:
        row = conn.execute(
            "SELECT activated_at_utc FROM calibration_band_reliability_table"
            " WHERE activated=1 AND stage=?",
            (_check_stage(stage),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row and row[0] else None


def _first_decision_by_station_day(
    conn: sqlite3.Connection,
    *,
    stage: str,
    min_decision_at_utc: str | None = None,
) -> dict[tuple[str, str], str]:
    """When each station-day's first decision was taken.

    The lane's rows are that first decision, and the veto has to know which
    of them the incumbent was actually serving.  Event date will not answer
    it: a decision for an event two days out is taken two days early, so a
    day AFTER an activation routinely carries a decision from before it.
    """

    query = (
        "SELECT station_id, event_date, MIN(decision_at_utc)"
        " FROM basket_probability_bridge_history"
        " WHERE stage=?"
    )
    parameters: list[Any] = [_check_stage(stage)]
    if min_decision_at_utc:
        query += " AND decision_at_utc>=?"
        parameters.append(str(min_decision_at_utc))
    query += " GROUP BY station_id, event_date"
    return {
        (str(station), str(event)): str(decided)
        for station, event, decided in conn.execute(query, parameters)
    }


def _lane_nll(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    factors: Mapping[tuple[float, float], float],
    axis: str,
) -> tuple[float, int]:
    """Total settled-bucket NLL over every row given, and how many scored."""

    total = 0.0
    scored = 0
    for day in sorted(by_day):
        for records in by_day[day]:
            mass = 0.0
            corrected = []
            keys = _record_keys(records, axis)
            for (probability, hit), value in zip(records, keys):
                factor = 1.0
                for band, band_factor in factors.items():
                    if band[0] <= value < band[1]:
                        factor = band_factor
                        break
                corrected.append((probability * factor, hit))
                mass += probability * factor
            if mass <= 0:
                continue
            for probability, hit in corrected:
                if hit:
                    total += -log(max(probability / mass, 1e-9))
                    scored += 1
    return total, scored


def _band_incumbent_veto(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    stations_by_day: Mapping[str, Sequence[str]],
    decisions: Mapping[tuple[str, str], str],
    *,
    factors: Mapping[tuple[float, float], float],
    axis: str,
    activated_at_utc: str | None,
) -> dict[str, Any]:
    """Is the incumbent table still worth serving on what came after it?

    Scored against identity on the station-days whose decision was taken
    after the activation -- out of sample by construction.  Losing by more
    than the hurdle retires it.  Winning, or not having enough evidence yet,
    changes nothing: this asks only "is this hurting", never "is this best".

    Why it exists (2026-09-10, the width lane's judgement applied here): a
    table held through ACCUMULATING is held unconditionally, and the only
    route back to identity was a fit that does not run while accumulating.
    A layer that can activate but never retire is a layer where one bad fit
    is permanent.
    """

    verdict: dict[str, Any] = {
        "vetoed": False,
        "activated_at_utc": activated_at_utc,
        "day_count": 0,
        "station_day_count": 0,
        "undo_gain": 0.0,
        "min_days": BAND_VETO_MIN_DAYS,
        "min_station_days": BAND_VETO_MIN_STATION_DAYS,
        "hurdle": BAND_VETO_MIN_GAIN,
    }
    if not activated_at_utc or all(
        abs(float(value) - 1.0) < 1e-12 for value in factors.values()
    ):
        return verdict
    after: dict[str, list[list[tuple[float, int]]]] = {}
    for day, rows in by_day.items():
        stations = list(stations_by_day.get(day) or ())
        kept = [
            records for index, records in enumerate(rows)
            if index < len(stations)
            and str(
                decisions.get((str(stations[index]), str(day)), "")
            ) >= str(activated_at_utc)
        ]
        if kept:
            after[str(day)] = kept
    station_days = sum(len(rows) for rows in after.values())
    verdict["day_count"] = len(after)
    verdict["station_day_count"] = station_days
    if (
        len(after) < BAND_VETO_MIN_DAYS
        or station_days < BAND_VETO_MIN_STATION_DAYS
    ):
        return verdict
    identity = {band: 1.0 for band in factors}
    kept_total, scored = _lane_nll(after, factors, axis)
    undone_total, _ = _lane_nll(after, identity, axis)
    if not scored:
        return verdict
    # Positive when IDENTITY beats the incumbent, which is the direction
    # this question is asked in.
    undo_gain = (kept_total - undone_total) / scored
    verdict["nll_kept"] = kept_total / scored
    verdict["nll_undone"] = undone_total / scored
    verdict["undo_gain"] = undo_gain
    verdict["vetoed"] = undo_gain > BAND_VETO_MIN_GAIN
    return verdict


def fit_band_reliability_table(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    bootstrap_samples: int = 1000,
    axis: str = BAND_AXIS_CLAIM,
    interval_alpha: float = DEFAULT_INTERVAL_ALPHA,
    until_event_date: str | None = None,
    min_decision_at_utc: str | None = None,
) -> dict[str, Any]:
    """Fit one stage's table under the fixed admission rule.

    A stage with too little settled history is not an error: it holds
    whatever it is already serving, records why, and promotes itself the day
    the evidence clears the hurdles.  It used to fit to identity there, which
    is not the same thing -- committing that answer throws a fitted table away
    because a bound moved, and the bound below can move by several weeks in
    one step when a stage's arithmetic changes.

    ``min_decision_at_utc`` defaults to the stage's own served-arithmetic
    anchor, the bound the centre/width lane has used since 2026-08-27 and for
    the identical reason; pass a value to override it, or the empty string to
    fit unbounded the way this lane did until 2026-09-10.
    """

    stage_id = _check_stage(stage)
    axis_id = str(axis)
    if axis_id not in BAND_AXES:
        raise ValueError(f"unknown band axis: {axis_id}")
    # Charter 105: the partition follows the axis, so a distance fit cannot
    # be scored on claim cells or the other way round.
    cells = cells_for_axis(axis_id)
    truth = _truth(conn)
    if min_decision_at_utc is None:
        # Imported here: center_width_store imports this module for the band
        # inverse, so a module-level import would close the cycle.
        from .center_width_store import evidence_anchor
        evidence_floor = evidence_anchor(conn, stage=stage_id)
    else:
        evidence_floor = str(min_decision_at_utc) or None
    serving, serving_stations = serving_lane_by_day(
        conn, truth, stage=stage_id, min_decision_at_utc=evidence_floor,
        with_stations=True,
    )
    # A walk-forward over this layer has to ask what the fit would have
    # served knowing only the days before some date, and the only honest way
    # to answer is to run this function with the later days withheld.  None
    # is production: every day the lane has.
    if until_event_date is not None:
        serving_stations = {
            day: rows for day, rows in serving_stations.items()
            if day < str(until_event_date)
        }
        serving = {
            day: rows for day, rows in serving.items()
            if day < str(until_event_date)
        }
    station_days = sum(len(rows) for rows in serving.values())
    # Only P0 has a second geometry to transfer from: the OOS prediction
    # archive is the cross-ensemble blend, which is exactly P0's root and
    # nothing else's.
    gate = (
        gate_lane_by_day(conn, truth) if stage_id == "P0" else {}
    )
    if until_event_date is not None:
        gate = {
            day: rows for day, rows in gate.items()
            if day < str(until_event_date)
        }
    # A stage whose distribution no gate re-scores has to earn every cell
    # on the forward window here instead.
    requires_forward_check = stage_id != "P0"
    # Before anything else: is what is already being served still earning
    # its place?  A held table is held unconditionally otherwise.
    incumbent_loaded = load_band_table_with_axis(conn, stage=stage_id)
    incumbent_veto = _band_incumbent_veto(
        serving,
        serving_stations,
        _first_decision_by_station_day(
            conn, stage=stage_id, min_decision_at_utc=evidence_floor
        ),
        factors=(
            {band: float(value) for band, value in incumbent_loaded[0]}
            if incumbent_loaded is not None else {}
        ),
        axis=(
            str(incumbent_loaded[2]) if incumbent_loaded is not None
            else axis_id
        ),
        activated_at_utc=_active_band_activated_at(conn, stage=stage_id),
    )
    if incumbent_veto["vetoed"]:
        # Identity, and the caller retires the incumbent rather than
        # activating this as a new table: a vetoed version must stop serving
        # the artifacts pinned to it too, which activation alone would not
        # do.
        return {
            "stage": stage_id,
            "factors": tuple((band, 1.0) for band in cells),
            "axis": axis_id,
            "interval_alpha": float(interval_alpha),
            "forward_nll_delta_vs_identity": -float(
                incumbent_veto["undo_gain"]
            ),
            "vetoed": True,
            "incumbent_veto": incumbent_veto,
            "evidence": {
                "schema": "band_reliability_fit_evidence_v2",
                "stage": stage_id,
                "axis": axis_id,
                "basis": "INCUMBENT_VETOED",
                "serving_day_count": len(serving),
                "serving_station_day_count": station_days,
                "incumbent_veto": incumbent_veto,
            },
            "serving_day_count": len(serving),
            "serving_station_day_count": station_days,
            "accumulating": len(serving) < MIN_QUALIFYING_DAYS,
        }
    accumulating = len(serving) < MIN_QUALIFYING_DAYS
    serving_report = (
        {}
        if accumulating
        else band_lane_report(
            serving,
            bands=cells,
            bootstrap_samples=bootstrap_samples,
            axis=axis_id,
            interval_alpha=interval_alpha,
            stations_by_day=serving_stations,
            min_cell_stations=MIN_CELL_STATIONS,
        )
    )
    gate_report = (
        band_lane_report(
            gate,
            bands=cells,
            bootstrap_samples=bootstrap_samples,
            axis=axis_id,
            interval_alpha=interval_alpha,
        )
        if gate
        else {}
    )
    factors: dict[tuple[float, float], float] = {
        band: 1.0 for band in cells
    }
    # The forward check's own fit.  Same rule, same bootstrap, but only the
    # days before the scoring window: a cell is admitted when the factor it
    # would have been given from those days alone improves NLL on days it
    # never saw.  The factor then SERVED is still the full-sample one --
    # admission is judged out of sample, the answer uses all the evidence.
    forward_fit_days, forward_score_days = forward_window_split(serving)
    forward_honest = bool(forward_fit_days) and bool(forward_score_days)
    forward_serving_report = (
        band_lane_report(
            {day: serving[day] for day in forward_fit_days},
            bands=cells,
            bootstrap_samples=bootstrap_samples,
            axis=axis_id,
            interval_alpha=interval_alpha,
            stations_by_day=(
                {day: serving_stations[day] for day in forward_fit_days}
                if serving_stations is not None else None
            ),
            min_cell_stations=MIN_CELL_STATIONS,
        )
        if not accumulating and forward_honest
        else {}
    )
    # The gate lane's own archive may start before the serving evidence
    # floor; every gate day before the scoring window is prior evidence.
    forward_gate_days = [
        day for day in sorted(gate)
        if forward_score_days and day < forward_score_days[0]
    ]
    forward_gate_report = (
        band_lane_report(
            {day: gate[day] for day in forward_gate_days},
            bands=cells,
            bootstrap_samples=bootstrap_samples,
            axis=axis_id,
            interval_alpha=interval_alpha,
        )
        if not accumulating and forward_gate_days
        else {}
    )
    forward_factors: dict[tuple[float, float], float] = {
        band: 1.0 for band in cells
    }

    def _front_factor(
        report: Mapping[tuple[float, float], Mapping[str, Any]],
        band: tuple[float, float],
    ) -> float | None:
        cell = report.get(band)
        if cell is None or not cell["qualified"]:
            return None
        return float(cell["conservative_factor"])

    if accumulating:
        # Hold, do not blank.  refit_all_stages activates whatever this
        # returns, so answering identity here retires a fitted table for the
        # sole reason that the lane cannot see enough days -- and with the
        # evidence bound above, a stage's window can shorten by weeks in one
        # step the moment its arithmetic changes.  The incumbent is only kept
        # when it is keyed on the axis being fitted; an axis move is a
        # decision somebody made on purpose and identity is the honest start
        # for it.
        _incumbent = load_band_table_with_axis(conn, stage=stage_id)
        if _incumbent is not None and str(_incumbent[2]) == axis_id:
            held = dict(_incumbent[0])
            for band in cells:
                if band in held:
                    factors[band] = float(held[band])
    basis: dict[str, str] = {}
    for band in cells:
        key = str(list(band))
        if accumulating:
            basis[key] = (
                "accumulating_evidence:held_incumbent"
                if factors[band] != 1.0 else "accumulating_evidence"
            )
            continue
        cell = serving_report.get(band)
        if cell is None:
            basis[key] = "no_sample"
            continue
        if not cell["qualified"]:
            basis[key] = "identity:" + str(cell["blocked_by"])
            continue
        if requires_forward_check:
            front = _front_factor(forward_serving_report, band)
            if front is None:
                basis[key] = "identity:forward_fit_not_qualified"
                continue
            candidate = dict(forward_factors)
            candidate[band] = front
            if _forward_nll_delta(
                serving,
                base_factors=forward_factors,
                candidate_factors=candidate,
                axis=axis_id,
                score_days=forward_score_days,
            ) <= 0:
                basis[key] = "identity:forward_window_not_improved"
                continue
            forward_factors[band] = front
        else:
            front = _front_factor(forward_serving_report, band)
            if front is not None:
                forward_factors[band] = front
        factors[band] = cell["conservative_factor"]
        basis[key] = "serving_lane"
    for band in cells:
        if factors[band] != 1.0 or accumulating:
            continue
        serving_cell = serving_report.get(band)
        gate_cell = gate_report.get(band)
        if serving_cell is None or gate_cell is None:
            continue
        if not gate_cell["qualified"]:
            continue
        # Transfer needs direction agreement with the serving lane's own
        # point estimate; the deep tail's geometry reversal is exactly what
        # this refuses.
        if (
            (gate_cell["ratio"] - 1.0)
            * (serving_cell["ratio"] - 1.0)
            <= 0
        ):
            continue
        # Out of sample the same way: the transfer is judged on the factor
        # the gate lane gives from the fit days alone, with its direction
        # checked against the serving lane on those same days.
        front_gate = _front_factor(forward_gate_report, band)
        front_serving_cell = forward_serving_report.get(band)
        if (
            front_gate is None
            or front_serving_cell is None
            or (forward_gate_report[band]["ratio"] - 1.0)
            * (front_serving_cell["ratio"] - 1.0) <= 0
        ):
            continue
        candidate = dict(forward_factors)
        candidate[band] = front_gate
        if _forward_nll_delta(
            serving,
            base_factors=forward_factors,
            candidate_factors=candidate,
            axis=axis_id,
            score_days=forward_score_days,
        ) > 0:
            forward_factors[band] = front_gate
            factors[band] = gate_cell["conservative_factor"]
            basis[str(list(band))] = "gate_transfer"

    def _cell_json(
        report: Mapping[tuple[float, float], dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            str(list(band)): dict(cell) for band, cell in report.items()
        }

    # What this whole table is worth on the forward window, against serving
    # nothing at all.  The per-cell gate above already asks this question for
    # the stages that require it, one cell at a time -- but P0 is exempt from
    # that gate (``requires_forward_check``), so for the one stage whose
    # table moves the most there was no forward number anywhere.  Reported,
    # never gating: it changes no admission, and it is deliberately kept out
    # of ``evidence`` so a table's sha does not churn on a new diagnostic.
    identity_factors = {band: 1.0 for band in cells}
    forward_nll_delta_vs_identity = (
        _forward_nll_delta(
            serving,
            base_factors=identity_factors,
            candidate_factors=forward_factors,
            axis=axis_id,
            score_days=forward_score_days,
        )
        if serving and forward_honest and not accumulating
        else 0.0
    )
    return {
        "stage": stage_id,
        # The partition follows the axis.  This read ``BAND_TABLE_CELLS``
        # literally and would KeyError on a distance fit, whose factors are
        # keyed on the five distance cells and not the eleven claim ones.
        "factors": tuple((band, factors[band]) for band in cells),
        "axis": axis_id,
        "interval_alpha": float(interval_alpha),
        "forward_nll_delta_vs_identity": forward_nll_delta_vs_identity,
        "evidence": {
            "schema": "band_reliability_fit_evidence_v2",
            "stage": stage_id,
            "axis": axis_id,
            "serving_day_count": len(serving),
            "serving_station_day_count": station_days,
            "gate_day_count": len(gate),
            "min_qualifying_days": MIN_QUALIFYING_DAYS,
            "min_cell_stations": MIN_CELL_STATIONS,
            "incumbent_veto": incumbent_veto,
            "accumulating": accumulating,
            "requires_forward_check": requires_forward_check,
            "forward_fit_days": list(forward_fit_days),
            "forward_score_days": list(forward_score_days),
            "forward_factors": [
                [list(band), forward_factors[band]] for band in cells
            ],
            "basis": basis,
            "serving_report": _cell_json(serving_report),
            "gate_report": _cell_json(gate_report),
        },
        "serving_day_count": len(serving),
        "serving_station_day_count": station_days,
        "accumulating": accumulating,
    }


def refit_all_stages(
    conn: sqlite3.Connection,
    *,
    bootstrap_samples: int = 1000,
    commit: bool = False,
    axis: str | Mapping[str, str] | None = None,
    interval_alpha: float = DEFAULT_INTERVAL_ALPHA,
) -> dict[str, Any]:
    """Fit every stage, journal every fit, activate when committing.

    ``axis=None`` refits each stage on the axis it is ALREADY serving, which
    is the only safe default once a stage can be on either.  Defaulting to
    ``claim`` instead would have silently reverted P0 on the next nightly
    sweep -- charter 105's switch landed at 2026-09-08T00:44Z and this sweep
    runs every 24 hours, so it had about seven hours to live.  The axis is a
    property of the table, and moving it is a decision somebody makes on
    purpose; an explicit ``axis`` here is that decision.

    That decision is PER STAGE, because the evidence for it is.  Measured
    2026-09-09 on the live store, fitting each stage on both axes: P1 puts
    all five distance cells through the forward window on a monotone curve
    (1.089, 1.056, 0.960, 0.876, 0.672) where the claim axis leaves seven of
    eleven at identity and one refused forward, while P0-M ties at two live
    cells either way on the thinnest sample of the three.  A single global
    axis would have made moving P1 mean moving P0-M on no evidence.  Pass a
    mapping (``{"P1": "centre_distance"}``) to move one; a bare string still
    means every stage, and an unlisted stage keeps the axis it serves.

    The stages are isolated from one another.  They share nothing but the
    connection: three distributions, three tables, three journal rows, and
    the fit of one says nothing about the fit of another.  So a stage that
    throws -- losing the write-lock race to the resident is the way this
    actually happens (2026-08-20) -- records ``error`` in its own entry and
    the rest are still fitted, activated and journalled.  P0 failing used
    to cost P0-M and P1 even their journal rows, which is the opposite of
    what a journal is for: the trail of what was known on a given day has
    to survive the day's accidents.

    A partial refit is still a failed refit; what a caller does about it is
    the caller's call.  The CLI exits non-zero on any ``error``, so the
    cron reports ✗ without discarding the stages that did land.

    ``interval_alpha`` is the counterfactual the fit's own comment promised:
    the served factor is the end of this interval nearest one, so the width
    IS how hard a table is allowed to pull, and measuring a different width
    was always meant to happen through this code path rather than a second
    copy of it.  Measured 2026-09-09 on P0's serving lane, the shipped 95%
    end bought +0.0155 nat out of sample where the point estimate on the
    same axis bought +0.0497 -- so the width is worth asking about, and the
    only honest way to ask is with production's own distance coordinate,
    which a hand-rolled replay does not have.

    It cannot be committed.  A non-default width is a measurement, and the
    fit's constants are a ruling; letting a flag activate a table nobody
    ruled on is exactly the drift the default exists to prevent.
    """

    if (
        commit
        and abs(float(interval_alpha) - DEFAULT_INTERVAL_ALPHA) > 1e-12
    ):
        raise ContractError("band_interval_alpha_counterfactual_not_committable")

    results: dict[str, Any] = {}
    for stage in BAND_STAGES:
        entry: dict[str, Any] = {"stage": stage}
        results[stage] = entry
        if stage not in BAND_SERVED_STAGES:
            entry["served_stage"] = False
            if commit:
                try:
                    entry["commit"] = retire_band_table(conn, stage=stage)
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = f"{type(exc).__name__}: {exc}"
            continue
        try:
            if isinstance(axis, Mapping):
                requested = axis.get(stage)
            else:
                requested = axis
            stage_axis = (
                str(requested) if requested is not None
                else band_table_axis(conn, stage=stage)
            )
            entry["axis"] = stage_axis
            fitted = fit_band_reliability_table(
                conn, stage=stage, bootstrap_samples=bootstrap_samples,
                axis=stage_axis, interval_alpha=interval_alpha,
            )
            active = load_band_table(conn, stage=stage)
            entry.update({
                "fitted_factors": [
                    [list(band), factor]
                    for band, factor in fitted["factors"]
                ],
                "fitted_table_sha256": band_table_sha256(
                    fitted["factors"], stage=stage, axis=stage_axis,
                ),
                "active_table_sha256": (
                    None if active is None else active[1]
                ),
                "serving_day_count": fitted["serving_day_count"],
                "serving_station_day_count": fitted[
                    "serving_station_day_count"
                ],
                "accumulating": fitted["accumulating"],
                "basis": fitted["evidence"]["basis"],
                "interval_alpha": fitted["interval_alpha"],
                "forward_nll_delta_vs_identity": fitted[
                    "forward_nll_delta_vs_identity"
                ],
            })
            entry["vetoed"] = bool(fitted.get("vetoed"))
            if commit:
                try:
                    if fitted.get("vetoed"):
                        # Retire, do not replace.  Activating an identity
                        # table would stop the unpinned readers and leave
                        # every artifact pinned to the vetoed sha serving
                        # it until the artifact expired.
                        outcome = retire_band_table(conn, stage=stage)
                    else:
                        outcome = persist_and_activate_band_table(
                            conn,
                            stage=stage,
                            axis=stage_axis,
                            factors=fitted["factors"],
                            evidence=fitted["evidence"],
                            serving_day_count=fitted["serving_day_count"],
                        )
                except Exception as exc:  # noqa: BLE001
                    # The fit itself succeeded; only the activation did
                    # not.  Journal that fact -- what was known on the day
                    # must survive the day's accidents -- knowing the
                    # journal needs the same lock that may just have
                    # refused: the attempt spends its own retry budget,
                    # and if it also fails the error lands in the entry
                    # rather than taking the remaining stages down.
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    try:
                        entry["fit_id"] = journal_band_fit(
                            conn,
                            stage=stage,
                            fitted=fitted,
                            outcome="FIT_NOT_ACTIVATED",
                            axis=stage_axis,
                        )
                    except Exception as journal_exc:  # noqa: BLE001
                        try:
                            conn.rollback()
                        except sqlite3.Error:
                            pass
                        entry["journal_error"] = (
                            f"{type(journal_exc).__name__}: {journal_exc}"
                        )
                    continue
                entry["commit"] = outcome
                entry["fit_id"] = journal_band_fit(
                    conn,
                    stage=stage,
                    fitted=fitted,
                    outcome=(
                        "INCUMBENT_VETOED"
                        if fitted.get("vetoed")
                        else "ACCUMULATING"
                        if fitted["accumulating"]
                        else str(outcome["state"])
                    ),
                    axis=stage_axis,
                )
        except Exception as exc:  # noqa: BLE001 -- isolation is the point
            # Whatever the stage was in the middle of, the next stage must
            # start from a clean connection: a transaction left open here
            # would fail the next ``BEGIN IMMEDIATE`` for an unrelated
            # reason and turn one lost stage into three.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            entry["error"] = f"{type(exc).__name__}: {exc}"
    return results
