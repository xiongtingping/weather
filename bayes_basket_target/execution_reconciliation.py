"""M6 persistence CAS and deterministic order reconciliation.

The module stores execution evidence only.  It has no exchange client and no
submit path.  A version drift, expired quote, unknown POST, partial fill,
cancel/fill race, or late fill is a hard reconciliation condition; it never
guesses and never creates a replacement order.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Mapping

from .atomic_ledger import TARGET_LEDGER_TABLES, target_ledger_schema_status
from .contracts import ContractError, canonical_json, content_sha256
from .execution_version_vector import (
    SCHEMA as EXECUTION_VECTOR_SCHEMA,
    VERSION_FIELDS,
    ExecutionVersionVectorError,
    build_execution_version_vector,
)


SCHEMA = "bayes_basket_target_execution_reconciliation_v1"
EVENT_TYPES = frozenset({"POST_ACK", "FILL", "PARTIAL_FILL", "CANCEL_ACK", "UNKNOWN_POST", "LATE_FILL", "EXPIRED"})
RECONCILIATION_STATES = frozenset({"pending", "unknown", "resolved_filled", "resolved_cancelled", "resolved_partial"})
EPS = 1e-9


class ExecutionCasError(ContractError):
    """Raised when a candidate no longer matches its bound evidence."""


class ReconciliationError(ContractError):
    """Raised when order evidence cannot be reconciled deterministically."""


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ReconciliationError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReconciliationError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReconciliationError(f"{name}_timezone_required")
    return result


def _number(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ReconciliationError(f"{name}_invalid_number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(f"{name}_invalid_number") from exc
    if not math.isfinite(result) or result < 0:
        raise ReconciliationError(f"{name}_invalid_number")
    return result


def _schema_guard(conn: sqlite3.Connection) -> None:
    status = target_ledger_schema_status(conn)
    required = set(TARGET_LEDGER_TABLES) | {
        "basket_target_execution_version_vectors",
        "basket_target_order_events",
        "basket_target_order_reconciliations",
    }
    existing = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - existing)
    if missing or not status["ok"]:
        raise ReconciliationError("target_execution_schema_incomplete:" + ",".join(missing or ["ledger_contract"]))


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    own = not conn.in_transaction
    savepoint = "basket_target_m6_execution"
    if own:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception:
        if own:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        if own:
            conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")


def _stored_vector(conn: sqlite3.Connection, intent_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT vector_policy,strict_delta_artifact_sha256,"
        "probability_sha256,official_ws_quote_sha256,"
        "inventory_sha256,order_state_sha256,station_policy_sha256,"
        "cash_policy_decision_sha256,economic_policy_artifact_sha256,"
        "economic_policy_schema,"
        "target_price_id,quote_observed_at,quote_expires_at,vector_sha256 "
        "FROM basket_target_execution_version_vectors WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise ExecutionCasError("execution_version_vector_missing")
    if str(row[0]) != "EXECUTION":
        raise ExecutionCasError("execution_version_vector_policy_invalid")
    if row[1] is not None:
        raise ExecutionCasError(
            "execution_version_vector_legacy_policy_not_formal"
        )
    raw = {
        field: row[index + 2]
        for index, field in enumerate(VERSION_FIELDS)
    }
    raw["schema"] = EXECUTION_VECTOR_SCHEMA
    raw["vector_sha256"] = row[len(VERSION_FIELDS) + 2]
    try:
        return build_execution_version_vector(raw).to_dict()
    except ExecutionVersionVectorError as exc:
        raise ExecutionCasError(
            "execution_version_vector_stored_invalid:" + str(exc)
        ) from exc


def assert_execution_version_cas(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str,
    current_vector: Mapping[str, Any],
    at_utc: str,
) -> dict[str, Any]:
    """Require all seven fields to match and the quote to remain unexpired."""

    _schema_guard(conn)
    intent_id = _text("order_intent_id", order_intent_id)
    at = _timestamp("at_utc", at_utc)
    expected = _stored_vector(conn, intent_id)
    try:
        current = build_execution_version_vector(current_vector)
    except ExecutionVersionVectorError as exc:
        raise ExecutionCasError(
            "execution_version_vector_current_invalid:" + str(exc)
        ) from exc
    for field in VERSION_FIELDS:
        if str(expected[field]) != str(current[field]):
            raise ExecutionCasError(f"execution_version_drift:{field}")
    now_dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    expiry_dt = datetime.fromisoformat(str(expected["quote_expires_at"]).replace("Z", "+00:00"))
    if now_dt >= expiry_dt:
        raise ExecutionCasError("execution_quote_expired")
    return {"schema": SCHEMA, "state": "cas_valid", "order_intent_id": intent_id, "vector_sha256": expected["vector_sha256"], "places_orders": False, "calls_order_gateway": False}


def validate_persisted_candidate_consumption_cas(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    current_vectors_by_intent: Mapping[str, Mapping[str, Any]],
    at_utc: str,
) -> dict[str, Any]:
    """Validate a durable candidate without granting submission authority.

    The current deployment is production-no-live.  This is therefore the
    terminal consumer boundary: every persisted intent must still match its
    complete execution vector and deadline, but a successful validation only
    authorizes evidence consumption.  A future live deployment must introduce
    a separate authority contract; it cannot reuse the generic cycle CAS.
    """

    _schema_guard(conn)
    cycle = _text("candidate_cycle_id", cycle_id)
    at = _timestamp("candidate_consumed_at_utc", at_utc)
    if not isinstance(current_vectors_by_intent, Mapping):
        raise ExecutionCasError(
            "candidate_current_vectors_mapping_required"
        )
    cycle_row = conn.execute(
        "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
        (cycle,),
    ).fetchone()
    if cycle_row is None:
        raise ExecutionCasError("candidate_cycle_missing")
    if str(cycle_row[0]) != "prepared":
        raise ExecutionCasError("candidate_cycle_not_prepared")
    decision = conn.execute(
        "SELECT will_order,details_json FROM auto_trade_decisions "
        "WHERE cycle_id=?",
        (cycle,),
    ).fetchone()
    if decision is None:
        raise ExecutionCasError("candidate_decision_missing")
    try:
        details = json.loads(str(decision[1]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExecutionCasError("candidate_decision_details_invalid") from exc
    if not isinstance(details, Mapping):
        raise ExecutionCasError("candidate_decision_details_invalid")
    no_live = (
        str(details.get("execution_mode") or "")
        == "production_no_live"
        or details.get("no_live") is True
    )
    if (
        int(decision[0]) != 0
        or not no_live
        or details.get("live_submit_authorized") is True
    ):
        raise ExecutionCasError(
            "candidate_no_live_authority_boundary_invalid"
        )
    intents = conn.execute(
        "SELECT order_intent_id,target_execution_hard_deadline_at "
        "FROM basket_target_order_intents WHERE cycle_id=? "
        "ORDER BY order_intent_id",
        (cycle,),
    ).fetchall()
    expected_ids = {str(row[0]) for row in intents}
    observed_ids = {
        str(key or "").strip() for key in current_vectors_by_intent
    }
    if not expected_ids:
        if observed_ids:
            raise ExecutionCasError(
                "candidate_zero_intent_vector_set_not_empty"
            )
        return {
            "schema": SCHEMA,
            "state": "NO_EXECUTION_VALIDATED",
            "cycle_id": cycle,
            "intent_count": 0,
            "vector_sha256s": [],
            "authorizes_order": False,
            "evidence_only": True,
            "places_orders": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "orders_submitted": 0,
        }
    if observed_ids != expected_ids:
        raise ExecutionCasError("candidate_intent_vector_set_drift")
    at_dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
    vector_sha256s: list[str] = []
    for intent_id, deadline in intents:
        deadline_text = _timestamp(
            "candidate_target_execution_hard_deadline_at",
            deadline,
        )
        if at_dt >= datetime.fromisoformat(
            deadline_text.replace("Z", "+00:00")
        ):
            raise ExecutionCasError(
                "candidate_target_execution_hard_deadline_expired"
            )
        receipt = assert_execution_version_cas(
            conn,
            order_intent_id=str(intent_id),
            current_vector=current_vectors_by_intent[str(intent_id)],
            at_utc=at,
        )
        vector_sha256s.append(str(receipt["vector_sha256"]))
    return {
        "schema": SCHEMA,
        "state": "EVIDENCE_ONLY_VALIDATED",
        "cycle_id": cycle,
        "intent_count": len(intents),
        "vector_sha256s": sorted(vector_sha256s),
        "authorizes_order": False,
        "evidence_only": True,
        "places_orders": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


def _intent_row(conn: sqlite3.Connection, intent_id: str) -> tuple[str, str, float, str, str]:
    row = conn.execute(
        "SELECT state,bucket,requested_size,token_id,order_action FROM basket_target_order_intents WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise ReconciliationError("order_intent_missing")
    return str(row[0]), str(row[1]), float(row[2]), str(row[3]), str(row[4])


def _event_identity(intent_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "order_intent_id": intent_id,
        "exchange_order_id": None if event.get("exchange_order_id") in (None, "") else str(event["exchange_order_id"]),
        "event_type": _text("event_type", event.get("event_type")).upper(),
        "observed_at": _timestamp("event_observed_at", event.get("observed_at")),
        "matched_size": _number("matched_size", event.get("matched_size", 0.0)),
        "matched_notional_usdc": _number("matched_notional_usdc", event.get("matched_notional_usdc", 0.0)),
        "event_payload": dict(event.get("event_payload") or {}),
    }


def _state_after_event(event_type: str, filled_size: float, requested_size: float) -> tuple[str, str, str]:
    if event_type == "UNKNOWN_POST":
        return "unknown", "reconcile_required", "unknown_post_state"
    if event_type in {"PARTIAL_FILL", "LATE_FILL"}:
        if filled_size >= requested_size - EPS:
            return "resolved_filled", "filled", "full_fill_authoritative"
        return "unknown" if event_type == "LATE_FILL" else "pending", "reconcile_required", "partial_or_late_fill"
    if event_type == "FILL":
        if filled_size >= requested_size - EPS:
            return "resolved_filled", "filled", "full_fill"
        return "pending", "reconcile_required", "partial_fill_requires_reconcile"
    if event_type == "CANCEL_ACK":
        if filled_size >= requested_size - EPS:
            return "resolved_filled", "filled", "fill_cancel_race_resolved_by_fill"
        if filled_size > EPS:
            return "unknown", "reconcile_required", "cancel_fill_race"
        return "resolved_cancelled", "cancelled", "cancel_ack_no_fill"
    if event_type in {"EXPIRED"}:
        if filled_size > EPS:
            return "unknown", "reconcile_required", "expired_with_fill"
        return "resolved_cancelled", "cancelled", "expired_no_fill"
    return "pending", "submitted", "post_ack_pending_fill"


def record_order_event(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one event and update the deterministic reconciliation state."""

    _schema_guard(conn)
    intent_id = _text("order_intent_id", order_intent_id)
    event_identity = _event_identity(intent_id, event)
    event_type = event_identity["event_type"]
    if event_type not in EVENT_TYPES:
        raise ReconciliationError("order_event_type_invalid")
    event_sha = content_sha256({"schema": "basket_target_order_event_v1", **event_identity})
    event_id = str(event.get("event_id") or _derive_event_id(event_sha))
    if event_id != _derive_event_id(event_sha):
        raise ReconciliationError("order_event_content_address_mismatch")
    with _transaction(conn):
        current_intent_state, bucket, requested_size, _token_id, _action = _intent_row(conn, intent_id)
        existing = conn.execute("SELECT event_sha256 FROM basket_target_order_events WHERE event_id=?", (event_id,)).fetchone()
        if existing is not None:
            if str(existing[0]) != event_sha:
                raise ReconciliationError("order_event_content_address_drift")
            return {"schema": SCHEMA, "state": "duplicate_event", "idempotent": True, "event_id": event_id, "order_intent_id": intent_id}
        prior = conn.execute(
            "SELECT state,exchange_order_id,filled_size,filled_notional_usdc FROM basket_target_order_reconciliations WHERE order_intent_id=?",
            (intent_id,),
        ).fetchone()
        prior_state = str(prior[0]) if prior else "pending"
        prior_order_id = str(prior[1]) if prior and prior[1] else None
        exchange_order_id = event_identity["exchange_order_id"] or prior_order_id
        if prior_order_id and event_identity["exchange_order_id"] and prior_order_id != event_identity["exchange_order_id"]:
            raise ReconciliationError("exchange_order_id_drift")
        prior_filled = float(prior[2]) if prior else 0.0
        filled_size = prior_filled + float(event_identity["matched_size"])
        recon_state, intent_state, reason = _state_after_event(event_type, filled_size, requested_size)
        if prior_state == "unknown" and event_type == "POST_ACK":
            recon_state, intent_state, reason = "unknown", "reconcile_required", "unknown_state_not_cleared_by_post_ack"
        conn.execute(
            "INSERT INTO basket_target_order_events(event_id,order_intent_id,exchange_order_id,event_type,observed_at,matched_size,matched_notional_usdc,event_json,event_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
            (event_id, intent_id, exchange_order_id, event_type, event_identity["observed_at"], event_identity["matched_size"], event_identity["matched_notional_usdc"], canonical_json(event_identity["event_payload"]), event_sha),
        )
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations(order_intent_id,exchange_order_id,state,filled_size,filled_notional_usdc,last_event_id,blocking_reason,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(order_intent_id) DO UPDATE SET exchange_order_id=excluded.exchange_order_id,state=excluded.state,filled_size=excluded.filled_size,filled_notional_usdc=excluded.filled_notional_usdc,last_event_id=excluded.last_event_id,blocking_reason=excluded.blocking_reason,updated_at=excluded.updated_at",
            (intent_id, exchange_order_id, recon_state, filled_size, (float(prior[3]) if prior else 0.0) + float(event_identity["matched_notional_usdc"]), event_id, None if intent_state in {"submitted", "filled", "cancelled"} else reason, event_identity["observed_at"]),
        )
        conn.execute("UPDATE basket_target_order_intents SET state=? WHERE order_intent_id=?", (intent_state, intent_id))
    return {"schema": SCHEMA, "state": intent_state, "reconciliation_state": recon_state, "reason": reason, "event_id": event_id, "order_intent_id": intent_id, "filled_size": filled_size, "places_orders": False, "calls_order_gateway": False}


def _derive_event_id(event_sha: str) -> str:
    return "event_" + event_sha[:40]


def resolve_reconciliation(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str,
    authoritative_state: str,
    filled_size: float,
    filled_notional_usdc: float,
    resolved_at_utc: str,
) -> dict[str, Any]:
    """Apply an explicit authoritative exchange state; never infer one."""

    _schema_guard(conn)
    intent_id = _text("order_intent_id", order_intent_id)
    state = _text("authoritative_state", authoritative_state).lower()
    if state not in {"filled", "cancelled", "partial"}:
        raise ReconciliationError("authoritative_reconciliation_state_invalid")
    filled = _number("filled_size", filled_size)
    notional = _number("filled_notional_usdc", filled_notional_usdc)
    resolved_at = _timestamp("resolved_at_utc", resolved_at_utc)
    with _transaction(conn):
        _intent_state, _bucket, requested_size, _token_id, _action = _intent_row(conn, intent_id)
        # An intent carries a token, never an exchange order id.  The only
        # place that id exists is the reconciliation row an earlier event
        # wrote, so carry it forward rather than replacing it: an
        # authoritative state must not erase or rewrite the exchange handle
        # that ``record_order_event`` later compares against.
        prior_order = conn.execute(
            "SELECT exchange_order_id FROM basket_target_order_reconciliations "
            "WHERE order_intent_id=?",
            (intent_id,),
        ).fetchone()
        exchange_order_id = (
            str(prior_order[0])
            if prior_order is not None and prior_order[0]
            else None
        )
        if state == "filled" and filled + EPS < requested_size:
            raise ReconciliationError("authoritative_filled_size_incomplete")
        if state == "cancelled" and filled > EPS:
            raise ReconciliationError("authoritative_cancelled_with_fill")
        if state == "partial" and not EPS < filled < requested_size - EPS:
            raise ReconciliationError("authoritative_partial_size_invalid")
        recon_state = {"filled": "resolved_filled", "cancelled": "resolved_cancelled", "partial": "resolved_partial"}[state]
        intent_state = {"filled": "filled", "cancelled": "cancelled", "partial": "reconcile_required"}[state]
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations(order_intent_id,exchange_order_id,state,filled_size,filled_notional_usdc,last_event_id,blocking_reason,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(order_intent_id) DO UPDATE SET exchange_order_id=excluded.exchange_order_id,state=excluded.state,filled_size=excluded.filled_size,filled_notional_usdc=excluded.filled_notional_usdc,last_event_id=excluded.last_event_id,blocking_reason=excluded.blocking_reason,updated_at=excluded.updated_at",
            (intent_id, exchange_order_id, recon_state, filled, notional, None, None if state != "partial" else "authoritative_partial_requires_manual_close", resolved_at),
        )
        conn.execute("UPDATE basket_target_order_intents SET state=? WHERE order_intent_id=?", (intent_state, intent_id))
    return {"schema": SCHEMA, "state": intent_state, "reconciliation_state": recon_state, "order_intent_id": intent_id, "filled_size": filled, "places_orders": False, "calls_order_gateway": False}


def can_submit_next_order(
    conn: sqlite3.Connection,
    *,
    bucket: str,
    token_id: str,
    exclude_order_intent_id: str = "",
) -> dict[str, Any]:
    """Return false while any same-bucket/token action is unknown or pending."""

    _schema_guard(conn)
    bucket_value = _text("bucket", bucket)
    token = _text("token_id", token_id)
    exclude = str(exclude_order_intent_id or "").strip()
    rows = conn.execute(
        "SELECT i.order_intent_id,i.state,r.state FROM basket_target_order_intents i LEFT JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id WHERE i.bucket=? AND i.token_id=? AND i.order_intent_id<>?",
        (bucket_value, token, exclude),
    ).fetchall()
    # A missing reconciliation row means the intent never reached the
    # exchange, so the intent's own state is the whole truth about it.
    # Reading that absence as a pending reconciliation made every intent
    # cancelled without an event -- which is what supersession writes -- block
    # its bucket and token forever.  An intent that is still live, or a real
    # reconciliation row that is pending or unknown, both still block.
    blockers = [
        {
            "order_intent_id": str(row[0]),
            "intent_state": str(row[1]),
            "reconciliation_state": "none" if row[2] is None else str(row[2]),
        }
        for row in rows
        if str(row[1]) in {"prepared", "submitting", "submitted", "reconcile_required"}
        or (row[2] is not None and str(row[2]) in {"pending", "unknown"})
    ]
    return {"schema": SCHEMA, "allowed": not blockers, "blockers": blockers, "reason": "same_bucket_unknown_or_pending" if blockers else "no_same_bucket_pending", "places_orders": False, "calls_order_gateway": False}


def assert_basket_hold_only(intents: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    """Reject merge, selective balancing, and early sell semantics."""

    violations: list[str] = []
    for index, row in enumerate(intents):
        if str(row.get("side") or "").upper() != "YES" or str(row.get("order_action") or "").upper() != "BUY":
            violations.append(f"intent_{index}_not_yes_buy_hold")
        for forbidden in ("merge", "selective_rebalance", "early_sell"):
            if row.get(forbidden) is True:
                violations.append(f"intent_{index}_{forbidden}_forbidden")
    if violations:
        raise ReconciliationError("basket_hold_semantics_invalid:" + ",".join(violations))
    return {"schema": SCHEMA, "hold_to_settlement": True, "merge": False, "selective_rebalance": False, "early_sell": False}


def apply_execution_receipt(conn: sqlite3.Connection, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the formal no-live receipt contract.

    Kept as a lazy wrapper so this low-level reconciliation module remains
    importable by the transition runtime without a circular import.  All
    production receipt callers must use this boundary; the older
    ``record_order_event`` function remains available for historical ledger
    replay and does not authorize any gateway action.
    """

    from .execution_transition_runtime import apply_execution_receipt as _apply

    return _apply(conn, receipt)


def build_no_live_execution_receipt(**kwargs: Any) -> dict[str, Any]:
    """Build a content-addressed simulated receipt without side effects."""

    from .execution_transition_runtime import (
        build_no_live_execution_receipt as _build,
    )

    return _build(**kwargs)


def can_submit_fok(
    conn: sqlite3.Connection,
    *,
    predecessor_order_intent_id: str,
    current_vector: Mapping[str, Any],
    at_utc: str,
) -> dict[str, Any]:
    """Gate FOK on an authoritative zero-fill cancel confirmation."""

    from .execution_transition_runtime import can_submit_fok as _can_submit

    return _can_submit(
        conn,
        predecessor_order_intent_id=predecessor_order_intent_id,
        current_vector=current_vector,
        at_utc=at_utc,
    )


__all__ = [
    "EVENT_TYPES",
    "ExecutionCasError",
    "RECONCILIATION_STATES",
    "SCHEMA",
    "VERSION_FIELDS",
    "assert_basket_hold_only",
    "assert_execution_version_cas",
    "apply_execution_receipt",
    "build_execution_version_vector",
    "build_no_live_execution_receipt",
    "can_submit_fok",
    "can_submit_next_order",
    "record_order_event",
    "resolve_reconciliation",
    "ReconciliationError",
    "validate_persisted_candidate_consumption_cas",
]
