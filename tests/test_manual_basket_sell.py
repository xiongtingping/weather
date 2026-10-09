"""Operator percentage sales, including interruption/re-entry. All gateways are stubs."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bayes_basket_target import manual_basket_order as M
from bayes_basket_target import manual_basket_sell as S

NOW = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)
BOOK = {"observed_at": "2026-10-07T23:59:59Z", "buckets": {
    "24°C": {"token_id": "yes24", "no_token": "no24", "min_order_size": 5,
               "bids": [(0.30, 10), (0.29, 100)], "no_bids": [(0.92, 10), (0.91, 100)]}}}
REQUEST = {"station": "ZUUU", "event_date": "2026-10-08", "bucket": "24°C", "side": "YES"}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "_book", lambda *a: BOOK)
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(M, "RECEIPT_PATH", tmp_path / "receipts.jsonl")
    monkeypatch.setattr(M, "RECONCILE_WAIT_SECONDS", 0)
    return lambda token: {"ok": True, "shares": 40.019}


@pytest.mark.parametrize("side", ["YES", "NO"])
@pytest.mark.parametrize("percent,shares", [(25, 10), (50, 20), (75, 30.01), (100, 40.01)])
def test_percentages_use_real_holdings_and_walk_bid_depth(setup, side, percent, shares):
    quote = S.quote_manual_sell({**REQUEST, "side": side, "percent": percent}, lookup=setup, now=NOW)
    assert quote["shares"] == shares
    expected_floor = (0.30 if shares == 10 else 0.29) if side == "YES" else (0.92 if shares == 10 else 0.91)
    assert quote["limit_price"] == expected_floor
    levels = BOOK["buckets"]["24°C"]["bids" if side == "YES" else "no_bids"]
    assert quote["estimated_proceeds"] == pytest.approx(min(shares, 10) * levels[0][0] + max(0, shares - 10) * levels[1][0])


@pytest.mark.parametrize("side", ["YES", "NO"])
def test_share_count_quote_walks_the_same_depth(setup, side):
    quote = S.quote_manual_sell({**REQUEST, "side": side, "shares": 12.5}, lookup=setup, now=NOW)
    levels = BOOK["buckets"]["24°C"]["bids" if side == "YES" else "no_bids"]
    assert quote["shares"] == 12.5 and quote["held_shares"] == 40.019 and quote["percent"] is None
    assert quote["limit_price"] == levels[1][0]
    assert quote["estimated_proceeds"] == pytest.approx(10 * levels[0][0] + 2.5 * levels[1][0])
    for shares, word in [(40.02, "超过交易所持仓"), (4.99, "最小单"), (1.234, "两位小数"), (0, "正数")]:
        with pytest.raises(M.ManualOrderError, match=word):
            S.quote_manual_sell({**REQUEST, "side": side, "shares": shares}, lookup=setup, now=NOW)


def request(shares=20, **over):
    return {**REQUEST, "confirm": "SELL", "batch_id": "sell-0001", "shares": shares, "limit_price": 0.29, "expected_held_shares": 40.019, **over}


def test_sale_payload_is_price_protected_and_receipt_reads_actual_cash(setup):
    sent = []
    positions = iter([40.019, 20.019])

    def send(payload):
        # Pending identity exists BEFORE the transport can leave the process.
        assert payload["idempotency_key"] in S.STATE_PATH.read_text()
        sent.append(payload)
        return {"ok": True, "orderID": "exchange-1", "status": "matched", "shares_matched": 20,
                "exchange_response": {"makingAmount": "20", "takingAmount": "5.9"}}

    result = S.place_manual_sell(request(), lookup=lambda token: {"ok": True, "shares": next(positions)},
                                 transport=send, now=NOW)
    assert result["ok"] and result["position_after"] == pytest.approx(20.019)
    assert result["results"][0]["usdc_received"] == 5.9
    assert sent[0]["side"] == "SELL" and sent[0]["token_id"] == "yes24"
    assert sent[0]["order_type"] == "FOK" and not sent[0]["post_only"]
    assert sent[0]["size"] == 20 and sent[0]["price"] == 0.29
    assert S.STATE_PATH.read_text() == "{}"


@pytest.mark.parametrize("over,word", [
    ({"confirm": "BUY"}, "确认"), ({"shares": 50}, "持仓已变"), ({"expected_held_shares": 39}, "持仓已变"),
    ({"shares": 4}, "最小单"), ({"shares": 5.001}, "两位小数"),
    ({"shares": float("nan")}, "有限数"), ({"limit_price": float("inf")}, "有限数"),
    ({"limit_price": 0.295}, "tick"), ({"limit_price": 0.31}, "深度不足"),
    ({"side": "MAYBE"}, "方向"), ({"bucket": "99°C"}, "梯子"),
])
def test_invalid_or_changed_inputs_never_reach_gateway(setup, over, word):
    with pytest.raises(M.ManualOrderError, match=word):
        S.place_manual_sell(request(**over), lookup=setup, now=NOW,
                            transport=lambda p: pytest.fail("unexpected real submission"))


def test_unknown_position_does_not_use_public_api(setup):
    with pytest.raises(M.ManualOrderError, match="持仓未知"):
        S.quote_manual_sell({**REQUEST, "percent": 25}, now=NOW,
                            lookup=lambda t: {"ok": False, "shares": 999})


@pytest.mark.parametrize("observed", ["2026-10-07T23:55:00Z", "2026-10-08T00:00:01Z", None])
def test_stale_future_missing_book_refuses(setup, monkeypatch, observed):
    monkeypatch.setattr(S, "_book", lambda *a: {**BOOK, "observed_at": observed})
    with pytest.raises(M.ManualOrderError, match="过期"):
        S.quote_manual_sell({**REQUEST, "percent": 25}, now=NOW, lookup=setup)


def test_small_holdings_and_insufficient_depth_refuse_quote(setup):
    for held, word in [(4.999, "最小单"), (500, "深度不足")]:
        with pytest.raises(M.ManualOrderError, match=word):
            S.quote_manual_sell({**REQUEST, "percent": 100}, now=NOW,
                                lookup=lambda t: {"ok": True, "shares": held})


def test_no_uses_registered_no_token(setup):
    sent = []
    S.place_manual_sell(request(side="NO", limit_price=0.91), lookup=setup, now=NOW,
        transport=lambda p: sent.append(p) or {"ok": False, "error": "paused"})
    assert sent[0]["token_id"] == "no24" and sent[0]["outcome_side"] == "NO"


def test_unknown_survives_restart_blocks_retry_then_reconciles(setup):
    sent = []
    unknown = lambda key: {"ok": False, "error": "lookup_timeout"}
    send = lambda p: sent.append(p) or {"ok": False, "transport_ambiguous": True}
    result = S.place_manual_sell(request(), lookup=setup, transport=send, reconcile=unknown, now=NOW)
    assert result["reconcile_required"] and len(sent) == 1
    assert result["results"][0]["event_type"] == "UNKNOWN_POST"
    # A different request after a process restart cannot escape the durable identity.
    with pytest.raises(M.ManualOrderError, match="reconcile_required"):
        S.place_manual_sell(request(batch_id="sell-0002"), lookup=setup, transport=send, reconcile=unknown, now=NOW)
    assert len(sent) == 1
    result = S.place_manual_sell(request(batch_id="sell-0003"), lookup=setup, transport=send,
        reconcile=lambda key: {"error": "order_never_reached_exchange"}, now=NOW)
    assert len(sent) == 2 and result["results"][0]["event_type"] == M.NEVER_REACHED
    assert S.STATE_PATH.read_text() == "{}"


def test_unknown_reconciles_fill_without_resend(setup):
    sent = []
    result = S.place_manual_sell(request(), lookup=setup, now=NOW,
        transport=lambda p: sent.append(p) or {"transport_ambiguous": True},
        reconcile=lambda key: {"ok": True, "orderID": "e1", "status": "matched", "shares_matched": 20,
                                "exchange_response": {"takingAmount": "5.8", "makingAmount": "20"}})
    assert len(sent) == 1 and result["ok"] and result["results"][0]["usdc_received"] == 5.8


def test_gateway_exception_is_unknown_never_a_false_refusal(setup):
    def send(p):
        raise TimeoutError()
    result = S.place_manual_sell(request(), lookup=setup, now=NOW, transport=send,
                                reconcile=lambda key: {"error": "timeout"})
    assert result["reconcile_required"]


def test_pending_resource_bound_refuses_without_submission(setup):
    S.STATE_PATH.write_text(" " * (S.MAX_STATE_BYTES + 1))
    with pytest.raises(M.ManualOrderError, match="超限"):
        S.place_manual_sell(request(), lookup=setup, now=NOW, transport=lambda p: pytest.fail())


def test_gateway_signs_operator_sell_as_explicit_limit_fok(setup):
    import importlib.util
    spec = importlib.util.spec_from_file_location("gateway_fok_test_helper", Path(__file__).with_name("test_clob_gateway_completion_fok.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _run_gateway = module._run_gateway
    sent = []
    S.place_manual_sell(request(), lookup=setup, now=NOW,
                        transport=lambda p: sent.append(p) or {"ok": False, "error": "paused"})
    out = _run_gateway({**sent[0], "token_id": "99"})
    assert out["result"].get("ok") is True, out
    assert [row["call"] for row in out["calls"]] == ["createOrder", "postOrder"]
    order = out["calls"][0]["order"]
    assert order["side"] == "SELL" and order["price"] == 0.29 and order["size"] == 20
    assert out["calls"][1]["orderType"] == "FOK" and not out["calls"][1]["postOnly"]


def test_control_routes_check_origin_and_native_confirmation(monkeypatch):
    import io
    import json
    import sys
    sys.path.insert(0, str(M.ROOT / "scripts"))
    import live_control_server as control
    called = []
    monkeypatch.setattr(S, "quote_manual_sell", lambda body: called.append("quote") or {"ok": True})
    monkeypatch.setattr(S, "place_manual_sell", lambda body: called.append("sell") or {"ok": True})
    monkeypatch.setattr(control, "native_order_confirmation", lambda body: (False, "cancelled"))

    def invoke(path, origin):
        raw = json.dumps(request()).encode()
        handler = object.__new__(control.Handler)
        handler.path = path
        handler.headers = {"Content-Length": str(len(raw)), "Origin": origin}
        handler.rfile = io.BytesIO(raw)
        result = []
        handler._send = lambda status, body: result.append((status, body))
        handler.do_POST()
        return result[0][0]

    assert invoke("/api/manual-basket-sell", "https://untrusted.example") == 403
    assert invoke("/api/manual-basket-sell-quote", "https://untrusted.example") == 403
    assert called == []
    assert invoke("/api/manual-basket-sell", "null") == 403 and called == []
    assert invoke("/api/manual-basket-sell-quote", "null") == 200 and called == ["quote"]
    assert invoke("/api/manual-basket-sell", "http://127.0.0.1:8791") == 200
    assert called == ["quote", "sell"]
