from __future__ import annotations

import json

import pytest

from dynamic_three_leg_clob_gateway_adapter import (
    LocalDynamicThreeLegClobGateway,
    evaluate_dynamic_three_leg_exchange_snapshot,
    evaluate_execution_guard_snapshot,
)


NOW = "2026-07-13T00:00:00.800Z"


def _intent(*, side: str = "BUY", expected: float = 0.0) -> dict:
    payload = {
        "token_id": "token-1", "side": side, "price": 0.50, "size": 4.0,
        "notional": 2.0, "expected_token_balance": expected, "post_only": False,
    }
    return {
        "token_id": "token-1", "order_action": side, "limit_price": 0.50,
        "size": 4.0, "notional": 2.0, "intent_json": json.dumps(payload),
    }


def _snapshot(*, token_balance: str = "0", collateral: str = "100000000", ask: str = "0.50", bid: str = "0.50") -> dict:
    return {
        "schema": "clob_execution_guard_snapshot_v1", "ok": True,
        "evidence_only": True, "authorizes_order": False,
        "token_id": "token-1", "side": "BUY", "observed_at": "2026-07-13T00:00:00.500Z",
        "amount_decimals": 6,
        "order_book": {
            "asset_id": "token-1",
            "timestamp": "2026-07-13T00:00:00.500Z",
            "asks": [{"price": ask, "size": "5"}],
            "bids": [{"price": bid, "size": "5"}],
        },
        "conditional_balance_allowance": {"balance": token_balance, "allowance": "100000000"},
        "collateral_balance_allowance": {"balance": collateral, "allowance": collateral},
    }


def test_buy_guard_requires_fresh_position_quote_depth_and_collateral() -> None:
    result = evaluate_execution_guard_snapshot(_intent(), _snapshot(), evaluated_at=NOW)
    assert result["ok"] is True
    assert result["position_lot_current"] is True
    assert result["market_quote_current"] is True
    assert result["balance_sufficient"] is True
    assert result["gateway_authorization_trusted"] is False


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (_snapshot(ask="0.51"), "clob_execution_guard_buy_quote_or_depth_not_executable"),
        (_snapshot(collateral="1000000"), "clob_execution_guard_balance_or_allowance_insufficient"),
    ],
)
def test_buy_guard_fails_closed_on_exchange_drift(snapshot: dict, reason: str) -> None:
    result = evaluate_execution_guard_snapshot(_intent(), snapshot, evaluated_at=NOW)
    assert result["ok"] is False
    assert reason in result["block_reasons"]


def test_buy_guard_uses_durable_intent_ledger_not_conditional_balance() -> None:
    snapshot = _snapshot(token_balance="1000000")
    snapshot["conditional_balance_allowance"] = None
    result = evaluate_execution_guard_snapshot(_intent(), snapshot, evaluated_at=NOW)
    assert result["ok"] is True
    assert result["position_lot_current"] is True


def test_sell_guard_requires_current_position_and_executable_bid_depth() -> None:
    intent = _intent(side="SELL", expected=5.0)
    snapshot = _snapshot(token_balance="5000000")
    snapshot["side"] = "SELL"
    snapshot["collateral_balance_allowance"] = None
    result = evaluate_execution_guard_snapshot(intent, snapshot, evaluated_at=NOW)
    assert result["ok"] is True
    assert result["conditional_usable"] == 5.0


def test_standing_0999_sell_may_rest_but_requires_exact_tick_and_balance() -> None:
    intent = _intent(side="SELL", expected=5.0)
    intent["limit_price"] = 0.999
    intent["size"] = 5.0
    intent["notional"] = 4.995
    payload = json.loads(intent["intent_json"])
    payload.update({"price": 0.999, "size": 5.0, "notional": 4.995, "standing_exit_coverage_order": True})
    intent["intent_json"] = json.dumps(payload)
    snapshot = _snapshot(token_balance="5000000", bid="0.50")
    snapshot.update({"side": "SELL", "collateral_balance_allowance": None, "token_metadata": {"tick_size": "0.001"}})
    result = evaluate_execution_guard_snapshot(intent, snapshot, evaluated_at=NOW)
    assert result["ok"] is True
    assert result["standing_exit_coverage_order"] is True
    unsupported = {**snapshot, "token_metadata": {"tick_size": "0.01"}}
    blocked = evaluate_execution_guard_snapshot(intent, unsupported, evaluated_at=NOW)
    assert blocked["ok"] is False
    assert "clob_execution_guard_standing_999_tick_unsupported" in blocked["block_reasons"]


def test_allowances_map_uses_max_exchange_allowance() -> None:
    snapshot = _snapshot()
    snapshot["collateral_balance_allowance"] = {
        "balance": "100000000",
        "allowances": {"exchange-a": "1000000", "exchange-b": "90000000"},
    }
    result = evaluate_execution_guard_snapshot(_intent(), snapshot, evaluated_at=NOW)
    assert result["ok"] is True
    assert result["collateral_allowance"] == 90.0


def test_gateway_refuses_nonlocal_or_authenticated_url() -> None:
    with pytest.raises(ValueError, match="must_be_local_http"):
        LocalDynamicThreeLegClobGateway("https://clob.polymarket.com")
    with pytest.raises(ValueError, match="must_be_local_http"):
        LocalDynamicThreeLegClobGateway("http://user:pass@127.0.0.1:8765")


def test_multi_token_snapshot_allows_one_sided_quotes_without_synthesizing_missing_side() -> None:
    request = {
        "request_id": "request-1",
        "buckets": [{"bucket": "35C", "yes_token_id": "y35", "no_token_id": "n35"}],
        "position_token_ids": [],
    }
    snapshot = {
        "schema": "clob_dynamic_three_leg_snapshot_v1",
        "ok": True,
        "evidence_only": True,
        "authorizes_order": False,
        "request_id": "request-1",
        "observed_at": "2026-07-13T00:00:00.800Z",
        "amount_decimals": 6,
        "top_quotes_by_token": {
            "y35": {"asset_id": "y35", "best_bid": None, "best_ask": 0.40, "timestamp": NOW},
            "n35": {"asset_id": "n35", "best_bid": 0.59, "best_ask": None, "timestamp": NOW},
        },
        "order_books_by_token": {},
        "token_metadata_by_token": {
            "y35": {"tick_size": "0.001", "neg_risk": True},
            "n35": {"tick_size": "0.001", "neg_risk": True},
        },
        "conditional_balance_allowance_by_token": {},
        "collateral_balance_allowance": {"balance": "100000000", "allowance": "100000000"},
    }
    result = evaluate_dynamic_three_leg_exchange_snapshot(request, snapshot, evaluated_at=NOW)
    assert result["ok"] is True
    assert result["buckets"]["35C"]["yes_ask"] == 0.40
    assert result["buckets"]["35C"]["yes_bid"] is None
    assert result["candidate_side_quote_validation_deferred_to_materializer"] is True
    assert {row["state"] for row in result["quote_diagnostics"]} == {"bid_missing", "ask_missing"}
