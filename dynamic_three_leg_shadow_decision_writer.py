"""Isolated shadow decision worker for the dynamic three-leg trading plan.

This module is deliberately limited to databases created by
``initialize_isolated_public_ssot_shadow_schema``.  It consumes a committed
posterior outbox, writes shadow ``auto_trade_decisions`` and durable shadow
intents atomically, and never calls an exchange or authorizes live submission.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from dynamic_three_leg_trading_plan_runtime import build_dynamic_three_leg_trading_plan
from lineage_identity_runtime import lineage_id_from_parts
from settlement_window_runtime import build_settlement_window_state
from second_yes_entry_window_runtime import build_second_yes_entry_window_state
from top_yes_wave_reentry_runtime import build_top_yes_wave_reentry_state
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


SCHEMA_VERSION = "dynamic_three_leg_shadow_decision_writer_v1"
DECISION_STATE = "shadow_intent_committed"
INTENT_STATE = "shadow_pending_submit"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_utc(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse_mapping(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("dynamic_three_leg_shadow_manifest_missing")
    manifest = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if manifest is None or _text(manifest[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("dynamic_three_leg_shadow_manifest_invalid")
    required = (
        "event_fact_history",
        "event_fact_latest",
        "realization_states",
        "posterior_commit_outbox",
        "posterior_outbox_execution_contracts",
    )
    missing = [table for table in required if not _table_exists(conn, table)]
    if missing:
        raise ShadowTransactionIntegrityError(f"dynamic_three_leg_shadow_ssot_tables_missing:{','.join(missing)}")


def initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn: sqlite3.Connection) -> None:
    """Add decision-only test tables to an explicitly isolated shadow DB."""

    _assert_isolated_shadow_db(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trading_plans (
            trading_plan_id TEXT PRIMARY KEY,
            plan_revision INTEGER NOT NULL CHECK(plan_revision >= 0),
            posterior_run_id TEXT NOT NULL,
            realization_state_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            market_snapshot_id TEXT NOT NULL,
            capital_snapshot_id TEXT NOT NULL,
            risk_snapshot_id TEXT NOT NULL,
            capital_epoch_id TEXT NOT NULL,
            risk_reservation_id TEXT NOT NULL,
            strategy_artifact_sha256 TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state='shadow_intent_committed'),
            created_at TEXT NOT NULL,
            UNIQUE(posterior_run_id, plan_revision),
            UNIQUE(realization_state_id, market_snapshot_id, strategy_artifact_sha256, plan_revision)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT NOT NULL UNIQUE,
            trading_plan_id TEXT NOT NULL,
            plan_revision INTEGER NOT NULL,
            posterior_run_id TEXT NOT NULL,
            realization_state_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL,
            run_date TEXT NOT NULL,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            outcome TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('YES', 'NO')),
            action TEXT NOT NULL CHECK(action='entry'),
            decided_state TEXT NOT NULL CHECK(decided_state='shadow_intent_committed'),
            will_order INTEGER NOT NULL CHECK(will_order=0),
            reason_codes_json TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_intents (
            order_intent_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL UNIQUE,
            trading_plan_id TEXT NOT NULL,
            plan_revision INTEGER NOT NULL,
            posterior_run_id TEXT NOT NULL,
            realization_state_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            candidate_id TEXT NOT NULL UNIQUE,
            bucket TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('YES', 'NO')),
            action TEXT NOT NULL CHECK(action='BUY'),
            limit_price REAL NOT NULL CHECK(limit_price > 0 AND limit_price < 1),
            notional REAL NOT NULL CHECK(notional > 0),
            state TEXT NOT NULL CHECK(state='shadow_pending_submit'),
            live_submit_authorized INTEGER NOT NULL CHECK(live_submit_authorized=0),
            intent_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def claim_isolated_posterior_commit_outbox(
    conn: sqlite3.Connection,
    *,
    outbox_event_id: str,
    lease_owner: str,
    claimed_at: str,
    lease_until: str,
) -> dict[str, Any]:
    """Claim a pending/expired shadow posterior event with a generation CAS."""

    _assert_isolated_shadow_db(conn)
    event_id, owner, now, expiry = (_text(outbox_event_id), _text(lease_owner), _text(claimed_at), _text(lease_until))
    if not event_id or not owner or not now or not expiry or expiry <= now:
        raise ValueError("dynamic_three_leg_shadow_outbox_claim_identity_or_lease_invalid")
    row = conn.execute(
        """
        SELECT o.posterior_run_id, o.fact_snapshot_id, o.state, o.lease_owner,
               o.lease_until, o.claim_generation, x.execution_eligible_at_commit,
               x.observation_to_order_hard_deadline_at, x.execution_na_reason
        FROM posterior_commit_outbox o
        LEFT JOIN posterior_outbox_execution_contracts x
          ON x.outbox_event_id=o.outbox_event_id
         AND x.posterior_run_id=o.posterior_run_id
        WHERE o.outbox_event_id=?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "dynamic_three_leg_shadow_outbox_not_found"}
    state = _text(row[2])
    if state == "completed":
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "already_completed",
            "outbox_event_id": event_id,
            "posterior_run_id": _text(row[0]),
            "fact_snapshot_id": _text(row[1]),
            "claim_generation": int(row[5]),
            "shadow_only": True,
        }
    claimed_time = _parse_utc(now)
    hard_deadline = _parse_utc(row[7])
    if row[6] is None or hard_deadline is None:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "dynamic_three_leg_shadow_outbox_execution_contract_missing_or_invalid",
            "outbox_event_id": event_id,
            "shadow_only": True,
        }
    if int(row[6] or 0) != 1 or claimed_time is None or claimed_time > hard_deadline:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": _text(row[8]) or "dynamic_three_leg_shadow_observation_to_order_deadline_expired",
            "outbox_event_id": event_id,
            "observation_to_order_hard_deadline_at": _text(row[7]),
            "shadow_only": True,
        }
    may_claim = state == "pending" or (state == "claimed" and _text(row[4]) and _text(row[4]) <= now)
    if not may_claim:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "dynamic_three_leg_shadow_outbox_lease_held",
            "outbox_event_id": event_id,
            "shadow_only": True,
        }
    cur = conn.execute(
        """
        UPDATE posterior_commit_outbox
        SET state='claimed', claimed_at=?, lease_owner=?, lease_until=?, claim_generation=claim_generation+1
        WHERE outbox_event_id=?
          AND (state='pending' OR (state='claimed' AND lease_until IS NOT NULL AND lease_until<=?))
        """,
        (now, owner, expiry, event_id, now),
    )
    if cur.rowcount != 1:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "dynamic_three_leg_shadow_outbox_claim_cas_conflict",
            "outbox_event_id": event_id,
            "shadow_only": True,
        }
    claimed = conn.execute(
        "SELECT posterior_run_id,fact_snapshot_id,claim_generation FROM posterior_commit_outbox WHERE outbox_event_id=?",
        (event_id,),
    ).fetchone()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "claimed",
        "outbox_event_id": event_id,
        "posterior_run_id": _text(claimed[0]),
        "fact_snapshot_id": _text(claimed[1]),
        "lease_owner": owner,
        "claim_generation": int(claimed[2]),
        "lease_until": expiry,
        "shadow_only": True,
    }


def release_isolated_posterior_commit_outbox_claim(
    conn: sqlite3.Connection,
    *,
    outbox_event_id: str,
    lease_owner: str,
    claim_generation: int,
    reason: str,
) -> bool:
    """Release a claim when pure plan construction is blocked before a write."""

    cur = conn.execute(
        """
        UPDATE posterior_commit_outbox
        SET state='pending', lease_owner=NULL, lease_until=NULL, last_error=?
        WHERE outbox_event_id=? AND state='claimed' AND lease_owner=? AND claim_generation=?
        """,
        (_text(reason), _text(outbox_event_id), _text(lease_owner), int(claim_generation)),
    )
    return cur.rowcount == 1


def _load_claimed_realization_state(
    conn: sqlite3.Connection,
    *,
    outbox_event_id: str,
    lease_owner: str,
    claim_generation: int,
    slug: str,
    event_date: str,
) -> dict[str, Any]:
    outbox = conn.execute(
        """
        SELECT posterior_run_id,fact_snapshot_id,state,lease_owner,claim_generation,lease_until
        FROM posterior_commit_outbox WHERE outbox_event_id=?
        """,
        (_text(outbox_event_id),),
    ).fetchone()
    if outbox is None or _text(outbox[2]) != "claimed" or _text(outbox[3]) != _text(lease_owner) or int(outbox[4]) != int(claim_generation):
        return {"ok": False, "reason": "dynamic_three_leg_shadow_claim_not_current"}
    latest = conn.execute(
        """
        SELECT h.id,h.fact_json,h.fact_snapshot_id,h.posterior_run_id,h.correlation_id
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id=l.fact_id
        WHERE l.slug=? AND l.event_date=?
        """,
        (_text(slug), _text(event_date)),
    ).fetchone()
    if latest is None:
        return {"ok": False, "reason": "dynamic_three_leg_shadow_public_latest_missing"}
    if _text(latest[2]) != _text(outbox[1]) or _text(latest[3]) != _text(outbox[0]):
        return {"ok": False, "reason": "dynamic_three_leg_shadow_outbox_not_latest_ssot"}
    fact = _parse_mapping(latest[1])
    ssot = _mapping(fact.get("posterior_ssot_fact"))
    realization_id = _text(ssot.get("realization_state_id"))
    if (
        _text(ssot.get("posterior_run_id")) != _text(outbox[0])
        or _text(ssot.get("fact_snapshot_id")) != _text(outbox[1])
        or not realization_id
    ):
        return {"ok": False, "reason": "dynamic_three_leg_shadow_public_fact_lineage_invalid"}
    row = conn.execute(
        """
        SELECT realization_state_id,posterior_run_id,fact_snapshot_id,resolution_contract_id,
               target_bucket,second_target_bucket,credible_bucket_set_json,current_report_bucket,
               current_report_bucket_probability,probability_above_current_report_bucket,
               current_report_bucket_hard_zero,settlement_exclusion_certificates_json,state_json,state_sha256
        FROM realization_states WHERE realization_state_id=?
        """,
        (realization_id,),
    ).fetchone()
    if row is None or _text(row[1]) != _text(outbox[0]) or _text(row[2]) != _text(outbox[1]):
        return {"ok": False, "reason": "dynamic_three_leg_shadow_realization_not_bound_to_ssot"}
    state_payload = _parse_mapping(row[12])
    projection = _mapping(state_payload.get("projection"))
    if (
        state_payload.get("schema") != "realization_state_l3_v2"
        or projection.get("ok") is not True
        or _sha256(state_payload) != _text(row[13])
    ):
        return {"ok": False, "reason": "dynamic_three_leg_shadow_l3_projection_invalid"}
    realization = {
        "realization_state_id": _text(row[0]),
        "posterior_run_id": _text(row[1]),
        "fact_snapshot_id": _text(row[2]),
        "resolution_contract_id": _text(row[3]),
        "target_bucket": _text(row[4]),
        "second_target_bucket": _text(row[5]),
        "credible_bucket_set": json.loads(_text(row[6])),
        "current_report_bucket": _text(row[7]),
        "current_report_bucket_probability": row[8],
        "probability_above_current_report_bucket": row[9],
        "current_report_bucket_hard_zero": bool(row[10]),
        "settlement_exclusion_certificates": _parse_mapping(row[11]),
        "market_bucket_probabilities": dict(_mapping(projection.get("market_bucket_probabilities"))),
        "strong_physical_evidence": dict(_mapping(projection.get("strong_physical_evidence"))),
        "peak_timing_state": dict(_mapping(projection.get("peak_timing_state"))),
    }
    return {
        "ok": True,
        "realization_state": realization,
        "posterior_run_id": _text(outbox[0]),
        "fact_snapshot_id": _text(outbox[1]),
        "correlation_id": _text(latest[4]),
        "fact_id": int(latest[0]),
    }


def build_isolated_dynamic_three_leg_shadow_decision_candidate(
    conn: sqlite3.Connection,
    *,
    outbox_event_id: str,
    lease_owner: str,
    claim_generation: int,
    slug: str,
    event_date: str,
    market_snapshot: Mapping[str, Any],
    capital_plan: Mapping[str, Any],
    settlement_window_state: Mapping[str, Any],
    top_yes_wave_reentry_state: Mapping[str, Any],
    strategy_artifact_sha256: str,
    decision_as_of: str = "",
) -> dict[str, Any]:
    """Read a claimed latest SSOT and build the current dynamic three-leg plan."""

    _assert_isolated_shadow_db(conn)
    loaded = _load_claimed_realization_state(
        conn,
        outbox_event_id=outbox_event_id,
        lease_owner=lease_owner,
        claim_generation=claim_generation,
        slug=slug,
        event_date=event_date,
    )
    if not loaded.get("ok"):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": loaded.get("reason"), "shadow_only": True}
    realization = _mapping(loaded.get("realization_state"))
    second_yes_window = build_second_yes_entry_window_state(
        realization_state=realization,
        decision_as_of=_text(decision_as_of or settlement_window_state.get("decision_as_of")),
    )
    plan = build_dynamic_three_leg_trading_plan(
        realization_state=realization,
        settlement_window_state=settlement_window_state,
        second_yes_entry_window_state=second_yes_window,
        top_yes_wave_reentry_state=top_yes_wave_reentry_state,
        market_snapshot=market_snapshot,
        capital_plan=capital_plan,
        strategy_artifact_sha256=strategy_artifact_sha256,
    )
    if not plan.get("ok"):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "dynamic_three_leg_shadow_plan_blocked",
            "trading_plan": plan,
            "shadow_only": True,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "candidate_ready",
        "shadow_only": True,
        "outbox_event_id": _text(outbox_event_id),
        "lease_owner": _text(lease_owner),
        "claim_generation": int(claim_generation),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "posterior_run_id": _text(loaded.get("posterior_run_id")),
        "fact_snapshot_id": _text(loaded.get("fact_snapshot_id")),
        "correlation_id": _text(loaded.get("correlation_id")),
        "realization_state": dict(_mapping(loaded.get("realization_state"))),
        "trading_plan": plan,
    }


def _decision_bundle(candidate: Mapping[str, Any], *, decision_as_of: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    plan = _mapping(candidate.get("trading_plan"))
    decisions: list[dict[str, Any]] = []
    intents: list[dict[str, Any]] = []
    for row in plan.get("candidates") or []:
        leg = _text(_mapping(row).get("strategy_leg"))
        decision_id = lineage_id_from_parts(
            "decision_id",
            {"trading_plan_id": plan.get("trading_plan_id"), "candidate_id": _mapping(row).get("candidate_id")},
        )
        order_intent_id = lineage_id_from_parts(
            "order_intent_id",
            {"decision_id": decision_id, "action": "BUY", "shadow_only": True},
        )
        details = {
            "schema": "dynamic_three_leg_shadow_decision_v1",
            "shadow_only": True,
            "live_submission_authorized": False,
            "posterior_run_id": candidate.get("posterior_run_id"),
            "fact_snapshot_id": candidate.get("fact_snapshot_id"),
            "realization_state_id": _mapping(candidate.get("realization_state")).get("realization_state_id"),
            "trading_plan_id": plan.get("trading_plan_id"),
            "plan_revision": plan.get("plan_revision"),
            "candidate": dict(_mapping(row)),
            "order_intent_id": order_intent_id,
        }
        decision = {
            "decision_id": decision_id,
            "order_intent_id": order_intent_id,
            "strategy_leg": leg,
            "candidate": dict(_mapping(row)),
            "details": details,
            "created_at": _text(decision_as_of),
        }
        decisions.append(decision)
        intents.append(
            {
                "order_intent_id": order_intent_id,
                "decision_id": decision_id,
                "candidate": dict(_mapping(row)),
                "intent_json": _json({**details, "state": INTENT_STATE}),
                "created_at": _text(decision_as_of),
            }
        )
    second_window = _mapping(plan.get("second_yes_entry_window_state"))
    for row in plan.get("retention_candidates") or []:
        candidate_row = dict(_mapping(row))
        if _text(candidate_row.get("strategy_leg")) != "second_yes":
            continue
        reason_code = _text(
            candidate_row.get("entry_block_reason")
            or second_window.get("entry_block_reason")
            or "second_yes_entry_window_closed"
        )
        decision_id = lineage_id_from_parts(
            "decision_id",
            {
                "trading_plan_id": plan.get("trading_plan_id"),
                "candidate_id": candidate_row.get("candidate_id"),
                "action": "HOLD",
                "reason_code": reason_code,
            },
        )
        decisions.append({
            "decision_id": decision_id,
            "order_intent_id": "",
            "strategy_leg": "second_yes",
            "reason_code": reason_code,
            "candidate": candidate_row,
            "details": {
                "schema": "dynamic_three_leg_shadow_no_order_decision_v1",
                "shadow_only": True,
                "live_submission_authorized": False,
                "posterior_run_id": candidate.get("posterior_run_id"),
                "fact_snapshot_id": candidate.get("fact_snapshot_id"),
                "realization_state_id": _mapping(candidate.get("realization_state")).get("realization_state_id"),
                "trading_plan_id": plan.get("trading_plan_id"),
                "plan_revision": plan.get("plan_revision"),
                "candidate": candidate_row,
                "strategy_leg": "second_yes",
                "entry_allowed": False,
                "entry_block_reason": reason_code,
                "second_yes_entry_window_state": dict(second_window),
                "same_bucket_existing_position_may_be_retained": True,
                "uses_ev_edge_lcb": False,
            },
            "created_at": _text(decision_as_of),
        })
    return decisions, intents


def _existing_plan_is_complete(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    plan_sha256: str,
    decision_count: int,
    intent_count: int,
    outbox_event_id: str,
) -> bool:
    plan = conn.execute("SELECT plan_sha256 FROM trading_plans WHERE trading_plan_id=?", (_text(plan_id),)).fetchone()
    if plan is None or _text(plan[0]) != _text(plan_sha256):
        return False
    decisions = int(conn.execute("SELECT COUNT(*) FROM auto_trade_decisions WHERE trading_plan_id=?", (_text(plan_id),)).fetchone()[0])
    intents = int(conn.execute("SELECT COUNT(*) FROM decision_intents WHERE trading_plan_id=?", (_text(plan_id),)).fetchone()[0])
    outbox = conn.execute("SELECT state FROM posterior_commit_outbox WHERE outbox_event_id=?", (_text(outbox_event_id),)).fetchone()
    return decisions == decision_count and intents == intent_count and outbox is not None and _text(outbox[0]) == "completed"


def _insert_row(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def commit_isolated_dynamic_three_leg_shadow_decisions(
    conn: sqlite3.Connection,
    *,
    candidate: Mapping[str, Any],
    decision_as_of: str,
    fault_after: str = "",
) -> dict[str, Any]:
    """Atomically persist a claimed dynamic plan and shadow decisions/intents.

    There is intentionally no submit queue or exchange call in this module.
    ``fault_after`` is test-only and accepts ``plan``, ``decisions`` or ``intents``.
    """

    _assert_isolated_shadow_db(conn)
    input_candidate = _mapping(candidate)
    plan = _mapping(input_candidate.get("trading_plan"))
    if input_candidate.get("ok") is not True or input_candidate.get("state") != "candidate_ready" or plan.get("ok") is not True:
        raise ValueError("dynamic_three_leg_shadow_decision_candidate_not_ready")
    plan_id = _text(plan.get("trading_plan_id"))
    if not plan_id or not _text(decision_as_of):
        raise ValueError("dynamic_three_leg_shadow_decision_identity_missing")
    candidates = [dict(_mapping(row)) for row in plan.get("candidates") or []]
    retention_candidates = [
        dict(_mapping(row)) for row in plan.get("retention_candidates") or []
    ]
    plan_sha256 = _sha256(plan)
    if _existing_plan_is_complete(
        conn,
        plan_id=plan_id,
        plan_sha256=plan_sha256,
        decision_count=len(candidates) + len(retention_candidates),
        intent_count=len(candidates),
        outbox_event_id=_text(input_candidate.get("outbox_event_id")),
    ):
        return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": True, "shadow_only": True, "trading_plan_id": plan_id}
    decisions, intents = _decision_bundle(input_candidate, decision_as_of=decision_as_of)
    if len(decisions) != len(candidates) + len(retention_candidates) or len(intents) != len(candidates):
        raise ShadowTransactionIntegrityError("dynamic_three_leg_shadow_decision_bundle_incomplete")
    conn.execute("SAVEPOINT dynamic_three_leg_shadow_decision_tx")
    try:
        current = _load_claimed_realization_state(
            conn,
            outbox_event_id=_text(input_candidate.get("outbox_event_id")),
            lease_owner=_text(input_candidate.get("lease_owner")),
            claim_generation=int(input_candidate.get("claim_generation") or 0),
            slug=_text(input_candidate.get("slug")),
            event_date=_text(input_candidate.get("event_date")),
        )
        if not current.get("ok"):
            raise ShadowTransactionIntegrityError(str(current.get("reason") or "dynamic_three_leg_shadow_claim_or_ssot_stale"))
        if _text(current.get("posterior_run_id")) != _text(input_candidate.get("posterior_run_id")) or _text(current.get("fact_snapshot_id")) != _text(input_candidate.get("fact_snapshot_id")):
            raise ShadowTransactionIntegrityError("dynamic_three_leg_shadow_candidate_ssot_binding_changed")
        plan_row = {
            "trading_plan_id": plan_id,
            "plan_revision": int(plan.get("plan_revision") or 0),
            "posterior_run_id": _text(input_candidate.get("posterior_run_id")),
            "realization_state_id": _text(_mapping(input_candidate.get("realization_state")).get("realization_state_id")),
            "fact_snapshot_id": _text(input_candidate.get("fact_snapshot_id")),
            "market_snapshot_id": _text(plan.get("market_snapshot_id")),
            "capital_snapshot_id": _text(plan.get("capital_snapshot_id")),
            "risk_snapshot_id": _text(plan.get("risk_snapshot_id")),
            "capital_epoch_id": _text(plan.get("capital_epoch_id")),
            "risk_reservation_id": _text(plan.get("risk_reservation_id")),
            "strategy_artifact_sha256": _text(plan.get("strategy_artifact_sha256")),
            "plan_json": _json(plan),
            "plan_sha256": plan_sha256,
            "state": DECISION_STATE,
            "created_at": _text(decision_as_of),
        }
        _insert_row(conn, "trading_plans", plan_row)
        if fault_after == "plan":
            raise RuntimeError("dynamic_three_leg_shadow_fault_after_plan")
        for decision in decisions:
            leg = _text(decision.get("strategy_leg"))
            reason_code = _text(decision.get("reason_code") or leg)
            leg_candidate = _mapping(decision.get("candidate"))
            _insert_row(
                conn,
                "auto_trade_decisions",
                {
                    "decision_id": _text(decision.get("decision_id")),
                    "trading_plan_id": plan_id,
                    "plan_revision": int(plan.get("plan_revision") or 0),
                    "posterior_run_id": _text(input_candidate.get("posterior_run_id")),
                    "realization_state_id": _text(_mapping(input_candidate.get("realization_state")).get("realization_state_id")),
                    "fact_snapshot_id": _text(input_candidate.get("fact_snapshot_id")),
                    "correlation_id": _text(input_candidate.get("correlation_id")),
                    "candidate_id": _text(leg_candidate.get("candidate_id")),
                    "run_id": _text(input_candidate.get("posterior_run_id")),
                    "run_date": _text(input_candidate.get("event_date")),
                    "slug": _text(input_candidate.get("slug")),
                    "event_date": _text(input_candidate.get("event_date")),
                    "outcome": _text(leg_candidate.get("bucket")),
                    "side": _text(leg_candidate.get("side")),
                    "action": "entry",
                    "decided_state": DECISION_STATE,
                    "will_order": 0,
                    "reason_codes_json": _json([reason_code]),
                    "reasons_json": _json([{
                        "reason_family": "strategy_state" if not decision.get("order_intent_id") else "strategy_leg",
                        "reason_code": reason_code,
                    }]),
                    "details_json": _json(decision.get("details")),
                    "created_at": _text(decision_as_of),
                },
            )
        if fault_after == "decisions":
            raise RuntimeError("dynamic_three_leg_shadow_fault_after_decisions")
        for intent in intents:
            row = _mapping(intent.get("candidate"))
            _insert_row(
                conn,
                "decision_intents",
                {
                    "order_intent_id": _text(intent.get("order_intent_id")),
                    "decision_id": _text(intent.get("decision_id")),
                    "trading_plan_id": plan_id,
                    "plan_revision": int(plan.get("plan_revision") or 0),
                    "posterior_run_id": _text(input_candidate.get("posterior_run_id")),
                    "realization_state_id": _text(_mapping(input_candidate.get("realization_state")).get("realization_state_id")),
                    "fact_snapshot_id": _text(input_candidate.get("fact_snapshot_id")),
                    "candidate_id": _text(row.get("candidate_id")),
                    "bucket": _text(row.get("bucket")),
                    "side": _text(row.get("side")),
                    "action": "BUY",
                    "limit_price": float(row.get("price")),
                    "notional": float(row.get("notional")),
                    "state": INTENT_STATE,
                    "live_submit_authorized": 0,
                    "intent_json": _text(intent.get("intent_json")),
                    "created_at": _text(decision_as_of),
                },
            )
        if fault_after == "intents":
            raise RuntimeError("dynamic_three_leg_shadow_fault_after_intents")
        cur = conn.execute(
            """
            UPDATE posterior_commit_outbox
            SET state='completed', completed_at=?, completed_decision_id=?, lease_until=NULL
            WHERE outbox_event_id=? AND state='claimed' AND lease_owner=? AND claim_generation=?
              AND posterior_run_id=? AND fact_snapshot_id=?
            """,
            (
                _text(decision_as_of),
                _text(decisions[0].get("decision_id")) if decisions else "",
                _text(input_candidate.get("outbox_event_id")),
                _text(input_candidate.get("lease_owner")),
                int(input_candidate.get("claim_generation") or 0),
                _text(input_candidate.get("posterior_run_id")),
                _text(input_candidate.get("fact_snapshot_id")),
            ),
        )
        if cur.rowcount != 1:
            raise ShadowTransactionIntegrityError("dynamic_three_leg_shadow_outbox_complete_cas_conflict")
    except Exception:
        conn.execute("ROLLBACK TO dynamic_three_leg_shadow_decision_tx")
        conn.execute("RELEASE dynamic_three_leg_shadow_decision_tx")
        raise
    conn.execute("RELEASE dynamic_three_leg_shadow_decision_tx")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "idempotent": False,
        "shadow_only": True,
        "live_submission_authorized": False,
        "trading_plan_id": plan_id,
        "decision_ids": [decision["decision_id"] for decision in decisions],
        "order_intent_ids": [intent["order_intent_id"] for intent in intents],
    }


def run_isolated_dynamic_three_leg_shadow_decision_cycle(
    conn: sqlite3.Connection,
    *,
    outbox_event_id: str,
    lease_owner: str,
    claimed_at: str,
    lease_until: str,
    slug: str,
    event_date: str,
    market_snapshot: Mapping[str, Any],
    capital_plan: Mapping[str, Any],
    settlement_window_policy: Mapping[str, Any],
    strategy_artifact_sha256: str,
) -> dict[str, Any]:
    """Claim → compose → atomically persist a dynamic three-leg shadow bundle."""

    claimed = claim_isolated_posterior_commit_outbox(
        conn,
        outbox_event_id=outbox_event_id,
        lease_owner=lease_owner,
        claimed_at=claimed_at,
        lease_until=lease_until,
    )
    if claimed.get("state") == "already_completed":
        return {**claimed, "idempotent": True, "live_submission_authorized": False}
    if not claimed.get("ok"):
        return claimed
    settlement_window = build_settlement_window_state(
        resolution_local_date=event_date,
        decision_as_of=claimed_at,
        policy=settlement_window_policy,
    )
    if settlement_window.get("ok") is not True:
        release_isolated_posterior_commit_outbox_claim(
            conn,
            outbox_event_id=outbox_event_id,
            lease_owner=lease_owner,
            claim_generation=int(claimed.get("claim_generation") or 0),
            reason="dynamic_three_leg_shadow_settlement_window_blocked",
        )
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "reason": "dynamic_three_leg_shadow_settlement_window_blocked",
            "settlement_window_state": settlement_window,
            "shadow_only": True,
        }
    candidate = build_isolated_dynamic_three_leg_shadow_decision_candidate(
        conn,
        outbox_event_id=outbox_event_id,
        lease_owner=lease_owner,
        claim_generation=int(claimed.get("claim_generation") or 0),
        slug=slug,
        event_date=event_date,
        market_snapshot=market_snapshot,
        capital_plan=capital_plan,
        settlement_window_state=settlement_window,
        top_yes_wave_reentry_state=build_top_yes_wave_reentry_state(
            posterior_run_id=_text(claimed.get("posterior_run_id")),
            completed_cycles=[],
        ),
        strategy_artifact_sha256=strategy_artifact_sha256,
        decision_as_of=claimed_at,
    )
    if not candidate.get("ok"):
        release_isolated_posterior_commit_outbox_claim(
            conn,
            outbox_event_id=outbox_event_id,
            lease_owner=lease_owner,
            claim_generation=int(claimed.get("claim_generation") or 0),
            reason=_text(candidate.get("reason")),
        )
        return candidate
    return commit_isolated_dynamic_three_leg_shadow_decisions(conn, candidate=candidate, decision_as_of=claimed_at)


__all__ = [
    "DECISION_STATE",
    "INTENT_STATE",
    "SCHEMA_VERSION",
    "build_isolated_dynamic_three_leg_shadow_decision_candidate",
    "claim_isolated_posterior_commit_outbox",
    "commit_isolated_dynamic_three_leg_shadow_decisions",
    "initialize_isolated_dynamic_three_leg_shadow_decision_schema",
    "release_isolated_posterior_commit_outbox_claim",
    "run_isolated_dynamic_three_leg_shadow_decision_cycle",
]
