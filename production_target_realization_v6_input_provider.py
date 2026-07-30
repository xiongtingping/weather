"""Freeze complete Target Realization v6 inputs before the outbox lease."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from dynamic_three_leg_clob_gateway_adapter import evaluate_dynamic_three_leg_exchange_snapshot
from dynamic_three_leg_v6_execution_materializer import materialize_target_realization_v6_execution
from production_dynamic_three_leg_input_provider import (
    _market_token_rows,
    load_latest_realization_state_for_outbox,
)
from target_realization_v6_runtime import build_target_realization_v6_plan


SCHEMA_VERSION = "production_target_realization_v6_input_provider_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _id(prefix: str, value: Any) -> str:
    return prefix + _sha(value)[:24]


def _dt(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ordinal(label: str) -> int:
    match = re.search(r"-?\d+", label)
    if not match:
        raise ValueError("v6_bucket_ordinal_unavailable")
    return int(match.group())


def _blocked(*reasons: str, **extra: Any) -> dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted({_text(r) for r in reasons if _text(r)}), "places_orders": False, **extra}


def _v6_position_and_coverage_snapshots(
    conn: sqlite3.Connection, *, slug: str, event_date: str, as_of: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    lots = conn.execute(
        """SELECT position_lot_id,position_lot_version,bucket,outcome_side,token_id,entry_strategy_leg,
                  acquired_size,remaining_size,remaining_cost_usdc,state,main_yes_position_cycle_id
             FROM dynamic_three_leg_v6_position_lots
            WHERE slug=? AND event_date=? AND state<>'closed' ORDER BY opened_at,position_lot_id""",
        (_text(slug), _text(event_date)),
    ).fetchall()
    exposures = [{
        "position_lot_id": _text(row[0]), "position_lot_version": _text(row[1]),
        "bucket": _text(row[2]), "side": _text(row[3]), "token_id": _text(row[4]),
        "strategy_leg": _text(row[5]), "acquired_size": float(row[6]), "position_size": float(row[7]),
        "notional_usdc": float(row[8]), "state": _text(row[9]), "main_yes_position_cycle_id": _text(row[10]),
    } for row in lots]
    unresolved = conn.execute(
        """SELECT order_intent_id,state FROM posterior_execution_intents
            WHERE state IN ('prepared','submitting','reconcile_required','partially_filled')
               OR (
                    state='gateway_accepted'
                    AND COALESCE(json_extract(intent_json,'$.standing_exit_coverage_order'),0)<>1
               )
            ORDER BY created_at,order_intent_id"""
    ).fetchall()
    coverage_rows = conn.execute(
        """SELECT standing_exit_plan_id,state,covered_size,uncovered_size,stage_released_size,coverage_version,
                  position_lot_id,position_lot_version,standing_sell_intent_id,standing_sell_order_id
             FROM dynamic_three_leg_v6_standing_exit_plans ORDER BY created_at,standing_exit_plan_id"""
    ).fetchall()
    position_core = {"slug": slug, "event_date": event_date, "as_of": as_of, "exposures": exposures, "unresolved": unresolved}
    position = {"complete": not unresolved, "position_snapshot_id": _id("v6positions_", position_core), "as_of": as_of, "exposures": exposures, "unresolved_intents": [dict(order_intent_id=_text(row[0]), state=_text(row[1])) for row in unresolved]}
    coverage = {
        "complete": not unresolved,
        "coverage_snapshot_id": _id("v6coverage_", {"as_of": as_of, "rows": coverage_rows, "unresolved": unresolved}),
        "all_conflicting_sell_and_cancel_reconciled": not unresolved and all(_text(row[1]) not in {"release_pending", "blocked"} for row in coverage_rows),
        "rows": [{"standing_exit_plan_id": row[0], "state": row[1], "covered_size": row[2], "uncovered_size": row[3], "stage_released_size": row[4], "coverage_version": row[5], "position_lot_id": row[6], "position_lot_version": row[7], "standing_sell_intent_id": row[8], "standing_sell_order_id": row[9]} for row in coverage_rows],
    }
    cycle_row = conn.execute(
        "SELECT cycle_json FROM dynamic_three_leg_v6_main_yes_cycles ORDER BY updated_at DESC,cycle_checkpoint_id DESC LIMIT 1"
    ).fetchone()
    prior_cycle = json.loads(_text(cycle_row[0])) if cycle_row else None
    no_row = conn.execute(
        "SELECT plan_json FROM target_realization_v6_plans ORDER BY created_at DESC,v6_trading_plan_id DESC LIMIT 1"
    ).fetchone()
    prior_no = None
    if no_row:
        prior_plan = json.loads(_text(no_row[0]))
        prior_no = next((dict(row) for row in prior_plan.get("candidates") or [] if isinstance(row, Mapping) and _text(row.get("strategy_leg")) in {"trajectory_no", "hard_exclusion_no"}), None)
        if prior_no is not None:
            prior_no["no_mode"] = _text(prior_plan.get("no_mode"))
    return position, coverage, prior_cycle, prior_no


def freeze_target_realization_v6_inputs(
    conn: sqlite3.Connection,
    *,
    outbox: Mapping[str, Any],
    preliminary_as_of: str,
    exchange_snapshot_fetcher: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    production_activation: Mapping[str, Any],
) -> dict[str, Any]:
    event = _mapping(outbox)
    loaded = load_latest_realization_state_for_outbox(conn, outbox=event)
    if loaded.get("ok") is not True:
        return {**loaded, "schema": SCHEMA_VERSION}
    realization = _mapping(loaded.get("realization_state"))
    position, coverage, prior_cycle, prior_no = _v6_position_and_coverage_snapshots(
        conn, slug=_text(event.get("slug")), event_date=_text(event.get("event_date")), as_of=_text(preliminary_as_of),
    )
    if position.get("complete") is not True or coverage.get("complete") is not True:
        return _blocked("v6_input_unresolved_execution_intent_requires_reconcile")
    distribution = _mapping(realization.get("market_bucket_probabilities"))
    if not distribution:
        return _blocked("v6_input_market_bucket_posterior_missing")
    try:
        labels = sorted(distribution, key=lambda label: (_ordinal(label), label))
    except ValueError as exc:
        return _blocked(str(exc))
    token_rows, token_blockers = _market_token_rows(
        conn, slug=_text(event.get("slug")), event_date=_text(event.get("event_date")), buckets=labels,
    )
    if token_blockers:
        return _blocked(*token_blockers)
    request_core = {
        "slug": _text(event.get("slug")), "event_date": _text(event.get("event_date")),
        "posterior_run_id": _text(event.get("posterior_run_id")), "buckets": token_rows,
        "position_token_ids": sorted({_text(row.get("token_id")) for row in position.get("exposures", []) if _text(row.get("token_id"))}),
    }
    request = {**request_core, "request_id": _id("v6clobrequest_", request_core)}
    try:
        raw_exchange = _mapping(exchange_snapshot_fetcher(request))
    except Exception as exc:
        return _blocked("v6_input_exchange_snapshot_exception", exception_type=type(exc).__name__)
    effective_as_of = _text(raw_exchange.get("observed_at"))
    exchange = evaluate_dynamic_three_leg_exchange_snapshot(request, raw_exchange, evaluated_at=effective_as_of, max_snapshot_age_ms=1_000)
    if exchange.get("ok") is not True:
        return _blocked(*(exchange.get("block_reasons") or ["v6_input_exchange_snapshot_invalid"]), exchange_snapshot=exchange)
    observed = _dt(exchange.get("as_of"))
    if observed is None:
        return _blocked("v6_input_exchange_time_invalid")
    market = {
        "market_snapshot_id": _id("v6market_", {"request": request["request_id"], "as_of": exchange.get("as_of"), "buckets": exchange.get("buckets")}),
        "as_of": exchange.get("as_of"), "expires_at": _iso(observed + timedelta(milliseconds=900)),
        "buckets": exchange.get("buckets"), "source": "local_clob_target_realization_v6_snapshot",
    }
    available_cash = float(exchange.get("available_cash_usdc") or 0.0)
    capital_core = {"request_id": request["request_id"], "as_of": exchange.get("as_of"), "available_cash_usdc": available_cash}
    capital_snapshot_id = _id("v6capital_", capital_core)
    risk_snapshot_id = _id("v6risk_", capital_core)
    capital_epoch_id = _id("v6epoch_", capital_core)
    evidence = _mapping(realization.get("strong_physical_evidence"))
    bucket_evidence = _mapping(evidence.get("bucket_evidence"))
    certificates = _mapping(realization.get("settlement_exclusion_certificates"))
    posterior_rows = []
    for label in labels:
        cert = _mapping(certificates.get(label))
        posterior_rows.append({
            "bucket": label, "ordinal": _ordinal(label), "probability": float(distribution[label]),
            "soft_unreachable": _mapping(bucket_evidence.get(label)).get("soft_unreachability_diagnostic") is True,
            "hard_exclusion_certificate": cert,
        })
    timing = _mapping(realization.get("peak_timing_state"))
    peak_at = _dt(timing.get("entry_peak_at"))
    second_allowed = bool(peak_at is not None and observed >= peak_at - timedelta(minutes=30) and observed < peak_at)
    plan = build_target_realization_v6_plan(
        posterior_run_id=_text(event.get("posterior_run_id")),
        realization_state_id=_text(realization.get("realization_state_id")),
        canonical_observation_event_id=_text(evidence.get("canonical_observation_event_id")),
        posterior_buckets=posterior_rows, peak_timing_state=timing,
        future_warming_or_rebound_risk=float(timing.get("future_peak_probability") or 0.0) > 0.0,
        second_yes_entry_allowed=second_allowed,
        position_exposure_snapshot=position, coverage_snapshot=coverage,
        prior_main_yes_cycle=prior_cycle, prior_no_state=prior_no,
        available_capital_usdc=available_cash, capital_snapshot_id=capital_snapshot_id,
        risk_snapshot_id=risk_snapshot_id, capital_epoch_id=capital_epoch_id,
        strategy_artifact_sha256=_text(production_activation.get("strategy_artifact_sha256")),
        decision_as_of=effective_as_of,
    )
    if plan.get("ok") is not True:
        return {**plan, "schema": SCHEMA_VERSION}
    materialized = materialize_target_realization_v6_execution(
        plan=plan, market_snapshot=market, position_snapshot_id=_text(position.get("position_snapshot_id")),
        fact_snapshot_id=_text(event.get("fact_snapshot_id")), slug=_text(event.get("slug")),
        event_date=_text(event.get("event_date")), correlation_id=_text(event.get("correlation_id")),
        observation_to_order_hard_deadline_at=_text(event.get("observation_to_order_hard_deadline_at")),
        decision_as_of=effective_as_of,
        position_exposures=position.get("exposures") or [],
        coverage_snapshot=coverage,
    )
    if materialized.get("ok") is not True:
        return {**materialized, "schema": SCHEMA_VERSION}
    return {
        "schema": SCHEMA_VERSION, "ok": True, "state": "available",
        "slug": _text(event.get("slug")), "event_date": _text(event.get("event_date")),
        "decision_as_of": effective_as_of, "realization_state": realization,
        "market_snapshot": market, "position_snapshot": position, "coverage_snapshot": coverage,
        "v6_plan": plan, "decisions": materialized["decisions"], "intents": materialized["intents"],
        "coverage_release_requests": materialized["coverage_release_requests"],
        "production_activation": dict(production_activation), "exchange_request": request,
        "exchange_snapshot": exchange, "capital_snapshot_id": capital_snapshot_id,
        "risk_snapshot_id": risk_snapshot_id, "capital_epoch_id": capital_epoch_id,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "freeze_target_realization_v6_inputs"]
