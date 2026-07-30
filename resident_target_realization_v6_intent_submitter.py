"""Generation-bound submit/reconcile lane for Target Realization v6 intents."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from dynamic_three_leg_v6_position_lot_runtime import apply_v6_reconciled_fill
from production_v6_standing_exit_writer import acknowledge_v6_standing_exit_coverage
from target_realization_v6_production_activation import validate_target_realization_v6_activation
from trade_exit_reason_contract import is_v6_executable_exit_reason_code, v6_exit_reason_family_for_code


SCHEMA_VERSION = "resident_target_realization_v6_intent_submitter_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _enabled(name: str) -> bool:
    return _text(os.environ.get(name)).lower() in {"1", "true", "yes", "on"}


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _activation(activation: Mapping[str, Any], artifact: str) -> dict[str, Any] | None:
    try:
        valid = validate_target_realization_v6_activation(activation)
    except ValueError:
        return None
    return valid if valid.get("state") == "enabled" and _text(valid.get("strategy_artifact_sha256")) == artifact else None


def _load(conn: sqlite3.Connection, intent_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM posterior_execution_intents WHERE order_intent_id=?", (_text(intent_id),)).fetchone()


def _latest_bound(conn: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
    return bool(conn.execute(
        """SELECT 1 FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id
            WHERE h.posterior_run_id=? AND h.fact_snapshot_id=?""",
        (_text(row.get("posterior_run_id")), _text(row.get("fact_snapshot_id"))),
    ).fetchone())


def _local_binding(conn: sqlite3.Connection, row: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    action = _text(row.get("order_action")).upper()
    schema = _text(payload.get("schema"))
    if schema == "target_realization_v6_standing_exit_intent_v1":
        standing = conn.execute(
            """SELECT standing_sell_intent_id,position_lot_id,position_lot_version,state
                 FROM dynamic_three_leg_v6_standing_exit_plans WHERE standing_exit_plan_id=?""",
            (_text(payload.get("standing_exit_plan_id")),),
        ).fetchone()
        lot = conn.execute(
            "SELECT position_lot_version,remaining_size,state FROM dynamic_three_leg_v6_position_lots WHERE position_lot_id=?",
            (_text(row.get("position_lot_id")),),
        ).fetchone()
        return bool(
            action == "SELL" and standing is not None and lot is not None
            and _text(standing[0]) == _text(row.get("order_intent_id"))
            and _text(standing[1]) == _text(row.get("position_lot_id"))
            and _text(standing[2]) == _text(row.get("position_lot_version"))
            and _text(standing[3]) == "intent_prepared"
            and _text(lot[0]) == _text(row.get("position_lot_version"))
            and float(lot[1]) + 1e-9 >= float(row.get("size") or 0)
            and _text(lot[2]) in {"coverage_required", "open"}
        )
    if schema == "target_realization_v6_target_sell_intent_v1":
        standing = conn.execute(
            """SELECT position_lot_id,position_lot_version,state,stage_released_size
                 FROM dynamic_three_leg_v6_standing_exit_plans WHERE standing_exit_plan_id=?""",
            (_text(payload.get("standing_exit_plan_id")),),
        ).fetchone()
        lot = conn.execute(
            "SELECT position_lot_version,remaining_size,state FROM dynamic_three_leg_v6_position_lots WHERE position_lot_id=?",
            (_text(row.get("position_lot_id")),),
        ).fetchone()
        commit = conn.execute(
            "SELECT state FROM dynamic_three_leg_v6_transaction_c_commits WHERE v6_trading_plan_id=? AND posterior_run_id=?",
            (_text(row.get("trading_plan_id")), _text(row.get("posterior_run_id"))),
        ).fetchone()
        return bool(
            action == "SELL" and standing is not None and lot is not None and commit is not None
            and _text(commit[0]) == "committed"
            and _text(standing[0]) == _text(row.get("position_lot_id"))
            and _text(standing[1]) == _text(row.get("position_lot_version"))
            and _text(standing[2]) in {"covered", "released"}
            and float(standing[3]) + 1e-9 >= float(row.get("size") or 0)
            and _text(lot[0]) == _text(row.get("position_lot_version"))
            and float(lot[1]) + 1e-9 >= float(row.get("size") or 0)
            and _text(lot[2]) in {"open", "coverage_required", "release_pending"}
        )
    commit = conn.execute(
        "SELECT state FROM dynamic_three_leg_v6_transaction_c_commits WHERE v6_trading_plan_id=? AND posterior_run_id=?",
        (_text(row.get("trading_plan_id")), _text(row.get("posterior_run_id"))),
    ).fetchone()
    reservation = conn.execute(
        "SELECT state,reserved_buy_notional_usdc FROM dynamic_three_leg_v6_risk_reservations WHERE risk_reservation_id=? AND v6_trading_plan_id=?",
        (_text(row.get("risk_reservation_id")), _text(row.get("trading_plan_id"))),
    ).fetchone()
    lot = conn.execute("SELECT 1 FROM dynamic_three_leg_v6_position_lots WHERE position_lot_id=?", (_text(row.get("position_lot_id")),)).fetchone()
    if action == "BUY":
        return bool(commit is not None and _text(commit[0]) == "committed" and reservation is not None and _text(reservation[0]) == "bound" and float(reservation[1]) + 1e-9 >= float(row.get("notional") or 0) and lot is None)
    return False


def submit_prepared_target_realization_v6_intent(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str,
    submit_as_of: str,
    production_activation: Mapping[str, Any],
    pre_submit_validator: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    gateway_place_order: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if not _enabled("PM_TARGET_REALIZATION_V6_PRODUCTION_ENABLED") or not _enabled("PM_TARGET_REALIZATION_V6_LIVE_SUBMIT_ENABLED"):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_submitter_disabled", "called_gateway": False}
    if conn.in_transaction:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_submitter_open_transaction", "called_gateway": False}
    row = _load(conn, order_intent_id)
    if row is None or _text(row["state"]) != "prepared" or int(row["live_submit_authorized"] or 0) != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_intent_not_prepared", "called_gateway": False}
    row_dict = dict(row)
    try:
        payload = json.loads(_text(row["intent_json"]))
    except (TypeError, ValueError):
        payload = {}
    if _activation(production_activation, _text(row["strategy_artifact_sha256"])) is None:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_activation_invalid", "called_gateway": False}
    action, family, code = _text(row["order_action"]).upper(), _text(row["reason_family"]), _text(row["reason_code"])
    if action == "SELL":
        if not is_v6_executable_exit_reason_code(code) or v6_exit_reason_family_for_code(code) != family:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_exit_reason_not_in_exact_closed_set", "called_gateway": False}
    elif action != "BUY" or family != "strategy_leg" or code not in {"main_yes", "second_yes", "trajectory_no", "hard_exclusion_no"}:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_buy_reason_invalid", "called_gateway": False}
    now, deadline, expiry = _parse(submit_as_of), _parse(row["observation_to_order_hard_deadline_at"]), _parse(row["quote_expires_at"])
    if now is None or deadline is None or expiry is None or now > deadline or now > expiry:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE posterior_execution_intents SET state='expired',last_error='target_realization_v6_submit_deadline_expired',updated_at=? WHERE order_intent_id=? AND state='prepared'", (_text(submit_as_of), _text(order_intent_id)))
        conn.execute("UPDATE auto_trade_decisions SET decided_state='expired',will_order=0 WHERE decision_id=?", (_text(row["decision_id"]),))
        conn.commit()
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "expired", "called_gateway": False}
    if not _latest_bound(conn, row_dict) or not _local_binding(conn, row_dict, payload):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_ssot_or_local_binding_stale", "called_gateway": False}
    validation = dict(pre_submit_validator(row_dict))
    if validation.get("ok") is not True or any(validation.get(flag) is not True for flag in ("position_lot_current", "market_quote_current", "balance_sufficient")):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_pre_submit_validation_failed", "validation": validation, "called_gateway": False}
    conn.execute("BEGIN IMMEDIATE")
    changed = conn.execute("UPDATE posterior_execution_intents SET state='submitting',updated_at=? WHERE order_intent_id=? AND state='prepared'", (_text(submit_as_of), _text(order_intent_id))).rowcount
    if changed != 1:
        conn.rollback()
        raise RuntimeError("target_realization_v6_submit_claim_cas_failed")
    conn.execute("UPDATE auto_trade_decisions SET decided_state='submitting' WHERE decision_id=?", (_text(row["decision_id"]),))
    conn.commit()
    try:
        response = dict(gateway_place_order(payload))
    except Exception as exc:
        response = {"ok": False, "reconcile_required": True, "error": "v6_gateway_exception", "detail": str(exc)}
    exchange_order_id = _text(response.get("orderID") or response.get("orderId"))
    if response.get("ok") is True and (exchange_order_id or response.get("accepted") is True):
        state, ok = "gateway_accepted", True
    elif response.get("reconcile_required") is True or response.get("pending") is True:
        state, ok = "reconcile_required", False
    else:
        state, ok = "failed", False
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """UPDATE posterior_execution_intents SET state=?,gateway_response_json=?,exchange_order_id=?,
                      expected_order_id=?,last_error=?,updated_at=? WHERE order_intent_id=? AND state='submitting'""",
            (state, _json(response), exchange_order_id or None, _text(response.get("expected_order_id")) or None, _text(response.get("error")) or None, _text(submit_as_of), _text(order_intent_id)),
        )
        conn.execute("UPDATE auto_trade_decisions SET decided_state=?,will_order=? WHERE decision_id=?", (state, 1 if state in {"gateway_accepted", "reconcile_required"} else 0, _text(row["decision_id"])))
        if state == "gateway_accepted" and payload.get("standing_exit_coverage_order") is True and exchange_order_id:
            ack = acknowledge_v6_standing_exit_coverage(
                conn, standing_exit_plan_id=_text(payload.get("standing_exit_plan_id")),
                order_intent_id=_text(order_intent_id), exchange_order_id=exchange_order_id,
                acknowledged_at=_text(submit_as_of),
            )
            if ack.get("ok") is not True:
                raise RuntimeError("target_realization_v6_standing_ack_not_committed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"schema": SCHEMA_VERSION, "ok": ok, "state": state, "called_gateway": True, "response": response}


def reconcile_target_realization_v6_intent(
    conn: sqlite3.Connection,
    *,
    order_intent_id: str,
    reconcile_as_of: str,
    gateway_lookup_order: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if conn.in_transaction:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_reconcile_open_transaction", "called_gateway": False}
    row = _load(conn, order_intent_id)
    if row is None or _text(row["state"]) not in {"submitting", "reconcile_required", "gateway_accepted", "partially_filled"}:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "target_realization_v6_intent_not_reconcilable", "called_gateway": False}
    lookup = {"order_intent_id": _text(row["order_intent_id"]), "idempotency_key": _text(row["idempotency_key"]), "expected_order_id": _text(row["expected_order_id"]), "exchange_order_id": _text(row["exchange_order_id"]), "token_id": _text(row["token_id"])}
    try:
        response = dict(gateway_lookup_order(lookup))
    except Exception as exc:
        response = {"ok": False, "status": "unknown", "error": str(exc)}
    status = _text(response.get("status")).lower()
    response_key = _text(response.get("idempotency_key"))
    order_id = _text(response.get("orderID") or response.get("orderId") or response.get("exchange_order_id"))
    known = {_text(row["expected_order_id"]), _text(row["exchange_order_id"])} - {""}
    identity_ok = bool((response_key and response_key == _text(row["idempotency_key"])) or (order_id and order_id in known))
    if not identity_ok:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "reconcile_required", "reason": "target_realization_v6_reconcile_identity_unproven", "called_gateway": True}
    filled = response.get("size_matched", response.get("matched_size", response.get("filled_size", 0)))
    try:
        cumulative = float(filled or 0.0)
    except (TypeError, ValueError):
        cumulative = 0.0
    original = float(row["size"])
    try:
        payload = json.loads(_text(row["intent_json"]))
    except (TypeError, ValueError):
        payload = {}
    is_resting_standing = payload.get("standing_exit_coverage_order") is True
    terminal = (
        "filled" if status == "filled" or cumulative >= original - 1e-9
        else "gateway_accepted" if cumulative > 0 and is_resting_standing
        else "partially_filled" if cumulative > 0
        else "gateway_accepted" if status in {"open", "live", "accepted"}
        else "reconcile_required"
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        fill_result = None
        if cumulative > 0:
            price = float(response.get("average_price") or response.get("price") or row["limit_price"])
            fill_result = apply_v6_reconciled_fill(
                conn, intent=dict(row), cumulative_filled_size=cumulative, fill_price=price,
                fill_identity="v6fill:" + (order_id or _text(row["idempotency_key"])) + f":{cumulative:.12f}",
                token_tick_size=payload.get("token_tick_size") or response.get("tick_size"),
                applied_at=_text(reconcile_as_of),
            )
            if fill_result.get("ok") is not True:
                raise RuntimeError("target_realization_v6_fill_application_blocked")
        if terminal == "gateway_accepted":
            if payload.get("standing_exit_coverage_order") is True and order_id:
                ack = acknowledge_v6_standing_exit_coverage(
                    conn, standing_exit_plan_id=_text(payload.get("standing_exit_plan_id")),
                    order_intent_id=_text(order_intent_id), exchange_order_id=order_id,
                    acknowledged_at=_text(reconcile_as_of),
                )
                if ack.get("ok") is not True:
                    raise RuntimeError("target_realization_v6_reconciled_standing_ack_blocked")
        conn.execute("UPDATE posterior_execution_intents SET state=?,gateway_response_json=?,exchange_order_id=?,updated_at=? WHERE order_intent_id=?", (terminal, _json(response), order_id or None, _text(reconcile_as_of), _text(order_intent_id)))
        conn.execute("UPDATE auto_trade_decisions SET decided_state=?,will_order=1 WHERE decision_id=?", (terminal, _text(row["decision_id"])))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"schema": SCHEMA_VERSION, "ok": terminal in {"gateway_accepted", "partially_filled", "filled"}, "state": terminal, "fill_result": fill_result, "called_gateway": True, "submitted_order": False}


__all__ = ["SCHEMA_VERSION", "reconcile_target_realization_v6_intent", "submit_prepared_target_realization_v6_intent"]
