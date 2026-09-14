"""What a lambda table did on the seats it was serving for.

Kept out of ``effective_edge_store`` deliberately, and the reason is that
module's own regression guard (charter 94,
``test_no_claims_layer_query_reads_the_post_execution_columns``): the
claims layer's population is every enumerated candidate window, and its
readings must stay a word away from the filled basket, so it may not read
``p.win_probability``, ``p.all_in_cost`` or ``s.hit`` off the seat ledger.
That guard is right and it caught this function on the way in.

This asks the opposite question on purpose.  A forward score is a question
about the book that was really bought -- charter 110's own words for why
the activation guard may not read the replay lane -- so it reads exactly
the columns the claims layer may not, and lives where the ledger is free
to read them.

The window is the serving interval ``effective_edge_store`` records at
activation.  A table is fitted on a trailing window and then serves, so
the seats decided while it served are after its fit: it chose them and did
not learn from them.  Every other number about a table is in sample
somewhere -- the bands are the trailing window by construction, and
``admitted_return`` is a counterfactual over candidate windows that its own
docstring calls a comparator rather than a book return.
"""
from __future__ import annotations

import sqlite3
from typing import Any

FORWARD_SERVING_SCORE_SCHEMA = "effective_edge_forward_serving_score_v1"


def forward_serving_score(
    conn: sqlite3.Connection,
    *,
    stage_scope: str,
    claim_scope: str = "window",
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
        # One claim scope's tables only (2026-09-14).  The interval table is
        # keyed by stage, and P0 carries a window table and a bucket table;
        # read together, a bucket interval was scored as the window table's
        # book and the window table's own intervals looked seconds long.
        # Production serves the window table, hence the default.
        from .effective_edge_store import (  # noqa: PLC0415
            _claim_scope_args,
            _interval_scope_clause,
        )

        intervals = conn.execute(
            "SELECT lambda_table_sha256, served_from_utc, served_until_utc"
            " FROM calibration_effective_edge_serving_interval"
            " WHERE stage_scope=?"
            + _interval_scope_clause(conn, claim_scope)
            + " ORDER BY served_from_utc",
            (scope, *_claim_scope_args(conn, claim_scope)),
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
        "claim_scope": str(claim_scope),
        "available": True,
        "interval_count": len(out),
        "scored_interval_count": sum(
            1 for row in out if row["settled_seats"] > 0
        ),
        "intervals": out,
    }
