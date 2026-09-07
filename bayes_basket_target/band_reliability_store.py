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
import random
import sqlite3
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from math import log
from typing import Any

from .contracts import ContractError, content_sha256

BAND_STAGES = ("P0", "P0-M", "P1")
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
FORWARD_WINDOW_FRACTION = 0.4
# Below this a stage is fitted and journalled but serves identity: ten
# days is where the by-day bootstrap stops being a re-description of two
# or three weather regimes.  A stage under it is accumulating, not broken.
MIN_QUALIFYING_DAYS = 10
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
    " activated_at_utc TEXT"
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
    "  ('ACTIVATED','UNCHANGED','ACCUMULATING','FIT_NOT_ACTIVATED')),"
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
)


def ensure_band_table_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE_DDL)
    conn.execute(_JOURNAL_DDL)
    conn.execute(_JOURNAL_STAGE_INDEX)
    columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(calibration_band_reliability_table)"
        )
    }
    if "stage" not in columns:
        # Pre-stage bootstrap rows were all P0 by construction.
        conn.execute(
            "ALTER TABLE calibration_band_reliability_table"
            " ADD COLUMN stage TEXT NOT NULL DEFAULT 'P0'"
        )
    if "axis" not in columns:
        # Which quantity the cells are read from (user ruling, 2026-09-07:
        # "band 表换轴").  Rows written before the column existed were all
        # fitted on the claim, so that is the default and no stored table
        # changes meaning.  It travels with the table rather than with the
        # artifact because charter 60 already pins the table by sha: a
        # pinned table that could not say which axis it was scored on would
        # be served through the wrong lookup, which is the one way this
        # change could reach an artifact it was never gated against.
        conn.execute(
            "ALTER TABLE calibration_band_reliability_table"
            " ADD COLUMN axis TEXT NOT NULL DEFAULT 'claim'"
        )
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


def load_band_table(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    table_sha256: str | None = None,
) -> tuple[tuple[tuple[tuple[float, float], float], ...], str] | None:
    """The stage's active table, or the pinned version an artifact names.

    A pinned sha that is missing fails closed: the artifact's scored math
    cannot be reproduced, so it must not be served an approximation.

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
        row = conn.execute(
            "SELECT band_table_sha256, factors_json, COALESCE(axis, ?)"
            " FROM calibration_band_reliability_table"
            " WHERE band_table_sha256=?",
            (BAND_AXIS_CLAIM, str(table_sha256)),
        ).fetchone()
        if row is None:
            raise ContractError("band_reliability_pinned_table_missing")
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

    loaded = load_band_table(conn, stage=stage, table_sha256=table_sha256)
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
        row = conn.execute(
            "SELECT axis FROM calibration_band_reliability_table"
            " WHERE activated=1 AND stage=?",
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


def journal_band_fit(
    conn: sqlite3.Connection,
    *,
    stage: str,
    fitted: Mapping[str, Any],
    outcome: str,
    fitted_at_utc: str | None = None,
) -> str:
    """Record one fit -- what was measured, what it produced, what happened."""

    ensure_band_table_schema(conn)
    stage_id = _check_stage(stage)
    now = fitted_at_utc or (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    factors = tuple(fitted["factors"])
    sha = band_table_sha256(factors, stage=stage_id)
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
) -> dict[tuple[str, str], list[list[tuple[float, int, float]]]]:
    """First decision per station-day for one stage, as pre-band claims.

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
    rows = conn.execute(
        "SELECT h.station_id, h.event_date, h.history_id,"
        " h.envelope_json, h.checkpoint_id, h.envelope_content_sha256,"
        " MAX(h.rowid)"
        " FROM basket_probability_bridge_history h"
        " JOIN (SELECT station_id, event_date,"
        "         MIN(decision_at_utc) AS first_decision_at"
        "       FROM basket_probability_bridge_history"
        "       WHERE stage=? GROUP BY station_id, event_date) f"
        "   ON f.station_id = h.station_id AND f.event_date = h.event_date"
        "  AND h.decision_at_utc = f.first_decision_at"
        " WHERE h.stage=?"
        " GROUP BY h.station_id, h.event_date",
        (_check_stage(stage), _check_stage(stage)),
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
) -> dict[str, list[list[tuple[float, int]]]]:
    """The fit's day-keyed view of ``serving_lane_by_station_day``."""

    by_day: dict[str, list[list[tuple[float, int]]]] = defaultdict(list)
    for (_station, event), day_rows in serving_lane_by_station_day(
        conn, truth, stage=stage, pre_band=pre_band,
    ).items():
        for entries in day_rows:
            by_day[str(event)].append([
                (probability, hit) for probability, hit, _lower in entries
            ])
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
        records = []
        for key in sorted(
            probabilities, key=lambda k: float(centers.get(k, k))
        ):
            center = float(centers.get(key, key))
            records.append((
                float(probabilities[key]),
                1 if (center - 0.5) <= actual < (center + 0.5) else 0,
            ))
        by_day[str(event)].append(records)
    return dict(by_day)


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


def band_lane_report(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    *,
    bands: Sequence[tuple[float, float]] = FINE_BANDS,
    bootstrap_samples: int = 1000,
    seed: int = 17,
    axis: str = BAND_AXIS_CLAIM,
) -> dict[tuple[float, float], dict[str, Any]]:
    days = sorted(by_day)
    if not days:
        return {}
    aggregates = _day_aggregates(by_day, bands, axis)

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
        sample = [random.choice(days) for _ in days]
        sampled = ratios(sample)
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
        low = ordered[int(0.025 * len(ordered))]
        high = ordered[int(0.975 * len(ordered))]
        interval_excludes_one = not (low <= 1.0 <= high)
        sign_stable = (
            first[band] is not None
            and second[band] is not None
            and (first[band] - 1.0) * (second[band] - 1.0) > 0
        )
        qualified = interval_excludes_one and sign_stable
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
                )
            ),
            "conservative_factor": min(
                FACTOR_CEILING, max(FACTOR_FLOOR, conservative)
            ),
        }
    return report


def _forward_nll_delta(
    by_day: Mapping[str, list[list[tuple[float, int]]]],
    *,
    base_factors: Mapping[tuple[float, float], float],
    candidate_factors: Mapping[tuple[float, float], float],
) -> float:
    """NLL improvement of candidate over base on the trailing window."""

    days = sorted(by_day)
    if not days:
        return 0.0
    cut = max(1, int(len(days) * (1.0 - FORWARD_WINDOW_FRACTION)))
    window = days[cut:] or days[-1:]

    def nll(factors: Mapping[tuple[float, float], float]) -> float:
        total = 0.0
        for day in window:
            for records in by_day[day]:
                mass = 0.0
                corrected = []
                for probability, hit in records:
                    factor = 1.0
                    for band, value in factors.items():
                        if band[0] <= probability < band[1]:
                            factor = value
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


def fit_band_reliability_table(
    conn: sqlite3.Connection,
    *,
    stage: str = "P0",
    bootstrap_samples: int = 1000,
    axis: str = BAND_AXIS_CLAIM,
) -> dict[str, Any]:
    """Fit one stage's table under the fixed admission rule.

    A stage with too little settled history is not an error: it fits to
    identity, records why, and promotes itself the day the evidence
    clears the hurdles.
    """

    stage_id = _check_stage(stage)
    axis_id = str(axis)
    if axis_id not in BAND_AXES:
        raise ValueError(f"unknown band axis: {axis_id}")
    # Charter 105: the partition follows the axis, so a distance fit cannot
    # be scored on claim cells or the other way round.
    cells = cells_for_axis(axis_id)
    truth = _truth(conn)
    serving = serving_lane_by_day(conn, truth, stage=stage_id)
    station_days = sum(len(rows) for rows in serving.values())
    # Only P0 has a second geometry to transfer from: the OOS prediction
    # archive is the cross-ensemble blend, which is exactly P0's root and
    # nothing else's.
    gate = (
        gate_lane_by_day(conn, truth) if stage_id == "P0" else {}
    )
    # A stage whose distribution no gate re-scores has to earn every cell
    # on the forward window here instead.
    requires_forward_check = stage_id != "P0"
    accumulating = len(serving) < MIN_QUALIFYING_DAYS
    serving_report = (
        {}
        if accumulating
        else band_lane_report(
            serving,
            bands=cells,
            bootstrap_samples=bootstrap_samples,
            axis=axis_id,
        )
    )
    gate_report = (
        band_lane_report(
            gate,
            bands=cells,
            bootstrap_samples=bootstrap_samples,
            axis=axis_id,
        )
        if gate
        else {}
    )
    factors: dict[tuple[float, float], float] = {
        band: 1.0 for band in cells
    }
    basis: dict[str, str] = {}
    for band in cells:
        key = str(list(band))
        if accumulating:
            basis[key] = "accumulating_evidence"
            continue
        cell = serving_report.get(band)
        if cell is None:
            basis[key] = "no_sample"
            continue
        if not cell["qualified"]:
            basis[key] = "identity:" + str(cell["blocked_by"])
            continue
        if requires_forward_check:
            candidate = dict(factors)
            candidate[band] = cell["conservative_factor"]
            if _forward_nll_delta(
                serving,
                base_factors=factors,
                candidate_factors=candidate,
            ) <= 0:
                basis[key] = "identity:forward_window_not_improved"
                continue
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
        candidate = dict(factors)
        candidate[band] = gate_cell["conservative_factor"]
        if _forward_nll_delta(
            serving,
            base_factors=factors,
            candidate_factors=candidate,
        ) > 0:
            factors[band] = gate_cell["conservative_factor"]
            basis[str(list(band))] = "gate_transfer"

    def _cell_json(
        report: Mapping[tuple[float, float], dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            str(list(band)): dict(cell) for band, cell in report.items()
        }

    return {
        "stage": stage_id,
        # The partition follows the axis.  This read ``BAND_TABLE_CELLS``
        # literally and would KeyError on a distance fit, whose factors are
        # keyed on the five distance cells and not the eleven claim ones.
        "factors": tuple((band, factors[band]) for band in cells),
        "axis": axis_id,
        "evidence": {
            "schema": "band_reliability_fit_evidence_v2",
            "stage": stage_id,
            "axis": axis_id,
            "serving_day_count": len(serving),
            "serving_station_day_count": station_days,
            "gate_day_count": len(gate),
            "min_qualifying_days": MIN_QUALIFYING_DAYS,
            "accumulating": accumulating,
            "requires_forward_check": requires_forward_check,
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
    axis: str | None = None,
) -> dict[str, Any]:
    """Fit every stage, journal every fit, activate when committing.

    ``axis=None`` refits each stage on the axis it is ALREADY serving, which
    is the only safe default once a stage can be on either.  Defaulting to
    ``claim`` instead would have silently reverted P0 on the next nightly
    sweep -- charter 105's switch landed at 2026-09-08T00:44Z and this sweep
    runs every 24 hours, so it had about seven hours to live.  The axis is a
    property of the table, and moving it is a decision somebody makes on
    purpose; an explicit ``axis`` here is that decision, applied to every
    stage.

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
    """

    results: dict[str, Any] = {}
    for stage in BAND_STAGES:
        entry: dict[str, Any] = {"stage": stage}
        results[stage] = entry
        try:
            stage_axis = (
                str(axis) if axis is not None
                else band_table_axis(conn, stage=stage)
            )
            entry["axis"] = stage_axis
            fitted = fit_band_reliability_table(
                conn, stage=stage, bootstrap_samples=bootstrap_samples,
                axis=stage_axis,
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
            })
            if commit:
                try:
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
                        "ACCUMULATING"
                        if fitted["accumulating"]
                        else str(outcome["state"])
                    ),
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
