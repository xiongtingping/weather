"""Reconcile-first cancellation and replacement for v6 staged main-YES exits."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping


SCHEMA_VERSION = "production_v6_standing_release_coordinator_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _deadline(value: str) -> str:
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    return (parsed.astimezone(timezone.utc) + timedelta(seconds=3)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _confirm_cancel(
    conn: sqlite3.Connection,
    *,
    request: sqlite3.Row,
    response: Mapping[str, Any],
    reconciled_at: str,
) -> dict[str, Any]:
    plan = conn.execute("SELECT * FROM dynamic_three_leg_v6_standing_exit_plans WHERE standing_exit_plan_id=?", (_text(request["standing_exit_plan_id"]),)).fetchone()
    if plan is None or _text(plan["standing_sell_order_id"]) != _text(request["standing_sell_order_id"]):
        raise RuntimeError("v6_standing_release_plan_or_order_drift")
    original_intent = conn.execute("SELECT * FROM posterior_execution_intents WHERE order_intent_id=?", (_text(plan["standing_sell_intent_id"]),)).fetchone()
    if original_intent is None:
        raise RuntimeError("v6_standing_release_original_intent_missing")
    target = float(plan["target_size"])
    prior_released = float(plan["stage_released_size"])
    release = float(request["requested_release_size"])
    if release <= 0 or prior_released + release > target + 1e-9:
        raise RuntimeError("v6_standing_release_size_invalid")
    next_released = prior_released + release
    residual = max(0.0, target - next_released)
    conn.execute("UPDATE posterior_execution_intents SET state='cancelled_superseded',updated_at=? WHERE order_intent_id=? AND state IN ('gateway_accepted','partially_filled','reconcile_required')", (_text(reconciled_at), _text(original_intent["order_intent_id"])))
    new_intent_id = "v6standint_" + _sha((plan["standing_exit_plan_id"], int(plan["coverage_version"]) + 1, residual))[:24] if residual > 1e-9 else ""
    new_decision_id = "v6standdec_" + _sha((new_intent_id, "replacement"))[:24] if new_intent_id else ""
    if residual > 1e-9:
        payload = json.loads(_text(original_intent["intent_json"]) or "{}")
        payload.update({
            "order_intent_id": new_intent_id,
            "size": residual,
            "notional": residual * 0.999,
            "expected_token_balance": residual + release,
            "replacement_after_coverage_release": True,
            "coverage_version": int(plan["coverage_version"]) + 1,
        })
        deadline = _deadline(reconciled_at)
        values = dict(original_intent)
        values.update({
            "order_intent_id": new_intent_id, "decision_id": new_decision_id,
            "size": residual, "notional": residual * 0.999, "state": "prepared",
            "observation_to_order_hard_deadline_at": deadline, "quote_expires_at": deadline,
            "idempotency_key": "v6-standing-999:" + _sha((new_intent_id, plan["token_id"], residual)),
            "intent_json": _json(payload), "intent_sha256": _sha(payload),
            "gateway_response_json": "{}", "exchange_post_started_at": None,
            "expected_order_id": None, "exchange_order_id": None, "last_error": None,
            "created_at": _text(reconciled_at), "updated_at": _text(reconciled_at),
        })
        fields = [key for key in values if key in {row[1] for row in conn.execute("PRAGMA table_info(posterior_execution_intents)")}]
        conn.execute(f"INSERT INTO posterior_execution_intents({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(values[field] for field in fields))
        conn.execute(
            """INSERT INTO auto_trade_decisions(
                run_id,run_date,slug,event_date,outcome,side,action,decided_state,will_order,
                reason_codes_json,reasons_json,details_json,created_at,correlation_id,fact_snapshot_id,
                posterior_run_id,decision_id,order_intent_id
            ) VALUES(?,?,?,?,?,?,?,'standing_replacement_intent_prepared',1,?,?,?,?,?,?,?,?,?)""",
            (
                original_intent["posterior_run_id"], _text(payload.get("event_date")), _text(payload.get("slug")),
                _text(payload.get("event_date")), plan["bucket"], plan["outcome_side"], "exit",
                _json([plan["exit_reason_code"]]), _json([{"reason_family": "market_alpha_take_profit", "reason_code": plan["exit_reason_code"]}]),
                _json(payload), _text(reconciled_at), "", original_intent["fact_snapshot_id"],
                original_intent["posterior_run_id"], new_decision_id, new_intent_id,
            ),
        )
    next_state = "intent_prepared" if residual > 1e-9 else "released"
    next_version = int(plan["coverage_version"]) + 1
    conn.execute(
        """UPDATE dynamic_three_leg_v6_standing_exit_plans
           SET state=?,covered_size=0,uncovered_size=?,stage_released_size=?,coverage_version=?,
               standing_sell_intent_id=?,standing_sell_order_id=NULL,updated_at=?
           WHERE standing_exit_plan_id=? AND coverage_version=?""",
        (next_state, residual, next_released, next_version, new_intent_id or None, _text(reconciled_at), plan["standing_exit_plan_id"], plan["coverage_version"]),
    )
    event = {"release_request_id": request["standing_exit_release_request_id"], "cancelled_order_id": request["standing_sell_order_id"], "released_size": release, "replacement_size": residual, "response": dict(response)}
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_standing_exit_coverage_events(
            coverage_event_id,standing_exit_plan_id,coverage_version,event_type,covered_size_before,
            covered_size_after,event_json,event_sha256,created_at
        ) VALUES(?,?,?,'cancel_reconciled',?,?,?,?,?)""",
        ("v6coverevt_" + _sha(event)[:24], plan["standing_exit_plan_id"], next_version, plan["covered_size"], 0.0, _json(event), _sha(event), _text(reconciled_at)),
    )
    conn.execute(
        """UPDATE dynamic_three_leg_v6_standing_exit_release_requests
           SET state='confirmed',gateway_response_json=?,updated_at=?
           WHERE standing_exit_release_request_id=? AND state IN ('cancelling','reconcile_required')""",
        (_json(response), _text(reconciled_at), request["standing_exit_release_request_id"]),
    )
    return {"replacement_order_intent_id": new_intent_id, "released_size": release, "replacement_size": residual, "standing_exit_state": next_state}


def process_v6_standing_release_once(
    conn: sqlite3.Connection,
    *,
    action_as_of: str,
    gateway_cancel_order: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    gateway_lookup_exchange_order: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if conn.in_transaction:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_standing_release_open_transaction", "called_gateway": False}
    conn.row_factory = sqlite3.Row
    request = conn.execute(
        "SELECT * FROM dynamic_three_leg_v6_standing_exit_release_requests WHERE state IN ('cancelling','reconcile_required','prepared') ORDER BY CASE state WHEN 'cancelling' THEN 0 WHEN 'reconcile_required' THEN 1 ELSE 2 END,created_at LIMIT 1"
    ).fetchone()
    if request is None:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "idle", "called_gateway": False}
    if _text(request["state"]) == "prepared":
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute("UPDATE dynamic_three_leg_v6_standing_exit_release_requests SET state='cancelling',updated_at=? WHERE standing_exit_release_request_id=? AND state='prepared'", (_text(action_as_of), request["standing_exit_release_request_id"])).rowcount
        if changed != 1:
            conn.rollback()
            raise RuntimeError("v6_standing_release_claim_cas_failed")
        conn.execute("UPDATE dynamic_three_leg_v6_standing_exit_plans SET state='release_pending',updated_at=? WHERE standing_exit_plan_id=? AND state='covered'", (_text(action_as_of), request["standing_exit_plan_id"]))
        conn.commit()
        try:
            response = dict(gateway_cancel_order({"order_id": request["standing_sell_order_id"], "idempotency_key": request["idempotency_key"]}))
        except Exception as exc:
            response = {"ok": False, "reconcile_required": True, "error": str(exc)}
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE dynamic_three_leg_v6_standing_exit_release_requests SET state='reconcile_required',gateway_response_json=?,updated_at=? WHERE standing_exit_release_request_id=? AND state='cancelling'", (_json(response), _text(action_as_of), request["standing_exit_release_request_id"]))
        conn.commit()
        return {"schema": SCHEMA_VERSION, "ok": response.get("ok") is True, "state": "reconcile_required", "called_gateway": True, "cancel_response": response}
    lookup = {"idempotency_key": request["idempotency_key"], "exchange_order_id": request["standing_sell_order_id"], "order_id": request["standing_sell_order_id"]}
    try:
        response = dict(gateway_lookup_exchange_order(lookup))
    except Exception as exc:
        response = {"ok": False, "status": "unknown", "error": str(exc)}
    status = _text(response.get("status")).lower()
    response_order = _text(response.get("orderID") or response.get("orderId") or response.get("exchange_order_id"))
    if status not in {"cancelled", "canceled"} or (response_order and response_order != _text(request["standing_sell_order_id"])):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "reconcile_required", "called_gateway": True, "response": response}
    conn.execute("BEGIN IMMEDIATE")
    try:
        confirmed = _confirm_cancel(conn, request=request, response=response, reconciled_at=action_as_of)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "confirmed", "called_gateway": True, **confirmed}


__all__ = ["SCHEMA_VERSION", "process_v6_standing_release_once"]
