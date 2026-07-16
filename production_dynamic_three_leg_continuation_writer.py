"""Atomic writer for same-posterior transitions after SELL_OLD completes."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Mapping

from dynamic_three_leg_position_lot_runtime import build_position_lot_identity
from dynamic_three_leg_production_activation import validate_dynamic_three_leg_activation
from lineage_identity_runtime import lineage_id_from_parts
from production_dynamic_three_leg_decision_writer import _execution_rows
from production_dynamic_three_leg_risk_reservation_writer import reserve_dynamic_three_leg_risk_budget


SCHEMA_VERSION = "production_dynamic_three_leg_continuation_writer_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _enabled(name: str) -> bool:
    return _text(os.environ.get(name)).lower() in {"1", "true", "yes", "on"}


def commit_production_dynamic_three_leg_continuation(
    conn: sqlite3.Connection,
    *,
    continuation_event_id: str,
    lease_owner: str,
    claim_generation: int,
    desired_trading_plan: Mapping[str, Any],
    transition_plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    capital_snapshot: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
    production_activation: Mapping[str, Any],
    decision_as_of: str,
) -> dict[str, Any]:
    if not conn.in_transaction:
        raise RuntimeError("dynamic_three_leg_continuation_outer_transaction_required")
    try:
        activation = validate_dynamic_three_leg_activation(production_activation)
    except ValueError as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [f"continuation_activation_rejected:{exc}"], "places_orders": False}
    if not _enabled("PM_DYNAMIC_THREE_LEG_PRODUCTION_ENABLED") or not _enabled("PM_DYNAMIC_THREE_LEG_LIVE_SUBMIT_ENABLED"):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["continuation_environment_disabled"], "places_orders": False}
    desired, transition, market = map(_mapping, (desired_trading_plan, transition_plan, market_snapshot))
    event = conn.execute(
        """SELECT posterior_run_id,fact_snapshot_id,slug,event_date,transition_generation,
                  state,lease_owner,claim_generation
             FROM dynamic_three_leg_continuation_events WHERE continuation_event_id=?""",
        (_text(continuation_event_id),),
    ).fetchone()
    blockers = []
    if event is None:
        blockers.append("continuation_event_missing")
    else:
        if _text(event[5]) != "claimed" or _text(event[6]) != _text(lease_owner) or int(event[7]) != int(claim_generation):
            blockers.append("continuation_claim_not_current")
        if _text(event[0]) != _text(desired.get("posterior_run_id")) or _text(event[1]) != _text(desired.get("fact_snapshot_id")):
            blockers.append("continuation_posterior_or_fact_mismatch")
    if desired.get("schema") != "dynamic_three_leg_trading_plan_v1" or transition.get("ok") is not True:
        blockers.append("continuation_plan_invalid")
    if _text(activation.get("strategy_artifact_sha256")) != _text(desired.get("strategy_artifact_sha256")):
        blockers.append("continuation_strategy_artifact_mismatch")
    if _text(transition.get("trading_plan_id")) != _text(desired.get("trading_plan_id")):
        blockers.append("continuation_transition_binding_mismatch")
    latest = conn.execute(
        """SELECT h.posterior_run_id,h.fact_snapshot_id
             FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id
            WHERE l.slug=? AND l.event_date=?""",
        (_text(event[2]) if event else "", _text(event[3]) if event else ""),
    ).fetchone()
    if event is None or latest is None or (_text(latest[0]), _text(latest[1])) != (_text(event[0]), _text(event[1])):
        blockers.append("continuation_posterior_superseded")
    deadline = _text(market.get("expires_at"))
    executable, execution_blockers = _execution_rows(
        transition_plan=transition,
        market_snapshot=market,
        decision_as_of=_text(decision_as_of),
        hard_deadline_at=deadline,
    )
    blockers.extend(execution_blockers)
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers)), "places_orders": False}

    reservation_plan = dict(desired)
    if _text(transition.get("phase")) != "BUY_NEW":
        reservation_plan["candidates"] = []
    reservation = reserve_dynamic_three_leg_risk_budget(
        conn,
        desired_trading_plan=reservation_plan,
        capital_snapshot=capital_snapshot,
        risk_snapshot=risk_snapshot,
        production_activation=activation,
        reservation_as_of=_text(decision_as_of),
        expires_at=deadline,
    )
    if reservation.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": list(reservation.get("block_reasons") or []), "places_orders": False}

    transition_id = _text(transition.get("transition_plan_id"))
    generation = int(event[4])
    decisions, intents = [], []
    for default_sequence, raw in enumerate(executable, start=1):
        row = dict(raw)
        decision_id = lineage_id_from_parts("decision_id", {
            "continuation_event_id": continuation_event_id,
            "transition_plan_id": transition_id,
            "action": row["order_action"],
            "candidate_id": row.get("candidate_id"),
            "position_lot_id": row.get("position_lot_id"),
        })
        order_intent_id = lineage_id_from_parts("order_intent_id", {"decision_id": decision_id, "deadline": deadline})
        if row["order_action"] == "BUY":
            lot_id, version = build_position_lot_identity(
                order_intent_id=order_intent_id, bucket=row["bucket"],
                outcome_side=row["outcome_side"], token_id=row["token_id"],
            )
            row["position_lot_id"], row["position_lot_version"] = lot_id, version
        sequence = int(row.get("execution_sequence") or default_sequence)
        payload = {
            "schema": "dynamic_three_leg_execution_intent_v1",
            "continuation_event_id": _text(continuation_event_id),
            "transition_generation": generation,
            "order_intent_id": order_intent_id,
            "transition_plan_id": transition_id,
            "trading_plan_id": desired.get("trading_plan_id"),
            "posterior_run_id": event[0], "fact_snapshot_id": event[1],
            "realization_state_id": desired.get("realization_state_id"),
            "position_snapshot_id": transition.get("position_snapshot_id"),
            "position_lot_id": row.get("position_lot_id"),
            "position_lot_version": row.get("position_lot_version"),
            "strategy_artifact_sha256": desired.get("strategy_artifact_sha256"),
            "market_snapshot_id": desired.get("market_snapshot_id"),
            "capital_snapshot_id": desired.get("capital_snapshot_id"),
            "risk_snapshot_id": desired.get("risk_snapshot_id"),
            "capital_epoch_id": desired.get("capital_epoch_id"),
            "risk_reservation_id": desired.get("risk_reservation_id"),
            "slug": event[2], "event_date": event[3],
            "bucket": row["bucket"], "outcome_side": row["outcome_side"],
            "token_id": row["token_id"], "side": row["order_action"],
            "price": row["limit_price"], "size": row["size"], "notional": row["notional"],
            "expected_token_balance": row.get("expected_token_balance"),
            "order_type": "GTC", "post_only": False,
            "reason_family": row["reason_family"], "reason_code": row["reason_code"],
            "execution_sequence": sequence,
            "observation_to_order_hard_deadline_at": deadline,
            "quote_expires_at": deadline,
        }
        idem = "dynamic-three-leg-continuation:" + _sha({"order_intent_id": order_intent_id, "terms": payload})
        payload["idempotency_key"] = idem
        decisions.append((decision_id, order_intent_id, row, payload))
        intents.append({
            "order_intent_id": order_intent_id, "decision_id": decision_id,
            "transition_plan_id": transition_id, "trading_plan_id": _text(desired.get("trading_plan_id")),
            "posterior_run_id": _text(event[0]), "realization_state_id": _text(desired.get("realization_state_id")),
            "fact_snapshot_id": _text(event[1]), "position_snapshot_id": _text(transition.get("position_snapshot_id")),
            "position_lot_id": _text(row.get("position_lot_id")), "position_lot_version": _text(row.get("position_lot_version")),
            "strategy_artifact_sha256": _text(desired.get("strategy_artifact_sha256")),
            "market_snapshot_id": _text(desired.get("market_snapshot_id")), "capital_snapshot_id": _text(desired.get("capital_snapshot_id")),
            "risk_snapshot_id": _text(desired.get("risk_snapshot_id")), "capital_epoch_id": _text(desired.get("capital_epoch_id")),
            "risk_reservation_id": _text(desired.get("risk_reservation_id")), "candidate_id": _text(row.get("candidate_id")) or None,
            "bucket": row["bucket"], "outcome_side": row["outcome_side"], "order_action": row["order_action"],
            "token_id": row["token_id"], "limit_price": row["limit_price"], "size": row["size"], "notional": row["notional"],
            "reason_family": row["reason_family"], "reason_code": row["reason_code"], "state": "prepared",
            "live_submit_authorized": 1, "observation_to_order_hard_deadline_at": deadline, "quote_expires_at": deadline,
            "idempotency_key": idem, "intent_json": _json(payload), "intent_sha256": _sha(payload),
            "created_at": _text(decision_as_of), "updated_at": _text(decision_as_of),
        })
    if _text(transition.get("phase")) == "HOLD":
        decision_id = lineage_id_from_parts("decision_id", {"continuation_event_id": continuation_event_id, "action": "HOLD"})
        decisions.append((decision_id, "", {"bucket": "", "outcome_side": "", "order_action": "HOLD", "reason_family": "strategy_state", "reason_code": "dynamic_three_leg_target_allocation_already_satisfied"}, {"state": "hold_no_order"}))

    transition_row = {
        "transition_plan_id": transition_id, "continuation_event_id": _text(continuation_event_id),
        "transition_generation": generation, "trading_plan_id": _text(desired.get("trading_plan_id")),
        "posterior_run_id": _text(event[0]), "realization_state_id": _text(desired.get("realization_state_id")),
        "fact_snapshot_id": _text(event[1]), "position_snapshot_id": _text(transition.get("position_snapshot_id")),
        "strategy_artifact_sha256": _text(desired.get("strategy_artifact_sha256")),
        "market_snapshot_id": _text(desired.get("market_snapshot_id")), "capital_snapshot_id": _text(desired.get("capital_snapshot_id")),
        "risk_snapshot_id": _text(desired.get("risk_snapshot_id")), "capital_epoch_id": _text(desired.get("capital_epoch_id")),
        "risk_reservation_id": _text(desired.get("risk_reservation_id")), "phase": _text(transition.get("phase")),
        "transition_json": _json(transition), "transition_sha256": _sha(transition),
        "state": "completed" if _text(transition.get("phase")) == "HOLD" else "prepared",
        "created_at": _text(decision_as_of), "updated_at": _text(decision_as_of),
    }
    fields = list(transition_row)
    conn.execute(
        f"INSERT INTO dynamic_three_leg_continuation_transition_plans({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(transition_row[field] for field in fields),
    )
    reservation_state = "bound" if _text(transition.get("phase")) == "BUY_NEW" else "released"
    conn.execute(
        """UPDATE dynamic_three_leg_risk_reservations SET state=?,bound_transition_plan_id=?,updated_at=?
             WHERE risk_reservation_id=? AND state='active' AND bound_transition_plan_id IS NULL""",
        (reservation_state, transition_id, _text(decision_as_of), _text(desired.get("risk_reservation_id"))),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise RuntimeError("continuation_risk_reservation_bind_failed")
    for decision_id, order_intent_id, row, payload in decisions:
        will_order = 0 if row["order_action"] == "HOLD" else 1
        conn.execute(
            """INSERT INTO auto_trade_decisions(
               run_id,run_date,slug,event_date,outcome,side,action,decided_state,will_order,
               reason_codes_json,reasons_json,details_json,created_at,correlation_id,
               fact_snapshot_id,posterior_run_id,decision_id,order_intent_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event[0], event[3], event[2], event[3], row["bucket"], row["outcome_side"],
                "hold" if not will_order else "exit" if row["order_action"] == "SELL" else "entry",
                "hold_no_order" if not will_order else "intent_prepared", will_order,
                _json([row["reason_code"]]), _json([{"reason_family": row["reason_family"], "reason_code": row["reason_code"]}]),
                _json(payload), _text(decision_as_of), continuation_event_id, event[1], event[0],
                decision_id, order_intent_id or None,
            ),
        )
    for intent in intents:
        fields = list(intent)
        conn.execute(
            f"INSERT INTO posterior_execution_intents({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            tuple(intent[field] for field in fields),
        )
    conn.execute(
        """UPDATE dynamic_three_leg_continuation_events
              SET state='completed',completed_transition_plan_id=?,lease_owner=NULL,lease_until=NULL,updated_at=?
            WHERE continuation_event_id=? AND state='claimed' AND lease_owner=? AND claim_generation=?""",
        (transition_id, _text(decision_as_of), _text(continuation_event_id), _text(lease_owner), int(claim_generation)),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise RuntimeError("continuation_completion_cas_failed")
    return {
        "schema": SCHEMA_VERSION, "ok": True, "state": "committed",
        "transition_plan_id": transition_id,
        "order_intent_ids": [row["order_intent_id"] for row in intents],
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "commit_production_dynamic_three_leg_continuation"]
