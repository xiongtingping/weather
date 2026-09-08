"""DB-resident, content-addressed lambda tables for charter 68's effective edge.

Charter 68 has the objective read ``lambda(a) * p`` instead of ``p``, where
``a`` is the market's agreement with a basket -- its price over its claim --
and ``lambda`` is the fraction of that claim history says actually arrives.
The table it shipped with was fitted once, by hand, on the 189 opening seats
settled before 2026-08-27.  A fitted constant is a fitted constant: the
selection distribution moves and the number stops describing it.

The warning is older than this module and is written into
``SELECTION_DISCOUNT`` in the ledger: the ratio is endogenous to selection
intensity, so tightening changes which seats survive and the factor you
measured no longer holds.  The failure mode that warning implies is worse
than drift -- it is blindness.  A rule that refuses the seats it distrusts
stops observing exactly the band it most needs to keep measuring, and the
stalest number in the table becomes the one nobody can refresh.

That failure was first answered structurally -- the chain ticket kept the
seat when the shrink sized it to nothing -- and charter 96 then retired the
ticket, which took the answer with it.  It is answered properly now, by the
population this module fits on (user ruling, 2026-09-07): **every window the
enumeration offered, not the ones the gate admitted.**  A seat exists
because ``lambda(a) > a`` held, so fitting lambda on seats conditions the
sample on the estimate.  Within a band the admitted windows are the cheapest
ones; cheap is where the market disagrees with us most; and that is
measurably where this model is worst -- so the estimate comes out low, the
gate tightens, and the next window's sample is drawn from a cheaper slice
still.  A ratchet, and it had turned: over 923 day-ahead station-days the
seated basis reads 0.159/0.368/0.795/0.686/0.650/0.979 -- not even monotone
-- against 0.237/0.599/0.764/0.812/0.925/1.231 on the same days' enumerated
windows, low in every band and by 40-50% in two.  A refused window still
resolves, and it still says what a claim of that size at that agreement was
worth; there is no longer any band the rule can blind itself to.

Measured at the basket, deliberately, and now with the test to show for it.
Per-bucket agreement is the tempting basis -- every priced bucket is an
observation, no selection touches it, and the aggregation is *exact* in
expectation: a window's agreement is precisely the claim-weighted mean of
its legs' agreements, and so is its lambda.  Measured out of sample it is
nonetheless worse (2026-09-07, 923 station-days split by day into two folds,
each fitted on one and scored on the other): the basket table lands on the
realised hit rate to three decimals (0.706 predicted against 0.706 actual,
0.697 against 0.697) while the per-bucket aggregation over-predicts by 8-9
points and loses 12-14% of Brier, at every claim floor from 0 to 0.05.  The
reason is that ``a_bucket`` alone does not distinguish the market's
favourite from a shoulder, and a window's legs are not a random draw from
their band -- they cluster on the centre, where the favourite lives, so the
top band's 1.69 gets applied to all three legs at once.  The window-level
``a`` encodes that distinction implicitly.  The rule shrinks baskets, the
estimator measures baskets, and the per-bucket reading stays a diagnostic.

Admission rule per band (fixed; the table changes, the rule does not):

* at least ``MIN_SEATS_PER_BAND`` scored candidate windows inside the
  rolling window, spanning ``MIN_STATION_DAYS_PER_BAND`` distinct
  station-days, and claim mass above ``MIN_CLAIM_MASS`` -- below any of the
  three, the band keeps the incumbent value and is reported as accumulating.
  The station-day threshold is not decoration: windows nest, one book cut
  six ways against one outcome is one observation wearing six hats, and
  without it twelve rows can be two days;
* the fitted value is clamped to ``[LAMBDA_FLOOR, LAMBDA_CEILING]``, which
  is not a statistical statement but a blast radius: a band that reads 0.02
  on a bad fortnight would zero every basket in it, and one that reads 3.0
  would size on a claim nobody made.

P1 is excluded from the fit for the same reason charter 68 exempts it from
the rule: its realized ratio runs above one exactly where the market
disagrees, because its edge is the observation and not the shape.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

from .band_reliability_store import BAND_TABLE_CELLS
from .basket_kernel.delta_v2 import (
    center_window_claim_factor,
    claim_transform_fingerprint,
    EFFECTIVE_EDGE_LAMBDA,
    EFFECTIVE_EDGE_P0M_LAMBDA,
    EFFECTIVE_EDGE_P1_LAMBDA,
)
from .contracts import content_sha256


# The rolling window the fit reads.  Long enough that a quiet week cannot
# empty a band, short enough that a regime two months gone stops voting.
WINDOW_DAYS = 28
MIN_SEATS_PER_BAND = 12
MIN_CLAIM_MASS = 4.0
# Windows nest.  A station-day offers six or so overlapping candidates cut
# from one book against one outcome, so twelve rows can be two days and the
# seat threshold above stops meaning what it meant when a row was a seat.
# The independent unit is the station-day; this is the threshold that keeps
# "a fortnight rewrites the rule" from coming back through the new door.
MIN_STATION_DAYS_PER_BAND = 8
LAMBDA_FLOOR = 0.05
LAMBDA_CEILING = 1.50
# One scope per stage (user ruling, 2026-09-05).  P0-M used to be pooled
# into P0's scope and, measured on the live book, contributed zero seats to
# it in the 28-day window: the pool was P0's table under two names.  Fitting
# it alone makes its scarcity visible -- every band accumulating, served at
# the incumbent -- instead of hiding it inside P0's numbers.
FIT_STAGES = ("P0",)
FIT_STAGES_P0M = ("P0-M",)
# P1 is fitted as its own scope, never pooled with the two above (user
# ruling, 2026-09-02).  Pooling would let P1's over-delivering bands lift the
# P0 numbers and P0's cliff drag P1's, and each would then size on a mixture
# nobody trades.  Its ceiling is one: the exemption charter 68 wrote was
# "trust the observation", never "trust it more than it trusts itself", so a
# band where P1 under-claims is served at par, not inflated.
FIT_STAGES_P1 = ("P1",)
P1_LAMBDA_CEILING = 1.00
STAGE_SCOPES = {
    "P0": FIT_STAGES,
    "P0-M": FIT_STAGES_P0M,
    "P1": FIT_STAGES_P1,
}
# Only seats whose claim went through the current band partition count
# (user ruling, 2026-09-05).  Lambda is fitted on the served claim, and the
# served claim is what the band table made of it; a seat banded under the
# retired five-cell table is a residual of arithmetic nobody serves.  The
# partition is identified by the receipt's table sha, never by date: charter
# 60 keeps old tables serving the artifacts pinned to them, so a calendar
# cutover would admit exactly the rows it means to exclude.
BAND_PARTITION_CELLS = len(BAND_TABLE_CELLS)

# Shadow lane, user ruling 2026-09-06 ("开影子"): the same table with one more
# axis -- which way the market moved the seat's window over the hours before
# the decision.  Measured that day on 5,922 checkpoints (852 station-days,
# 45 stations): unstratified, the move's direction separates realised/claimed
# by 40-50 hit points; stratified by the CURRENT agreement a, eight to nine
# points survive.  So the move is four fifths the level this table already
# reads, and the question the lane accumulates evidence for is whether the
# remaining fifth prices anything -- on real seats, per band.  It measures
# and journals; nothing serves it, and the ledger's fingerprint does not
# know it exists.  A P0 opening seat has no prior belief by construction
# (charter 48 opens at listing; the first book and the first belief land in
# the same minute), so its tier is ``no_prior`` and the lane says so rather
# than inventing a move.
DQ_SHADOW_MODE = str(
    os.environ.get("PM_EFFECTIVE_EDGE_DQ_SHADOW", "shadow") or "shadow"
).strip().lower()
DQ_SHADOW_HORIZON_HOURS = 6.0
FORWARD_SERVING_SCORE_SCHEMA = "effective_edge_forward_serving_score_v1"
DQ_SHADOW_THRESHOLD = 0.05
DQ_TIERS = ("down", "steady", "up", "no_prior")

TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_effective_edge_lambda ("
    " lambda_table_sha256 TEXT PRIMARY KEY,"
    " bands_json TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " window_days INTEGER NOT NULL,"
    " seat_count INTEGER NOT NULL,"
    " activated INTEGER NOT NULL CHECK(activated IN (0,1)),"
    " activated_at_utc TEXT)"
)
SHADOW_DQ_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_effective_edge_lambda_dq_shadow ("
    " shadow_sha256 TEXT PRIMARY KEY,"
    " stage_scope TEXT NOT NULL,"
    " tiers_json TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " window_days INTEGER NOT NULL,"
    " seat_count INTEGER NOT NULL,"
    " horizon_hours REAL NOT NULL,"
    " threshold REAL NOT NULL)"
)
# When each table actually served, so a table can be scored on the seats it
# was serving for rather than on the ones it was fitted on.
SERVING_INTERVAL_DDL = (
    "CREATE TABLE IF NOT EXISTS"
    " calibration_effective_edge_serving_interval ("
    " stage_scope TEXT NOT NULL,"
    " lambda_table_sha256 TEXT NOT NULL,"
    " served_from_utc TEXT NOT NULL,"
    " served_until_utc TEXT,"
    " PRIMARY KEY(stage_scope, served_from_utc))"
)
JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS calibration_effective_edge_fit_journal ("
    " fit_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " lambda_table_sha256 TEXT NOT NULL,"
    " fitted_at_utc TEXT NOT NULL,"
    " admitted INTEGER NOT NULL,"
    " accumulating_bands TEXT NOT NULL,"
    " evidence_json TEXT NOT NULL)"
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE_DDL)
    conn.execute(JOURNAL_DDL)
    conn.execute(SHADOW_DQ_DDL)
    conn.execute(SERVING_INTERVAL_DDL)
    # Rows written before P1 had a scope are P0 rows; the default says so.
    for table in (
        "calibration_effective_edge_lambda",
        "calibration_effective_edge_fit_journal",
    ):
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if "stage_scope" not in columns:
            conn.execute(
                f"ALTER TABLE {table}"
                " ADD COLUMN stage_scope TEXT NOT NULL DEFAULT 'P0'"
            )


EFFECTIVE_EDGE_SCOPES_SERVED = ("P0", "P0-M", "P1")


def _bootstrap_bands(stage_scope: str = "P0") -> list[tuple[float, float]]:
    """The kernel constant a scope starts from before any fit is activated."""

    source = {
        "P1": EFFECTIVE_EDGE_P1_LAMBDA,
        "P0-M": EFFECTIVE_EDGE_P0M_LAMBDA,
    }.get(stage_scope, EFFECTIVE_EDGE_LAMBDA)
    return [(float(bound), float(lam)) for bound, lam in source]


def _incumbent_bands(
    conn: sqlite3.Connection | None = None, stage_scope: str = "P0"
) -> list[tuple[float, float]]:
    """What a band keeps when this fit cannot measure it.

    The last activated table for the scope (user ruling, 2026-09-05: the
    lane promotes itself, so what it last promoted is what a thin band
    holds), and the kernel constant only before anything was ever
    activated.  Reading the constant every time would snap a thin band
    back to the hand-fitted bootstrap each morning and un-promote the
    fit that had cleared the hurdle the day before.
    """

    if conn is not None:
        active = load_active_lambda(conn, stage_scope=stage_scope)
        if active:
            return _project_onto_partition(
                [(float(bound), float(lam)) for bound, lam in active],
                _bootstrap_bands(stage_scope),
            )
    return _bootstrap_bands(stage_scope)


def _project_onto_partition(
    stored: Sequence[tuple[float, float]],
    partition: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Carry a stored table onto the partition the code now serves.

    The band VALUES are evidence and live in the database; the band
    BOUNDARIES are a rule and live in the kernel constant.  When the rule
    changes -- charter 96 splitting the top band at 0.95 -- the stored
    values still describe the seats they were fitted on, so each new band
    inherits the stored value of whichever old band contained it and then
    fits itself from its own seats as they settle.  Nothing served moves on
    the day of a split, which is the same migration charter 68(b) used when
    P0-M left P0's scope.

    Identity when the boundaries already agree, so the ordinary day pays
    one tuple comparison.
    """

    stored = [(float(bound), float(lam)) for bound, lam in stored]
    bounds = [float(bound) for bound, _lam in partition]
    if [bound for bound, _lam in stored] == bounds:
        return stored
    out: list[tuple[float, float]] = []
    lower = 0.0
    for bound in bounds:
        # The new band's own lower edge names the old band that held it --
        # every point of the new band lies inside one old band, because a
        # split only adds boundaries and never moves one.
        out.append((bound, float(stored[_band_index(lower, stored)][1])))
        lower = bound
    return out


def _band_partition_by_position(
    conn: sqlite3.Connection, position_ids: Sequence[str]
) -> dict[str, int | None] | None:
    """Which band partition each seat's claim was served under.

    Read off the envelope receipt the seat's decision moment carries, then
    the partition size of the table that receipt names.  None for the whole
    lookup when the database has no envelopes or band tables -- a replay
    fixture, or a book built before receipts existed -- in which case the
    caller counts every seat and says so, rather than silently fitting on
    nothing.  Per seat, None means no receipt: an unbanded claim, which is
    not a claim under the current partition either.
    """

    if not position_ids:
        return {}
    try:
        sizes = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT band_table_sha256, json_array_length(factors_json)"
                " FROM calibration_band_reliability_table"
            )
        }
        rows = conn.execute(
            "SELECT p.position_id,"
            " (SELECT json_extract(e.lineage_json,'$.band_reliability_json')"
            "    FROM basket_probability_bridge_history h"
            "    JOIN basket_probability_envelope e"
            "      ON e.envelope_id = h.checkpoint_id"
            "   WHERE h.station_id = p.station_id"
            "     AND h.event_date = p.event_date"
            "     AND h.stage = p.stage"
            "     AND h.decision_at_utc = p.decision_at_utc"
            "   LIMIT 1)"
            " FROM paper_trade_position p"
            f" WHERE p.position_id IN ({','.join('?' for _ in position_ids)})",
            tuple(position_ids),
        ).fetchall()
    except sqlite3.Error:
        return None
    out: dict[str, int | None] = {}
    for position_id, raw in rows:
        sha = None
        if raw:
            try:
                sha = json.loads(str(raw)).get("band_table_sha256")
            except (ValueError, AttributeError):
                sha = None
        out[str(position_id)] = sizes.get(str(sha)) if sha else None
    return out


def _has_candidate_column(conn: sqlite3.Connection, name: str) -> bool:
    """Whether the candidate table carries one of charter 110's keys."""

    try:
        return any(
            str(row[1]) == name
            for row in conn.execute(
                "PRAGMA table_info(paper_trade_candidate_window)"
            )
        )
    except sqlite3.Error:
        return False


def _has_band_axis_column(conn: sqlite3.Connection) -> bool:
    """Whether the candidate table carries charter 110's partition key."""

    return _has_candidate_column(conn, "band_axis")


def _active_claim_vintage(
    conn: sqlite3.Connection, stage_scope: str
) -> list[str] | None:
    """The claim-transform fingerprint the served table was fitted under.

    ``None`` for a table activated before the key existed, which is not the
    current vintage: those rows were fitted on claims this code no longer
    computes, and saying so is the whole point of the key.
    """

    try:
        row = conn.execute(
            "SELECT bands_json FROM calibration_effective_edge_lambda"
            " WHERE activated=1 AND stage_scope=?",
            (stage_scope,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        payload = json.loads(str(row[0]))
    except (TypeError, ValueError):
        return None
    value = payload.get("claim_transform_sha256")
    if isinstance(value, list) and value:
        return [str(item) for item in value]
    return None


def _fit_axis(
    conn: sqlite3.Connection, stages: Sequence[str]
) -> str | None:
    """The band axis this fit is for: the one those stages serve today.

    ``None`` when no stage in the set has an activated band table, which is
    also what a candidate row written before the column existed carries --
    so a database with no band tables at all still fits on its whole
    history rather than on nothing.
    """

    try:
        from .band_reliability_store import (
            band_table_axis,
            load_band_table,
        )
    except Exception:  # noqa: BLE001 -- a lane without the store fits as before
        return None
    seen = []
    for stage in stages:
        try:
            # Existence first.  ``band_table_axis`` answers ``claim`` for a
            # database that has never held a table, because that is what
            # every row written before the column meant -- but "no table at
            # all" is not a vintage, and partitioning on it would exclude
            # every candidate row in a fixture or a replay extract that
            # never had band lineage to record.  No table, no partition.
            if load_band_table(conn, stage=str(stage)) is None:
                continue
            seen.append(band_table_axis(conn, stage=str(stage)))
        except Exception:  # noqa: BLE001 -- an absent table is not an error
            continue
    if not seen:
        return None
    # One axis per fit.  Stages on different axes cannot be pooled, and the
    # caller groups stages that share one (EFFECTIVE_EDGE_STAGES and its
    # siblings are already split that way).
    distinct = sorted(set(seen))
    return distinct[0] if len(distinct) == 1 else None


def _band_partition_by_decision(
    conn: sqlite3.Connection,
    stages: Sequence[str],
    floor_day: str,
) -> dict[tuple[str, str, str, str], int | None] | None:
    """The same lookup as above, keyed by the decision rather than the seat.

    A candidate window is not a seat and has no ``position_id``, but it does
    carry the four columns the envelope receipt is found by -- which is what
    the seat lookup was using ``position_id`` to reach anyway.  Same rule,
    same ``None`` semantics: ``None`` for the whole lookup on a fixture with
    no receipts, ``None`` per key for a decision whose envelope names no
    band table.

    Driven from the candidate table rather than from the history: the
    history is four hundred thousand rows and the candidates are a few
    thousand, and scanning the big side of that join took the fit from a
    second to past ten minutes.
    """

    try:
        sizes = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                "SELECT band_table_sha256, json_array_length(factors_json)"
                " FROM calibration_band_reliability_table"
            )
        }
        placeholders = ",".join("?" for _ in stages)
        rows = conn.execute(
            "SELECT c.station_id, c.event_date, c.stage, c.decision_at_utc,"
            " json_extract(e.lineage_json,'$.band_reliability_json')"
            " FROM paper_trade_candidate_window c"
            " JOIN basket_probability_bridge_history h"
            "   ON h.station_id = c.station_id"
            "  AND h.event_date = c.event_date"
            "  AND h.stage = c.stage"
            "  AND h.decision_at_utc = c.decision_at_utc"
            " JOIN basket_probability_envelope e"
            "   ON e.envelope_id = h.checkpoint_id"
            f" WHERE c.stage IN ({placeholders}) AND c.event_date >= ?"
            "   AND c.hit IS NOT NULL"
            " GROUP BY 1, 2, 3, 4",
            (*stages, floor_day),
        ).fetchall()
    except sqlite3.Error:
        return None
    if not sizes:
        return None
    out: dict[tuple[str, str, str, str], int | None] = {}
    for station_id, event_date, stage, decided, raw in rows:
        sha = None
        if raw:
            try:
                sha = json.loads(str(raw)).get("band_table_sha256")
            except (ValueError, AttributeError):
                sha = None
        out[(str(station_id), str(event_date), str(stage), str(decided))] = (
            sizes.get(str(sha)) if sha else None
        )
    return out


def _band_index(agreement: float, bands: Sequence[tuple[float, float]]) -> int:
    for index, (bound, _lam) in enumerate(bands):
        if agreement < bound:
            return index
    return len(bands) - 1


def fit_lambda_table(
    conn: sqlite3.Connection,
    *,
    window_days: int = WINDOW_DAYS,
    as_of: str | None = None,
    stage_scope: str = "P0",
    include_replayed: bool = False,
) -> dict[str, Any]:
    """Fit one lambda per agreement band from the rolling settled window.

    Returns the table plus the evidence for every band, including the ones
    that could not be fitted.  A band short of evidence keeps the incumbent
    value: the alternative -- fitting it anyway -- is how a fortnight with
    four seats in it rewrites the number that governs the largest nominal
    edges on the book.
    """

    if stage_scope not in STAGE_SCOPES:
        raise ValueError(f"effective_edge_fit_scope_invalid:{stage_scope}")
    stages = STAGE_SCOPES[stage_scope]
    ceiling = P1_LAMBDA_CEILING if stage_scope == "P1" else LAMBDA_CEILING
    incumbent = _incumbent_bands(conn, stage_scope)
    newest = conn.execute(
        "SELECT MAX(event_date) FROM paper_trade_settlement"
    ).fetchone()
    latest = str(as_of or (newest[0] if newest else "") or "")
    if not latest:
        raise ValueError("effective_edge_fit_no_settlements")
    floor_day = (
        datetime.fromisoformat(latest).date()
        - __import__("datetime").timedelta(days=int(window_days))
    ).isoformat()

    placeholders = ",".join("?" for _ in stages)
    # EVERY window the enumeration offered, not the one the walk seated.
    #
    # Two corrections live in this one query, and they were found a day
    # apart.  The first: charter 94 made the order a resting ladder, so the
    # seat's own columns became the basket that FILLED -- and on the 97
    # settled seats the legs that went on to win filled 18% of the time
    # against 88% for the legs that went on to lose, because a resting bid
    # is lifted exactly when the market has decided that bucket is not
    # happening.  A fit on those columns reads maker adverse selection and
    # serves it as the honesty of a claim.
    #
    # The second is larger and is what this query is (user ruling
    # 2026-09-07, "lambda 改拟在全部候选窗口上").  A seat exists because the
    # gate admitted it, and the gate admits on lambda(a) > a -- so fitting
    # lambda on seats conditions the sample on the quantity being
    # estimated.  Within a band the admitted windows are the cheapest ones,
    # cheap is where the market disagrees with us most, and that is
    # measurably where this model is worst; so the estimate comes out low,
    # the gate tightens, and the next fortnight's sample is drawn from an
    # even cheaper slice.  A ratchet, and it had turned: measured over 923
    # day-ahead station-days, the seated basis reads 0.159/0.368/0.795/
    # 0.686/0.650/0.979 -- not even monotone -- while the same days'
    # enumerated windows read 0.237/0.599/0.764/0.812/0.925/1.231, low in
    # every band and by 40-50% in two of them.
    #
    # ``win_probability`` here is the LIFTED claim: ``_support_windows``
    # already multiplied by charter 81's centre lift and the off-centre
    # deflation, because that is the number the gate divides the price by.
    # Nothing may lift it again.
    # Charter 110: one axis per fit.  The band axis moves every claim, and
    # the claim is the denominator of ``a = price / claim`` -- the very axis
    # lambda is keyed on.  Measured 2026-09-08 on 757 live candidate
    # windows, switching P0 to the distance axis moved ``a`` on all of them
    # and carried 6.1% across a band boundary into a different lambda value.
    # A fit pooling both vintages is fitting one curve to two different x
    # axes.
    #
    # Rows whose axis is unknown are OUT.  They predate the column and their
    # envelopes no longer say; treating them as "probably the current axis"
    # is the silent borrowing charter 104 forbids for a claim, and a
    # partition key deserves the same rule.  They are counted, not hidden.
    axis = _fit_axis(conn, stages)
    # A candidate table without the column cannot be partitioned on it, and
    # that is an ordinary state rather than an error: a replay extract, a
    # fixture, any database whose ledger has not run the migration yet.  It
    # fits on everything, exactly as it did before charter 110.
    if axis is not None and not _has_band_axis_column(conn):
        axis = None
    axis_clause = "" if axis is None else " AND band_axis IS ?"
    axis_args: tuple[Any, ...] = () if axis is None else (axis,)
    # Charter 110 extended (2026-09-08): the same rule, one layer out.  The
    # band table moves the claim; charter 81's lift, the off-centre
    # deflation and the convergence lift move it again afterwards, and
    # ``a = price / claim`` is keyed on what comes out.  A row written under
    # a different version of those tables carries a claim this code would
    # no longer compute, so it is not evidence about today's ``a``.
    # Two fingerprints, one per path: a row is current if the tables ITS
    # claim went through are the ones this code now serves.  Hashing both
    # into one key would retire every centre-holding row whenever the
    # off-centre table moved, and those rows never read it.
    claim_vintages: tuple[str, ...] | None = (
        claim_transform_fingerprint(holds_centre=True),
        claim_transform_fingerprint(holds_centre=False),
    )
    if not _has_candidate_column(conn, "claim_transform_sha256"):
        # A database whose ledger has not run the migration cannot be
        # partitioned on a column it does not have, and that is ordinary --
        # a fixture, a replay extract.  It fits on everything, as before.
        claim_vintages = None
    claim_clause = (
        ""
        if claim_vintages is None
        else " AND claim_transform_sha256 IN (?,?)"
    )
    claim_args: tuple[Any, ...] = (
        () if claim_vintages is None else claim_vintages
    )
    rows = conn.execute(
        "SELECT win_probability, all_in_price, hit, station_id, event_date,"
        " stage, decision_at_utc"
        " FROM paper_trade_candidate_window"
        f" WHERE stage IN ({placeholders}) AND event_date >= ?"
        "   AND hit IS NOT NULL" + axis_clause + claim_clause,
        (*stages, floor_day, *axis_args, *claim_args),
    ).fetchall()
    served_row_count = len(rows)
    # A band axis change does not kill a settled window, it mis-keys it.  The
    # price the book quoted and the outcome the day settled to do not depend
    # on our probabilities at all; only the claim does.  So the replay lane
    # recomputes the claim on the table that serves today and files the row
    # under the axis it now belongs to, and the fit may read those -- how
    # honest a claim is, is a property of the claim.  The ACTIVATION guard
    # may not: whether a table earns its seat is a question about the book
    # that was really bought, and ``admitted_return`` stays on the served
    # table alone.  Off unless asked for.
    replayed_row_count = 0
    if include_replayed:
        try:
            replayed = conn.execute(
                "SELECT win_probability, all_in_price, hit, station_id,"
                " event_date, stage, decision_at_utc"
                " FROM paper_trade_candidate_window_replay"
                f" WHERE stage IN ({placeholders}) AND event_date >= ?"
                "   AND hit IS NOT NULL" + axis_clause + claim_clause,
                (*stages, floor_day, *axis_args, *claim_args),
            ).fetchall()
        except sqlite3.Error:
            # No replay lane in this database: a fixture, an extract.  Not
            # an error, and not silently the same as an empty one -- the
            # receipt reports zero either way and the caller asked.
            replayed = []
        # A source row that is already current under this axis needs no
        # stand-in; the replay carries its own key and the served row wins.
        seen = {
            (str(r[3]), str(r[4]), str(r[5]), str(r[6])) for r in rows
        }
        replayed = [
            r for r in replayed
            if (str(r[3]), str(r[4]), str(r[5]), str(r[6])) not in seen
        ]
        replayed_row_count = len(replayed)
        rows = list(rows) + replayed
    excluded_axis = 0 if axis is None else conn.execute(
        "SELECT COUNT(*) FROM paper_trade_candidate_window"
        f" WHERE stage IN ({placeholders}) AND event_date >= ?"
        "   AND hit IS NOT NULL AND band_axis IS NOT ?",
        (*stages, floor_day, axis),
    ).fetchone()[0]
    excluded_claim = 0 if claim_vintages is None else conn.execute(
        "SELECT COUNT(*) FROM paper_trade_candidate_window"
        f" WHERE stage IN ({placeholders}) AND event_date >= ?"
        "   AND hit IS NOT NULL"
        "   AND (claim_transform_sha256 IS NULL"
        "        OR claim_transform_sha256 NOT IN (?,?))",
        (*stages, floor_day, *claim_vintages),
    ).fetchone()[0]
    # A band this fit cannot measure keeps the incumbent -- but only while
    # the incumbent describes the same claim.  When the transform has MOVED,
    # the served number was fitted on a quantity that no longer exists, and
    # carrying it forward is the silent borrowing charter 104 forbids.  The
    # scope falls back to the kernel bootstrap and says so.
    #
    # "Moved" means the served table names a vintage and it is not this one.
    # A table that names none predates the key and cannot be shown to have
    # moved, so it is inherited and the fingerprint is recorded on the way
    # past -- which is also the only way out of the deadlock this created
    # when it read absence as change: the bootstrap fit scores worse than the
    # incumbent, the activation guard refuses it, the incumbent is never
    # rewritten, and so it never acquires the vintage that would settle the
    # question.  Measured 2026-09-08: P0 and P0-M both sat in exactly that
    # loop, while P1 escaped only because its bootstrap happened to score
    # identically.
    active_vintage = _active_claim_vintage(conn, stage_scope)
    claim_vintage_changed = bool(
        claim_vintages is not None
        and active_vintage is not None
        and active_vintage != list(claim_vintages)
    )
    if claim_vintage_changed:
        incumbent = _project_onto_partition(
            _bootstrap_bands(stage_scope), _bootstrap_bands(stage_scope)
        )
        tally_incumbent_note = "bootstrap_after_claim_transform_change"
    elif active_vintage is None:
        tally_incumbent_note = "activated_table_vintage_unrecorded"
    else:
        tally_incumbent_note = "activated_table"
    partition = _band_partition_by_decision(conn, stages, floor_day)
    band_filter: dict[str, Any] = {
        "cells": BAND_PARTITION_CELLS,
        "applied": partition is not None,
        "excluded_old_partition": 0,
        "excluded_no_receipt": 0,
    }

    tally = [
        {"seats": 0, "claim": 0.0, "hits": 0, "days": set()}
        for _ in incumbent
    ]
    for win, price, hit, station_id, event_date, stage, decided in rows:
        claim = float(win or 0.0)
        price = float(price or 0.0)
        if claim <= 0.0 or price <= 0.0:
            continue
        if partition is not None:
            cells = partition.get(
                (str(station_id), str(event_date), str(stage), str(decided))
            )
            if cells is None:
                band_filter["excluded_no_receipt"] += 1
                continue
            if cells != BAND_PARTITION_CELLS:
                band_filter["excluded_old_partition"] += 1
                continue
        slot = tally[_band_index(price / claim, incumbent)]
        slot["seats"] += 1
        slot["claim"] += claim
        slot["hits"] += int(hit or 0)
        slot["days"].add((str(station_id), str(event_date)))

    bands: list[tuple[float, float]] = []
    evidence: list[dict[str, Any]] = []
    accumulating: list[int] = []
    for index, (bound, incumbent_lambda) in enumerate(incumbent):
        slot = tally[index]
        enough = (
            slot["seats"] >= MIN_SEATS_PER_BAND
            and slot["claim"] >= MIN_CLAIM_MASS
            and len(slot["days"]) >= MIN_STATION_DAYS_PER_BAND
        )
        raw = (slot["hits"] / slot["claim"]) if slot["claim"] > 0 else None
        if enough and raw is not None:
            served = min(ceiling, max(LAMBDA_FLOOR, raw))
        else:
            served = incumbent_lambda
            accumulating.append(index)
        bands.append((bound, served))
        evidence.append({
            "band_upper_bound": bound if bound != float("inf") else None,
            "seats": slot["seats"],
            # The independent unit behind the row count, printed beside it
            # so a band that looks well-evidenced and is not can be seen.
            "station_days": len(slot["days"]),
            "claim_mass": round(slot["claim"], 3),
            "hits": slot["hits"],
            "raw_lambda": None if raw is None else round(raw, 4),
            "served_lambda": round(served, 4),
            "incumbent_lambda": incumbent_lambda,
            "clamped": bool(
                enough and raw is not None and abs(raw - served) > 1e-9
            ),
            "accumulating": index in accumulating,
        })

    payload = {
        "schema": "effective_edge_lambda_table_v1",
        "stage_scope": stage_scope,
        "stages": list(stages),
        "bands": [
            {"upper_bound": None if b == float("inf") else b, "lambda": lam}
            for b, lam in bands
        ],
        "window_days": int(window_days),
        "as_of_event_date": latest,
        "fit_floor_event_date": floor_day,
        "band_partition_cells": BAND_PARTITION_CELLS,
        # Charter 110 extended: the vintage this table's numbers describe.
        # Stored on the payload because that is what ``_active_claim_vintage``
        # reads back when the next fit asks whether it may inherit.
        "claim_transform_sha256": (
            None if claim_vintages is None else list(claim_vintages)
        ),
    }
    return {
        "table": payload,
        "band_filter": band_filter,
        "stage_scope": stage_scope,
        # Charter 110: which band vintage this fit is for, and how much of
        # the sample the partition left out.  Displayed rather than
        # swallowed -- a fit that quietly halved its own evidence looks
        # exactly like a fit that had half as much.
        "band_axis": axis,
        "excluded_other_band_axis": int(excluded_axis),
        "claim_transform_sha256": (
            None if claim_vintages is None else list(claim_vintages)
        ),
        "excluded_other_claim_transform": int(excluded_claim),
        "claim_transform_changed": claim_vintage_changed,
        "incumbent_basis": tally_incumbent_note,
        "served_row_count": int(served_row_count),
        "replayed_row_count": int(replayed_row_count),
        "include_replayed": bool(include_replayed),
        "content_sha256": content_sha256(payload),
        "bands": bands,
        "evidence": evidence,
        "accumulating_bands": accumulating,
        "seat_count": sum(slot["seats"] for slot in tally),
        # The alarm, and the reason this module exists: a band nobody can
        # measure any more is a band whose number is frozen at whatever it
        # was when the observations stopped.
        "blind_bands": [
            index for index in accumulating if tally[index]["seats"] == 0
        ],
    }


def forward_serving_score(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
) -> dict[str, Any]:
    """What each table did on the seats it was serving for.

    The one reading of a table that the table did not grade itself on.  A
    table is fitted on a trailing window and then serves; the seats decided
    WHILE it served are strictly after its fit, so they are out of sample
    for it in the only sense that matters here -- it chose them, it did not
    learn from them.

    Every other number this module produces is in sample somewhere.  The
    fit's own bands are the trailing window by construction.
    ``admitted_return`` is a counterfactual over candidate windows and its
    docstring is explicit that it is a comparator, not a book return: it
    buys one share of every window a table admits, which for a permissive
    table is most of the enumeration including the windows the objective
    ranked last.  Neither answers "what happened to the money this table
    actually caused to be spent".

    So this reads the seats: claim against outcome for realization, stake
    against pnl for return, per serving interval.  Realization and return
    are reported side by side and neither substitutes for the other --
    measured 2026-09-09 over the 200 settled bought baskets, P1 realized
    1.315 of its claim and still returned -19.2%, because a claim arriving
    more often than promised is not the same as a claim arriving cheaply
    enough.

    Read-only.  It reports; it does not gate.  Whether a forward record
    should bind the activation guard is a policy question, and a lane with
    no history cannot answer it yet.
    """

    scope = str(stage_scope or "").strip() or "P0"
    # No ``ensure_schema`` here, and the docstring above is why: this
    # reports and a reporter must be usable on a read-only handle.  Calling
    # it would create the table as a side effect of asking a question, and
    # would raise on the read-only connection a board or an audit opens.
    # A missing table is answered below, as a database that has not run the
    # migration rather than an error.
    try:
        intervals = conn.execute(
            "SELECT lambda_table_sha256, served_from_utc, served_until_utc"
            " FROM calibration_effective_edge_serving_interval"
            " WHERE stage_scope=? ORDER BY served_from_utc",
            (scope,),
        ).fetchall()
    except sqlite3.Error as exc:
        return {
            "schema": FORWARD_SERVING_SCORE_SCHEMA,
            "stage_scope": scope,
            "available": False,
            "reason": f"{type(exc).__name__}:{exc}"[:120],
            "intervals": [],
        }
    out: list[dict[str, Any]] = []
    for sha, since, until in intervals:
        # An open interval ends now, which is what "still serving" means.
        clause = "" if until is None else " AND p.decision_at_utc < ?"
        args: tuple[Any, ...] = (
            (scope, str(since)) if until is None
            else (scope, str(since), str(until))
        )
        try:
            rows = conn.execute(
                "SELECT p.win_probability, s.hit, s.stake_usdc, s.pnl_usdc"
                " FROM paper_trade_settlement s"
                " JOIN paper_trade_position p"
                "   ON p.position_id=s.position_id"
                " WHERE p.stage=? AND s.hit IS NOT NULL"
                "   AND p.decision_at_utc >= ?" + clause,
                args,
            ).fetchall()
        except sqlite3.Error:
            rows = []
        claimed = sum(float(r[0] or 0.0) for r in rows)
        realized = sum(float(r[1] or 0.0) for r in rows)
        stake = sum(float(r[2] or 0.0) for r in rows)
        pnl = sum(float(r[3] or 0.0) for r in rows)
        out.append({
            "lambda_table_sha256": str(sha),
            "served_from_utc": str(since),
            "served_until_utc": None if until is None else str(until),
            "still_serving": until is None,
            "settled_seats": len(rows),
            "claimed": round(claimed, 6),
            "realized": round(realized, 6),
            # None rather than a number nobody can read: a table that has
            # served no settled seat has no record, and 0.0 would read as
            # one.
            "realized_over_claimed": (
                round(realized / claimed, 6) if claimed > 0 else None
            ),
            "stake_usdc": round(stake, 6),
            "pnl_usdc": round(pnl, 6),
            "roi": round(pnl / stake, 6) if stake > 0 else None,
        })
    return {
        "schema": FORWARD_SERVING_SCORE_SCHEMA,
        "stage_scope": scope,
        "available": True,
        "interval_count": len(out),
        "scored_interval_count": sum(
            1 for row in out if row["settled_seats"] > 0
        ),
        "intervals": out,
    }


def admitted_return(
    conn: sqlite3.Connection,
    bands: Sequence[tuple[float, float]],
    stages: Sequence[str],
    floor_day: str,
) -> dict[str, Any]:
    """What a table admits, and what that admission set is worth.

    One share of every candidate window the table admits -- ``lambda(a) >
    a``, the gate's own rule -- against the price it was quoted and the
    outcome it settled to.  This cannot be computed from seats: a seat
    exists only where some table already admitted it, so the seated book
    has no opinion about what a DIFFERENT table would have bought.  The
    candidate population is what makes the counterfactual exist.

    What it is NOT, and the distinction cost a wrong reading on the day it
    landed: this is not the book's return.  The book seats the objective's
    head window, one per station-day-stage; this buys the whole admission
    set, which for a permissive table is most of the enumeration including
    the windows the objective ranked last.  Read as a book return it makes
    a wide table look catastrophic for being wide -- P1, whose identity
    table admits every window under a = 1, reads -25.4% here while its
    seats are a different question entirely.

    It is a COMPARATOR: two tables scored on one counterfactual over one
    population, where the only difference is which windows each admits.  A
    table whose admission set is worth less is worse, and that is all this
    is asked.
    """

    placeholders = ",".join("?" for _ in stages)
    try:
        rows = conn.execute(
            "SELECT win_probability, all_in_price, hit"
            " FROM paper_trade_candidate_window"
            f" WHERE stage IN ({placeholders}) AND event_date >= ?"
            "   AND hit IS NOT NULL",
            (*stages, floor_day),
        ).fetchall()
    except sqlite3.Error:
        return {"available": False, "windows": 0}
    cost = payout = 0.0
    admitted = 0
    for claim, price, hit in rows:
        claim = float(claim or 0.0)
        price = float(price or 0.0)
        if claim <= 0.0 or price <= 0.0:
            continue
        agreement = price / claim
        if bands[_band_index(agreement, bands)][1] <= agreement:
            continue
        admitted += 1
        cost += price
        payout += float(hit or 0)
    return {
        "available": bool(rows),
        "windows": len(rows),
        "admitted": admitted,
        "cost": round(cost, 4),
        "payout": round(payout, 4),
        # No trades is a return of zero, not an undefined one: a table that
        # admits nothing earns nothing, and that is a real comparison.
        "roi": round((payout - cost) / cost, 6) if cost > 0 else 0.0,
    }


# The guard the 2026-09-07 fit needed and did not have.  Default on; the
# switch exists so a deliberate promotion against the back-test stays
# possible and has to be typed.
ACTIVATION_RETURN_GUARD = str(
    os.environ.get("PM_EFFECTIVE_EDGE_ACTIVATION_GUARD", "on") or "on"
).strip().lower()


def persist_and_activate(
    conn: sqlite3.Connection, fit: dict[str, Any]
) -> dict[str, Any]:
    """Store the fit and make it the served table -- if it beats the one it
    would replace on the only question that matters.

    Activation used to be unconditional, on the reasoning that the per-band
    admission rule had already run: a band without evidence keeps the
    incumbent, so activating could not serve a number the hurdles had not
    cleared.  That reasoning is sound about each band and wrong about the
    table, because the quantity the hurdles clear is not the quantity the
    gate uses.  ``lambda`` is a band AVERAGE and the gate uses it as a
    THRESHOLD, so a table admits precisely the members of each band whose
    own agreement sits closest to the average -- the dearest ones, whose
    own realisation is furthest below it.  The fit can improve every band's
    estimate and make every purchase worse.

    Measured 2026-09-07, the first day the candidate population existed to
    measure it on: the newly fitted P0 table read better evidence in every
    band (12 windows where there had been none at 0.80, 22 at 0.95, 148 at
    the top against 58 seats for the whole table) and would have taken the
    book from 64 windows at +9.2% to 130 at +0.4%.  Every band it moved, it
    moved the wrong way: 0.795 -> 0.648 gave up a band returning +12.2%,
    0.650 -> 0.919 bought one returning -12.3%, 0.979 -> 1.217 diluted
    +7.9% to +1.7%.

    So the fit is journalled always and served only when the windows it
    would have bought beat the windows the incumbent bought, over the same
    population and the same rolling floor.
    """

    ensure_schema(conn)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    sha = str(fit["content_sha256"])
    scope = str(fit.get("stage_scope") or "P0")
    stages = list(fit["table"].get("stages") or STAGE_SCOPES[scope])
    floor_day = str(fit["table"].get("fit_floor_event_date") or "")
    candidate_bands = [
        (float(bound), float(lam)) for bound, lam in fit["bands"]
    ]
    incumbent_bands = _incumbent_bands(conn, scope)
    proposed = admitted_return(conn, candidate_bands, stages, floor_day)
    standing = admitted_return(conn, incumbent_bands, stages, floor_day)
    guard = {
        "mode": ACTIVATION_RETURN_GUARD,
        "proposed": proposed,
        "incumbent": standing,
        "bands_unchanged": candidate_bands == incumbent_bands,
    }
    # An unchanged table is a refresh of the evidence, not a promotion, and
    # nothing it could buy differs -- it activates so the journal keeps
    # moving.  A changed one has to earn it.
    blocked = bool(
        ACTIVATION_RETURN_GUARD == "on"
        and not guard["bands_unchanged"]
        and proposed.get("available")
        and standing.get("available")
        and float(proposed["roi"]) < float(standing["roi"]) - 1e-9
    )
    guard["blocked"] = blocked
    if not blocked:
        # Deactivation is per scope: activating P1's table must not retire
        # P0's.
        conn.execute(
            "UPDATE calibration_effective_edge_lambda SET activated=0"
            " WHERE activated=1 AND stage_scope=?",
            (scope,),
        )
        # Close the outgoing table's serving interval and open this one's.
        #
        # ``activated_at_utc`` on the table says when a table started; it
        # never said when one STOPPED, because activation flips the old row
        # off and writes nothing.  So there was no window to score a table
        # over, and the only number anyone could put on a table was
        # ``admitted_return`` -- a counterfactual over candidate windows,
        # which its own docstring says is a comparator and not the book's
        # return.  Measured 2026-09-09: 42 tables fitted, 3 carrying an
        # activation stamp, all three stamped the same minute, and no
        # history behind them at all.
        #
        # An interval is what makes a forward score possible: a table was
        # fitted before it served, so the seats decided WHILE it served are
        # out of sample for it, and that is the only reading of "did this
        # table earn its seat" that the table did not grade itself on.
        # Recorded here rather than derived later, because a derivation
        # would have to guess at the gaps -- a scope with no admitted table
        # serves nothing, and that is not the same as the incumbent
        # continuing.
        conn.execute(
            "UPDATE calibration_effective_edge_serving_interval"
            " SET served_until_utc=? WHERE stage_scope=? AND"
            " served_until_utc IS NULL",
            (now, scope),
        )
        conn.execute(
            "INSERT INTO calibration_effective_edge_serving_interval("
            " stage_scope, lambda_table_sha256, served_from_utc,"
            " served_until_utc) VALUES(?,?,?,NULL)"
            " ON CONFLICT(stage_scope, served_from_utc) DO NOTHING",
            (scope, sha, now),
        )
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(lambda_table_sha256) DO UPDATE SET"
        "  activated=excluded.activated,"
        "  activated_at_utc=excluded.activated_at_utc,"
        "  evidence_json=excluded.evidence_json",
        (
            sha,
            json.dumps(fit["table"], ensure_ascii=False, sort_keys=True),
            json.dumps(
                {"bands": fit["evidence"], "activation_guard": guard},
                ensure_ascii=False,
            ),
            now,
            int(fit["table"]["window_days"]),
            int(fit["seat_count"]),
            0 if blocked else 1,
            None if blocked else now,
            scope,
        ),
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_fit_journal("
        " lambda_table_sha256, fitted_at_utc, admitted, accumulating_bands,"
        " evidence_json, stage_scope) VALUES(?,?,?,?,?,?)",
        (
            sha, now,
            len(fit["bands"]) - len(fit["accumulating_bands"]),
            json.dumps(fit["accumulating_bands"]),
            json.dumps(
                {"bands": fit["evidence"], "activation_guard": guard},
                ensure_ascii=False,
            ),
            scope,
        ),
    )
    conn.commit()
    return {
        "lambda_table_sha256": sha,
        "activated_at_utc": None if blocked else now,
        "stage_scope": scope,
        "activation_guard": guard,
    }


# NOT yet wired to serving, and the reason is structural rather than a
# missing afternoon.  The compile chain holds no database handle by design
# -- compile_production_plan -> Optimizer.compile -> _compile_target_plan ->
# compile_delta_v2_runtime_plan -> _solve_over_support_windows all take
# artifacts, never a connection -- so serving this table means threading it
# through the production decision boundary, or hiding a database read inside
# a pure function.  Neither is a change to make on an armed book at the end
# of a session, and the house rule for the probability layer is explicit:
# batch such edits and run them in shadow first.
#
# So the layer measures, alarms and activates in the database, and the
# kernel constant is still what serves.  Promoting a fitted table is a small
# reviewable edit to that constant, informed by exactly the evidence this
# module journals -- the same shape as the station exclusion list, where the
# machine computes candidates and a person promotes them.
def load_active_lambda(
    conn: sqlite3.Connection,
    *,
    stage_scope: str = "P0",
) -> tuple[tuple[float, float], ...] | None:
    """The served table, or None when nothing has been fitted yet.

    None is the honest answer rather than the incumbent, so a caller can
    tell "no fit has ever run" from "a fit ran and reproduced the
    bootstrap"; the kernel constant is the fallback either way.
    """

    try:
        row = conn.execute(
            "SELECT bands_json FROM calibration_effective_edge_lambda"
            " WHERE activated=1 AND stage_scope=?"
            " ORDER BY fitted_at_utc DESC LIMIT 1",
            (str(stage_scope),),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
        bands = payload["bands"]
    except (ValueError, KeyError, TypeError):
        return None
    out: list[tuple[float, float]] = []
    for band in bands:
        bound = band.get("upper_bound")
        out.append((
            float("inf") if bound is None else float(bound),
            float(band["lambda"]),
        ))
    return tuple(out) or None


def _window_best_ask_sum(buckets_json: str, window: Sequence[str]) -> float | None:
    """The window's best-ask sum on one committed book; None if any leg is unquoted.

    Best ask, deliberately: the ledger's ``prices_json`` is the best ask per
    bucket (measured identical on the 2026-08-15 ZGSZ seat), so the move is
    read on the same basis at both ends.
    """

    try:
        entries = json.loads(str(buckets_json))
    except (ValueError, TypeError):
        return None
    asks: dict[str, float] = {}
    for entry in entries or []:
        levels = entry.get("ask_levels") or []
        if levels:
            asks[str(entry.get("bucket"))] = float(levels[0]["price"])
    total = 0.0
    for bucket in window:
        if bucket not in asks:
            return None
        total += asks[bucket]
    return total


def window_price_move(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
    window: Sequence[str],
    price_now: float,
    horizon_hours: float = DQ_SHADOW_HORIZON_HOURS,
    threshold: float = DQ_SHADOW_THRESHOLD,
) -> dict[str, Any]:
    """How the market moved this window over the hours before a decision.

    The earlier end is ``convergence_evidence.load_convergence_evidence``'s
    pairing -- the one loader every reader of "what did the system hold H
    hours ago" shares -- so this lane and the convergence arm cannot drift
    apart on which checkpoint or which book counts.  ``no_prior`` when no
    such checkpoint or book exists, which is every opening seat under
    charter 48.
    """

    from .convergence_evidence import load_convergence_evidence, window_sum

    evidence = load_convergence_evidence(
        conn, station_id=station_id, event_date=event_date,
        as_of_utc=decision_at_utc, horizon_hours=horizon_hours,
    )
    if evidence is None:
        return {"tier": "no_prior", "dq": None, "prior_stage": None}
    price_then = window_sum(evidence.get("prior_asks"), window)
    if price_then is None:
        return {
            "tier": "no_prior", "dq": None,
            "prior_stage": evidence.get("prior_stage"),
        }
    dq = float(price_now) - float(price_then)
    if dq <= -float(threshold):
        tier = "down"
    elif dq >= float(threshold):
        tier = "up"
    else:
        tier = "steady"
    return {
        "tier": tier, "dq": round(dq, 4),
        "prior_stage": str(evidence.get("prior_stage")),
        "prior_at": str(evidence.get("prior_decision_at_utc")),
        "book_at": str(evidence.get("prior_book_observed_at")),
    }


def fit_lambda_table_dq_shadow(
    conn: sqlite3.Connection,
    *,
    window_days: int = WINDOW_DAYS,
    as_of: str | None = None,
    stage_scope: str = "P0",
    horizon_hours: float = DQ_SHADOW_HORIZON_HOURS,
    threshold: float = DQ_SHADOW_THRESHOLD,
) -> dict[str, Any]:
    """The served table split by the market's move; measured, never served.

    Same rolling window, same seats, same admission rule per cell as
    ``fit_lambda_table`` -- a (band, tier) cell short of seats or claim mass
    reports None and the band's served lambda stands in.  Cells are what a
    person reads to decide whether the axis earns a place in the table.
    """

    if stage_scope not in STAGE_SCOPES:
        raise ValueError(f"effective_edge_fit_scope_invalid:{stage_scope}")
    stages = STAGE_SCOPES[stage_scope]
    ceiling = P1_LAMBDA_CEILING if stage_scope == "P1" else LAMBDA_CEILING
    incumbent = _incumbent_bands(conn, stage_scope)
    newest = conn.execute(
        "SELECT MAX(event_date) FROM paper_trade_settlement"
    ).fetchone()
    latest = str(as_of or (newest[0] if newest else "") or "")
    if not latest:
        raise ValueError("effective_edge_fit_no_settlements")
    floor_day = (
        datetime.fromisoformat(latest).date() - timedelta(days=int(window_days))
    ).isoformat()
    placeholders = ",".join("?" for _ in stages)
    rows = conn.execute(
        # The same population as the served fit, for the same two reasons,
        # and it has to be the same one: this axis is only meaningful
        # stratified by the ``a`` the served table bands on, so measuring
        # the two on different samples compares nothing.  The window the
        # move is priced over is the candidate's own leg list, and
        # ``all_in_price`` is that window's price at the decision -- the
        # shadow's ``price_now`` by construction, with no ladder to re-sum.
        "SELECT win_probability, all_in_price, hit, buckets_json,"
        "       station_id, event_date, stage, decision_at_utc"
        " FROM paper_trade_candidate_window"
        f" WHERE stage IN ({placeholders}) AND event_date >= ?"
        "   AND hit IS NOT NULL",
        (*stages, floor_day),
    ).fetchall()
    partition = _band_partition_by_decision(conn, stages, floor_day)
    tally = [
        {tier: {"seats": 0, "claim": 0.0, "hits": 0} for tier in DQ_TIERS}
        for _ in incumbent
    ]
    excluded = 0
    for (win, cost, hit, window_json, station_id, event_date, stage,
         decision_at) in rows:
        claim = float(win or 0.0)
        price = float(cost or 0.0)
        if claim <= 0.0 or price <= 0.0:
            continue
        if partition is not None and partition.get(
            (str(station_id), str(event_date), str(stage), str(decision_at))
        ) != BAND_PARTITION_CELLS:
            excluded += 1
            continue
        try:
            window = [str(b) for b in json.loads(str(window_json or "[]"))]
        except (ValueError, TypeError):
            window = []
        price_now = price
        move = window_price_move(
            conn, station_id=str(station_id), event_date=str(event_date),
            decision_at_utc=str(decision_at), window=window, price_now=price_now,
            horizon_hours=horizon_hours, threshold=threshold,
        ) if window else {"tier": "no_prior"}
        slot = tally[_band_index(price / claim, incumbent)][move["tier"]]
        slot["seats"] += 1
        slot["claim"] += claim
        slot["hits"] += int(hit or 0)

    bands: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for index, ((bound, served), slots) in enumerate(zip(incumbent, tally)):
        tiers: dict[str, Any] = {}
        detail: dict[str, Any] = {}
        for tier in DQ_TIERS:
            slot = slots[tier]
            raw = (slot["hits"] / slot["claim"]) if slot["claim"] > 0 else None
            enough = (
                slot["seats"] >= MIN_SEATS_PER_BAND
                and slot["claim"] >= MIN_CLAIM_MASS
                and raw is not None
            )
            fitted = (
                round(min(ceiling, max(LAMBDA_FLOOR, raw)), 4) if enough else None
            )
            tiers[tier] = fitted
            detail[tier] = {
                "seats": slot["seats"],
                "claim_mass": round(slot["claim"], 3),
                "hits": slot["hits"],
                "raw_lambda": None if raw is None else round(raw, 4),
                "fitted_lambda": fitted,
                "admissible": bool(enough),
            }
        bands.append({
            "upper_bound": None if bound == float("inf") else bound,
            "served_lambda": round(float(served), 4),
            "tiers": tiers,
        })
        evidence.append({
            "band_index": index,
            "band_upper_bound": None if bound == float("inf") else bound,
            "served_lambda": round(float(served), 4),
            "tiers": detail,
        })
    payload = {
        "schema": "effective_edge_lambda_dq_shadow_v1",
        "stage_scope": stage_scope,
        "stages": list(stages),
        "horizon_hours": float(horizon_hours),
        "threshold": float(threshold),
        "window_days": int(window_days),
        "as_of_event_date": latest,
        "fit_floor_event_date": floor_day,
        "band_partition_cells": BAND_PARTITION_CELLS,
        "bands": bands,
    }
    return {
        "table": payload,
        "stage_scope": stage_scope,
        "content_sha256": content_sha256(payload),
        "evidence": evidence,
        "seat_count": sum(
            slot["seats"] for slots in tally for slot in slots.values()
        ),
        "excluded_old_partition": excluded,
        "admitted_cells": [
            (item["band_index"], tier)
            for item in evidence for tier, cell in item["tiers"].items()
            if cell["admissible"]
        ],
    }


def persist_dq_shadow(conn: sqlite3.Connection, fit: dict[str, Any]) -> dict[str, Any]:
    """Journal the shadow fit.  No activation column exists: nothing serves it."""

    ensure_schema(conn)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    sha = str(fit["content_sha256"])
    table = fit["table"]
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda_dq_shadow("
        " shadow_sha256, stage_scope, tiers_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, horizon_hours, threshold)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(shadow_sha256) DO UPDATE SET"
        "  fitted_at_utc=excluded.fitted_at_utc,"
        "  evidence_json=excluded.evidence_json",
        (
            sha, str(fit["stage_scope"]),
            json.dumps(table, ensure_ascii=False, sort_keys=True),
            json.dumps(fit["evidence"], ensure_ascii=False),
            now, int(table["window_days"]), int(fit["seat_count"]),
            float(table["horizon_hours"]), float(table["threshold"]),
        ),
    )
    conn.commit()
    return {"shadow_sha256": sha, "fitted_at_utc": now, "stage_scope": fit["stage_scope"]}


def load_dq_shadow(
    conn: sqlite3.Connection, *, stage_scope: str = "P0"
) -> dict[str, Any] | None:
    """The latest shadow fit for a scope, with its evidence; None before the first."""

    try:
        row = conn.execute(
            "SELECT shadow_sha256, tiers_json, evidence_json, fitted_at_utc,"
            " seat_count FROM calibration_effective_edge_lambda_dq_shadow"
            " WHERE stage_scope=? ORDER BY fitted_at_utc DESC LIMIT 1",
            (str(stage_scope),),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        table = json.loads(str(row[1]))
        evidence = json.loads(str(row[2]))
    except ValueError:
        return None
    return {
        "shadow_sha256": str(row[0]),
        "table": table,
        "evidence": evidence,
        "fitted_at_utc": str(row[3]),
        "seat_count": int(row[4] or 0),
    }


__all__ = [
    "DQ_SHADOW_HORIZON_HOURS",
    "DQ_SHADOW_MODE",
    "DQ_SHADOW_THRESHOLD",
    "DQ_TIERS",
    "FIT_STAGES",
    "FIT_STAGES_P1",
    "P1_LAMBDA_CEILING",
    "STAGE_SCOPES",
    "LAMBDA_CEILING",
    "LAMBDA_FLOOR",
    "MIN_CLAIM_MASS",
    "MIN_SEATS_PER_BAND",
    "WINDOW_DAYS",
    "ensure_schema",
    "fit_lambda_table",
    "fit_lambda_table_dq_shadow",
    "load_active_lambda",
    "load_dq_shadow",
    "persist_and_activate",
    "persist_dq_shadow",
    "window_price_move",
]


def load_lambda_as_of(
    conn: sqlite3.Connection,
    *,
    stage_scope: str = "P0",
    as_of_utc: str | None = None,
) -> tuple[tuple[float, float], ...] | None:
    """The table this scope was serving at ``as_of_utc``; the active one when
    None.

    Read off the fit journal, not the table: every daily fit journals its
    sha whether or not the sha changed, while the table's ``activated_at``
    is bumped to today each time the same sha is re-activated -- so the
    table remembers only the latest re-activation and the journal remembers
    the sequence.  The row serving at a moment is the latest journaled fit
    at or before it, since each fit is activated the instant it is
    journaled.  This is what lets the paper ledger price a historical
    decision on the table production served that day rather than on
    today's, and what keeps a promotion from rewriting the book behind it.
    None before the scope's first fit, which the kernel constant covers.
    """

    if as_of_utc is None:
        return load_active_lambda(conn, stage_scope=stage_scope)
    try:
        row = conn.execute(
            "SELECT t.bands_json"
            " FROM calibration_effective_edge_fit_journal j"
            " JOIN calibration_effective_edge_lambda t"
            "   ON t.lambda_table_sha256 = j.lambda_table_sha256"
            " WHERE j.stage_scope=? AND j.fitted_at_utc<=?"
            " ORDER BY j.fitted_at_utc DESC, j.fit_id DESC LIMIT 1",
            (str(stage_scope), str(as_of_utc)),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        bands = json.loads(str(row[0]))["bands"]
    except (ValueError, KeyError, TypeError):
        return None
    out = [
        (
            float("inf") if band.get("upper_bound") is None
            else float(band["upper_bound"]),
            float(band["lambda"]),
        )
        for band in bands
    ]
    return tuple(out) or None


def load_effective_edge_tables(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str | None = None,
) -> dict[str, tuple[tuple[float, float], ...] | None]:
    """One table per served scope, as of a moment -- what the dispatch reads.

    Charter 68(c), user ruling 2026-09-05: the rolling fit promotes itself.
    Production hands this to the solver with ``as_of_utc`` None, the paper
    ledger with the decision's own timestamp; a scope with no activation yet
    is None here and the kernel constant in the dispatch, which is the
    bootstrap and nothing more.
    """

    return {
        scope: load_lambda_as_of(conn, stage_scope=scope, as_of_utc=as_of_utc)
        for scope in EFFECTIVE_EDGE_SCOPES_SERVED
    }
