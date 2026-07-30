"""Atomic Target Realization v6 Transaction C writer.

This boundary never calls CLOB.  It commits the full v6 policy state and the
durable decisions/intents derived from one claimed posterior outbox.  A
network submitter can only act after this transaction commits.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence

from trade_exit_reason_contract import (
    is_v6_executable_exit_reason_code,
    v6_exit_reason_family_for_code,
)


SCHEMA_VERSION = "production_dynamic_three_leg_v6_transaction_c_v1"
ACTIVATION_SCHEMA = "target_realization_v6_production_activation_v1"


class V6TransactionCIntegrityError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_schema(conn: sqlite3.Connection) -> None:
    required = {
        "posterior_commit_outbox",
        "auto_trade_decisions",
        "posterior_execution_intents",
        "target_realization_v6_plans",
        "dynamic_three_leg_v6_main_yes_cycles",
        "dynamic_three_leg_v6_capital_allocations",
        "dynamic_three_leg_v6_risk_reservations",
        "dynamic_three_leg_v6_standing_exit_release_requests",
        "dynamic_three_leg_v6_transaction_c_commits",
    }
    missing = sorted(table for table in required if not _table_exists(conn, table))
    if missing:
        raise V6TransactionCIntegrityError("v6_transaction_c_tables_missing:" + ",".join(missing))


def _activation_blockers(
    activation: Mapping[str, Any],
    *,
    strategy_artifact_sha256: str,
    exact_exit_reason_policy_sha256: str,
) -> list[str]:
    blockers: list[str] = []
    if activation.get("schema") != ACTIVATION_SCHEMA or activation.get("state") != "enabled":
        blockers.append("v6_transaction_c_activation_not_enabled")
    if _text(activation.get("strategy_artifact_sha256")) != strategy_artifact_sha256:
        blockers.append("v6_transaction_c_strategy_artifact_mismatch")
    if _text(activation.get("exact_exit_reason_policy_sha256")) != exact_exit_reason_policy_sha256:
        blockers.append("v6_transaction_c_exit_policy_mismatch")
    if activation.get("live_decision_write_authorized") is not True:
        blockers.append("v6_transaction_c_decision_write_not_authorized")
    if activation.get("live_submit_authorized") is not True:
        blockers.append("v6_transaction_c_submit_not_authorized")
    if not _text(activation.get("cutover_artifact_sha256")):
        blockers.append("v6_transaction_c_cutover_artifact_missing")
    return blockers


def _validate_intents(plan: Mapping[str, Any], intents: Sequence[Mapping[str, Any]]) -> list[str]:
    blockers: list[str] = []
    candidates = {_text(row.get("candidate_id")): row for row in _rows(plan.get("candidates"))}
    no_buckets: set[str] = set()
    for row in intents:
        action = _text(row.get("order_action")).upper()
        reason_code = _text(row.get("reason_code"))
        reason_family = _text(row.get("reason_family"))
        if action == "SELL":
            if (
                not is_v6_executable_exit_reason_code(reason_code)
                or v6_exit_reason_family_for_code(reason_code) != reason_family
            ):
                blockers.append("v6_transaction_c_exit_reason_outside_exact_closed_set")
        elif action == "BUY":
            candidate = candidates.get(_text(row.get("candidate_id")))
            if candidate is None:
                blockers.append("v6_transaction_c_buy_candidate_missing")
                continue
            if reason_family != "strategy_leg" or reason_code != _text(candidate.get("strategy_leg")):
                blockers.append("v6_transaction_c_buy_reason_binding_invalid")
            try:
                requested = float(row.get("notional"))
                final = float(candidate.get("final_incremental_target_notional_usdc"))
            except (TypeError, ValueError):
                blockers.append("v6_transaction_c_buy_notional_invalid")
            else:
                if requested <= 0 or requested > final + 1e-9:
                    blockers.append("v6_transaction_c_buy_exceeds_scaled_allocation")
            if _text(candidate.get("strategy_leg")) in {"trajectory_no", "hard_exclusion_no"}:
                no_buckets.add(_text(candidate.get("bucket")))
        else:
            blockers.append("v6_transaction_c_intent_action_invalid")
    if len(no_buckets) > 1:
        blockers.append("v6_transaction_c_multiple_no_buckets")
    return sorted(set(blockers))


def commit_target_realization_v6_transaction_c(
    conn: sqlite3.Connection,
    *,
    outbox_event_id: str,
    lease_owner: str,
    claim_generation: int,
    plan: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    intents: Sequence[Mapping[str, Any]],
    production_activation: Mapping[str, Any],
    exact_exit_reason_policy_sha256: str,
    committed_at: str,
    coverage_release_requests: Sequence[Mapping[str, Any]] = (),
    fault_after: str = "",
) -> dict[str, Any]:
    """Commit v6 state, decisions, intents and outbox completion atomically."""

    _assert_schema(conn)
    plan = dict(plan)
    decision_rows = [dict(row) for row in decisions]
    intent_rows = [dict(row) for row in intents]
    release_rows = [dict(row) for row in coverage_release_requests]
    blockers: list[str] = []
    if plan.get("schema") != "target_realization_v6_composer_v1" or plan.get("ok") is not True:
        blockers.append("v6_transaction_c_plan_invalid")
    artifact = _text(plan.get("strategy_artifact_sha256"))
    policy_sha = _text(exact_exit_reason_policy_sha256)
    blockers.extend(
        _activation_blockers(
            production_activation,
            strategy_artifact_sha256=artifact,
            exact_exit_reason_policy_sha256=policy_sha,
        )
    )
    blockers.extend(_validate_intents(plan, intent_rows))
    for row in release_rows:
        if (
            not _text(row.get("standing_exit_release_request_id"))
            or not _text(row.get("standing_exit_plan_id"))
            or not is_v6_executable_exit_reason_code(row.get("target_realization_reason_code"))
            or v6_exit_reason_family_for_code(row.get("target_realization_reason_code")) != "target_realization"
            or _text(row.get("state")) != "prepared"
        ):
            blockers.append("v6_transaction_c_coverage_release_request_invalid")
    if not decision_rows:
        blockers.append("v6_transaction_c_decision_rows_missing")
    if any(_text(row.get("posterior_run_id")) != _text(plan.get("posterior_run_id")) for row in intent_rows):
        blockers.append("v6_transaction_c_intent_posterior_mismatch")
    outbox = conn.execute(
        "SELECT posterior_run_id,fact_snapshot_id,correlation_id,state,lease_owner,claim_generation FROM posterior_commit_outbox WHERE outbox_event_id=?",
        (_text(outbox_event_id),),
    ).fetchone()
    if outbox is None:
        blockers.append("v6_transaction_c_outbox_missing")
    elif (
        _text(outbox[0]) != _text(plan.get("posterior_run_id"))
        or _text(outbox[3]) != "claimed"
        or _text(outbox[4]) != _text(lease_owner)
        or int(outbox[5] or 0) != int(claim_generation)
    ):
        blockers.append("v6_transaction_c_outbox_claim_or_lineage_invalid")
    transaction_c_id = "v6txc_" + _sha({"outbox_event_id": outbox_event_id, "v6_trading_plan_id": plan.get("v6_trading_plan_id")})[:24]
    existing = conn.execute(
        "SELECT commit_sha256 FROM dynamic_three_leg_v6_transaction_c_commits WHERE transaction_c_id=?",
        (transaction_c_id,),
    ).fetchone()
    commit_identity = {
        "transaction_c_id": transaction_c_id,
        "outbox_event_id": _text(outbox_event_id),
        "v6_trading_plan_id": _text(plan.get("v6_trading_plan_id")),
        "posterior_run_id": _text(plan.get("posterior_run_id")),
        "strategy_artifact_sha256": artifact,
        "exact_exit_reason_policy_sha256": policy_sha,
        "decision_ids": [_text(row.get("decision_id")) for row in decision_rows],
        "order_intent_ids": [_text(row.get("order_intent_id")) for row in intent_rows],
        "coverage_release_request_ids": [_text(row.get("standing_exit_release_request_id")) for row in release_rows],
    }
    commit_sha = _sha(commit_identity)
    if existing is not None:
        if _text(existing[0]) != commit_sha:
            raise V6TransactionCIntegrityError("v6_transaction_c_idempotency_drift")
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "already_committed", "idempotent": True, "transaction_c_id": transaction_c_id, "places_orders": False}
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers)), "wrote_rows": False, "places_orders": False}

    cycle = dict(plan.get("main_yes_cycle") or {})
    allocation = dict(plan.get("capital_allocation") or {})
    plan_json = _json(plan)
    cycle_json = _json(cycle)
    allocation_json = _json(allocation)
    conn.execute("SAVEPOINT target_realization_v6_transaction_c")
    try:
        conn.execute(
            """INSERT INTO target_realization_v6_plans(
                v6_trading_plan_id,posterior_run_id,realization_state_id,canonical_observation_event_id,
                strategy_artifact_sha256,main_yes_position_cycle_id,capital_allocation_id,main_yes_bucket,
                second_yes_bucket,no_mode,capital_scale_factor,plan_json,plan_sha256,state,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'committed',?,?)""",
            (
                _text(plan.get("v6_trading_plan_id")), _text(plan.get("posterior_run_id")),
                _text(plan.get("realization_state_id")), _text(plan.get("canonical_observation_event_id")),
                artifact, _text(plan.get("main_yes_position_cycle_id")), _text(plan.get("capital_allocation_id")),
                _text(plan.get("main_yes_bucket")), _text(plan.get("second_yes_bucket")) or None,
                _text(plan.get("no_mode")), float(allocation.get("capital_scale_factor")),
                plan_json, _sha(plan), _text(committed_at), _text(committed_at),
            ),
        )
        conn.execute(
            """INSERT INTO dynamic_three_leg_v6_main_yes_cycles(
                cycle_checkpoint_id,main_yes_position_cycle_id,v6_trading_plan_id,posterior_run_id,
                canonical_observation_event_id,bucket,main_yes_mode,cycle_generation,
                sell_stage_high_watermark,reentry_confirmation_count,cycle_json,cycle_sha256,state,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "v6cyclecp_" + _sha((plan.get("v6_trading_plan_id"), cycle))[:24],
                _text(cycle.get("main_yes_position_cycle_id")), _text(plan.get("v6_trading_plan_id")),
                _text(plan.get("posterior_run_id")), _text(plan.get("canonical_observation_event_id")),
                _text(cycle.get("bucket")), _text(cycle.get("main_yes_mode")), int(cycle.get("cycle_generation")),
                int(cycle.get("sell_stage_high_watermark")), int(cycle.get("reentry_confirmation_count")),
                cycle_json, _sha(cycle), _text(cycle.get("state")), _text(committed_at), _text(committed_at),
            ),
        )
        conn.execute(
            """INSERT INTO dynamic_three_leg_v6_capital_allocations(
                capital_allocation_id,v6_trading_plan_id,capital_snapshot_id,risk_snapshot_id,capital_epoch_id,
                capital_scale_factor,requested_incremental_notional_usdc,allocated_incremental_notional_usdc,
                allocation_json,allocation_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _text(allocation.get("capital_allocation_id")), _text(plan.get("v6_trading_plan_id")),
                _text(allocation.get("capital_snapshot_id")), _text(allocation.get("risk_snapshot_id")),
                _text(allocation.get("capital_epoch_id")), float(allocation.get("capital_scale_factor")),
                float(allocation.get("requested_incremental_notional_usdc")),
                float(allocation.get("allocated_incremental_notional_usdc")),
                allocation_json, _sha(allocation), _text(committed_at),
            ),
        )
        reservation_id = _text(plan.get("risk_reservation_id"))
        if not reservation_id:
            raise V6TransactionCIntegrityError("v6_transaction_c_risk_reservation_identity_missing")
        conn.execute(
            """INSERT INTO dynamic_three_leg_v6_risk_reservations(
                risk_reservation_id,v6_trading_plan_id,posterior_run_id,capital_allocation_id,
                capital_snapshot_id,risk_snapshot_id,capital_epoch_id,strategy_artifact_sha256,
                reserved_buy_notional_usdc,state,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'bound',?,?)""",
            (
                reservation_id, _text(plan.get("v6_trading_plan_id")), _text(plan.get("posterior_run_id")),
                _text(allocation.get("capital_allocation_id")), _text(allocation.get("capital_snapshot_id")),
                _text(allocation.get("risk_snapshot_id")), _text(allocation.get("capital_epoch_id")), artifact,
                float(allocation.get("allocated_incremental_notional_usdc")), _text(committed_at), _text(committed_at),
            ),
        )
        if fault_after == "policy_state":
            raise RuntimeError("v6_transaction_c_fault_after_policy_state")
        for row in release_rows:
            conn.execute(
                """INSERT INTO dynamic_three_leg_v6_standing_exit_release_requests(
                    standing_exit_release_request_id,standing_exit_plan_id,v6_trading_plan_id,main_yes_position_cycle_id,
                    target_realization_reason_code,requested_release_size,standing_sell_order_id,
                    idempotency_key,state,gateway_response_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,'prepared','{}',?,?)""",
                (
                    _text(row.get("standing_exit_release_request_id")), _text(row.get("standing_exit_plan_id")),
                    _text(row.get("v6_trading_plan_id")), _text(row.get("main_yes_position_cycle_id")),
                    _text(row.get("target_realization_reason_code")),
                    float(row.get("requested_release_size")), _text(row.get("standing_sell_order_id")),
                    _text(row.get("idempotency_key")), _text(row.get("created_at") or committed_at),
                    _text(row.get("updated_at") or committed_at),
                ),
            )
        for row in decision_rows:
            conn.execute(
                """INSERT INTO auto_trade_decisions(
                    run_id,run_date,slug,event_date,outcome,side,action,decided_state,will_order,
                    reason_codes_json,reasons_json,details_json,created_at,correlation_id,fact_snapshot_id,
                    posterior_run_id,decision_id,order_intent_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    _text(plan.get("posterior_run_id")), _text(row.get("run_date")), _text(row.get("slug")),
                    _text(row.get("event_date")), _text(row.get("outcome")), _text(row.get("side")),
                    _text(row.get("action")), _text(row.get("decided_state")), int(bool(row.get("will_order"))),
                    _json(row.get("reason_codes") or []), _json(row.get("reasons") or []), _json(row.get("details") or {}),
                    _text(committed_at), _text(outbox[2]), _text(outbox[1]), _text(outbox[0]),
                    _text(row.get("decision_id")), _text(row.get("order_intent_id")) or None,
                ),
            )
        if fault_after == "decisions":
            raise RuntimeError("v6_transaction_c_fault_after_decisions")
        for row in intent_rows:
            fields = list(row)
            conn.execute(
                f"INSERT INTO posterior_execution_intents({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
                tuple(row[field] for field in fields),
            )
        if fault_after == "intents":
            raise RuntimeError("v6_transaction_c_fault_after_intents")
        conn.execute(
            """INSERT INTO dynamic_three_leg_v6_transaction_c_commits(
                transaction_c_id,outbox_event_id,v6_trading_plan_id,posterior_run_id,strategy_artifact_sha256,
                exact_exit_reason_policy_sha256,decision_count,intent_count,commit_sha256,state,committed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'committed',?)""",
            (
                transaction_c_id, _text(outbox_event_id), _text(plan.get("v6_trading_plan_id")),
                _text(plan.get("posterior_run_id")), artifact, policy_sha, len(decision_rows), len(intent_rows),
                commit_sha, _text(committed_at),
            ),
        )
        conn.execute(
            """UPDATE posterior_commit_outbox
               SET state='completed',completed_at=?,completed_decision_id=?,lease_owner=NULL,lease_until=NULL
               WHERE outbox_event_id=? AND state='claimed' AND lease_owner=? AND claim_generation=?""",
            (_text(committed_at), _text(decision_rows[0].get("decision_id")), _text(outbox_event_id), _text(lease_owner), int(claim_generation)),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            raise V6TransactionCIntegrityError("v6_transaction_c_outbox_completion_cas_failed")
        if fault_after == "outbox":
            raise RuntimeError("v6_transaction_c_fault_after_outbox")
    except Exception:
        conn.execute("ROLLBACK TO target_realization_v6_transaction_c")
        conn.execute("RELEASE target_realization_v6_transaction_c")
        raise
    conn.execute("RELEASE target_realization_v6_transaction_c")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "committed",
        "idempotent": False,
        "transaction_c_id": transaction_c_id,
        "decision_count": len(decision_rows),
        "intent_count": len(intent_rows),
        "places_orders": False,
    }


__all__ = [
    "ACTIVATION_SCHEMA",
    "SCHEMA_VERSION",
    "V6TransactionCIntegrityError",
    "commit_target_realization_v6_transaction_c",
]
