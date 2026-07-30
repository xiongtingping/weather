#!/usr/bin/env python3
"""Watch and refresh the Chengdu 2026-07-25 40C loss-reduction GTC ladder.

This is deliberately narrower than the settlement-floor allocator.  It only
manages the explicitly authorised 40C YES BUY ladder, ignores the basket
``sum(all_in_yes_ask)`` gate, and never treats an unfilled GTC as inventory.
Every run re-reads positions and the official WS quote shard, then changes
only the residual difference to the configured target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATE_PATH = DATA / "loss_reduction_order_watch_chengdu_2026-07-25_40c.json"

import sys

sys.path.insert(0, str(ROOT))
from scripts.settlement_floor_execute import (  # noqa: E402
    _fetch_positions,
    _finite,
    _json,
    _post_local,
    _quote,
    _read_ws_snapshot,
    _registry,
    _gateway_health,
)


STRATEGY_ID = "weather_settlement_floor_biased_basket_v1"
EVENT_DATE = "2026-07-25"
CITY = "chengdu"
SLUG = "highest-temperature-in-chengdu-on-july-25-2026"
BUCKET = "40°C"
TICK_SIZE = 0.01
BASE_SHARES = 111.9565
TARGET_ADDITIONAL_SHARES = 380.10
TARGET_FINAL_SHARES = BASE_SHARES + TARGET_ADDITIONAL_SHARES
TOKEN_ID = "96188562985268659984361899185662183262052319335020379766458695635552398404712"

# The three orders created by the initial explicit user instruction.  State
# persists replacements after a price refresh; unrelated orders are never
# cancelled because they are not in this allow-list.
INITIAL_ORDER_IDS = [
    "0xafcabc6c1ab04baffc5edc90f08a6cff5c22308929cb707abcc11a9281729725",
    "0xc0e59424b8d7200df0062f5fa80b38689595be48581d5df0cf895d818d7fac8a",
    "0xc477769600ee531dd60499228c1aa44e4ef3d9836a341da8ac6fc88afcdaa6e5",
]


def _number(value: Any, default: float = 0.0) -> float:
    parsed = _finite(value)
    return float(parsed) if parsed is not None else float(default)


def _load_state() -> dict[str, Any]:
    state = {
        "strategy_id": STRATEGY_ID,
        "event_date": EVENT_DATE,
        "city": CITY,
        "bucket": BUCKET,
        "token_id": TOKEN_ID,
        "base_shares": BASE_SHARES,
        "target_additional_shares": TARGET_ADDITIONAL_SHARES,
        "target_final_shares": TARGET_FINAL_SHARES,
        "order_ids": list(INITIAL_ORDER_IDS),
        "blocked_order_ids": [],
        "blocked_reason": "",
    }
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return state
    if not isinstance(raw, Mapping):
        return state
    if str(raw.get("strategy_id") or "") != STRATEGY_ID or str(raw.get("token_id") or "") != TOKEN_ID:
        return state
    ids = [str(item) for item in (raw.get("order_ids") or []) if str(item).strip()]
    if ids:
        state["order_ids"] = ids
    state["blocked_order_ids"] = [
        str(item) for item in (raw.get("blocked_order_ids") or []) if str(item).strip()
    ]
    state["blocked_reason"] = str(raw.get("blocked_reason") or "")
    return state


def _save_state(state: Mapping[str, Any]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="loss-reduction-watch-", suffix=".json", dir=str(DATA))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(state), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp_name, STATE_PATH)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _open_orders(condition_id: str) -> dict[str, Any]:
    response = _post_local(
        "/lookup-open-orders",
        {"max_pages": 1, "market": condition_id},
        timeout_seconds=5.0,
        retry_attempts=1,
    )
    if response.get("ok") is not True:
        raise RuntimeError("loss_reduction_open_orders_unknown")
    return response


def _live_monitored_orders(response: Mapping[str, Any], order_ids: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in response.get("open_orders") or []:
        if not isinstance(raw, Mapping):
            continue
        order_id = str(raw.get("id") or raw.get("orderID") or "")
        token = str(raw.get("asset_id") or raw.get("token_id") or "")
        if token != TOKEN_ID or order_id not in order_ids:
            continue
        if str(raw.get("side") or "").upper() != "BUY":
            continue
        if str(raw.get("status") or "").upper() not in {"LIVE", "OPEN", "ACTIVE"}:
            continue
        original = _number(raw.get("original_size"), 0.0)
        matched = _number(raw.get("size_matched"), 0.0)
        rows.append({
            "order_id": order_id,
            "price": _number(raw.get("price"), 0.0),
            "original_size": original,
            "size_matched": matched,
            "remaining_size": max(0.0, original - matched),
            "status": str(raw.get("status") or ""),
        })
    return rows


def _best_bid(quote: Mapping[str, Any]) -> float:
    bids = quote.get("bids")
    if isinstance(bids, list):
        prices = [
            _number(level.get("price"), 0.0)
            for level in bids
            if isinstance(level, Mapping) and 0.0 < _number(level.get("price"), 0.0) < 1.0
        ]
        if prices:
            return max(prices)
    return _number(quote.get("best_bid"), 0.0)


def _ladder(best_bid: float, residual: float) -> list[dict[str, float]]:
    if residual <= 0.005:
        return []
    cap = min(0.99, max(TICK_SIZE, math.floor(best_bid / TICK_SIZE + 1e-9) * TICK_SIZE))
    prices = [round(max(TICK_SIZE, cap - TICK_SIZE * offset), 2) for offset in range(3)]
    first = round(residual / 3.0, 2)
    second = round(residual / 3.0, 2)
    third = round(residual - first - second, 2)
    sizes = [first, second, third]
    return [
        {"price": price, "size": size}
        for price, size in zip(prices, sizes)
        if size >= 0.01 and 0.0 < price < 1.0
    ]


def _same_ladder(existing: list[Mapping[str, Any]], desired: list[Mapping[str, Any]]) -> bool:
    if len(existing) != len(desired):
        return False
    got = sorted((round(_number(row.get("price")), 2), round(_number(row.get("remaining_size")), 2)) for row in existing)
    want = sorted((round(_number(row.get("price")), 2), round(_number(row.get("size")), 2)) for row in desired)
    return got == want


def _cancel(order_id: str) -> dict[str, Any]:
    key = "loss-reduction-cancel:" + hashlib.sha256(order_id.encode("utf-8")).hexdigest()
    return _post_local(
        "/cancel-order",
        {"order_id": order_id, "idempotency_key": key},
        timeout_seconds=15.0,
        retry_attempts=0,
    )


def _lookup_order(order_id: str) -> dict[str, Any]:
    return _post_local(
        "/lookup-exchange-order",
        {"order_id": order_id},
        timeout_seconds=15.0,
        retry_attempts=0,
    )


def _place(*, price: float, size: float) -> dict[str, Any]:
    identity = {
        "strategy_id": STRATEGY_ID,
        "slug": SLUG,
        "event_date": EVENT_DATE,
        "city": CITY,
        "bucket": BUCKET,
        "token_id": TOKEN_ID,
        "side": "BUY",
        "order_type": "GTC",
        "price": price,
        "size": size,
        "mode": "loss_reduction_staged",
    }
    key = "settlement-floor-loss-reduction:" + hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    return _post_local(
        "/place-order",
        {
            "schema": "settlement_floor_buy_intent_v1",
            "strategy_id": STRATEGY_ID,
            "strategy_source": STRATEGY_ID,
            "order_execution_domain": "settlement_floor_basket",
            "order_execution_source": "loss_reduction_order_watch",
            "order_execution_reason": "dynamic_best_bid_refresh",
            "order_intent_action": "entry",
            "slug": SLUG,
            "event_date": EVENT_DATE,
            "city": CITY,
            "bucket": BUCKET,
            "outcome": BUCKET,
            "outcome_side": "YES",
            "token_id": TOKEN_ID,
            "side": "BUY",
            "price": price,
            "tick_size": TICK_SIZE,
            "order_type": "GTC",
            "post_only": False,
            "direct_target_order_context": False,
            "allow_market": False,
            "size": size,
            "idempotency_key": key,
            "order_intent_id": key,
        },
        timeout_seconds=30.0,
        retry_attempts=0,
    )


def run_once(*, execute: bool) -> dict[str, Any]:
    state = _load_state()
    blocked_order_ids = [str(item) for item in state.get("blocked_order_ids") or [] if str(item).strip()]
    if blocked_order_ids:
        reconciles = [{"order_id": order_id, "response": _lookup_order(order_id)} for order_id in blocked_order_ids]
        unresolved = [
            item for item in reconciles
            if item["response"].get("ok") is not True
            or str(item["response"].get("status") or "").lower() in {"", "unknown", "submitted_unconfirmed"}
        ]
        result = {
            "schema": "loss_reduction_order_watch_v1",
            "strategy_id": STRATEGY_ID,
            "event_date": EVENT_DATE,
            "city": CITY,
            "bucket": BUCKET,
            "execution_requested": execute,
            "action": "blocked_reconcile_required" if unresolved else "reconcile_cleared_wait_next_cycle",
            "reason": state.get("blocked_reason") or "loss_reduction_order_reconcile_required",
            "blocked_order_ids": blocked_order_ids,
            "reconciles": reconciles,
        }
        if not unresolved:
            state["blocked_order_ids"] = []
            state["blocked_reason"] = ""
            _save_state(state)
        return result
    registry = _registry(SLUG, EVENT_DATE, [BUCKET])
    condition_id = str(registry[BUCKET]["condition_id"])
    snapshot = _read_ws_snapshot(max_age_seconds=300.0)
    quote = _quote(TOKEN_ID, max_age_seconds=300.0, snapshot=snapshot)
    positions = _fetch_positions({TOKEN_ID})
    current = _number(positions.get(TOKEN_ID, {}).get("shares"), 0.0)
    residual = max(0.0, float(state["target_final_shares"]) - current)
    open_response = _open_orders(condition_id)
    monitored_ids = {str(item) for item in state.get("order_ids") or []}
    existing = _live_monitored_orders(open_response, monitored_ids)
    best_bid = _best_bid(quote)
    desired = _ladder(best_bid, residual)
    result: dict[str, Any] = {
        "schema": "loss_reduction_order_watch_v1",
        "strategy_id": STRATEGY_ID,
        "event_date": EVENT_DATE,
        "city": CITY,
        "bucket": BUCKET,
        "current_shares": current,
        "base_shares": float(state["base_shares"]),
        "target_final_shares": float(state["target_final_shares"]),
        "residual_shares": residual,
        "best_bid": best_bid,
        "quote_observed_at": quote.get("book_observed_at"),
        "existing_orders": existing,
        "desired_ladder": desired,
        "execution_requested": execute,
        "action": "none",
    }
    if residual <= 0.005:
        if existing and execute:
            cancellations = [_cancel(str(item["order_id"])) for item in existing]
            if any(item.get("reconcile_required") or item.get("ok") is not True for item in cancellations):
                result.update({"action": "blocked", "reason": "loss_reduction_cancel_reconcile_required", "cancellations": cancellations})
                return result
            state["order_ids"] = []
            _save_state(state)
            result.update({"action": "cancelled_residual_zero", "cancellations": cancellations})
        else:
            result["action"] = "target_reached" if not existing else "waiting_to_cancel"
        return result
    if not desired:
        result.update({"action": "blocked", "reason": "loss_reduction_best_bid_missing"})
        return result
    if _same_ladder(existing, desired):
        result["action"] = "ladder_unchanged"
        return result
    if not execute:
        result["action"] = "refresh_needed_dry_run"
        return result
    health = _gateway_health()
    result["gateway_health"] = {key: health.get(key) for key in ("ready", "order_submission_enabled", "live_pause_present")}
    if health.get("ready") is not True or health.get("order_submission_enabled") is not True or health.get("live_pause_present") is True:
        result.update({"action": "blocked", "reason": "loss_reduction_gateway_not_authorized"})
        return result
    cancellations = []
    for row in existing:
        response = _cancel(str(row["order_id"]))
        cancellations.append({"order_id": row["order_id"], "response": response})
        if response.get("reconcile_required") or response.get("ok") is not True:
            result.update({"action": "blocked", "reason": "loss_reduction_cancel_reconcile_required", "cancellations": cancellations})
            return result
    placed = []
    new_ids = []
    for row in desired:
        response = _place(price=float(row["price"]), size=float(row["size"]))
        placed.append({**row, "response": response})
        if response.get("reconcile_required") or response.get("ok") is not True:
            unknown_id = str(response.get("expected_order_id") or response.get("orderID") or "")
            state["blocked_order_ids"] = sorted(set(blocked_order_ids + ([unknown_id] if unknown_id else [])))
            state["blocked_reason"] = "loss_reduction_order_reconcile_required"
            prior_ids = [str(item) for item in state.get("order_ids") or []]
            state["order_ids"] = sorted(set(prior_ids + ([unknown_id] if unknown_id else [])))
            _save_state(state)
            result.update({"action": "blocked", "reason": "loss_reduction_place_reconcile_required", "cancellations": cancellations, "placed": placed})
            return result
        order_id = str(response.get("orderID") or response.get("order_id") or "")
        if order_id:
            new_ids.append(order_id)
    state["order_ids"] = new_ids
    _save_state(state)
    result.update({"action": "ladder_refreshed", "cancellations": cancellations, "placed": placed})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_once(execute=bool(args.execute)), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
