"""Target Realization v6 lot accounting with atomic BUY-fill coverage creation."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from typing import Any, Mapping

from production_v6_standing_exit_writer import commit_v6_buy_fill_standing_exit
from trade_exit_reason_contract import is_v6_executable_exit_reason_code


SCHEMA_VERSION = "dynamic_three_leg_v6_position_lot_runtime_v1"
V6_LEGS = {"main_yes", "second_yes", "trajectory_no", "hard_exclusion_no"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _target_stage_fully_reconciled(
    conn: sqlite3.Connection,
    *,
    intent: Mapping[str, Any],
    payload: Mapping[str, Any],
    current_lot_id: str,
    current_intent_fully_filled: bool,
) -> bool:
    """Require every lot in one plan/cycle/stage to complete before watermark advance."""

    plan_id = _text(intent.get("trading_plan_id"))
    cycle_id = _text(payload.get("main_yes_position_cycle_id"))
    reason_code = _text(intent.get("reason_code"))
    if not plan_id or not cycle_id or not reason_code:
        return False
    expected = {
        _text(row[0])
        for row in conn.execute(
            """SELECT position_lot_id FROM posterior_execution_intents
               WHERE trading_plan_id=? AND reason_family='target_realization'
                 AND reason_code=? AND order_action='SELL'
                 AND json_extract(intent_json,'$.main_yes_position_cycle_id')=?
                 AND position_lot_id IS NOT NULL""",
            (plan_id, reason_code, cycle_id),
        )
        if _text(row[0])
    }
    expected.update({
        _text(row[0])
        for row in conn.execute(
            """SELECT s.position_lot_id
                 FROM dynamic_three_leg_v6_standing_exit_release_requests r
                 JOIN dynamic_three_leg_v6_standing_exit_plans s
                   ON s.standing_exit_plan_id=r.standing_exit_plan_id
                WHERE r.v6_trading_plan_id=? AND r.main_yes_position_cycle_id=?
                  AND r.target_realization_reason_code=?""",
            (plan_id, cycle_id, reason_code),
        )
        if _text(row[0])
    })
    expected.add(_text(current_lot_id))
    completed = {
        _text(row[0])
        for row in conn.execute(
            """SELECT position_lot_id FROM posterior_execution_intents
               WHERE trading_plan_id=? AND reason_family='target_realization'
                 AND reason_code=? AND order_action='SELL' AND state='filled'
                 AND json_extract(intent_json,'$.main_yes_position_cycle_id')=?
                 AND position_lot_id IS NOT NULL""",
            (plan_id, reason_code, cycle_id),
        )
        if _text(row[0])
    }
    if current_intent_fully_filled:
        completed.add(_text(current_lot_id))
    return bool(expected) and expected <= completed


def apply_v6_reconciled_fill(
    conn: sqlite3.Connection,
    *,
    intent: Mapping[str, Any],
    cumulative_filled_size: float,
    fill_price: float,
    fill_identity: str,
    token_tick_size: Any,
    applied_at: str,
    fault_after: str = "",
) -> dict[str, Any]:
    """Apply only a new cumulative fill delta; BUY also creates 0.999 coverage."""

    row = dict(intent)
    intent_id = _text(row.get("order_intent_id"))
    action = _text(row.get("order_action")).upper()
    lot_id = _text(row.get("position_lot_id"))
    cumulative = _finite(cumulative_filled_size)
    price = _finite(fill_price)
    limit_price = _finite(row.get("limit_price"))
    blockers: list[str] = []
    if not all((intent_id, lot_id, _text(fill_identity), _text(applied_at))):
        blockers.append("v6_position_lot_fill_identity_missing")
    if action not in {"BUY", "SELL"} or cumulative is None or cumulative <= 0:
        blockers.append("v6_position_lot_fill_terms_invalid")
    price_violates_limit = bool(
        price is None
        or limit_price is None
        or (action == "BUY" and price > limit_price + 1e-9)
        or (action == "SELL" and price + 1e-9 < limit_price)
    )
    if price_violates_limit:
        blockers.append("v6_position_lot_accounting_price_mismatch")
    try:
        expected_version = int(row.get("position_lot_version"))
    except (TypeError, ValueError):
        expected_version = 0
        blockers.append("v6_position_lot_version_invalid")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers))}

    previous = conn.execute(
        "SELECT cumulative_filled_size,position_lot_version_after FROM dynamic_three_leg_v6_position_lot_events WHERE order_intent_id=? ORDER BY cumulative_filled_size DESC LIMIT 1",
        (intent_id,),
    ).fetchone()
    previous_cumulative = float(previous[0]) if previous else 0.0
    if cumulative <= previous_cumulative + 1e-9:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "already_applied", "idempotent": True, "position_lot_id": lot_id}
    delta = cumulative - previous_cumulative
    lot = conn.execute(
        "SELECT position_lot_version,bucket,outcome_side,token_id,entry_strategy_leg,entry_strategy_artifact_sha256,entry_order_intent_id,remaining_size,remaining_cost_usdc,state FROM dynamic_three_leg_v6_position_lots WHERE position_lot_id=?",
        (lot_id,),
    ).fetchone()
    try:
        payload = json.loads(_text(row.get("intent_json")) or "{}")
    except (TypeError, ValueError):
        payload = {}
    bucket = _text(row.get("bucket"))
    side = _text(row.get("outcome_side")).upper()
    token = _text(row.get("token_id"))
    artifact = _text(row.get("strategy_artifact_sha256"))
    reason_family = _text(row.get("reason_family"))
    reason_code = _text(row.get("reason_code"))
    conn.execute("SAVEPOINT v6_position_fill")
    try:
        if action == "BUY":
            leg = reason_code if reason_family == "strategy_leg" else ""
            if leg not in V6_LEGS or side not in {"YES", "NO"}:
                raise V6PositionLotError("v6_position_lot_buy_provenance_invalid")
            if lot is None:
                if previous is not None or expected_version != 1:
                    raise V6PositionLotError("v6_position_lot_buy_history_inconsistent")
                before, after = 0, 1
                conn.execute(
                    """INSERT INTO dynamic_three_leg_v6_position_lots(
                        position_lot_id,position_lot_version,management_scope,slug,event_date,bucket,outcome_side,
                        token_id,entry_strategy_leg,entry_strategy_artifact_sha256,entry_order_intent_id,
                        entry_posterior_run_id,main_yes_position_cycle_id,entry_price,acquired_size,remaining_size,remaining_cost_usdc,
                        standing_exit_plan_id,state,opened_at,closed_at,updated_at
                    ) VALUES(?,1,'target_realization_v6',?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'coverage_required',?,NULL,?)""",
                    (
                        lot_id, _text(payload.get("slug")), _text(payload.get("event_date")), bucket, side, token,
                        leg, artifact, intent_id, _text(row.get("posterior_run_id")),
                        _text(payload.get("main_yes_position_cycle_id")) or None, price, delta, delta,
                        delta * price, _text(applied_at), _text(applied_at),
                    ),
                )
            else:
                before = int(lot[0])
                expected_current = int(previous[1]) if previous else expected_version
                if before != expected_current or _text(lot[6]) != intent_id or _text(lot[9]) == "closed":
                    raise V6PositionLotError("v6_position_lot_buy_binding_conflict")
                after = before + 1
                conn.execute(
                    """UPDATE dynamic_three_leg_v6_position_lots
                       SET position_lot_version=?,acquired_size=acquired_size+?,remaining_size=remaining_size+?,
                           remaining_cost_usdc=remaining_cost_usdc+?,state='coverage_required',updated_at=?
                       WHERE position_lot_id=? AND position_lot_version=?""",
                    (after, delta, delta, delta * price, _text(applied_at), lot_id, before),
                )
        else:
            if lot is None or not is_v6_executable_exit_reason_code(reason_code):
                raise V6PositionLotError("v6_position_lot_sell_provenance_invalid")
            before = int(lot[0])
            expected_current = int(previous[1]) if previous else expected_version
            remaining, remaining_cost = float(lot[7]), float(lot[8])
            if before != expected_current or delta > remaining + 1e-9 or _text(lot[9]) == "closed":
                raise V6PositionLotError("v6_position_lot_sell_binding_conflict")
            after = before + 1
            average = remaining_cost / remaining if remaining > 0 else 0.0
            next_size = max(0.0, remaining - delta)
            next_cost = max(0.0, remaining_cost - delta * average)
            closed = next_size <= 1e-9
            conn.execute(
                """UPDATE dynamic_three_leg_v6_position_lots
                   SET position_lot_version=?,remaining_size=?,remaining_cost_usdc=?,state=?,closed_at=?,updated_at=?
                   WHERE position_lot_id=? AND position_lot_version=?""",
                (after, 0.0 if closed else next_size, 0.0 if closed else next_cost, "closed" if closed else "open", _text(applied_at) if closed else None, _text(applied_at), lot_id, before),
            )
        event_payload = {
            "position_lot_id": lot_id, "position_lot_version_before": before,
            "position_lot_version_after": after, "order_intent_id": intent_id,
            "order_action": action, "cumulative_filled_size": cumulative,
            "applied_size": delta, "applied_price": price,
            "reason_family": reason_family, "reason_code": reason_code,
            "fill_identity": _text(fill_identity), "applied_at": _text(applied_at),
        }
        conn.execute(
            """INSERT INTO dynamic_three_leg_v6_position_lot_events(
                position_lot_event_id,position_lot_id,position_lot_version_before,position_lot_version_after,
                order_intent_id,order_action,cumulative_filled_size,applied_size,applied_price,reason_family,
                reason_code,event_json,event_sha256,applied_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "v6lotevt_" + _sha(event_payload)[:24], lot_id, before, after, intent_id, action,
                cumulative, delta, price, reason_family, reason_code, _json(event_payload), _sha(event_payload), _text(applied_at),
            ),
        )
        if action == "SELL" and _text(payload.get("standing_exit_plan_id")):
            standing_id = _text(payload.get("standing_exit_plan_id"))
            standing = conn.execute(
                "SELECT target_size,covered_size,stage_released_size,state,coverage_version FROM dynamic_three_leg_v6_standing_exit_plans WHERE standing_exit_plan_id=?",
                (standing_id,),
            ).fetchone()
            if standing is None:
                raise V6PositionLotError("v6_position_lot_sell_standing_plan_missing")
            target_before, covered_before, released_before = float(standing[0]), float(standing[1]), float(standing[2])
            target_after = max(0.0, target_before - delta)
            if payload.get("standing_exit_coverage_order") is True:
                covered_after = max(0.0, covered_before - delta)
                released_after = released_before
            else:
                if released_before + 1e-9 < delta:
                    raise V6PositionLotError("v6_position_lot_target_sell_exceeds_released_coverage")
                covered_after = covered_before
                released_after = max(0.0, released_before - delta)
            standing_state = "closed" if target_after <= 1e-9 else "covered" if released_after <= 1e-9 else "released"
            conn.execute(
                """UPDATE dynamic_three_leg_v6_standing_exit_plans
                   SET target_size=?,covered_size=?,stage_released_size=?,position_lot_version=?,state=?,updated_at=?
                   WHERE standing_exit_plan_id=? AND coverage_version=?""",
                (target_after, covered_after, released_after, str(after), standing_state, _text(applied_at), standing_id, int(standing[4])),
            )
            coverage_event = {
                "standing_exit_plan_id": standing_id, "order_intent_id": intent_id,
                "order_action": "SELL", "applied_size": delta, "target_size_after": target_after,
                "covered_size_after": covered_after, "stage_released_size_after": released_after,
            }
            conn.execute(
                """INSERT INTO dynamic_three_leg_v6_standing_exit_coverage_events(
                    coverage_event_id,standing_exit_plan_id,coverage_version,event_type,covered_size_before,
                    covered_size_after,event_json,event_sha256,created_at
                ) VALUES(?,?,?,'partial_fill',?,?,?,?,?)""",
                ("v6coverevt_" + _sha(coverage_event)[:24], standing_id, int(standing[4]), covered_before, covered_after, _json(coverage_event), _sha(coverage_event), _text(applied_at)),
            )
            if (
                payload.get("schema") == "target_realization_v6_target_sell_intent_v1"
                and cumulative >= float(row.get("size") or 0.0) - 1e-9
                and _target_stage_fully_reconciled(
                    conn,
                    intent=row,
                    payload=payload,
                    current_lot_id=lot_id,
                    current_intent_fully_filled=True,
                )
            ):
                cycle_id = _text(payload.get("main_yes_position_cycle_id"))
                target_stage = int(payload.get("target_sell_stage") or 0)
                cycle_row = conn.execute(
                    """SELECT cycle_checkpoint_id,sell_stage_high_watermark,cycle_json
                         FROM dynamic_three_leg_v6_main_yes_cycles
                        WHERE main_yes_position_cycle_id=? ORDER BY updated_at DESC,cycle_checkpoint_id DESC LIMIT 1""",
                    (cycle_id,),
                ).fetchone()
                if cycle_row is None or target_stage < int(cycle_row[1]):
                    raise V6PositionLotError("v6_position_lot_target_stage_cycle_invalid")
                cycle_payload = json.loads(_text(cycle_row[2]) or "{}")
                cycle_payload["sell_stage_high_watermark"] = target_stage
                cycle_payload["requested_sell_stage"] = target_stage
                conn.execute(
                    """UPDATE dynamic_three_leg_v6_main_yes_cycles
                       SET sell_stage_high_watermark=?,cycle_json=?,cycle_sha256=?,updated_at=?
                       WHERE cycle_checkpoint_id=? AND sell_stage_high_watermark<=?""",
                    (target_stage, _json(cycle_payload), _sha(cycle_payload), _text(applied_at), cycle_row[0], target_stage),
                )
                conn.execute(
                    """UPDATE dynamic_three_leg_v6_target_sell_continuations
                       SET state='completed',updated_at=?
                       WHERE order_intent_id=? AND state='prepared'""",
                    (_text(applied_at), intent_id),
                )
        if fault_after == "lot":
            raise RuntimeError("v6_position_lot_fault_after_lot")
        coverage = None
        if action == "BUY":
            coverage_fill = {
                **payload,
                "strategy_leg": leg,
                "filled_size": delta,
                "posterior_run_id": _text(row.get("posterior_run_id")),
                "realization_state_id": _text(row.get("realization_state_id")),
                "position_lot_id": lot_id,
                "position_lot_version": str(after),
                "token_id": token,
                "bucket": bucket,
                "side": side,
                "buy_intent_id": intent_id,
                "fill_identity": _text(fill_identity),
                "trading_plan_id": _text(row.get("trading_plan_id")),
                "fact_snapshot_id": _text(row.get("fact_snapshot_id")),
                "position_snapshot_id": _text(row.get("position_snapshot_id")),
                "market_snapshot_id": _text(row.get("market_snapshot_id")),
                "capital_snapshot_id": _text(row.get("capital_snapshot_id")),
                "risk_snapshot_id": _text(row.get("risk_snapshot_id")),
                "capital_epoch_id": _text(row.get("capital_epoch_id")),
                "risk_reservation_id": _text(row.get("risk_reservation_id")),
                "quote_expires_at": _text(row.get("quote_expires_at")),
                "observation_to_order_hard_deadline_at": _text(row.get("observation_to_order_hard_deadline_at")),
            }
            coverage = commit_v6_buy_fill_standing_exit(
                conn, fill=coverage_fill, strategy_artifact_sha256=artifact,
                tick_size=token_tick_size, created_at=_text(applied_at),
                fault_after="intent" if fault_after == "coverage" else "",
            )
            if coverage.get("ok") is not True:
                raise V6PositionLotError("v6_position_lot_standing_coverage_not_prepared")
        conn.execute("RELEASE v6_position_fill")
    except Exception:
        conn.execute("ROLLBACK TO v6_position_fill")
        conn.execute("RELEASE v6_position_fill")
        raise
    return {
        "schema": SCHEMA_VERSION, "ok": True, "state": "fill_applied",
        "position_lot_id": lot_id, "position_lot_version": str(after),
        "applied_size": delta, "standing_exit": coverage, "places_orders": False,
    }


class V6PositionLotError(RuntimeError):
    pass


__all__ = ["SCHEMA_VERSION", "V6PositionLotError", "apply_v6_reconciled_fill"]
