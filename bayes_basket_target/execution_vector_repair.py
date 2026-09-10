"""Controlled repair for legacy blocked cycles missing NO_EXECUTION lineage.

This module is maintenance-only. It never invents executable evidence and
never updates or deletes an existing version vector.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import canonical_json, content_sha256
from .execution_version_vector import (
    NO_EXECUTION_POLICY_SCHEMA,
    build_no_execution_version_policy,
)


DECISION_SCHEMA = "basket_target_decision_fact_v1"
REPAIR_SCHEMA = "bayes_basket_target_blocked_execution_vector_repair_v1"
MAX_BLOCKED_ROWS = 1024
REQUIRED_TABLES = frozenset(
    {
        "auto_trade_decisions",
        "basket_target_execution_cycles",
        "basket_target_execution_version_vectors",
        "basket_target_order_intents",
        "basket_target_capital_reservations",
        "basket_target_order_events",
        "basket_target_order_reconciliations",
        "basket_policy_training_fact",
    }
)


class ExecutionVectorRepairError(RuntimeError):
    """Raised when retained facts cannot prove a safe append-only repair."""


def _json_mapping(name: str, value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExecutionVectorRepairError(f"{name}_json_invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ExecutionVectorRepairError(f"{name}_mapping_required")
    return dict(parsed)


def _reason_codes(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExecutionVectorRepairError("reason_codes_json_invalid") from exc
    if (
        isinstance(parsed, (str, bytes, bytearray))
        or not isinstance(parsed, Sequence)
    ):
        raise ExecutionVectorRepairError("reason_codes_list_required")
    result = [str(item).strip() for item in parsed]
    if not result or any(not item for item in result) or len(result) != len(set(result)):
        raise ExecutionVectorRepairError("reason_codes_invalid")
    return result


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


# Everything SQL can say about a blocked cycle already carrying its
# NO_EXECUTION vector.  A row this matches needs no repair; a row it does not
# match is work, and work is what the bound below is for.
#
# Kept as strict as the Python check it stands in for, so a malformed vector
# falls OUT of coverage and INTO the bounded candidate set rather than being
# waved through: one vector for the cycle, one decision for the cycle, the
# vector naming this decision under NO_EXECUTION, every nullable evidence
# column null, and the decision's lineage matching its cycle's.
_COVERED_PREDICATE = """
EXISTS (
  SELECT 1 FROM basket_target_execution_version_vectors v
  WHERE v.cycle_id = d.cycle_id
    AND v.decision_id = d.decision_id
    AND v.vector_policy = 'NO_EXECUTION'
    AND v.order_intent_id IS NULL AND v.target_price_id IS NULL
    AND v.quote_observed_at IS NULL AND v.quote_expires_at IS NULL
    AND v.probability_sha256 IS NULL AND v.official_ws_quote_sha256 IS NULL
    AND v.inventory_sha256 IS NULL AND v.order_state_sha256 IS NULL
    AND v.station_policy_sha256 IS NULL
    AND v.cash_policy_decision_sha256 IS NULL
    AND v.strict_delta_artifact_sha256 IS NULL
)
AND (SELECT COUNT(*) FROM basket_target_execution_version_vectors v2
     WHERE v2.cycle_id = d.cycle_id) = 1
AND (SELECT COUNT(*) FROM auto_trade_decisions d2
     WHERE d2.cycle_id = d.cycle_id) = 1
AND COALESCE(d.plan_id,'') = COALESCE(c.plan_id,'')
AND COALESCE(d.station_id,'') = COALESCE(c.station_id,'')
AND COALESCE(d.event_date,'') = COALESCE(c.event_date,'')
AND COALESCE(d.settlement_timezone,'') = COALESCE(c.settlement_timezone,'')
AND COALESCE(d.strategy_lane,'') = COALESCE(c.strategy_lane,'')
AND ABS(COALESCE(c.requested_reserve_usdc,0.0)) <= 1e-9
"""

_BLOCKED_FROM = (
    " FROM auto_trade_decisions d"
    " JOIN basket_target_execution_cycles c ON c.cycle_id=d.cycle_id"
    " WHERE d.decided_state='blocked' AND d.will_order=0"
)


def _covered_blocked_count(conn: sqlite3.Connection) -> int:
    """How many blocked decisions are already proven, counted not listed."""

    return int(
        conn.execute(
            "SELECT COUNT(*)" + _BLOCKED_FROM + " AND " + _COVERED_PREDICATE
        ).fetchone()[0]
    )


def _blocked_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The blocked decisions that still owe a NO_EXECUTION vector.

    Every blocked decision ever written used to come back here, and the
    bound below then refused the whole check.  The bound is a resource
    guard on WORK, and a decision whose vector is already in the table is
    not work: it is the finished part of the job being re-fetched to prove
    it is finished.

    Measured 2026-09-11 on the live SSOT: 36,499 blocked decisions, of
    which 36,499 already carry a conforming vector and 0 need repair.  The
    scan crossed 1,024 in July, so from then on this returned
    ``resource_bound_exceeded`` -- validating nothing, reporting
    ``covered_count: 0``, and failing the migration postcheck that reads
    it.  Two months of schema changes had nowhere to land behind it.

    The trade, said out loud: a covered row's reason codes, details and
    content address are no longer re-derived on every run -- SQL cannot
    parse JSON or recompute a sha.  It buys back a check that has been
    running on zero rows since July, and anything SQL cannot vouch for
    lands in the candidate set where the full validation still runs.
    """

    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT d.decision_id,d.cycle_id,d.plan_id,d.station_id,d.event_date,"
            "d.settlement_timezone,d.strategy_lane,d.decided_state,d.will_order,"
            "d.reason_codes_json,d.details_json,d.decision_sha256,d.created_at,"
            "d.target_execution_hard_deadline_at,"
            "c.plan_id AS cycle_plan_id,c.station_id AS cycle_station_id,"
            "c.event_date AS cycle_event_date,"
            "c.settlement_timezone AS cycle_settlement_timezone,"
            "c.strategy_lane AS cycle_strategy_lane,"
            "c.requested_reserve_usdc"
            + _BLOCKED_FROM
            + " AND NOT (" + _COVERED_PREDICATE + ")"
            " ORDER BY d.created_at,d.decision_id LIMIT ?",
            (MAX_BLOCKED_ROWS + 1,),
        ).fetchall()
    finally:
        conn.row_factory = previous


def _validate_decision(row: sqlite3.Row) -> list[str]:
    reasons = _reason_codes(row["reason_codes_json"])
    details = _json_mapping("details_json", row["details_json"])
    lineage_pairs = (
        ("plan_id", "cycle_plan_id"),
        ("station_id", "cycle_station_id"),
        ("event_date", "cycle_event_date"),
        ("settlement_timezone", "cycle_settlement_timezone"),
        ("strategy_lane", "cycle_strategy_lane"),
    )
    if any(str(row[left] or "") != str(row[right] or "") for left, right in lineage_pairs):
        raise ExecutionVectorRepairError("blocked_decision_cycle_lineage_drift")
    if abs(float(row["requested_reserve_usdc"] or 0.0)) > 1e-9:
        raise ExecutionVectorRepairError("blocked_cycle_nonzero_requested_reserve")
    identity = {
        "cycle_id": str(row["cycle_id"]),
        "plan_id": str(row["plan_id"]),
        "station_id": str(row["station_id"]),
        "event_date": str(row["event_date"]),
        "settlement_timezone": str(row["settlement_timezone"]),
        "strategy_lane": str(row["strategy_lane"]),
        "decided_state": "blocked",
        "will_order": 0,
        "reason_codes": reasons,
        "details": details,
    }
    # Addressed economically since 2026-09-02: the deadline is no longer in
    # the identity, and the scope key is read off the row when the migration
    # has filled it.  One function on both sides, so this tool cannot drift
    # from the ledger it repairs.
    from .atomic_ledger import (  # noqa: PLC0415
        DECISION_SCHEMA_V2,
        economic_decision_identity,
    )

    scope_key = None
    try:
        scope_key = row["decision_scope_key"]
    except (IndexError, KeyError):
        scope_key = None
    economic = economic_decision_identity(
        station_id=identity["station_id"],
        event_date=identity["event_date"],
        settlement_timezone=identity["settlement_timezone"],
        strategy_lane=identity["strategy_lane"],
        decided_state="blocked",
        will_order=0,
        reason_codes=reasons,
        details=details,
        decision_scope_key=str(scope_key) if scope_key else None,
    )
    expected = content_sha256({"schema": DECISION_SCHEMA_V2, **economic})
    if str(row["decision_sha256"] or "").lower() != expected:
        raise ExecutionVectorRepairError("blocked_decision_content_address_drift")
    return reasons


def inspect_legacy_blocked_execution_vector_repair(
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    """Read-only proof of blocked-cycle NO_EXECUTION vector coverage."""

    missing_tables = sorted(REQUIRED_TABLES - _table_names(conn))
    if missing_tables:
        return {
            "schema": REPAIR_SCHEMA,
            "ok": False,
            "state": "schema_incomplete",
            "missing_tables": missing_tables,
            "blocked_decision_count": 0,
            "unrepaired_count": 0,
            "covered_count": 0,
            "repair_candidate_count": 0,
            "invalid_count": 0,
            "blockers": ["execution_vector_repair_required_tables_missing"],
            "calls_order_gateway": False,
            "places_orders": False,
        }

    rows = _blocked_rows(conn)
    if len(rows) > MAX_BLOCKED_ROWS:
        return {
            "schema": REPAIR_SCHEMA,
            "ok": False,
            "state": "resource_bound_exceeded",
            "missing_tables": [],
            "blocked_decision_count": len(rows),
            "unrepaired_count": len(rows),
            "covered_count": 0,
            "repair_candidate_count": 0,
            "invalid_count": 0,
            "blockers": ["blocked_execution_vector_scan_limit_exceeded"],
            "calls_order_gateway": False,
            "places_orders": False,
        }

    candidates: list[dict[str, Any]] = []
    # Seeded from SQL, not counted in the loop.  The loop now only sees rows
    # that still owe a vector, so a count built there would report zero
    # coverage on a book that is entirely covered.
    covered = _covered_blocked_count(conn)
    invalid: list[dict[str, str]] = []
    for row in rows:
        decision_id = str(row["decision_id"])
        cycle_id = str(row["cycle_id"])
        try:
            reasons = _validate_decision(row)
            decision_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM auto_trade_decisions WHERE cycle_id=?",
                    (cycle_id,),
                ).fetchone()[0]
            )
            if decision_count != 1:
                raise ExecutionVectorRepairError(
                    "blocked_cycle_decision_cardinality_invalid"
                )
            vector_rows = conn.execute(
                "SELECT order_intent_id,decision_id,training_fact_id,vector_policy,"
                "target_price_id,quote_observed_at,quote_expires_at,"
                "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
                "order_state_sha256,station_policy_sha256,"
                "cash_policy_decision_sha256,strict_delta_artifact_sha256,"
                "vector_json,vector_sha256 "
                "FROM basket_target_execution_version_vectors WHERE cycle_id=?",
                (cycle_id,),
            ).fetchall()
            policy = build_no_execution_version_policy(
                {"policy": "NO_EXECUTION", "reason_codes": reasons}
            )
            if vector_rows:
                if len(vector_rows) != 1:
                    raise ExecutionVectorRepairError(
                        "blocked_cycle_execution_vector_cardinality_invalid"
                    )
                vector = vector_rows[0]
                nullable_evidence = (
                    vector[0],
                    vector[4],
                    vector[5],
                    vector[6],
                    vector[7],
                    vector[8],
                    vector[9],
                    vector[10],
                    vector[11],
                    vector[12],
                    vector[13],
                )
                if (
                    str(vector[1] or "") != decision_id
                    or str(vector[3] or "") != "NO_EXECUTION"
                    or any(value is not None for value in nullable_evidence)
                ):
                    raise ExecutionVectorRepairError(
                        "blocked_cycle_existing_vector_semantics_invalid"
                    )
                training_fact_id = (
                    str(vector[2]).strip() if vector[2] is not None else ""
                )
                if training_fact_id:
                    training_fact_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM basket_policy_training_fact "
                            "WHERE fact_id=? AND cycle_id=? AND decision_id=? "
                            "AND decision_sha256=?",
                            (
                                training_fact_id,
                                cycle_id,
                                decision_id,
                                str(row["decision_sha256"]),
                            ),
                        ).fetchone()[0]
                    )
                    if training_fact_count != 1:
                        raise ExecutionVectorRepairError(
                            "blocked_cycle_training_fact_lineage_invalid"
                        )
                stored = _json_mapping("vector_json", vector[14])
                if (
                    stored.get("schema") != NO_EXECUTION_POLICY_SCHEMA
                    or stored != policy.to_dict()
                    or str(vector[15] or "").lower() != policy.policy_sha256
                ):
                    raise ExecutionVectorRepairError(
                        "blocked_cycle_existing_vector_content_address_drift"
                    )
                covered += 1
                continue

            downstream_counts = {
                "intents": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM basket_target_order_intents "
                        "WHERE cycle_id=? OR decision_id=?",
                        (cycle_id, decision_id),
                    ).fetchone()[0]
                ),
                "reservations": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM basket_target_capital_reservations "
                        "WHERE cycle_id=?",
                        (cycle_id,),
                    ).fetchone()[0]
                ),
                "training_facts": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM basket_policy_training_fact "
                        "WHERE cycle_id=? OR decision_id=?",
                        (cycle_id, decision_id),
                    ).fetchone()[0]
                ),
            }
            if any(downstream_counts.values()):
                raise ExecutionVectorRepairError(
                    "blocked_cycle_downstream_evidence_present"
                )
            candidates.append(
                {
                    "cycle_id": cycle_id,
                    "decision_id": decision_id,
                    "created_at": str(row["created_at"]),
                    "reason_codes": reasons,
                    "vector_json": canonical_json(policy.to_dict()),
                    "vector_sha256": policy.policy_sha256,
                }
            )
        except (ExecutionVectorRepairError, ValueError) as exc:
            invalid.append(
                {
                    "cycle_id": cycle_id,
                    "decision_id": decision_id,
                    "reason": str(exc),
                }
            )

    blockers = []
    if invalid:
        blockers.append("blocked_execution_vector_lineage_invalid")
    if candidates:
        blockers.append("legacy_blocked_execution_vectors_missing")
    return {
        "schema": REPAIR_SCHEMA,
        "ok": not blockers,
        "state": "complete" if not blockers else "repair_required",
        "missing_tables": [],
        # Both halves, because they answer different questions: how much
        # work is left, and how much of the book stands proven behind it.
        "blocked_decision_count": len(rows) + covered,
        "unrepaired_count": len(rows),
        "covered_count": covered,
        "repair_candidate_count": len(candidates),
        "invalid_count": len(invalid),
        "candidate_decision_ids": [item["decision_id"] for item in candidates],
        "invalid": invalid,
        "_candidates": candidates,
        "blockers": blockers,
        "calls_order_gateway": False,
        "places_orders": False,
    }


def repair_legacy_blocked_execution_vectors(
    conn: sqlite3.Connection,
    *,
    commit: bool = False,
) -> dict[str, Any]:
    """Append missing NO_EXECUTION rows atomically after full-batch proof."""

    before = inspect_legacy_blocked_execution_vector_repair(conn)
    candidates = list(before.pop("_candidates", []))
    if before["invalid_count"] or before["missing_tables"]:
        raise ExecutionVectorRepairError(
            "blocked_execution_vector_repair_precheck_failed:"
            + ",".join(before["blockers"])
        )
    own_transaction = not conn.in_transaction
    savepoint = "repair_legacy_blocked_execution_vectors"
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for item in candidates:
            conn.execute(
                "INSERT INTO basket_target_execution_version_vectors("
                "order_intent_id,cycle_id,decision_id,training_fact_id,"
                "vector_policy,target_price_id,quote_observed_at,"
                "quote_expires_at,vector_json,vector_sha256,created_at"
                ") VALUES(NULL,?,?,NULL,'NO_EXECUTION',NULL,NULL,NULL,?,?,?)",
                (
                    item["cycle_id"],
                    item["decision_id"],
                    item["vector_json"],
                    item["vector_sha256"],
                    item["created_at"],
                ),
            )
        after = inspect_legacy_blocked_execution_vector_repair(conn)
        after.pop("_candidates", None)
        if not after["ok"]:
            raise ExecutionVectorRepairError(
                "blocked_execution_vector_repair_postcheck_failed:"
                + ",".join(after["blockers"])
            )
    except BaseException:
        if own_transaction:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        if own_transaction:
            if commit:
                conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")
    return {
        "schema": REPAIR_SCHEMA,
        "state": "repaired" if candidates else "already_complete",
        "inserted_count": len(candidates),
        "before": before,
        "after": after,
        "append_only": True,
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
    }


__all__ = [
    "ExecutionVectorRepairError",
    "inspect_legacy_blocked_execution_vector_repair",
    "repair_legacy_blocked_execution_vectors",
]
