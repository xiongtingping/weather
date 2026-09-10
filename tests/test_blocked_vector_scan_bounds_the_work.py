"""The scan bound counted the finished job and refused to do the unfinished one.

``verify``/``inspect_legacy_blocked_execution_vector_repair`` proves that every
blocked cycle carries its NO_EXECUTION version vector.  It fetched EVERY
blocked decision ever written, and refused outright past 1,024 of them --
``resource_bound_exceeded``, ``covered_count: 0``, and the controlled
migration's postcheck reads that result and aborts on it.

Measured 2026-09-11 on the live SSOT: 36,499 blocked decisions, of which
36,499 already carried a conforming vector and 0 needed repair.  The book
crossed 1,024 in July, so the check had been validating nothing and blocking
every schema change behind it for two months -- while reporting a number,
1,024, that made it look like a near miss.

A bound belongs on the WORK.  A decision whose vector is already in the table
is not work; it is the finished part being re-fetched to prove it is
finished, and SQL can prove that without carrying the rows into Python.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.execution_vector_repair import (  # noqa: E402
    MAX_BLOCKED_ROWS,
    inspect_legacy_blocked_execution_vector_repair as verify,
)
from bayes_basket_target.execution_version_vector import (  # noqa: E402
    build_no_execution_version_policy,
)
from bayes_basket_target.ssot import (  # noqa: E402
    apply_production_ssot_schema,
)

REASONS = ["p1_entry_window_not_open"]


def _decision_sha(**identity) -> str:
    from bayes_basket_target.atomic_ledger import (
        DECISION_SCHEMA_V2,
        economic_decision_identity,
    )
    from bayes_basket_target.contracts import content_sha256

    return content_sha256({
        "schema": DECISION_SCHEMA_V2,
        **economic_decision_identity(**identity),
    })


def _ledger(
    tmp_path: Path,
    *,
    blocked: int,
    covered: bool = True,
    uncovered: frozenset[int] = frozenset(),
    malformed: frozenset[int] = frozenset(),
):
    """Seeded at INSERT, because the vectors table is append-only: a test
    cannot delete or amend a row to build the shape it wants."""

    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    policy = build_no_execution_version_policy(
        {"policy": "NO_EXECUTION", "reason_codes": REASONS}
    )
    for index in range(blocked):
        cycle = f"cycle-{index:06d}"
        decision = f"decision-{index:06d}"
        scope = {
            "station_id": "KLGA",
            "event_date": "2026-09-11",
            "settlement_timezone": "America/New_York",
            "strategy_lane": "basket",
        }
        conn.execute(
            "INSERT INTO basket_target_execution_cycles(cycle_id,plan_id,"
            "station_id,event_date,settlement_timezone,strategy_lane,"
            "plan_sha256,requested_reserve_usdc,state,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cycle, f"plan-{index:06d}", scope["station_id"],
             scope["event_date"], scope["settlement_timezone"],
             scope["strategy_lane"], "0" * 64, 0.0, "prepared",
             "2026-09-11T00:00:00Z", "2026-09-11T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions(decision_id,cycle_id,plan_id,"
            "station_id,event_date,settlement_timezone,strategy_lane,"
            "decided_state,will_order,reason_codes_json,details_json,"
            "decision_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision, cycle, f"plan-{index:06d}", scope["station_id"],
             scope["event_date"], scope["settlement_timezone"],
             scope["strategy_lane"], "blocked", 0, json.dumps(REASONS),
             "{}",
             _decision_sha(decided_state="blocked", will_order=0,
                           reason_codes=REASONS, details={},
                           decision_scope_key=None, **scope),
             "2026-09-11T00:00:00Z"),
        )
        if covered and index not in uncovered:
            conn.execute(
                "INSERT INTO basket_target_execution_version_vectors("
                "cycle_id,decision_id,vector_policy,probability_sha256,"
                "vector_json,vector_sha256,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (cycle, decision, "NO_EXECUTION",
                 ("f" * 64) if index in malformed else None,
                 json.dumps(policy.to_dict(), sort_keys=True),
                 policy.policy_sha256, "2026-09-11T00:00:00Z"),
            )
    conn.commit()
    return conn


def test_a_book_past_the_bound_but_wholly_covered_is_not_refused(tmp_path):
    """The live shape, in miniature."""

    conn = _ledger(tmp_path, blocked=MAX_BLOCKED_ROWS + 25, covered=True)

    result = verify(conn)

    assert result["ok"] is True, result.get("blockers")
    assert result["state"] == "complete"
    assert result["unrepaired_count"] == 0
    assert result["covered_count"] == MAX_BLOCKED_ROWS + 25
    assert result["repair_candidate_count"] == 0
    # Both halves reported, so "how much is left" and "how much stands
    # proven" are separate numbers rather than one that means neither.
    assert result["blocked_decision_count"] == MAX_BLOCKED_ROWS + 25


def test_the_bound_still_refuses_when_the_WORK_is_past_it(tmp_path):
    """The guard is not removed, it is pointed at the right population."""

    conn = _ledger(tmp_path, blocked=MAX_BLOCKED_ROWS + 5, covered=False)

    result = verify(conn)

    assert result["ok"] is False
    assert result["state"] == "resource_bound_exceeded"
    assert result["blockers"] == ["blocked_execution_vector_scan_limit_exceeded"]


def test_one_uncovered_decision_behind_a_covered_book_is_still_found(tmp_path):
    """The whole point: work hidden behind finished work."""

    conn = _ledger(
        tmp_path, blocked=MAX_BLOCKED_ROWS + 25,
        covered=True, uncovered=frozenset({777}),
    )

    result = verify(conn)

    assert result["unrepaired_count"] == 1
    assert result["covered_count"] == MAX_BLOCKED_ROWS + 24
    assert result["repair_candidate_count"] == 1


def test_a_vector_that_carries_evidence_is_not_waved_through(tmp_path):
    """A NO_EXECUTION vector with execution evidence on it is malformed.

    SQL decides coverage now, so it has to be as strict as the Python check
    it stands in for -- otherwise a bad row leaves the candidate set by
    passing the cheaper test.
    """

    conn = _ledger(
        tmp_path, blocked=MAX_BLOCKED_ROWS + 25,
        covered=True, malformed=frozenset({123}),
    )

    result = verify(conn)

    assert result["unrepaired_count"] == 1
    assert result["covered_count"] == MAX_BLOCKED_ROWS + 24
    assert result["invalid_count"] == 1, result
