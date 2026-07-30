"""Fresh-quote continuation after standing coverage release is reconciled."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from dynamic_three_leg_clob_gateway_adapter import evaluate_dynamic_three_leg_exchange_snapshot
from dynamic_three_leg_v6_execution_materializer import materialize_target_realization_v6_execution
from production_dynamic_three_leg_input_provider import _market_token_rows


SCHEMA_VERSION = "production_v6_target_sell_continuation_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dt(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def process_v6_target_sell_continuation_once(
    conn: sqlite3.Connection,
    *,
    action_as_of: str,
    exchange_snapshot_fetcher: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> dict[str, Any]:
    if conn.in_transaction:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_target_continuation_open_transaction", "called_gateway": False}
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT r.standing_exit_release_request_id,r.v6_trading_plan_id,r.main_yes_position_cycle_id,
                  r.target_realization_reason_code,
                  p.plan_json,p.posterior_run_id,s.standing_exit_plan_id,s.state AS standing_state,
                  s.covered_size,s.stage_released_size,s.position_lot_id,s.position_lot_version,
                  l.slug,l.event_date,l.bucket,l.outcome_side,l.token_id,l.entry_strategy_leg,
                  l.acquired_size,l.remaining_size,l.remaining_cost_usdc,l.main_yes_position_cycle_id
                  ,c.target_sell_continuation_id AS existing_continuation_id
                  ,c.order_intent_id AS existing_continuation_intent_id
                  ,ci.state AS existing_continuation_intent_state
             FROM dynamic_three_leg_v6_standing_exit_release_requests r
             JOIN target_realization_v6_plans p ON p.v6_trading_plan_id=r.v6_trading_plan_id
             JOIN dynamic_three_leg_v6_standing_exit_plans s ON s.standing_exit_plan_id=r.standing_exit_plan_id
             JOIN dynamic_three_leg_v6_position_lots l ON l.position_lot_id=s.position_lot_id
             LEFT JOIN dynamic_three_leg_v6_target_sell_continuations c
               ON c.standing_exit_release_request_id=r.standing_exit_release_request_id
             LEFT JOIN posterior_execution_intents ci ON ci.order_intent_id=c.order_intent_id
            WHERE r.state='confirmed'
              AND s.state IN ('covered','released')
              AND s.stage_released_size + 0.000000001 >= r.requested_release_size
              AND (
                    c.target_sell_continuation_id IS NULL
                    OR (
                        c.state='prepared'
                        AND ci.state IN ('expired','failed','cancelled_superseded')
                    )
              )
            ORDER BY r.updated_at,r.standing_exit_release_request_id LIMIT 1"""
    ).fetchone()
    if row is None:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "idle", "called_gateway": False}
    latest = conn.execute(
        """SELECT h.fact_snapshot_id FROM event_fact_latest x JOIN event_fact_history h ON h.id=x.fact_id
            WHERE x.slug=? AND x.event_date=? AND h.posterior_run_id=?""",
        (row["slug"], row["event_date"], row["posterior_run_id"]),
    ).fetchone()
    if latest is None:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_target_continuation_posterior_not_latest", "called_gateway": False}
    token_rows, blockers = _market_token_rows(conn, slug=row["slug"], event_date=row["event_date"], buckets=[row["bucket"]])
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": blockers, "called_gateway": False}
    request_core = {"slug": row["slug"], "event_date": row["event_date"], "posterior_run_id": row["posterior_run_id"], "buckets": token_rows, "position_token_ids": [row["token_id"]]}
    request = {**request_core, "request_id": "v6targetquote_" + _sha(request_core)[:24]}
    try:
        raw = dict(exchange_snapshot_fetcher(request))
    except Exception as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_target_continuation_exchange_exception", "exception_type": type(exc).__name__, "called_gateway": True}
    observed_at = _text(raw.get("observed_at"))
    exchange = evaluate_dynamic_three_leg_exchange_snapshot(request, raw, evaluated_at=observed_at, max_snapshot_age_ms=1_000)
    observed = _dt(exchange.get("as_of"))
    if exchange.get("ok") is not True or observed is None:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": exchange.get("block_reasons") or ["v6_target_continuation_exchange_invalid"], "called_gateway": True}
    deadline = _iso(observed + timedelta(seconds=3))
    market = {"market_snapshot_id": "v6targetmarket_" + _sha((request["request_id"], exchange.get("buckets"), observed_at))[:24], "as_of": observed_at, "expires_at": _iso(observed + timedelta(milliseconds=900)), "buckets": exchange.get("buckets")}
    plan = json.loads(_text(row["plan_json"]))
    # A continuation owns only the already-approved target SELL.  It must not
    # replay any BUY candidate from the original event plan while refreshing
    # the target quote after standing-coverage reconciliation.
    plan["candidates"] = []
    plan["off_role_main_yes_exits"] = []
    if isinstance(plan.get("capital_allocation"), dict):
        plan["capital_allocation"] = {**plan["capital_allocation"], "allocations": []}
    if _text(row["target_realization_reason_code"]) == "target_realization_main_yes_invalidated":
        plan["main_yes_bucket"] = _text(row["bucket"])
        plan["main_yes_position_cycle_id"] = _text(row["main_yes_position_cycle_id"])
        plan["main_yes_cycle"] = {"sell_stage_high_watermark": 0, "requested_sell_stage": 4}
        plan["target_realization_exit"] = {
            "exit_reason_family": "target_realization",
            "exit_reason_code": "target_realization_main_yes_invalidated",
            "main_yes_position_cycle_id": _text(row["main_yes_position_cycle_id"]),
            "stage_target_ratio": 1.0,
            "release_standing_coverage_first": True,
        }
    exposure = {
        "position_lot_id": row["position_lot_id"], "position_lot_version": _text(row["position_lot_version"]),
        "strategy_leg": row["entry_strategy_leg"], "bucket": row["bucket"], "side": row["outcome_side"],
        "token_id": row["token_id"], "acquired_size": float(row["acquired_size"]),
        "position_size": float(row["remaining_size"]), "notional_usdc": float(row["remaining_cost_usdc"]),
        "main_yes_position_cycle_id": row["main_yes_position_cycle_id"],
    }
    coverage = {"rows": [{"standing_exit_plan_id": row["standing_exit_plan_id"], "position_lot_id": row["position_lot_id"], "position_lot_version": _text(row["position_lot_version"]), "state": row["standing_state"], "covered_size": float(row["covered_size"]), "stage_released_size": float(row["stage_released_size"])}]}
    materialized = materialize_target_realization_v6_execution(
        plan=plan, market_snapshot=market, position_snapshot_id="v6continuation_" + _sha((row["position_lot_id"], observed_at))[:24],
        fact_snapshot_id=_text(latest[0]), slug=row["slug"], event_date=row["event_date"],
        correlation_id="v6-target-continuation:" + row["standing_exit_release_request_id"],
        observation_to_order_hard_deadline_at=deadline, decision_as_of=observed_at,
        position_exposures=[exposure], coverage_snapshot=coverage,
    )
    target_intents = [intent for intent in materialized.get("intents") or [] if intent.get("order_action") == "SELL" and intent.get("reason_family") == "target_realization"]
    if len(target_intents) != 1:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_target_continuation_exact_sell_not_materialized", "materialized": materialized, "called_gateway": True}
    intent = target_intents[0]
    if _text(intent.get("reason_code")) != _text(row["target_realization_reason_code"]):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "v6_target_continuation_reason_binding_drift", "called_gateway": True}
    decision = next(item for item in materialized["decisions"] if _text(item.get("order_intent_id")) == _text(intent.get("order_intent_id")))
    decision_id = "v6targetcontdec_" + _sha((row["standing_exit_release_request_id"], market["market_snapshot_id"], intent["order_intent_id"]))[:24]
    intent["decision_id"] = decision_id
    decision["decision_id"] = decision_id
    continuation = {"standing_exit_release_request_id": row["standing_exit_release_request_id"], "v6_trading_plan_id": row["v6_trading_plan_id"], "market_snapshot_id": market["market_snapshot_id"], "order_intent_id": intent["order_intent_id"], "reason_code": intent["reason_code"]}
    continuation_id = "v6targetcont_" + _sha(continuation)[:24]
    conn.execute("BEGIN IMMEDIATE")
    try:
        fields = list(intent)
        conn.execute(f"INSERT INTO posterior_execution_intents({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", tuple(intent[field] for field in fields))
        conn.execute(
            """INSERT INTO auto_trade_decisions(
                run_id,run_date,slug,event_date,outcome,side,action,decided_state,will_order,
                reason_codes_json,reasons_json,details_json,created_at,correlation_id,fact_snapshot_id,
                posterior_run_id,decision_id,order_intent_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["posterior_run_id"], row["event_date"], row["slug"], row["event_date"], decision["outcome"], decision["side"], decision["action"], decision["decided_state"], 1, _json(decision["reason_codes"]), _json(decision["reasons"]), _json(decision["details"]), observed_at, continuation["standing_exit_release_request_id"], latest[0], row["posterior_run_id"], decision["decision_id"], intent["order_intent_id"]),
        )
        if _text(row["existing_continuation_id"]):
            conn.execute(
                """UPDATE dynamic_three_leg_v6_target_sell_continuations
                   SET order_intent_id=?,market_snapshot_id=?,continuation_json=?,continuation_sha256=?,updated_at=?
                   WHERE target_sell_continuation_id=? AND state='prepared' AND order_intent_id=?""",
                (intent["order_intent_id"], market["market_snapshot_id"], _json(continuation), _sha(continuation), observed_at, row["existing_continuation_id"], row["existing_continuation_intent_id"]),
            )
            continuation_id = _text(row["existing_continuation_id"])
        else:
            conn.execute(
                """INSERT INTO dynamic_three_leg_v6_target_sell_continuations(
                    target_sell_continuation_id,standing_exit_release_request_id,v6_trading_plan_id,
                    posterior_run_id,order_intent_id,market_snapshot_id,state,continuation_json,
                    continuation_sha256,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'prepared',?,?,?,?)""",
                (continuation_id, row["standing_exit_release_request_id"], row["v6_trading_plan_id"], row["posterior_run_id"], intent["order_intent_id"], market["market_snapshot_id"], _json(continuation), _sha(continuation), observed_at, observed_at),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "target_sell_intent_prepared", "target_sell_continuation_id": continuation_id, "order_intent_id": intent["order_intent_id"], "called_gateway": True, "places_orders": False}


__all__ = ["SCHEMA_VERSION", "process_v6_target_sell_continuation_once"]
