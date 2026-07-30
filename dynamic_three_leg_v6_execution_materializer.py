"""Materialize Target Realization v6 decisions and durable intent rows."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from dynamic_three_leg_position_lot_runtime import build_position_lot_identity
from standing_exit_999_runtime import tick_supports_0999


SCHEMA_VERSION = "dynamic_three_leg_v6_execution_materializer_v1"


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


def _id(prefix: str, value: Any) -> str:
    return prefix + _sha(value)[:24]


def materialize_target_realization_v6_execution(
    *,
    plan: Mapping[str, Any],
    market_snapshot: Mapping[str, Any],
    position_snapshot_id: str,
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    correlation_id: str,
    observation_to_order_hard_deadline_at: str,
    decision_as_of: str,
    position_exposures: Sequence[Mapping[str, Any]] = (),
    coverage_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    if plan.get("schema") != "target_realization_v6_composer_v1" or plan.get("ok") is not True:
        blockers.append("v6_execution_plan_invalid")
    if not all((_text(position_snapshot_id), _text(fact_snapshot_id), _text(slug), _text(event_date), _text(correlation_id))):
        blockers.append("v6_execution_lineage_missing")
    quotes = market_snapshot.get("buckets") if isinstance(market_snapshot.get("buckets"), Mapping) else {}
    decisions: list[dict[str, Any]] = []
    intents: list[dict[str, Any]] = []
    for candidate in plan.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        leg = _text(candidate.get("strategy_leg"))
        bucket = _text(candidate.get("bucket"))
        side = _text(candidate.get("side")).upper()
        candidate_id = _text(candidate.get("candidate_id"))
        final = _finite(candidate.get("final_incremental_target_notional_usdc"))
        reason_code = leg
        decision_id = _id("v6decision_", (plan.get("v6_trading_plan_id"), candidate_id, "BUY"))
        row_blockers: list[str] = []
        if not all((leg, bucket, candidate_id)) or side not in {"YES", "NO"} or final is None or final < 0:
            row_blockers.append("v6_execution_candidate_invalid")
        if candidate.get("entry_allowed", True) is not True:
            row_blockers.append(_text(candidate.get("entry_block_reason")) or "v6_execution_entry_window_closed")
        quote = quotes.get(bucket) if isinstance(quotes.get(bucket), Mapping) else {}
        price = _finite(quote.get("yes_ask" if side == "YES" else "no_ask"))
        token = _text(quote.get("yes_token_id" if side == "YES" else "no_token_id"))
        tick = quote.get("yes_tick_size" if side == "YES" else "no_tick_size")
        if final is not None and final > 1e-9:
            if price is None or not 0 < price < 1 or not token:
                row_blockers.append("v6_execution_quote_or_token_missing")
            if not tick_supports_0999(tick):
                row_blockers.append("v6_execution_future_standing_999_tick_unsupported")
        will_order = bool(final is not None and final > 1e-9 and not row_blockers)
        intent_id = _id("v6intent_", (decision_id, observation_to_order_hard_deadline_at)) if will_order else ""
        details = {
            "schema": "target_realization_v6_decision_v1",
            "v6_trading_plan_id": _text(plan.get("v6_trading_plan_id")),
            "candidate": dict(candidate),
            "capital_scale_factor": _finite(plan.get("capital_allocation", {}).get("capital_scale_factor")),
            "block_reasons": row_blockers,
            "will_order": will_order,
        }
        decisions.append({
            "decision_id": decision_id,
            "order_intent_id": intent_id,
            "run_date": _text(event_date),
            "slug": _text(slug),
            "event_date": _text(event_date),
            "outcome": bucket,
            "side": side,
            "action": "entry" if will_order else "hold",
            "decided_state": "intent_prepared" if will_order else "blocked_no_order" if row_blockers else "hold_no_incremental_capital",
            "will_order": will_order,
            "reason_codes": [reason_code] if will_order else row_blockers or ["v6_no_incremental_buy_required"],
            "reasons": [{"reason_family": "strategy_leg" if will_order else "execution_block", "reason_code": code} for code in ([reason_code] if will_order else row_blockers or ["v6_no_incremental_buy_required"])],
            "details": details,
        })
        if not will_order:
            continue
        size = float(final) / float(price)
        lot_id, lot_version = build_position_lot_identity(
            order_intent_id=intent_id, bucket=bucket, outcome_side=side, token_id=token,
        )
        payload = {
            "schema": "target_realization_v6_buy_intent_v1",
            "order_intent_id": intent_id,
            "v6_trading_plan_id": _text(plan.get("v6_trading_plan_id")),
            "strategy_leg": leg,
            "main_yes_position_cycle_id": _text(plan.get("main_yes_position_cycle_id")) if leg == "main_yes" else "",
            "capital_scale_factor": _finite(plan.get("capital_allocation", {}).get("capital_scale_factor")),
            "token_tick_size": _text(tick),
            "slug": _text(slug), "event_date": _text(event_date),
            "bucket": bucket, "outcome_side": side, "token_id": token,
            "side": "BUY", "price": price, "size": size, "notional": final,
            "order_type": "GTC", "post_only": False,
            "direct_target_order_context": True, "direct_target_exit_order": False,
            "expected_token_balance": 0.0,
            "reason_family": "strategy_leg", "reason_code": reason_code,
            "observation_to_order_hard_deadline_at": _text(observation_to_order_hard_deadline_at),
        }
        idempotency_key = "target-realization-v6:" + _sha((intent_id, token, "BUY", price, size))
        intent = {
            "order_intent_id": intent_id, "decision_id": decision_id,
            "transition_plan_id": _text(plan.get("v6_trading_plan_id")),
            "trading_plan_id": _text(plan.get("v6_trading_plan_id")),
            "posterior_run_id": _text(plan.get("posterior_run_id")),
            "realization_state_id": _text(plan.get("realization_state_id")),
            "fact_snapshot_id": _text(fact_snapshot_id), "position_snapshot_id": _text(position_snapshot_id),
            "position_lot_id": lot_id, "position_lot_version": lot_version,
            "strategy_artifact_sha256": _text(plan.get("strategy_artifact_sha256")),
            "market_snapshot_id": _text(market_snapshot.get("market_snapshot_id")),
            "capital_snapshot_id": _text(plan.get("capital_allocation", {}).get("capital_snapshot_id")),
            "risk_snapshot_id": _text(plan.get("capital_allocation", {}).get("risk_snapshot_id")),
            "capital_epoch_id": _text(plan.get("capital_allocation", {}).get("capital_epoch_id")),
            "risk_reservation_id": _text(plan.get("risk_reservation_id")),
            "candidate_id": candidate_id, "bucket": bucket, "outcome_side": side,
            "order_action": "BUY", "token_id": token, "limit_price": price,
            "size": size, "notional": float(final), "reason_family": "strategy_leg", "reason_code": reason_code,
            "state": "prepared", "live_submit_authorized": 1,
            "observation_to_order_hard_deadline_at": _text(observation_to_order_hard_deadline_at),
            "quote_expires_at": _text(market_snapshot.get("expires_at")),
            "idempotency_key": idempotency_key, "intent_json": _json(payload), "intent_sha256": _sha(payload),
            "created_at": _text(decision_as_of), "updated_at": _text(decision_as_of),
        }
        intents.append(intent)
    release_requests: list[dict[str, Any]] = []
    target_exit = plan.get("target_realization_exit") if isinstance(plan.get("target_realization_exit"), Mapping) else None
    if target_exit:
        reason_code = _text(target_exit.get("exit_reason_code"))
        cycle = plan.get("main_yes_cycle") if isinstance(plan.get("main_yes_cycle"), Mapping) else {}
        previous_stage = int(cycle.get("sell_stage_high_watermark") or 0)
        requested_stage = int(cycle.get("requested_sell_stage") or previous_stage)
        ratios = (0.0, 0.25, 0.50, 0.75, 1.0)
        coverage_rows = {
            _text(row.get("position_lot_id")): row
            for row in (coverage_snapshot or {}).get("rows", [])
            if isinstance(row, Mapping) and _text(row.get("position_lot_id"))
        }
        managed = [
            row for row in position_exposures
            if isinstance(row, Mapping)
            and _text(row.get("strategy_leg")) in {"main_yes", "second_yes"}
            and _text(row.get("bucket")) == _text(plan.get("main_yes_bucket"))
            and _text(row.get("side")).upper() == "YES"
            and float(row.get("position_size") or 0.0) > 1e-9
        ]
        if not managed:
            decisions.append({
                "decision_id": _id("v6decision_", (plan.get("v6_trading_plan_id"), "target_exit", "no_lot")),
                "order_intent_id": "", "run_date": _text(event_date), "slug": _text(slug),
                "event_date": _text(event_date), "outcome": _text(plan.get("main_yes_bucket")),
                "side": "YES", "action": "hold", "decided_state": "hold_no_managed_main_yes_lot",
                "will_order": False, "reason_codes": ["v6_target_exit_no_managed_main_yes_lot"],
                "reasons": [{"reason_family": "target_realization", "reason_code": "v6_target_exit_no_managed_main_yes_lot"}],
                "details": {"target_realization_exit": dict(target_exit)},
            })
        for exposure in managed:
            lot_id = _text(exposure.get("position_lot_id"))
            acquired = float(exposure.get("acquired_size") or 0.0)
            remaining = float(exposure.get("position_size") or 0.0)
            release_size = min(remaining, acquired * max(0.0, ratios[requested_stage] - ratios[previous_stage]))
            if reason_code == "target_realization_main_yes_invalidated":
                release_size = remaining
            coverage = coverage_rows.get(lot_id, {})
            already_released = float(coverage.get("stage_released_size") or 0.0)
            release_decision_id = _id("v6decision_", (plan.get("v6_trading_plan_id"), lot_id, reason_code, "coverage_release"))
            target_decision_id = _id("v6decision_", (plan.get("v6_trading_plan_id"), lot_id, reason_code, "target_sell"))
            if release_size <= 1e-9:
                continue
            if (
                already_released + 1e-9 < release_size
                and _text(coverage.get("state")) == "covered"
                and _text(coverage.get("standing_sell_order_id"))
            ):
                request_core = {
                    "standing_exit_plan_id": _text(coverage.get("standing_exit_plan_id")),
                    "v6_trading_plan_id": _text(plan.get("v6_trading_plan_id")),
                    "main_yes_position_cycle_id": _text(plan.get("main_yes_position_cycle_id")),
                    "target_realization_reason_code": reason_code,
                    "requested_release_size": release_size,
                    "standing_sell_order_id": _text(coverage.get("standing_sell_order_id")),
                }
                release_requests.append({
                    **request_core,
                    "standing_exit_release_request_id": _id("v6standrelreq_", request_core),
                    "idempotency_key": "v6-standing-release:" + _sha(request_core),
                    "state": "prepared", "created_at": _text(decision_as_of), "updated_at": _text(decision_as_of),
                })
                decisions.append({
                    "decision_id": release_decision_id, "order_intent_id": "", "run_date": _text(event_date),
                    "slug": _text(slug), "event_date": _text(event_date), "outcome": _text(plan.get("main_yes_bucket")),
                    "side": "YES", "action": "hold", "decided_state": "standing_coverage_release_prepared",
                    "will_order": False, "reason_codes": ["v6_standing_coverage_release_precedes_target_sell"],
                    "reasons": [{"reason_family": "coverage", "reason_code": "v6_standing_coverage_release_precedes_target_sell"}],
                    "details": {"target_realization_exit": dict(target_exit), "release_request": request_core},
                })
                continue
            if _text(coverage.get("state")) not in {"covered", "released"} or already_released + 1e-9 < release_size:
                decisions.append({
                    "decision_id": release_decision_id, "order_intent_id": "", "run_date": _text(event_date),
                    "slug": _text(slug), "event_date": _text(event_date), "outcome": _text(plan.get("main_yes_bucket")),
                    "side": "YES", "action": "hold", "decided_state": "coverage_reconcile_required",
                    "will_order": False, "reason_codes": ["v6_target_sell_coverage_not_released"],
                    "reasons": [{"reason_family": "coverage", "reason_code": "v6_target_sell_coverage_not_released"}],
                    "details": {"target_realization_exit": dict(target_exit), "position_lot_id": lot_id},
                })
                continue
            quote = quotes.get(_text(plan.get("main_yes_bucket"))) if isinstance(quotes.get(_text(plan.get("main_yes_bucket"))), Mapping) else {}
            price = _finite(quote.get("yes_bid"))
            token = _text(quote.get("yes_token_id"))
            if price is None or not token:
                decisions.append({
                    "decision_id": target_decision_id, "order_intent_id": "", "run_date": _text(event_date),
                    "slug": _text(slug), "event_date": _text(event_date), "outcome": _text(plan.get("main_yes_bucket")),
                    "side": "YES", "action": "hold", "decided_state": "target_sell_quote_missing",
                    "will_order": False, "reason_codes": ["v6_target_sell_fresh_bid_missing"],
                    "reasons": [{"reason_family": "execution", "reason_code": "v6_target_sell_fresh_bid_missing"}],
                    "details": {"target_realization_exit": dict(target_exit)},
                })
                continue
            intent_id = _id("v6intent_", (target_decision_id, observation_to_order_hard_deadline_at))
            payload = {
                "schema": "target_realization_v6_target_sell_intent_v1", "order_intent_id": intent_id,
                "v6_trading_plan_id": _text(plan.get("v6_trading_plan_id")),
                "main_yes_position_cycle_id": _text(plan.get("main_yes_position_cycle_id")),
                "standing_exit_plan_id": _text(coverage.get("standing_exit_plan_id")),
                "target_sell_stage": requested_stage, "slug": _text(slug), "event_date": _text(event_date),
                "bucket": _text(plan.get("main_yes_bucket")), "outcome_side": "YES", "token_id": token,
                "side": "SELL", "price": price, "size": release_size, "notional": release_size * price,
                "order_type": "GTC", "post_only": False, "direct_target_order_context": True,
                "direct_target_exit_order": True, "expected_token_balance": remaining,
                "reason_family": "target_realization", "reason_code": reason_code,
            }
            idempotency_key = "target-realization-v6:" + _sha((intent_id, token, "SELL", price, release_size))
            intents.append({
                "order_intent_id": intent_id, "decision_id": target_decision_id,
                "transition_plan_id": _text(plan.get("v6_trading_plan_id")), "trading_plan_id": _text(plan.get("v6_trading_plan_id")),
                "posterior_run_id": _text(plan.get("posterior_run_id")), "realization_state_id": _text(plan.get("realization_state_id")),
                "fact_snapshot_id": _text(fact_snapshot_id), "position_snapshot_id": _text(position_snapshot_id),
                "position_lot_id": lot_id, "position_lot_version": _text(exposure.get("position_lot_version")),
                "strategy_artifact_sha256": _text(plan.get("strategy_artifact_sha256")),
                "market_snapshot_id": _text(market_snapshot.get("market_snapshot_id")),
                "capital_snapshot_id": _text(plan.get("capital_allocation", {}).get("capital_snapshot_id")),
                "risk_snapshot_id": _text(plan.get("capital_allocation", {}).get("risk_snapshot_id")),
                "capital_epoch_id": _text(plan.get("capital_allocation", {}).get("capital_epoch_id")),
                "risk_reservation_id": _text(plan.get("risk_reservation_id")), "candidate_id": None,
                "bucket": _text(plan.get("main_yes_bucket")), "outcome_side": "YES", "order_action": "SELL",
                "token_id": token, "limit_price": price, "size": release_size, "notional": release_size * price,
                "reason_family": "target_realization", "reason_code": reason_code, "state": "prepared",
                "live_submit_authorized": 1, "observation_to_order_hard_deadline_at": _text(observation_to_order_hard_deadline_at),
                "quote_expires_at": _text(market_snapshot.get("expires_at")), "idempotency_key": idempotency_key,
                "intent_json": _json(payload), "intent_sha256": _sha(payload), "created_at": _text(decision_as_of), "updated_at": _text(decision_as_of),
            })
            decisions.append({
                "decision_id": target_decision_id, "order_intent_id": intent_id, "run_date": _text(event_date),
                "slug": _text(slug), "event_date": _text(event_date), "outcome": _text(plan.get("main_yes_bucket")),
                "side": "YES", "action": "exit", "decided_state": "intent_prepared", "will_order": True,
                "reason_codes": [reason_code], "reasons": [{"reason_family": "target_realization", "reason_code": reason_code}],
                "details": payload,
            })
    for off_role in plan.get("off_role_main_yes_exits") or []:
        if not isinstance(off_role, Mapping):
            continue
        lot_id = _text(off_role.get("position_lot_id"))
        exposure = next((row for row in position_exposures if isinstance(row, Mapping) and _text(row.get("position_lot_id")) == lot_id), None)
        if exposure is None:
            continue
        subplan = dict(plan)
        subplan["candidates"] = []
        subplan["off_role_main_yes_exits"] = []
        subplan["main_yes_bucket"] = _text(off_role.get("bucket"))
        subplan["main_yes_position_cycle_id"] = _text(off_role.get("main_yes_position_cycle_id"))
        subplan["main_yes_cycle"] = {
            "sell_stage_high_watermark": 0,
            "requested_sell_stage": 4,
        }
        subplan["target_realization_exit"] = dict(off_role)
        sub = materialize_target_realization_v6_execution(
            plan=subplan,
            market_snapshot=market_snapshot,
            position_snapshot_id=position_snapshot_id,
            fact_snapshot_id=fact_snapshot_id,
            slug=slug,
            event_date=event_date,
            correlation_id=correlation_id,
            observation_to_order_hard_deadline_at=observation_to_order_hard_deadline_at,
            decision_as_of=decision_as_of,
            position_exposures=[exposure],
            coverage_snapshot=coverage_snapshot,
        )
        blockers.extend(sub.get("block_reasons") or [])
        decisions.extend(sub.get("decisions") or [])
        intents.extend(sub.get("intents") or [])
        release_requests.extend(sub.get("coverage_release_requests") or [])
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "available" if not blockers else "blocked",
        "block_reasons": sorted(set(blockers)),
        "decisions": decisions,
        "intents": intents if not blockers else [],
        "coverage_release_requests": release_requests if not blockers else [],
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "materialize_target_realization_v6_execution"]
