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
    assert first["order_type"] == "FOK" and first["fok_price_cap"] == first["price"] == 0.08
    assert first["strategy_id"] == M.STRATEGY_ID
    assert plan["notional_usdc"] == pytest.approx(50 * 0.08 + 12 * 0.16)


@pytest.mark.parametrize("over, word", [
    ({"confirm": ""}, "确认"),
    ({"legs": [{"bucket": "99°C", "shares": 10, "limit_price": 0.1}]}, "梯子"),
    ({"legs": [{"bucket": "24°C", "shares": 4, "limit_price": 0.08}]}, "最小单"),
    ({"legs": [{"bucket": "24°C", "shares": 50, "limit_price": 0.07}]}, "盘口已变"),
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
