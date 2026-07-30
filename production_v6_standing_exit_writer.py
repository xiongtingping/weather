"""Atomic BUY-fill to Target Realization v6 standing-coverage intent writer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from standing_exit_999_runtime import build_standing_exit_plan
from trade_exit_reason_contract import (
    MARKET_ALPHA_TAKE_PROFIT_FAMILY,
    is_v6_executable_exit_reason_code,
    v6_exit_reason_family_for_code,
)


SCHEMA_VERSION = "production_v6_standing_exit_writer_v1"


class V6StandingExitIntegrityError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _coverage_deadline(value: str) -> str:
    parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("standing_exit_created_at_timezone_missing")
    return (parsed.astimezone(timezone.utc) + timedelta(seconds=3)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def commit_v6_buy_fill_standing_exit(
    conn: sqlite3.Connection,
    *,
    fill: Mapping[str, Any],
    strategy_artifact_sha256: str,
    tick_size: Any,
    created_at: str,
    fault_after: str = "",
) -> dict[str, Any]:
    """Persist one fill identity and its unique 0.999 GTC SELL intent atomically."""

    plan = build_standing_exit_plan(
        fill=fill,
        strategy_artifact_sha256=strategy_artifact_sha256,
        tick_size=tick_size,
    )
    if plan.get("ok") is not True:
        return {**plan, "wrote_rows": False}
    required_fill = (
        "trading_plan_id", "fact_snapshot_id", "position_snapshot_id", "market_snapshot_id",
        "capital_snapshot_id", "risk_snapshot_id", "capital_epoch_id", "risk_reservation_id",
        "quote_expires_at", "observation_to_order_hard_deadline_at",
    )
    missing = [field for field in required_fill if not _text(fill.get(field))]
    if missing:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["standing_exit_fill_execution_lineage_missing:" + ",".join(missing)],
            "wrote_rows": False,
            "places_orders": False,
        }
    reason_code = _text(plan.get("exit_reason_code"))
    if (
        not is_v6_executable_exit_reason_code(reason_code)
        or v6_exit_reason_family_for_code(reason_code) != MARKET_ALPHA_TAKE_PROFIT_FAMILY
    ):
        raise V6StandingExitIntegrityError("standing_exit_reason_outside_v6_exact_closed_set")
    existing = conn.execute(
        "SELECT standing_exit_plan_id,plan_sha256 FROM dynamic_three_leg_v6_standing_exit_plans WHERE fill_identity=?",
        (_text(plan.get("fill_identity")),),
    ).fetchone()
    plan_sha = _sha(plan)
    if existing is not None:
        if _text(existing[0]) != _text(plan.get("standing_exit_plan_id")) or _text(existing[1]) != plan_sha:
            raise V6StandingExitIntegrityError("standing_exit_fill_identity_replay_drift")
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "already_prepared", "idempotent": True, "standing_exit_plan_id": existing[0], "places_orders": False}

    decision_id = "v6standdec_" + _sha((plan.get("standing_exit_plan_id"), "decision"))[:24]
    intent_id = "v6standint_" + _sha((plan.get("standing_exit_plan_id"), "intent"))[:24]
    event_id = "v6coverevt_" + _sha((plan.get("standing_exit_plan_id"), 1, "fill_observed"))[:24]
    idempotency_key = "v6-standing-999:" + _sha((intent_id, plan.get("token_id"), plan.get("target_size")))
    payload = {
        "schema": "target_realization_v6_standing_exit_intent_v1",
        "order_intent_id": intent_id,
        "standing_exit_plan_id": plan["standing_exit_plan_id"],
        "standing_exit_coverage_order": True,
        "posterior_run_id": plan["posterior_run_id"],
        "realization_state_id": plan["realization_state_id"],
        "position_lot_id": plan["position_lot_id"],
        "position_lot_version": plan["position_lot_version"],
        "strategy_leg": plan["strategy_leg"],
        "bucket": plan["bucket"],
        "outcome_side": plan["side"],
        "token_id": plan["token_id"],
        "side": "SELL",
        "price": 0.999,
        "size": plan["target_size"],
        "notional": plan["target_size"] * 0.999,
        "order_type": "GTC",
        "post_only": False,
        "reason_family": MARKET_ALPHA_TAKE_PROFIT_FAMILY,
        "reason_code": reason_code,
        "expected_token_balance": plan["target_size"],
        "fill_identity": plan["fill_identity"],
    }
    coverage_deadline = _coverage_deadline(created_at)
    intent = {
        "order_intent_id": intent_id,
        "decision_id": decision_id,
        "transition_plan_id": plan["standing_exit_plan_id"],
        "trading_plan_id": _text(fill.get("trading_plan_id")),
        "posterior_run_id": plan["posterior_run_id"],
        "realization_state_id": plan["realization_state_id"],
        "fact_snapshot_id": _text(fill.get("fact_snapshot_id")),
        "position_snapshot_id": _text(fill.get("position_snapshot_id")),
        "position_lot_id": plan["position_lot_id"],
        "position_lot_version": plan["position_lot_version"],
        "strategy_artifact_sha256": _text(strategy_artifact_sha256),
        "market_snapshot_id": _text(fill.get("market_snapshot_id")),
        "capital_snapshot_id": _text(fill.get("capital_snapshot_id")),
        "risk_snapshot_id": _text(fill.get("risk_snapshot_id")),
        "capital_epoch_id": _text(fill.get("capital_epoch_id")),
        "risk_reservation_id": _text(fill.get("risk_reservation_id")),
        "candidate_id": None,
        "bucket": plan["bucket"],
        "outcome_side": plan["side"],
        "order_action": "SELL",
        "token_id": plan["token_id"],
        "limit_price": 0.999,
        "size": float(plan["target_size"]),
        "notional": float(plan["target_size"]) * 0.999,
        "reason_family": MARKET_ALPHA_TAKE_PROFIT_FAMILY,
        "reason_code": reason_code,
        "state": "prepared",
        "live_submit_authorized": 1,
        "observation_to_order_hard_deadline_at": coverage_deadline,
        "quote_expires_at": coverage_deadline,
        "idempotency_key": idempotency_key,
        "intent_json": _json(payload),
        "intent_sha256": _sha(payload),
        "created_at": _text(created_at),
        "updated_at": _text(created_at),
    }
    conn.execute("SAVEPOINT v6_standing_exit_fill")
    try:
        conn.execute(
            """INSERT INTO dynamic_three_leg_v6_standing_exit_plans(
                standing_exit_plan_id,strategy_artifact_sha256,posterior_run_id,realization_state_id,
                strategy_leg,position_lot_id,position_lot_version,token_id,bucket,outcome_side,buy_intent_id,
                fill_identity,target_size,covered_size,uncovered_size,stage_released_size,coverage_version,
                standing_sell_intent_id,standing_sell_order_id,exit_reason_code,state,plan_json,plan_sha256,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,'intent_prepared',?,?,?,?)""",
            (
                plan["standing_exit_plan_id"], _text(strategy_artifact_sha256), plan["posterior_run_id"],
                plan["realization_state_id"], plan["strategy_leg"], plan["position_lot_id"],
                plan["position_lot_version"], plan["token_id"], plan["bucket"], plan["side"],
                plan["buy_intent_id"], plan["fill_identity"], plan["target_size"], 0.0, plan["target_size"],
                0.0, 1, intent_id, reason_code, _json(plan), plan_sha, _text(created_at), _text(created_at),
            ),
        )
        conn.execute(
            """INSERT INTO dynamic_three_leg_v6_standing_exit_coverage_events(
                coverage_event_id,standing_exit_plan_id,coverage_version,event_type,covered_size_before,
                covered_size_after,event_json,event_sha256,created_at
            ) VALUES(?,?,1,'fill_observed',0,0,?,?,?)""",
            (event_id, plan["standing_exit_plan_id"], _json({"fill_identity": plan["fill_identity"], "target_size": plan["target_size"]}), _sha({"fill_identity": plan["fill_identity"], "target_size": plan["target_size"]}), _text(created_at)),
        )
        if fault_after == "coverage":
            raise RuntimeError("v6_standing_exit_fault_after_coverage")
        fields = list(intent)
        conn.execute(
            f"INSERT INTO posterior_execution_intents({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            tuple(intent[field] for field in fields),
        )
        if fault_after == "intent":
            raise RuntimeError("v6_standing_exit_fault_after_intent")
        conn.execute(
            """INSERT INTO auto_trade_decisions(
                run_id,run_date,slug,event_date,outcome,side,action,decided_state,will_order,
                reason_codes_json,reasons_json,details_json,created_at,correlation_id,fact_snapshot_id,
                posterior_run_id,decision_id,order_intent_id
            ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)""",
            (
                plan["posterior_run_id"], _text(fill.get("run_date")), _text(fill.get("slug")),
                _text(fill.get("event_date")), plan["bucket"], plan["side"], "exit", "standing_coverage_intent_prepared",
                _json([reason_code]), _json([{"reason_family": MARKET_ALPHA_TAKE_PROFIT_FAMILY, "reason_code": reason_code}]),
                _json(payload), _text(created_at), _text(fill.get("correlation_id")), _text(fill.get("fact_snapshot_id")),
                plan["posterior_run_id"], decision_id, intent_id,
            ),
        )
        if fault_after == "decision":
            raise RuntimeError("v6_standing_exit_fault_after_decision")
    except Exception:
        conn.execute("ROLLBACK TO v6_standing_exit_fill")
        conn.execute("RELEASE v6_standing_exit_fill")
        raise
    conn.execute("RELEASE v6_standing_exit_fill")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "standing_exit_intent_prepared",
        "standing_exit_plan_id": plan["standing_exit_plan_id"],
        "order_intent_id": intent_id,
        "decision_id": decision_id,
        "idempotent": False,
        "places_orders": False,
    }


def acknowledge_v6_standing_exit_coverage(
    conn: sqlite3.Connection,
    *,
    standing_exit_plan_id: str,
    order_intent_id: str,
    exchange_order_id: str,
    acknowledged_at: str,
) -> dict[str, Any]:
    """Mark exact remaining size covered only after a bound exchange ACK."""

    row = conn.execute(
        """SELECT standing_sell_intent_id,state,target_size,covered_size,uncovered_size,
                  stage_released_size,coverage_version,standing_sell_order_id
             FROM dynamic_three_leg_v6_standing_exit_plans WHERE standing_exit_plan_id=?""",
        (_text(standing_exit_plan_id),),
    ).fetchone()
    if row is None:
        return {"schema": "v6_standing_exit_ack_v1", "ok": False, "state": "blocked", "reason": "standing_exit_plan_missing"}
    if _text(row[0]) != _text(order_intent_id) or not _text(exchange_order_id):
        return {"schema": "v6_standing_exit_ack_v1", "ok": False, "state": "blocked", "reason": "standing_exit_ack_identity_mismatch"}
    if _text(row[1]) == "covered":
        if _text(row[7]) != _text(exchange_order_id):
            raise V6StandingExitIntegrityError("standing_exit_ack_replay_drift")
        return {"schema": "v6_standing_exit_ack_v1", "ok": True, "state": "already_covered", "idempotent": True}
    if _text(row[1]) != "intent_prepared":
        return {"schema": "v6_standing_exit_ack_v1", "ok": False, "state": "blocked", "reason": "standing_exit_ack_state_invalid"}
    version = int(row[6])
    target = max(0.0, float(row[2]) - float(row[5]))
    next_state = "released" if float(row[5]) > 1e-9 else "covered"
    event = {"standing_exit_plan_id": _text(standing_exit_plan_id), "coverage_version": version + 1, "exchange_order_id": _text(exchange_order_id), "covered_size": target, "state": next_state}
    conn.execute(
        """UPDATE dynamic_three_leg_v6_standing_exit_plans
           SET state=?,covered_size=?,uncovered_size=0,coverage_version=?,standing_sell_order_id=?,updated_at=?
           WHERE standing_exit_plan_id=? AND state='intent_prepared' AND coverage_version=?""",
        (next_state, target, version + 1, _text(exchange_order_id), _text(acknowledged_at), _text(standing_exit_plan_id), version),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise V6StandingExitIntegrityError("standing_exit_ack_cas_failed")
    conn.execute(
        """INSERT INTO dynamic_three_leg_v6_standing_exit_coverage_events(
            coverage_event_id,standing_exit_plan_id,coverage_version,event_type,covered_size_before,
            covered_size_after,event_json,event_sha256,created_at
        ) VALUES(?,?,?,'sell_acknowledged',?,?,?,?,?)""",
        ("v6coverevt_" + _sha(event)[:24], _text(standing_exit_plan_id), version + 1, float(row[3]), target, _json(event), _sha(event), _text(acknowledged_at)),
    )
    return {"schema": "v6_standing_exit_ack_v1", "ok": True, "state": next_state, "standing_exit_plan_id": _text(standing_exit_plan_id), "covered_size": target, "coverage_version": version + 1}


__all__ = ["SCHEMA_VERSION", "V6StandingExitIntegrityError", "acknowledge_v6_standing_exit_coverage", "commit_v6_buy_fill_standing_exit"]
