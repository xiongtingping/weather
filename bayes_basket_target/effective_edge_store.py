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

That failure is closed structurally rather than statistically, and not by
anything in this file: the chain ticket keeps the seat when the shrink
sizes it to nothing.  Measured on the book the day charter 68 landed,
79-99% of opening seats are tickets and they still span all four agreement
bands -- 19 seats below 0.50, 52, 102, and 118 above 0.85.  The observation
stream survives the rule that would otherwise have cut it.  Should that
stop being true, ``fit_lambda_table`` says so per band rather than quietly
serving a number nobody can support any more.

Measured at the basket, deliberately.  Per-bucket agreement is the tempting
basis -- every priced bucket is an observation, and no selection touches it
-- but it answers a different question: on this book the top band reads
1.82 per bucket against 0.89 per basket, because a basket's agreement is a
cost-over-claim ratio across buckets whose individual agreements scatter
around it.  The rule shrinks baskets, so the estimator measures baskets.

Admission rule per band (fixed; the table changes, the rule does not):

* at least ``MIN_SEATS_PER_BAND`` settled opening seats inside the rolling
  window, and claim mass above ``MIN_CLAIM_MASS`` -- below either, the band
  keeps the incumbent value and is reported as accumulating rather than
  fitted to whatever three seats happened to land there;
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
import sqlite3
from datetime import UTC, datetime
from typing import Any, Sequence

from .band_reliability_store import BAND_TABLE_CELLS
from .basket_kernel.delta_v2 import (
    center_window_claim_factor,
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
            return [(float(bound), float(lam)) for bound, lam in active]
    return _bootstrap_bands(stage_scope)


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
    rows = conn.execute(
        "SELECT p.win_probability, p.all_in_cost, s.hit, p.position_id"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(position_id)"
        f" WHERE s.stage IN ({placeholders}) AND s.event_date >= ?",
        (*stages, floor_day),
    ).fetchall()
    partition = _band_partition_by_position(
        conn, [str(row[3]) for row in rows]
    )
    band_filter: dict[str, Any] = {
        "cells": BAND_PARTITION_CELLS,
        "applied": partition is not None,
        "excluded_old_partition": 0,
        "excluded_no_receipt": 0,
    }

    tally = [
        {"seats": 0, "claim": 0.0, "hits": 0} for _ in incumbent
    ]
    for win, cost, hit, position_id in rows:
        # Charter 81: the solver's agreement is price over the LIFTED claim
        # (every seat holds the centre since charter 49, and the ledger
        # records the served window mass before the lift), so the fit reads
        # the same basis -- else lambda(a) is looked up at an a the table
        # never saw.  Identity when the lift is off.
        raw_claim = float(win or 0.0)
        claim = raw_claim * center_window_claim_factor(raw_claim)
        price = float(cost or 0.0)
        if claim <= 0.0 or price <= 0.0:
            continue
        if partition is not None:
            cells = partition.get(str(position_id))
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

    bands: list[tuple[float, float]] = []
    evidence: list[dict[str, Any]] = []
    accumulating: list[int] = []
    for index, (bound, incumbent_lambda) in enumerate(incumbent):
        slot = tally[index]
        enough = (
            slot["seats"] >= MIN_SEATS_PER_BAND
            and slot["claim"] >= MIN_CLAIM_MASS
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
    }
    return {
        "table": payload,
        "band_filter": band_filter,
        "stage_scope": stage_scope,
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


def persist_and_activate(
    conn: sqlite3.Connection, fit: dict[str, Any]
) -> dict[str, Any]:
    """Store the fit and make it the served table.

    Activation is unconditional once the fit exists, because the admission
    rule already ran band by band inside the fit: a band without evidence
    carries the incumbent value, so activating cannot serve a number the
    hurdles did not clear.  What activation buys is that the bands which DID
    clear stop being stale.
    """

    ensure_schema(conn)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    sha = str(fit["content_sha256"])
    scope = str(fit.get("stage_scope") or "P0")
    # Deactivation is per scope: activating P1's table must not retire P0's.
    conn.execute(
        "UPDATE calibration_effective_edge_lambda SET activated=0"
        " WHERE activated=1 AND stage_scope=?",
        (scope,),
    )
    conn.execute(
        "INSERT INTO calibration_effective_edge_lambda("
        " lambda_table_sha256, bands_json, evidence_json, fitted_at_utc,"
        " window_days, seat_count, activated, activated_at_utc, stage_scope)"
        " VALUES(?,?,?,?,?,?,1,?,?)"
        " ON CONFLICT(lambda_table_sha256) DO UPDATE SET"
        "  activated=1, activated_at_utc=excluded.activated_at_utc,"
        "  evidence_json=excluded.evidence_json",
        (
            sha,
            json.dumps(fit["table"], ensure_ascii=False, sort_keys=True),
            json.dumps(fit["evidence"], ensure_ascii=False),
            now,
            int(fit["table"]["window_days"]),
            int(fit["seat_count"]),
            now,
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
            json.dumps(fit["evidence"], ensure_ascii=False),
            scope,
        ),
    )
    conn.commit()
    return {
        "lambda_table_sha256": sha, "activated_at_utc": now,
        "stage_scope": scope,
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


__all__ = [
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
    "load_active_lambda",
    "persist_and_activate",
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
