"""The board's one-click basket: what it refuses before sending, and the stop rule.

No test here reaches the gateway: every send goes to a stub.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import bayes_basket_target.manual_basket_order as M

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
BOOK = {
    "observed_at": "2026-10-05T11:59:30Z",
    "buckets": {
        "24°C": {"token_id": "t24", "asks": [(0.07, 8.0), (0.08, 100.0)], "best_bid": 0.06,
                 "tick": 0.001, "min_order_size": 5.0},
        "25°C": {"token_id": "t25", "asks": [(0.15, 6.0), (0.16, 300.0)], "best_bid": 0.14,
                 "tick": 0.001, "min_order_size": 5.0},
    },
}


def _request(**over):
    base = {"confirm": "BUY", "station": "ZUUU", "event_date": "2026-10-06",
            "batch_id": "batch-0001", "mode": "taker",
            "legs": [{"bucket": "24°C", "shares": 50, "limit_price": 0.08},
                     {"bucket": "25°C", "shares": 12, "limit_price": 0.16}]}
    base.update(over)
    return base


def test_a_take_becomes_capped_fok_buy_yes_with_the_books_token() -> None:
    plan = M.plan_orders(_request(), BOOK, now=NOW)
    first = plan["legs"][0]["payload"]
    assert first["token_id"] == "t24" and first["side"] == "BUY" and first["outcome_side"] == "YES"
    assert first["order_type"] == "FAK" and first["fok_price_cap"] == first["price"] == 0.08
    assert first["strategy_id"] == M.STRATEGY_ID
    assert plan["notional_usdc"] == pytest.approx(50 * 0.08 + 12 * 0.16)


@pytest.mark.parametrize("over, word", [
    ({"confirm": ""}, "确认"),
    ({"legs": [{"bucket": "99°C", "shares": 10, "limit_price": 0.1}]}, "梯子"),
    ({"legs": [{"bucket": "24°C", "shares": 4, "limit_price": 0.08}]}, "最小单"),
    ({"mode": "maker", "legs": [{"bucket": "24°C", "shares": 50, "limit_price": 0.07}]}, "卖一"),
    ({"legs": [{"bucket": "25°C", "shares": 2000, "limit_price": 0.16}]}, "上限"),
])
def test_refused_before_anything_is_sent(over, word) -> None:
    book = BOOK
    if "上限" == word:
        book = {**BOOK, "buckets": {**BOOK["buckets"], "25°C": {**BOOK["buckets"]["25°C"], "asks": [(0.16, 5000.0)]}}}
    with pytest.raises(M.ManualOrderError, match=word):
        M.plan_orders(_request(**over), book, now=NOW)


def test_a_stale_book_refuses() -> None:
    with pytest.raises(M.ManualOrderError, match="过期"):
        M.plan_orders(_request(), {**BOOK, "observed_at": "2026-10-05T11:50:00Z"}, now=NOW)


def test_the_batch_stops_at_the_first_leg_that_is_not_clean(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "latest_book", lambda *a: BOOK)
    monkeypatch.setattr(M, "RECEIPT_PATH", tmp_path / "r.jsonl")
    monkeypatch.setattr(M.sqlite3, "connect", lambda *a, **k: _Conn())
    sent = []

    def refuse(payload):
        sent.append(payload["token_id"])
        return {"ok": False, "error": "clob_order_service_live_paused"}

    out = M.place_manual_basket(_request(), transport=refuse, now=NOW)
    assert sent == ["t24"]
    assert [r["event_type"] for r in out["results"]] == ["SUBMIT_REFUSED", "NOT_SENT"]
    assert out["ok"] is False and (tmp_path / "r.jsonl").read_text().count("\n") == 1


class _Conn:
    def execute(self, *_a):
        return self

    def close(self):
        pass


def test_prices_go_down_to_the_venues_grid() -> None:
    assert M._on_tick(0.299) == 0.29 and M._on_tick(0.29) == 0.29
    assert M._on_tick(0.0315) == 0.031
    plan = M.plan_orders(_request(mode="manual", legs=[{"bucket": "25°C", "shares": 10, "limit_price": 0.157}]), BOOK, now=NOW)
    assert plan["legs"][0]["payload"]["price"] == 0.15 and "tick_size" not in plan["legs"][0]["payload"]


def test_the_gateway_client_never_goes_through_a_proxy(monkeypatch) -> None:
    import urllib.request

    import clob_order_service_client as C

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    # An empty ProxyHandler replaces the default one that reads the
    # environment and the system settings; having no proxies it opens
    # nothing itself, so the opener holds no proxying handler at all.
    assert not any(isinstance(h, urllib.request.ProxyHandler) and h.proxies
                   for h in C._DIRECT.handlers)
    assert not any(hasattr(h, "http_open") and isinstance(h, urllib.request.ProxyHandler)
                   for h in C._DIRECT.handlers)
    default = urllib.request.build_opener()
    assert any(isinstance(h, urllib.request.ProxyHandler) for h in default.handlers)


def test_holdings_read_shares_from_the_gateway_and_cost_from_the_data_api(monkeypatch) -> None:
    monkeypatch.setattr(M, "latest_book", lambda *a: BOOK)
    monkeypatch.setattr(M.sqlite3, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(M, "funder_address", lambda: "0x" + "1" * 40)
    out = M.station_day_holdings(
        "ZUUU", "2026-10-06",
        lookup=lambda token: {"ok": True, "shares": {"t24": 17.0, "t25": 0.0}[token]},
        positions=lambda user: {"t24": {"size": 17.0, "avg_price": 0.27}},
    )
    rows = {r["bucket"]: r for r in out["rows"]}
    assert rows["24°C"]["shares"] == 17.0 and rows["24°C"]["cost_usdc"] == pytest.approx(4.59)
    assert rows["25°C"]["shares"] == 0.0 and rows["25°C"]["cost_usdc"] is None
    assert not rows["24°C"]["disagrees"]


def test_holdings_unknown_when_nothing_answers_and_data_api_backs_up_the_gateway(monkeypatch) -> None:
    monkeypatch.setattr(M, "latest_book", lambda *a: BOOK)
    monkeypatch.setattr(M.sqlite3, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(M, "funder_address", lambda: "0x" + "1" * 40)
    timeout = lambda token: {"ok": False, "error": "gateway_transport:TimeoutError"}  # noqa: E731

    def no_api(user):
        raise TimeoutError("timed out")

    dead = M.station_day_holdings("ZUUU", "2026-10-06", lookup=timeout, positions=no_api)
    assert dead["unknown"] is True and all(r["shares"] is None for r in dead["rows"])
    backed = M.station_day_holdings("ZUUU", "2026-10-06", lookup=timeout,
                                    positions=lambda user: {"t24": {"size": 17.0, "avg_price": 0.27}})
    rows = {r["bucket"]: r for r in backed["rows"]}
    assert backed["unknown"] is False
    assert rows["24°C"]["shares"] == 17.0 and rows["24°C"]["shares_source"] == "data_api"
    assert rows["25°C"]["shares"] == 0.0


def test_a_take_is_sized_to_whole_cents() -> None:
    """2026-10-07: 24.17 shares at 0.32 (7.7344 USDC) was refused by the venue."""

    assert M.fok_shares(0.32, 24.17) == 24.0
    assert round(0.32 * M.fok_shares(0.32, 24.17), 6) == 7.68
    assert M.fok_shares(0.5, 13.33) == 13.32
    assert M.fok_shares(0.031, 50) == 50.0
    for price, shares in [(0.32, 24.17), (0.27, 33.3), (0.415, 17.77), (0.031, 49.99)]:
        got = M.fok_shares(price, shares)
        assert got <= shares and abs(round(price * got, 2) - price * got) < 1e-9
    plan = M.plan_orders(_request(legs=[{"bucket": "25°C", "shares": 24.17, "limit_price": 0.16}]),
                         BOOK, now=NOW)
    leg = plan["legs"][0]["payload"]
    assert abs(round(leg["price"] * leg["size"], 2) - leg["price"] * leg["size"]) < 1e-9


def test_a_no_leg_is_priced_on_the_mirrored_yes_bids_and_sent_on_the_no_token() -> None:
    book = {**BOOK, "buckets": {
        **BOOK["buckets"],
        "24°C": {**BOOK["buckets"]["24°C"], "no_token": "n24",
                 "no_asks": [(0.94, 30.0), (0.95, 500.0)], "no_best_bid": 0.93},
    }}
    plan = M.plan_orders(_request(legs=[
        {"bucket": "24°C", "side": "YES", "shares": 50, "limit_price": 0.08},
        {"bucket": "24°C", "side": "NO", "shares": 10, "limit_price": 0.94},
    ]), book, now=NOW)
    yes, no = (leg["payload"] for leg in plan["legs"])
    assert yes["token_id"] == "t24" and yes["outcome_side"] == "YES"
    assert no["token_id"] == "n24" and no["outcome_side"] == "NO" and no["side"] == "BUY"
    assert no["order_type"] == "FAK" and no["fok_price_cap"] == 0.94
    assert yes["idempotency_key"] != no["idempotency_key"]
    with pytest.raises(M.ManualOrderError, match="NO token"):
        M.plan_orders(_request(legs=[{"bucket": "25°C", "side": "NO", "shares": 10, "limit_price": 0.9}]),
                      BOOK, now=NOW)


def test_a_stale_balance_refusal_waits_for_a_fresh_reading_and_resends_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "latest_book", lambda *a: BOOK)
    monkeypatch.setattr(M, "RECEIPT_PATH", tmp_path / "r.jsonl")
    monkeypatch.setattr(M.sqlite3, "connect", lambda *a, **k: _Conn())
    calls = []

    def gateway(payload):
        calls.append(payload["idempotency_key"])
        if len(calls) == 1:
            return {"ok": False, "error": M.COLLATERAL_STALE, "exchange_post_started": False}
        return {"ok": True, "orderID": "x1", "status": "matched", "size_matched": payload["size"]}

    out = M.place_manual_basket(_request(legs=[{"bucket": "24°C", "shares": 50, "limit_price": 0.08}]),
                                transport=gateway, now=NOW, wait_collateral=lambda: True)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert out["results"][0]["event_type"] == "FILL"
    calls.clear()
    out = M.place_manual_basket(_request(legs=[{"bucket": "24°C", "shares": 50, "limit_price": 0.08}]),
                                transport=gateway, now=NOW, wait_collateral=lambda: False)
    assert len(calls) == 1 and out["results"][0]["event_type"] == "SUBMIT_REFUSED"


def test_an_unknown_post_is_reconciled_never_resent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "latest_book", lambda *a: BOOK)
    monkeypatch.setattr(M, "RECEIPT_PATH", tmp_path / "r.jsonl")
    monkeypatch.setattr(M.sqlite3, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(M, "RECONCILE_WAIT_SECONDS", 0.0)
    sent = []

    def gateway(payload):
        sent.append(payload["idempotency_key"])
        return {"ok": False, "error": "clob_proxy_submit_failed:curl: (28) SSL connection timeout",
                "exchange_post_started": True, "reconcile_required": True}

    leg = [{"bucket": "24°C", "shares": 50, "limit_price": 0.08}]
    out = M.place_manual_basket(_request(legs=leg), transport=gateway, now=NOW,
                                reconcile=lambda key: {"ok": False, "error": "order_never_reached_exchange"})
    assert len(sent) == 1 and out["results"][0]["event_type"] == M.NEVER_REACHED
    out = M.place_manual_basket(_request(legs=leg), transport=gateway, now=NOW,
                                reconcile=lambda key: {"ok": True, "orderID": "x", "status": "matched", "size_matched": 50})
    assert out["results"][0]["event_type"] == "FILL"
    out = M.place_manual_basket(_request(legs=leg), transport=gateway, now=NOW,
                                reconcile=lambda key: {"ok": False, "error": "lookup_timeout"})
    assert out["results"][0]["event_type"] == "UNKNOWN_POST" and len(sent) == 3


def test_a_take_is_a_capped_fak_sent_at_the_asked_size_whatever_the_book_shows() -> None:
    # User 2026-10-16: 「上限价 0.05 内只剩 19.20 份，需要 20.0 份，不要因为这种阻断」.
    book = {**BOOK, "buckets": {**BOOK["buckets"],
            "21°C": {"token_id": "t21", "asks": [(0.05, 19.2), (0.06, 500.0)], "best_bid": 0.04,
                     "tick": 0.001, "min_order_size": 5.0}}}
    leg = M.plan_orders(_request(legs=[{"bucket": "21°C", "shares": 20, "limit_price": 0.05}]),
                        book, now=NOW)["legs"][0]["payload"]
    assert leg["order_type"] == "FAK" and leg["fok_price_cap"] == 0.05 and leg["size"] == 20.0
    with pytest.raises(M.ManualOrderError, match="吃单下限"):
        M.plan_orders(_request(legs=[{"bucket": "21°C", "shares": 19.2, "limit_price": 0.05}]), book, now=NOW)


def test_an_unmatched_fak_is_killed_and_stops_the_batch(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(M, "latest_book", lambda *a: BOOK)
    monkeypatch.setattr(M, "RECEIPT_PATH", tmp_path / "r.jsonl")
    monkeypatch.setattr(M.sqlite3, "connect", lambda *a, **k: _Conn())
    out = M.place_manual_basket(_request(), now=NOW,
                                transport=lambda p: {"ok": True, "orderID": "x", "status": "unmatched"})
    assert [r["event_type"] for r in out["results"]] == ["EXPIRED", "NOT_SENT"]
