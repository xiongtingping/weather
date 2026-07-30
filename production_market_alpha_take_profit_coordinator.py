"""Quote-triggered coordinator for the independent current-top YES wave exit."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Mapping

from dynamic_three_leg_position_lot_runtime import load_dynamic_three_leg_position_snapshot
from market_alpha_take_profit_activation import validate_market_alpha_take_profit_activation
from market_alpha_take_profit_quote_runtime import build_market_alpha_take_profit_quote
from production_market_alpha_take_profit_writer import commit_market_alpha_take_profit_candidate


SCHEMA_VERSION = "production_market_alpha_take_profit_coordinator_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value) or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def run_market_alpha_take_profit_once(
    conn: sqlite3.Connection,
    *,
    raw_guard_snapshot_fetcher: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    activation: Mapping[str, Any],
    preliminary_as_of: str,
) -> dict[str, Any]:
    if conn.in_transaction:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "market_alpha_coordinator_requires_clean_transaction", "places_orders": False}
    try:
        authorized = validate_market_alpha_take_profit_activation(activation)
    except ValueError as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "market_alpha_coordinator_activation_rejected", "detail": str(exc), "places_orders": False}
    rows = conn.execute(
        """SELECT p.position_lot_id,p.position_lot_version,p.slug,p.event_date,p.bucket,
                  p.outcome_side,p.token_id,p.entry_strategy_leg,
                  p.entry_strategy_artifact_sha256,p.remaining_size,p.remaining_cost_usdc,
                  p.frozen_take_profit_price,p.opened_at,h.fact_json,h.posterior_run_id,
                  h.fact_snapshot_id
             FROM dynamic_three_leg_position_lots p
             JOIN event_fact_latest l ON l.slug=p.slug AND l.event_date=p.event_date
             JOIN event_fact_history h ON h.id=l.fact_id
            WHERE p.state='open' AND p.remaining_size>0
              AND p.entry_strategy_leg IN ('top_yes_wave','second_yes')
              AND p.outcome_side='YES'
            ORDER BY p.updated_at,p.position_lot_id LIMIT 24"""
    ).fetchall()
    if not rows:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "idle_no_open_top_yes_wave", "places_orders": False}
    row = None
    public: dict[str, Any] = {}
    for candidate_row in rows:
        candidate_public = _mapping(_parse(candidate_row[13]).get("posterior_ssot_fact"))
        if _text(candidate_public.get("target_bucket")) == _text(candidate_row[4]):
            row, public = candidate_row, candidate_public
            break
    if row is None:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "hold_no_open_lot_is_current_top", "places_orders": False}
    public.update({
        "slug": _text(row[2]),
        "event_date": _text(row[3]),
        "posterior_run_id": _text(row[14]),
        "fact_snapshot_id": _text(row[15]),
    })
    if not _text(public.get("realization_state_id")) or not _text(public.get("target_bucket")):
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "market_alpha_latest_posterior_realization_missing", "places_orders": False}
    position_snapshot = load_dynamic_three_leg_position_snapshot(
        conn, slug=_text(row[2]), event_date=_text(row[3]), as_of=_text(preliminary_as_of)
    )
    if position_snapshot.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "market_alpha_position_snapshot_unavailable", "places_orders": False}
    if any(_text(item.get("position_lot_id")) == _text(row[0]) for item in position_snapshot.get("unresolved_intents", [])):
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "hold_position_has_unresolved_intent", "places_orders": False}
    size = float(row[9])
    request = {
        "token_id": _text(row[6]),
        "side": "SELL",
        "limit_price": 0.90,
        "size": size,
        "notional": size * 0.90,
    }
    try:
        raw = _mapping(raw_guard_snapshot_fetcher(request))
    except Exception as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "market_alpha_quote_fetch_exception", "exception_type": type(exc).__name__, "places_orders": False}
    evaluated_at = _text(raw.get("observed_at"))
    quote = build_market_alpha_take_profit_quote(
        token_id=_text(row[6]), requested_size=size, raw_snapshot=raw, evaluated_at=evaluated_at
    )
    if quote.get("ok") is not True:
        return {"schema": SCHEMA_VERSION, "ok": True, "state": "hold_take_profit_not_executable", "quote": quote, "places_orders": False}
    position = {
        "management_scope": "dynamic_three_leg_weather",
        "strategy_leg": "top_yes_wave",
        "entry_strategy_leg": _text(row[7]),
        "bucket": _text(row[4]),
        "side": _text(row[5]),
        "token_id": _text(row[6]),
        "position_lot_id": _text(row[0]),
        "position_lot_version": _text(row[1]),
        "position_size": size,
        "entry_strategy_artifact_sha256": _text(row[8]),
        "frozen_take_profit_price": 0.90,
        "effective_take_profit_price": 0.90,
        "take_profit_price_source": (
            "entry_frozen_top_yes_wave"
            if _text(row[7]) == "top_yes_wave"
            else "promoted_current_top_yes_policy"
        ),
        "opened_at": _text(row[12]),
    }
    result = commit_market_alpha_take_profit_candidate(
        conn,
        posterior_ssot_fact=public,
        position_snapshot_id=_text(position_snapshot.get("position_snapshot_id")),
        position=position,
        executable_sell_quote=quote,
        activation=authorized,
        decision_as_of=evaluated_at,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": result.get("ok") is True,
        "state": "intent_prepared" if result.get("ok") is True else "blocked",
        "writer_result": result,
        "quote": quote,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "run_market_alpha_take_profit_once"]
