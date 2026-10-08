"""Charter 256: the 跟单线 -- one account's weather trades, mirrored at our scale.

User rulings 2026-10-08 「不用研究，开始做跟单策略」「按照等比例缩小，我资金没有他多」
「按他已投入仓位算」: the source's trades on the fleet's daily-high markets, each
``ratio x size`` with ratio = our equity / the source's marked open positions,
bought never above the source's price and sold never under it.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import account_equity  # noqa: E402
from bayes_basket_target import copy_follow_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from tests.test_tiebreak_lane import _insert, store  # noqa: E402,F401 -- ``store`` is a fixture

ST, ED, TZ = "ZSPD", "2026-09-20", "Asia/Shanghai"
SLUG = "highest-temperature-in-shanghai-on-september-20-2026"
NOW = "2026-09-18T12:02:30.000Z"
START = "2026-09-18T11:00:00Z"
USER = "0x8fbd7cf5f806f563080864694415829f7229a959"
SETTINGS = {"enabled": True, "live_ws_book": False, "follow_from_utc": START, "max_slippage": 0.0}
#: bucket: (bid, ask).  Our equity 200 against the source's 8,000 -> ratio 0.025.
LADDER = {"27°C": (0.18, 0.20), "28°C": (0.28, 0.32), "29°C": (0.18, 0.20)}


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())


def _book(ladder=LADDER, ask_shares=100.0):
    return [{"bucket": b, "ordinal": i, "ask_available": True, "price_tick_size": 0.01,
             "ask_levels": [{"price": a, "shares": ask_shares}], "bid_levels": [{"price": bid, "shares": 100.0}]}
            for i, (b, (bid, a)) in enumerate(ladder.items())]


def _seed(conn, *, book=None, observed=NOW, equity=200.0):
    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": "snap1", "market_id": "m", "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "sequence": 1, "observed_at": observed, "quote_version": "v",
        "quote_observed_at": observed, "quote_expires_at": "2026-09-18T13:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(book if book is not None else _book()), "state": "COMMITTED",
        "created_at": observed,
    })
    registry_sha = "regc1".ljust(64, "0")
    _insert(conn, "basket_target_market_registry_snapshot",
            {"snapshot_sha256": registry_sha, "source_scope": "shared_live_registry_export_only"})
    for bucket in LADDER:
        _insert(conn, "basket_target_market_registry", {
            "snapshot_sha256": registry_sha, "slug": SLUG, "event_date": ED, "outcome": bucket,
            "city_slug": "shanghai", "station_id": ST, "settlement_timezone": TZ, "settlement_unit": "C",
            "yes_token": f"yes_{bucket}", "no_token": f"no_{bucket}", "market_active": 1,
            "market_accepting_orders": 1, "registry_json": json.dumps({"order_min_size": 5.0}),
            "refreshed_at": NOW, "source_eligible": 1, "source_lane": "awc",
        })
    account_equity.ensure_account_equity_table(conn)
    _insert(conn, account_equity.TABLE, {
        "equity_id": "eq1", "observed_at_utc": NOW, "cash_usdc": equity / 2, "positions_value_usdc": equity / 2,
        "equity_usdc": equity, "resting_buy_usdc": 0.0, "content_sha256": "e" * 64, "created_at_utc": NOW,
    })


def _trade(bucket="28°C", *, side="BUY", size=400.0, price=0.30, at="2026-09-18T12:00:00Z", token=None,
           slug=SLUG, tx="0xa"):
    return {"type": "TRADE", "timestamp": _ts(at), "asset": token or f"yes_{bucket}", "side": side,
            "size": size, "price": price, "usdcSize": size * price, "transactionHash": tx,
            "eventSlug": slug, "conditionId": "0xc", "title": f"Shanghai {bucket}"}


class Source:
    """The data API, as the lane reads it: the source's trades and its value."""

    def __init__(self, trades=(), value=8000.0):
        self.trades = list(trades)
        self.value = value
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/value":
            return [{"user": USER, "value": self.value}]
        assert path == "/activity" and params["type"] == "TRADE"
        rows = [t for t in self.trades if t["timestamp"] >= int(params["start"])]
        return rows[int(params["offset"]): int(params["offset"]) + int(params["limit"])]


def _transport(sent: list, *, fill: bool = True):
    def transport(path, payload):
        sent.append((path, payload))
        if payload.get("order_type") == "FOK" and fill:
            return {"ok": True, "orderID": f"0xfill{len(sent)}", "status": "matched",
                    "size_matched": payload["size"],
                    "exchange_response": {"makingAmount": round(payload["size"] * payload["price"], 2),
                                          "takingAmount": payload["size"]}}
        return {"ok": True, "orderID": f"0xrest{len(sent)}", "status": "live"}
    return transport


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(lane, "_cash_refusal", lambda conn, now_utc, needed: None)
    monkeypatch.setattr(lane, "PAUSE_FLAG_PATH", tmp_path / "live_paused.flag")
    monkeypatch.setattr(lane, "_TOKEN_CACHE", {})
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: True)
    monkeypatch.setattr("bayes_basket_target.lane_orders.egress_send_gate", lambda: {"hold": False})
    monkeypatch.setattr("bayes_basket_target.exit_orders.egress_send_gate", lambda: {"hold": False})
    monkeypatch.setattr("bayes_basket_target.exit_orders.live_intent_armed", lambda: True)


def _run(conn, source, sent, *, now=NOW, fill=True, wallet=None, authorized=True, **overrides):
    return lane.execute_copy_follow(
        conn, transport=_transport(sent, fill=fill), now_utc=now, live_submit_authorized=authorized,
        settings={**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS, **overrides}, fetch=source,
        position_reader=lambda token: {"ok": True, "shares": wallet if wallet is not None else 1e9},
        ratio_cache={},
    )


def _intents(conn):
    return conn.execute(
        "SELECT bucket, token_id, side, order_action, limit_price, requested_size, intent_json"
        " FROM basket_target_order_intents ORDER BY created_at, order_intent_id"
    ).fetchall()


# ---------------------------------------------------------------------------
# The lane's switch.


def test_the_lane_is_off_unless_the_file_turns_it_on() -> None:
    assert strategy_lanes.LANE_COPY_FOLLOW in strategy_lanes.KNOWN_LANES
    assert strategy_lanes.DEFAULT_LANES["copy_follow"]["enabled"] is False
    assert strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS["ratio_basis"] == "source_positions_value"
    assert strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS["source_user"] == USER


# ---------------------------------------------------------------------------
# Mapping a source trade onto the fleet.


def test_event_slugs_parse_to_a_city_and_a_date() -> None:
    assert lane.parse_event_slug(SLUG) == ("shanghai", "2026-09-20")
    assert lane.parse_event_slug("highest-temperature-in-buenos-aires-on-october-10-2026") == (
        "buenos-aires", "2026-10-10")
    assert lane.parse_event_slug("will-it-rain-in-london") is None
    assert lane.parse_event_slug("highest-temperature-in-nyc-on-februray-30-2026") is None


def test_a_token_maps_to_its_registry_row_yes_or_no(store) -> None:
    _seed(store)
    cities = lane.city_stations()
    assert cities["shanghai"] == ST
    assert lane.map_source_token(store, event_slug=SLUG, token_id="yes_28°C", cities=cities) == {
        "station_id": ST, "event_date": ED, "bucket": "28°C", "side": "YES", "settlement_timezone": TZ,
        "settlement_unit": "C"}
    assert lane.map_source_token(store, event_slug=SLUG, token_id="no_27°C", cities=cities)["side"] == "NO"
    assert lane.map_source_token(store, event_slug=SLUG, token_id="other", cities=cities) == {
        "refusal": "token_not_on_registry"}
    assert lane.map_source_token(
        store, event_slug="highest-temperature-in-hong-kong-on-september-20-2026", token_id="x", cities=cities,
    ) == {"refusal": "city_not_in_fleet:hong-kong"}


# ---------------------------------------------------------------------------
# Ingesting the source's trades.


def test_trades_are_stamped_with_the_ratio_and_refused_by_name(store) -> None:
    _seed(store)
    source = Source([
        _trade(at="2026-09-18T12:00:00Z"),
        _trade(at="2026-09-18T10:00:00Z", tx="0xold"),                    # before the follow start
        _trade(slug="highest-temperature-in-hong-kong-on-september-20-2026", token="hk", tx="0xhk"),
    ])
    lane.ensure_lane_schema(store)
    cfg = {**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS}
    receipt = lane.ingest_source_trades(store, cfg=cfg, now_utc=NOW, fetch=source, ratio_cache={})
    # The read starts at the follow start, so the older trade is never fetched.
    assert receipt["state"] == "ingested" and receipt["ingested"] == 2 and receipt["admitted"] == 1
    rows = store.execute(
        f"SELECT token_id, ratio, scaled_shares, refusal FROM {lane.SOURCE_TABLE} ORDER BY token_id"
    ).fetchall()
    assert rows == [("hk", 0.025, None, "city_not_in_fleet:hong-kong"), ("yes_28°C", 0.025, 10.0, None)]
    # Read again: nothing new is written.
    again = lane.ingest_source_trades(store, cfg=cfg, now_utc=NOW, fetch=source, ratio_cache={})
    assert again["state"] == "no_new_trades"
    assert store.execute(f"SELECT COUNT(*) FROM {lane.SOURCE_TABLE}").fetchone()[0] == 2


def test_no_ratio_writes_nothing_and_keeps_the_trades_ahead(store) -> None:
    _seed(store)
    lane.ensure_lane_schema(store)
    cfg = {**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS}
    receipt = lane.ingest_source_trades(store, cfg=cfg, now_utc="2026-09-18T14:00:00.000Z",
                                        fetch=Source([_trade()]), ratio_cache={})
    # The account read is 2h old by then: no scale, no row.
    assert receipt["state"] == "ratio_unavailable" and receipt["refusal"] == "account_equity_stale"
    assert store.execute(f"SELECT COUNT(*) FROM {lane.SOURCE_TABLE}").fetchone()[0] == 0


def test_a_side_not_followed_and_a_stale_trade_are_recorded_not_followed(store) -> None:
    _seed(store)
    lane.ensure_lane_schema(store)
    cfg = {**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS, "sides": ["YES"], "max_source_age_seconds": 60}
    lane.ingest_source_trades(store, cfg=cfg, now_utc=NOW, fetch=Source([
        _trade(token="no_28°C", tx="0xno", at="2026-09-18T12:02:00Z"),
        _trade(at="2026-09-18T11:30:00Z", tx="0xstale"),
    ]), ratio_cache={})
    refusals = dict(store.execute(f"SELECT transaction_hash, refusal FROM {lane.SOURCE_TABLE}").fetchall())
    assert refusals == {"0xno": "side_not_followed:NO", "0xstale": "source_trade_stale"}


def test_the_target_adds_buys_removes_sells_and_never_goes_negative(store) -> None:
    _seed(store)
    lane.ensure_lane_schema(store)
    cfg = {**strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS, **SETTINGS}
    lane.ingest_source_trades(store, cfg=cfg, now_utc=NOW, fetch=Source([
        _trade(side="SELL", size=200, price=0.35, at="2026-09-18T11:10:00Z", tx="0x1"),  # pre-start inventory
        _trade(size=400, price=0.30, at="2026-09-18T11:20:00Z", tx="0x2"),
        _trade(side="SELL", size=120, price=0.34, at="2026-09-18T11:30:00Z", tx="0x3"),
    ]), ratio_cache={})
    want = lane.source_targets(store, user=USER, since_event_date="2026-09-17")["yes_28°C"]
    assert want["target"] == pytest.approx(7.0)       # 0 (clipped) + 10 - 3
    assert want["buy_price"] == 0.30 and want["sell_price"] == 0.34


# ---------------------------------------------------------------------------
# The order a gap asks for.


def test_an_ask_at_or_under_the_source_price_is_taken_capped_there() -> None:
    quote = lane.side_quote(_book({"28°C": (0.27, 0.29)})[0], "YES")
    plan = lane.plan_buy(quote, price_cap=0.3049, gap=10.0, min_shares=5.0)
    assert plan == {"order_kind": "take", "limit_price": 0.30, "fok_price_cap": 0.30, "shares": 10.0,
                    "notional_usdc": 3.0}


def test_otherwise_a_rung_rests_at_bid_plus_one_under_the_source_price() -> None:
    quote = lane.side_quote(_book({"28°C": (0.26, 0.34)})[0], "YES")
    assert lane.plan_buy(quote, price_cap=0.30, gap=10.0, min_shares=5.0)["limit_price"] == 0.27
    # The source paid under bid+1: the rung sits at the source's price.
    quote = lane.side_quote(_book({"28°C": (0.30, 0.34)})[0], "YES")
    assert lane.plan_buy(quote, price_cap=0.30, gap=10.0, min_shares=5.0) == {
        "order_kind": "rest", "limit_price": 0.30, "shares": 10.0, "notional_usdc": 3.0}


def test_a_bid_above_the_source_price_waits() -> None:
    quote = lane.side_quote(_book({"28°C": (0.33, 0.35)})[0], "YES")
    assert lane.plan_buy(quote, price_cap=0.30, gap=10.0, min_shares=5.0) == {
        "refusal": "market_above_price_cap"}


def test_a_take_under_the_dollar_minimum_rests_instead() -> None:
    quote = lane.side_quote(_book({"28°C": (0.08, 0.10)})[0], "YES")
    plan = lane.plan_buy(quote, price_cap=0.10, gap=6.0, min_shares=5.0)
    assert plan["order_kind"] == "rest" and plan["limit_price"] == 0.09


def test_a_no_leg_reads_the_mirrored_book() -> None:
    quote = lane.side_quote(_book({"28°C": (0.28, 0.32)})[0], "NO")
    assert quote["bid"] == 0.68 and quote["ask"] == 0.72


def test_a_sell_never_goes_under_the_source_price_nor_past_what_is_held() -> None:
    assert lane.plan_sell(price_floor=0.341, surplus=8.0, sellable=6.0, min_shares=5.0) == {
        "order_kind": "sell", "limit_price": 0.35, "shares": 6.0, "notional_usdc": 2.1}
    assert lane.plan_sell(price_floor=0.34, surplus=8.0, sellable=4.0, min_shares=5.0) == {
        "refusal": "below_venue_minimum"}


# ---------------------------------------------------------------------------
# End to end.


def test_a_followed_buy_is_taken_then_held_and_a_followed_sell_mirrors_it(store) -> None:
    _seed(store, book=_book({"27°C": (0.18, 0.20), "28°C": (0.28, 0.30), "29°C": (0.18, 0.20)}))
    source = Source([_trade(size=400, price=0.30)])
    sent: list = []
    receipt = _run(store, source, sent)
    assert receipt["planned"] == 1 and receipt["filled"] == 1
    (path, payload), = sent
    assert payload["order_type"] == "FOK" and payload["side"] == "BUY" and payload["price"] == 0.30
    assert payload["size"] == 10.0 and payload["token_id"] == "yes_28°C"
    assert lane.lane_position(store, station_id=ST, event_date=ED, token_id="yes_28°C")["held"] == 10.0
    # Held at target: the next pass sends nothing.
    sent.clear()
    assert _run(store, source, sent)["planned"] == 0 and sent == []
    # The source sells half: the lane sells five at the source's price.
    source.trades.append(_trade(side="SELL", size=200, price=0.33, at="2026-09-18T12:01:00Z", tx="0xs"))
    receipt = _run(store, source, sent, wallet=10.0)
    (path, payload), = sent
    assert payload["side"] == "SELL" and payload["order_type"] == "GTD" and payload["post_only"] is False
    assert payload["price"] == 0.33 and payload["size"] == 5.0 and payload["outcome_side"] == "YES"
    assert "expiration" in payload


def test_a_resting_rung_is_post_only_gtd_and_holds_the_token_while_open(store) -> None:
    _seed(store)  # 28°C bid 0.28 / ask 0.32: the source's 0.30 is under the ask
    source = Source([_trade(size=400, price=0.30)])
    sent: list = []
    _run(store, source, sent)
    (path, payload), = sent
    assert payload["order_type"] == "GTD" and payload["post_only"] is True and payload["price"] == 0.29
    intent = json.loads(_intents(store)[0][6])
    assert intent["expiration_basis"] == "rung_horizon_seconds"
    # Fifteen minutes out, past the venue's floor.
    assert payload["expiration"] >= _ts("2026-09-18T12:17:00Z")
    sent.clear()
    receipt = _run(store, source, sent)
    assert sent == [] and receipt["refusals"] == {"order_open": 1}


def test_the_slippage_dial_lets_a_buy_pay_past_the_source_price(store) -> None:
    _seed(store)  # 28°C ask 0.32, two cents over the source's 0.30
    assert strategy_lanes.COPY_FOLLOW_DEFAULT_SETTINGS["max_slippage"] == 0.02
    sent: list = []
    _run(store, Source([_trade(size=400, price=0.30)]), sent, max_slippage=0.02)
    (path, payload), = sent
    assert payload["order_type"] == "FOK" and payload["price"] == 0.32 and payload["fok_price_cap"] == 0.32
    plan = json.loads(_intents(store)[0][6])["plan"]
    assert plan["source_price"] == 0.30


def test_a_no_trade_is_bought_as_no(store) -> None:
    _seed(store)  # NO 27°C: bid 0.80 / ask 0.82
    sent: list = []
    _run(store, Source([_trade(token="no_27°C", size=400, price=0.82)]), sent)
    (path, payload), = sent
    assert payload["outcome_side"] == "NO" and payload["token_id"] == "no_27°C"
    assert payload["order_type"] == "FOK" and payload["price"] == 0.82


def test_the_daily_cost_limit_holds_a_buy(store) -> None:
    _seed(store)
    sent: list = []
    receipt = _run(store, Source([_trade(size=400, price=0.30)]), sent, max_daily_cost_usdc=2.0)
    assert sent == [] and receipt["refusals"] == {"daily_cost_limit": 1}


def test_nothing_is_read_while_the_operator_has_paused(store, tmp_path) -> None:
    _seed(store)
    (tmp_path / "live_paused.flag").write_text("x", encoding="utf-8")
    source = Source([_trade()])
    receipt = _run(store, source, [])
    assert receipt["state"] == "live_paused" and source.calls == []


def test_an_order_that_never_reached_the_venue_waits_before_the_next(store) -> None:
    _seed(store)
    source = Source([_trade(size=400, price=0.30)])
    sent: list = []
    first = _run(store, source, sent, authorized=False)
    assert first["planned"] == 1 and first["submitted"] == 0 and sent == []
    second = _run(store, source, sent, now="2026-09-18T12:03:30.000Z")
    assert second["refusals"] == {"unsent_retry_wait": 1}


# ---------------------------------------------------------------------------
# Wiring.


def test_the_production_cycle_runs_the_lane_at_its_head_and_its_tail() -> None:
    tree = ast.parse((ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8"))
    cycle = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "run_target_production_cycle")
    calls = [n for n in ast.walk(cycle)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "execute_copy_follow"]
    assert len(calls) == 2


def test_the_charter_and_agents_carry_the_ruling() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    clause = next(line for line in charter.splitlines() if line.startswith("256. "))
    for phrase in ("按他已投入仓位算", "source_positions_value", "max_slippage", "缺省关", "第三个允许自动买 NO"):
        assert phrase in clause
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "宪章 256 跟单线" in agents
