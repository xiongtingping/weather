"""The 0.998 recycle lane (operator ruling 2026-08-31; repaired 2026-09-17
under "打通0.998卖单"; widened 2026-09-18 under 「直接挂单卖出啊，和收盘没关系
吧」): a held leg the market bids at 0.998 is sold into that bid whenever it
is, and a won leg still held once its day has closed on a settled truth is
sold at 0.998 in the minutes before the venue closes the market -- both
through the shared exit sender.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import certain_win_recycle as lane
from bayes_basket_target import exit_orders
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_reconciliation import record_order_event
from bayes_basket_target.exit_orders import lane_sold_and_open
from bayes_basket_target.inventory_producer import ledger_holdings_by_token

ROOT = Path(__file__).resolve().parents[1]
ST, ED, TZ = "ZUUU", "2026-09-16", "Asia/Shanghai"
DAY_END = "2026-09-16T16:00:00Z"
NOW = "2026-09-16T16:10:00.000Z"
TOKENS = {"25°C": "t25", "26°C": "t26"}
SHA = "a" * 64


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
    """Insert ``values``, filling every other NOT NULL column with a
    type-appropriate placeholder (the charter 144 fixture's helper)."""

    row = dict(values)
    for _cid, name, ctype, notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
        if name in row or not notnull or default is not None:
            continue
        lname = str(name)
        if "sha256" in lname:
            row[name] = SHA
        elif lname.endswith("_json"):
            row[name] = "{}"
        elif str(ctype).upper().startswith(("INT", "REAL", "NUM")):
            row[name] = 1
        else:
            row[name] = "x"
    columns = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        [row[c] for c in columns],
    )


def _bought(conn: sqlite3.Connection, holdings: dict, *, at: str = "2026-09-15T22:15:14Z") -> None:
    """The opening fills the ledger nets a sale against."""

    _insert(conn, "basket_target_execution_cycles", {"cycle_id": "c0", "plan_id": "p0", "station_id": ST, "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket", "requested_reserve_usdc": 1.0, "state": "settled", "created_at": at, "updated_at": at})
    for bucket, size in holdings.items():
        intent_id = f"i_c0_{bucket}"
        _insert(conn, "basket_target_order_intents", {
            "order_intent_id": intent_id, "decision_id": "d0", "cycle_id": "c0", "bucket": bucket,
            "token_id": TOKENS[bucket], "side": "YES", "order_action": "BUY", "limit_price": 0.2,
            "requested_size": size, "requested_notional_usdc": 0.2 * size, "state": "submitted",
            "live_submit_authorized": 1, "idempotency_key": intent_id, "intent_json": "{}", "created_at": at,
        })
        record_order_event(conn, order_intent_id=intent_id, event={
            "event_type": "FILL", "exchange_order_id": f"0x{intent_id}", "observed_at": at,
            "matched_size": float(size), "matched_notional_usdc": 0.2 * float(size), "event_payload": {},
        })


def _registry(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(
        f"CREATE TABLE {table}(station_id TEXT, event_date TEXT, outcome TEXT, yes_token TEXT,"
        " market_accepting_orders INTEGER, registry_json TEXT, refreshed_at TEXT)"
    )


def _registry_row(conn, table, *, bucket, accepting=1, refreshed_at="2026-09-16T09:00:00Z", tick=0.001, min_size=5.0):
    conn.execute(
        f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?)",
        (ST, ED, bucket, TOKENS[bucket], accepting,
         json.dumps({"order_price_min_tick_size": tick, "order_min_size": min_size}), refreshed_at),
    )


def _inventory(conn, holdings: dict, *, observed_at="2026-09-16T15:30:00Z", key="filled_by_bucket") -> None:
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"inv_{observed_at}", ST, ED, "v1", observed_at, 1,
         json.dumps({key: holdings, "token_ids_by_bucket": TOKENS}), SHA, "COMMITTED", observed_at),
    )


def _store(*, tmax_c: float = 25.4, label: bool = False, target_registry: bool = True) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "CREATE TABLE basket_confirmed_inventory_snapshot(inventory_snapshot_id TEXT, station_id TEXT,"
        " event_date TEXT, inventory_version TEXT, observed_at TEXT, complete INTEGER, inventory_json TEXT,"
        " content_sha256 TEXT, state TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth(station_id TEXT, local_date TEXT,"
        " settlement_timezone TEXT, official_tmax_c REAL, source_lane TEXT, qc_status TEXT,"
        " authority_scope TEXT, revision_no INTEGER, observation_ids_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_station_day_settlement_label(station_id TEXT, event_date TEXT,"
        " truth_revision_no INTEGER, settled_bucket TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_history(history_id TEXT, station_id TEXT, event_date TEXT,"
        " state TEXT, decision_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_bridge_bucket_probability(history_id TEXT, bucket TEXT,"
        " ordinal INTEGER, lower_c REAL, upper_c REAL)"
    )
    _registry(conn, "basket_target_market_registry")
    _bought(conn, {"25°C": 6.0, "26°C": 5.0})
    _inventory(conn, {"25°C": 6.0, "26°C": 5.0})
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?)",
        (ST, ED, TZ, tmax_c, "AWC", "ACCEPTED", "FORMAL_NATIVE_ELIGIBLE", 0, "[]"),
    )
    conn.execute("INSERT INTO basket_probability_bridge_history VALUES('h1',?,?,'COMMITTED','2026-09-16T08:00:00Z')", (ST, ED))
    for ordinal, (bucket, low, high) in enumerate((("24°C or below", None, 25.0), ("25°C", 25.0, 26.0), ("26°C", 26.0, 27.0), ("27°C or higher", 27.0, None))):
        conn.execute("INSERT INTO basket_probability_bridge_bucket_probability VALUES('h1',?,?,?,?)", (bucket, ordinal, low, high))
    if label:
        conn.execute("INSERT INTO basket_station_day_settlement_label VALUES(?,?,0,'25°C')", (ST, ED))
    if target_registry:
        for bucket in TOKENS:
            _registry_row(conn, "basket_target_market_registry", bucket=bucket)
    canonical = sqlite3.connect(":memory:")
    _registry(canonical, "reported_trajectory_market_registry")
    canonical.execute("CREATE TABLE reported_trajectory_canonical_station_day_bounds(station_id TEXT, event_date TEXT, settlement_timezone TEXT)")
    canonical.execute("INSERT INTO reported_trajectory_canonical_station_day_bounds VALUES(?,?,?)", (ST, ED, TZ))
    return conn, canonical


def _plans(conn, canonical, now=NOW):
    return lane.plan_certain_win_recycles(conn, canonical, now_utc=now)


def test_the_won_leg_is_sold_at_0998_once_the_day_closes_on_the_settled_truth() -> None:
    conn, canonical = _store()
    plans = _plans(conn, canonical)
    assert [(p["bucket"], p["shares"], p["limit_price"], p["token_id"], p["won_by"]) for p in plans] == [
        ("25°C", 6.0, 0.998, "t25", "truth_on_ladder"),
    ]
    assert plans[0]["tick_size"] == 0.001 and plans[0]["observed_max_c"] == 25.4
    assert plans[0]["order_intent_id"].startswith("recycle998_")
    # The day still open is not a won day, however the afternoon looks.
    assert _plans(conn, canonical, now="2026-09-16T15:50:00.000Z") == []
    # A week after close the tokens have redeemed; nothing to recycle.
    assert _plans(conn, canonical, now="2026-09-24T16:10:00.000Z") == []


def test_the_label_names_the_winner_when_written_and_the_ladder_rule_agrees() -> None:
    conn, canonical = _store(label=True)
    assert [(p["bucket"], p["won_by"]) for p in _plans(conn, canonical)] == [("25°C", "settlement_label")]
    # A truth off every held rung: 26.2C settles 26C, which the book holds too.
    conn2, canonical2 = _store(tmax_c=26.2)
    assert [p["bucket"] for p in _plans(conn2, canonical2)] == ["26°C"]
    # 27.5C settles the open-top rung the book does not hold: nothing won.
    conn3, canonical3 = _store(tmax_c=27.5)
    assert _plans(conn3, canonical3) == []


def test_the_holdings_key_is_filled_by_bucket_and_the_name_the_lane_read_for_a_month_still_reads() -> None:
    conn, canonical = _store()
    assert lane.RULE_VERSION == "certain_win_recycle_bid_or_settled_v3"
    _inventory(conn, {"25°C": 7.0}, observed_at="2026-09-16T15:45:00Z", key="filled_shares_by_bucket")
    assert [(p["bucket"], p["shares"]) for p in _plans(conn, canonical)] == [("25°C", 7.0)]
    _inventory(conn, {"25°C": 8.0}, observed_at="2026-09-16T15:50:00Z")
    assert [(p["bucket"], p["shares"]) for p in _plans(conn, canonical)] == [("25°C", 8.0)]


def test_the_newest_registry_row_wins_and_a_market_the_venue_closed_is_not_sold_into() -> None:
    conn, canonical = _store()
    # The canonical registry recorded the venue closing the market after the target one last looked.
    _registry_row(canonical, "reported_trajectory_market_registry", bucket="25°C", accepting=0, refreshed_at="2026-09-16T16:08:00Z")
    assert _plans(conn, canonical) == []
    # An older closed row does not outrank a newer open one.
    conn2, canonical2 = _store()
    _registry_row(canonical2, "reported_trajectory_market_registry", bucket="25°C", accepting=0, refreshed_at="2026-09-15T16:08:00Z")
    assert [p["bucket"] for p in _plans(conn2, canonical2)] == ["25°C"]


def test_the_token_and_tick_survive_the_target_registry_being_pruned() -> None:
    conn, canonical = _store(target_registry=False)
    _registry_row(canonical, "reported_trajectory_market_registry", bucket="25°C", tick=0.01)
    plans = _plans(conn, canonical)
    assert [(p["token_id"], p["limit_price"], p["tick_size"]) for p in plans] == [("t25", 0.99, 0.01)]
    # Neither registry: the inventory's own token map, at the default tick.
    conn2, canonical2 = _store(target_registry=False)
    assert [(p["token_id"], p["limit_price"]) for p in _plans(conn2, canonical2)] == [("t25", 0.99)]


def test_recycle_price_floors_to_the_tick() -> None:
    assert lane.recycle_price(0.001) == 0.998
    assert lane.recycle_price(0.01) == 0.99
    assert lane.recycle_price(None) == 0.99
    assert lane.recycle_price(0.1) == 0.9


def test_a_leg_under_the_exchange_minimum_is_left_to_settlement() -> None:
    conn, canonical = _store()
    _inventory(conn, {"25°C": 4.0}, observed_at="2026-09-16T15:45:00Z")
    assert _plans(conn, canonical) == []


def test_the_sell_is_a_gtc_limit_not_post_only_recorded_like_any_order_and_the_leg_is_then_sold(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store()
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: True)
    monkeypatch.setattr(exit_orders, "wall_clock_utc", lambda: "2026-09-16T16:10:30.000Z")
    sent: list[dict] = []

    def transport(path, payload):
        sent.append(dict(payload))
        # The bid already stands at 0.999: the limit fills at once, above its price.
        return {"ok": True, "orderID": "0xrecycle25", "status": "matched", "size_matched": payload["size"],
                "exchange_response": {"takingAmount": round(payload["size"] * 0.999, 6)}}

    receipt = lane.execute_certain_win_recycles(conn, canonical, transport=transport, now_utc=NOW, live_submit_authorized=True)
    assert receipt["planned"] == 1 and receipt["submitted"] == 1
    assert sent == [{
        "strategy_id": "weather_basket_yes_v2", "order_intent_id": sent[0]["order_intent_id"],
        "idempotency_key": sent[0]["order_intent_id"], "token_id": "t25", "outcome_side": "YES",
        "side": "SELL", "price": 0.998, "size": 6.0, "order_type": "GTC", "post_only": False,
        "tick_size": 0.001,
    }]
    row = conn.execute(
        "SELECT i.bucket, i.order_action, i.state, r.state, r.filled_size, i.intent_json"
        " FROM basket_target_order_intents i JOIN basket_target_order_reconciliations r"
        " ON r.order_intent_id=i.order_intent_id WHERE i.order_intent_id LIKE 'recycle998_%'"
    ).fetchone()
    assert row[:5] == ("25°C", "SELL", "filled", "resolved_filled", pytest.approx(6.0))
    assert json.loads(row[5])["rule"] == lane.RULE_VERSION
    # The inventory nets the sale.
    holdings = ledger_holdings_by_token(conn, station_id=ST, event_date=ED)["holdings"]
    assert "t25" not in holdings or holdings["t25"]["shares"] < 0.01
    assert lane_sold_and_open(conn, lane="recycle998", station_id=ST, event_date=ED, bucket="25°C") == (pytest.approx(6.0), False)
    # Next cycle: sold, nothing planned.
    assert _plans(conn, canonical, now="2026-09-16T16:20:00.000Z") == []


def test_a_refused_send_is_retried_on_the_next_cycles_then_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store()
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: True)
    monkeypatch.setattr(exit_orders, "wall_clock_utc", lambda: "2026-09-16T16:10:30.000Z")
    calls: list = []
    refuse = lambda path, payload: (calls.append(payload), {"ok": False, "error": "market_closed"})[1]
    for minute in range(lane.MAX_ATTEMPTS_PER_LEG + 2):
        now = f"2026-09-16T16:{10 + minute:02d}:00.000Z"
        lane.execute_certain_win_recycles(conn, canonical, transport=refuse, now_utc=now, live_submit_authorized=True)
    assert len(calls) == lane.MAX_ATTEMPTS_PER_LEG
    assert conn.execute("SELECT COUNT(*) FROM basket_target_order_intents WHERE order_intent_id LIKE 'recycle998_%'").fetchone()[0] == lane.MAX_ATTEMPTS_PER_LEG


def test_an_open_sell_blocks_a_second_and_disarmed_never_reaches_the_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store()
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: True)
    monkeypatch.setattr(exit_orders, "wall_clock_utc", lambda: "2026-09-16T16:10:30.000Z")
    resting = lambda path, payload: {"ok": True, "orderID": "0xrest", "status": "live"}
    lane.execute_certain_win_recycles(conn, canonical, transport=resting, now_utc=NOW, live_submit_authorized=True)
    assert _plans(conn, canonical, now="2026-09-16T16:20:00.000Z") == []
    conn2, canonical2 = _store()
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: False)
    calls: list = []
    receipt = lane.execute_certain_win_recycles(conn2, canonical2, transport=lambda p, payload: calls.append(payload), now_utc=NOW, live_submit_authorized=True)
    assert calls == [] and receipt["submitted"] == 0
    assert receipt["receipts"][0]["reason"] == "live_intent_disarmed"
    assert conn2.execute("SELECT event_type FROM basket_target_order_events WHERE order_intent_id LIKE 'recycle998_%'").fetchall() == [("NEVER_REACHED_EXCHANGE",)]


def test_the_daemon_runs_the_lane_live_on_the_shared_sell_transport() -> None:
    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    hook = source.index("execute_certain_win_recycles(")
    assert 'if PRODUCTION_EXECUTION_MODE == "production_live":' in source[hook - 1200:hook]
    assert "default_transport as _sell_transport" in source[hook - 1200:hook]
    assert "transport=_sell_transport" in source[hook:hook + 400]
    assert '"certain_win_recycle": recycle_receipt' in source


# --- the bid path (2026-09-18: 「直接挂单卖出啊，和收盘没关系吧」) -------------

OPEN_DAY = "2026-09-16T09:30:00.000Z"  # 17:30 in Chengdu: the day is still open


def _book(conn, *, observed_at="2026-09-16T09:25:00.000Z", bids=None, tick=0.001, min_size=5.0, sequence=1):
    """One committed station-day book; ``bids`` maps bucket -> [(price, shares)]."""

    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_official_ws_snapshot(snapshot_id TEXT, station_id TEXT,"
        " event_date TEXT, settlement_timezone TEXT, sequence INTEGER, observed_at TEXT,"
        " buckets_json TEXT, state TEXT)"
    )
    bids = {"25°C": [(0.999, 400.0), (0.998, 50.0), (0.99, 9.0)], "26°C": [(0.001, 30.0)]} if bids is None else bids
    buckets = [
        {"bucket": bucket, "token_id": TOKENS[bucket], "price_tick_size": tick, "min_order_size": min_size,
         "bid_levels": [{"price": p, "shares": s} for p, s in levels]}
        for bucket, levels in bids.items()
    ]
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot VALUES(?,?,?,?,?,?,?,?)",
        (f"snap_{observed_at}_{sequence}", ST, ED, TZ, sequence, observed_at, json.dumps(buckets), "COMMITTED"),
    )


def test_a_leg_the_market_bids_at_0998_is_sold_into_the_bid_before_the_day_closes() -> None:
    conn, canonical = _store()
    _book(conn)
    plans = _plans(conn, canonical, now=OPEN_DAY)
    assert [(p["bucket"], p["trigger"], p["shares"], p["limit_price"], p["token_id"], p["best_bid"]) for p in plans] == [
        ("25°C", lane.TRIGGER_BID, 6.0, 0.998, "t25", 0.999),
    ]
    assert plans[0]["depth_at_price"] == 450.0 and plans[0]["settlement_timezone"] == TZ
    # 26°C is bid at 0.001: the market has not said it won, nothing is sold.


def test_the_bid_path_needs_the_price_a_fresh_book_and_the_depth() -> None:
    # Bid under the price: held, not sold.
    conn, canonical = _store()
    _book(conn, bids={"25°C": [(0.997, 500.0)]})
    assert _plans(conn, canonical, now=OPEN_DAY) == []
    # A book older than fifteen minutes is not a price anyone will pay.
    conn2, canonical2 = _store()
    _book(conn2, observed_at="2026-09-16T09:10:00.000Z")
    assert _plans(conn2, canonical2, now=OPEN_DAY) == []
    # A book written after "now" is not read.
    assert _plans(conn2, canonical2, now="2026-09-16T09:05:00.000Z") == []
    # Depth at the price under the venue minimum: nothing; above it, sized to the depth.
    conn3, canonical3 = _store()
    _book(conn3, bids={"25°C": [(0.999, 4.0), (0.99, 500.0)]})
    assert _plans(conn3, canonical3, now=OPEN_DAY) == []
    conn4, canonical4 = _store()
    _book(conn4, bids={"25°C": [(0.999, 5.5)]})
    assert [p["shares"] for p in _plans(conn4, canonical4, now=OPEN_DAY)] == [5.5]
    # A whole-cent holding is offered whole (12.46 * 100 is 1245.999... in binary).
    conn5, canonical5 = _store()
    _inventory(conn5, {"25°C": 12.46}, observed_at="2026-09-16T15:45:00Z")
    _book(conn5)
    assert [p["shares"] for p in _plans(conn5, canonical5, now=OPEN_DAY)] == [12.46]


def test_the_bid_path_sends_a_short_gtd_and_the_remainder_is_offered_again(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store()
    _book(conn, bids={"25°C": [(0.999, 5.0)]})
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: True)
    monkeypatch.setattr(lane, "wall_clock_utc", lambda: "2026-09-16T09:30:10.000Z")
    monkeypatch.setattr(exit_orders, "wall_clock_utc", lambda: "2026-09-16T09:30:10.000Z")
    sent: list[dict] = []

    def transport(path, payload):
        sent.append(dict(payload))
        return {"ok": True, "orderID": f"0x{len(sent)}", "status": "matched", "size_matched": payload["size"],
                "exchange_response": {"takingAmount": round(payload["size"] * 0.999, 6)}}

    receipt = lane.execute_certain_win_recycles(conn, canonical, transport=transport, now_utc=OPEN_DAY, live_submit_authorized=True)
    assert receipt["planned"] == 1 and receipt["planned_into_bid"] == 1 and receipt["submitted"] == 1
    assert sent[0]["order_type"] == "GTD" and sent[0]["post_only"] is False
    assert sent[0]["side"] == "SELL" and sent[0]["price"] == 0.998 and sent[0]["size"] == 5.0
    assert sent[0]["expiration"] == 1789551010 + lane.GTD_HORIZON_SECONDS  # 09:30:10Z + 300 s
    intent = json.loads(conn.execute(
        "SELECT intent_json FROM basket_target_order_intents WHERE order_intent_id LIKE 'recycle998_%'"
    ).fetchone()[0])
    assert intent["execution_candidate"] == "EXIT_GTD_LIMIT" and intent["plan"]["trigger"] == lane.TRIGGER_BID
    # The order filled and closed: nothing of this lane is left open to hold the day's quotes.
    assert conn.execute(
        "SELECT state FROM basket_target_order_intents WHERE order_intent_id LIKE 'recycle998_%'"
    ).fetchall() == [("filled",)]
    # The inventory has not netted the sale yet: 1 of 6 left, under the minimum -- left to settle.
    _book(conn, observed_at="2026-09-16T09:35:00.000Z", bids={"25°C": [(0.999, 500.0)]}, sequence=2)
    assert _plans(conn, canonical, now="2026-09-16T09:36:00.000Z") == []
    # Bought 6 more (12 held, 5 sold and not yet netted): 7 offered.
    _inventory(conn, {"25°C": 12.0, "26°C": 5.0}, observed_at="2026-09-16T15:40:00Z")
    assert [p["shares"] for p in _plans(conn, canonical, now="2026-09-16T09:36:00.000Z")] == [7.0]


def test_a_sale_the_inventory_has_netted_is_not_taken_off_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store()
    _book(conn, bids={"25°C": [(0.999, 5.0)]})
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: True)
    monkeypatch.setattr(exit_orders, "wall_clock_utc", lambda: "2026-09-16T09:30:10.000Z")
    lane.execute_certain_win_recycles(
        conn, canonical, now_utc=OPEN_DAY, live_submit_authorized=True,
        transport=lambda path, payload: {"ok": True, "orderID": "0xa", "status": "matched",
                                         "size_matched": payload["size"]},
    )
    sold_id = conn.execute(
        "SELECT order_intent_id FROM basket_target_order_intents WHERE order_intent_id LIKE 'recycle998_%'"
    ).fetchone()[0]
    # The inventory caught up: 11 held after the 5 sold, and its lineage names the sale.
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("inv_late", ST, ED, "v2", "2026-09-16T15:40:00Z", 1,
         json.dumps({"filled_by_bucket": {"25°C": 11.0}, "token_ids_by_bucket": TOKENS,
                     "lineage": [{"order_intent_id": sold_id, "order_action": "SELL", "filled_size": 5.0}]}),
         SHA, "COMMITTED", "2026-09-16T15:40:00Z"),
    )
    _book(conn, observed_at="2026-09-16T09:35:00.000Z", bids={"25°C": [(0.999, 500.0)]}, sequence=2)
    assert [p["shares"] for p in _plans(conn, canonical, now="2026-09-16T09:36:00.000Z")] == [11.0]


def test_bid_attempts_do_not_spend_the_settled_paths_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store()
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: True)
    monkeypatch.setattr(exit_orders, "wall_clock_utc", lambda: "2026-09-16T09:30:10.000Z")
    refuse = lambda path, payload: {"ok": False, "error": "book_moved"}
    for minute in range(lane.MAX_BID_ATTEMPTS_PER_LEG + 2):
        now = f"2026-09-16T09:{30 + minute:02d}:00.000Z"
        _book(conn, observed_at=f"2026-09-16T09:{29 + minute:02d}:00.000Z", sequence=minute + 1)
        lane.execute_certain_win_recycles(conn, canonical, transport=refuse, now_utc=now, live_submit_authorized=True)
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents WHERE order_intent_id LIKE 'recycle998_%'"
    ).fetchone()[0] == lane.MAX_BID_ATTEMPTS_PER_LEG
    # After the close, with no fresh book, the settled path still has all of its own.
    plans = _plans(conn, canonical)
    assert [(p["bucket"], p["trigger"]) for p in plans] == [("25°C", lane.TRIGGER_SETTLED)]
