"""The recycle lane: held YES/NO legs sell at exactly 0.998 on a fresh fine
tick, after any older 0.99 offer is confirmed absent. A resting offer shuts
neither its token nor its station-day's quotes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import certain_win_recycle as lane
from bayes_basket_target import exit_orders
from bayes_basket_target.atomic_ledger import apply_target_ledger_schema
from bayes_basket_target.execution_reconciliation import (
    RESTING_RECYCLE_SELL_PREFIX,
    can_submit_next_order,
    record_order_event,
)
from bayes_basket_target.inventory_producer import ledger_holdings_by_token

ROOT = Path(__file__).resolve().parents[1]
ST, ED, TZ = "ZUUU", "2026-09-16", "Asia/Shanghai"
NOW = "2026-09-16T09:30:00.000Z"  # 17:30 in Chengdu: the day is still open
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


def _inventory(conn, holdings: dict, *, observed_at="2026-09-16T09:00:00Z", key="filled_by_bucket", lineage=None) -> None:
    body = {key: holdings, "token_ids_by_bucket": TOKENS}
    if lineage is not None:
        body["lineage"] = lineage
    conn.execute(
        "INSERT INTO basket_confirmed_inventory_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"inv_{observed_at}", ST, ED, "v1", observed_at, 1, json.dumps(body), SHA, "COMMITTED", observed_at),
    )


def _book(conn, *, observed_at="2026-09-16T09:29:30.000Z", bids=None, tick=0.001, sequence=1):
    """One committed station-day book; ``bids`` maps bucket -> [(price, shares)]."""

    conn.execute(
        "CREATE TABLE IF NOT EXISTS basket_official_ws_snapshot(snapshot_id TEXT, station_id TEXT,"
        " event_date TEXT, settlement_timezone TEXT, sequence INTEGER, observed_at TEXT,"
        " buckets_json TEXT, state TEXT)"
    )
    bids = {"25°C": [(0.999, 400.0)], "26°C": [(0.001, 30.0)]} if bids is None else bids
    buckets = [
        {"bucket": bucket, "token_id": TOKENS[bucket], "price_tick_size": tick, "min_order_size": 5.0,
         "bid_levels": [{"price": p, "shares": s} for p, s in levels]}
        for bucket, levels in bids.items()
    ]
    conn.execute(
        "INSERT INTO basket_official_ws_snapshot VALUES(?,?,?,?,?,?,?,?)",
        (f"snap_{observed_at}_{sequence}", ST, ED, TZ, sequence, observed_at, json.dumps(buckets), "COMMITTED"),
    )


def _store(*, target_registry: bool = True, holdings: dict | None = None) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    holdings = {"25°C": 6.0, "26°C": 5.0} if holdings is None else holdings
    conn = sqlite3.connect(":memory:")
    apply_target_ledger_schema(conn)
    conn.execute(
        "CREATE TABLE basket_confirmed_inventory_snapshot(inventory_snapshot_id TEXT, station_id TEXT,"
        " event_date TEXT, inventory_version TEXT, observed_at TEXT, complete INTEGER, inventory_json TEXT,"
        " content_sha256 TEXT, state TEXT, created_at TEXT)"
    )
    _registry(conn, "basket_target_market_registry")
    _bought(conn, holdings)
    _inventory(conn, holdings)
    _book(conn, observed_at="2026-09-16T09:29:00.000Z", sequence=0)
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


def _armed(monkeypatch, at="2026-09-16T09:30:10.000Z"):
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: True)
    monkeypatch.setattr(exit_orders, "wall_clock_utc", lambda: at)


def _resting(path, payload):
    return {"ok": True, "orderID": f"0x{payload['order_intent_id'][-8:]}", "status": "live"}


def _matched(path, payload):
    return {"ok": True, "orderID": f"0x{payload['order_intent_id'][-8:]}", "status": "matched",
            "size_matched": payload["size"],
            "exchange_response": {"takingAmount": round(payload["size"] * 0.999, 6)}}


def test_every_eligible_held_leg_is_offered_at_0998_while_its_day_is_open() -> None:
    conn, canonical = _store()
    _book(conn)
    plans = _plans(conn, canonical)
    assert lane.RULE_VERSION == "certain_win_recycle_resting_0998_v5"
    assert [(p["bucket"], p["shares"], p["limit_price"], p["token_id"], p["best_bid"]) for p in plans] == [
        ("25°C", 6.0, 0.998, "t25", 0.999),  # the leg the market bids highest goes first
        ("26°C", 5.0, 0.998, "t26", 0.001),
    ]
    assert all(p["settlement_timezone"] == TZ and p["order_intent_id"].startswith("recycle998_") for p in plans)
    # No current official tick: do not guess from the registry.
    conn2, canonical2 = _store()
    conn2.execute("DELETE FROM basket_official_ws_snapshot")
    assert _plans(conn2, canonical2) == []


def test_no_holding_recycles_its_own_token_once_and_never_the_yes_token(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store(holdings={})
    conn.execute(
        "UPDATE basket_target_market_registry SET registry_json=? WHERE outcome='25°C'",
        (json.dumps({"no_token": "no_t25", "order_min_size": 5,
                     "order_price_min_tick_size": 0.01}),),
    )
    _insert(conn, "basket_target_execution_cycles", {
        "cycle_id": "no_cycle", "plan_id": "no_plan", "station_id": ST,
        "event_date": ED, "settlement_timezone": TZ, "strategy_lane": "basket",
        "requested_reserve_usdc": 5.76, "state": "settled", "created_at": NOW,
        "updated_at": NOW,
    })
    _insert(conn, "basket_target_order_intents", {
        "order_intent_id": "tail_short_buy_no", "decision_id": "tail_short_decision_test",
        "cycle_id": "no_cycle", "bucket": "25°C", "token_id": "no_t25",
        "side": "NO", "order_action": "BUY", "limit_price": 0.96,
        "requested_size": 6.0, "requested_notional_usdc": 5.76,
        "state": "submitted", "live_submit_authorized": 1,
        "idempotency_key": "tail_short_buy_no", "intent_json": "{}", "created_at": NOW,
    })
    record_order_event(conn, order_intent_id="tail_short_buy_no", event={
        "event_type": "FILL", "exchange_order_id": "0xno_buy", "observed_at": NOW,
        "matched_size": 6.0, "matched_notional_usdc": 5.76, "event_payload": {},
    })
    conn.execute("UPDATE basket_target_market_registry SET registry_json=? WHERE outcome='25°C'",
                 (json.dumps({"no_token": "a_different_token", "order_min_size": 5}),))
    assert _plans(conn, canonical) == []  # no guessed token from a mismatched registry
    conn.execute("UPDATE basket_target_market_registry SET registry_json=? WHERE outcome='25°C'",
                 (json.dumps({"no_token": "no_t25", "order_min_size": 5,
                              "order_price_min_tick_size": 0.01}),))
    plans = _plans(conn, canonical)
    assert [(p["side"], p["token_id"], p["shares"], p["limit_price"]) for p in plans] == [
        ("NO", "no_t25", 6.0, 0.998),
    ]
    _armed(monkeypatch)
    calls = []
    lane.execute_certain_win_recycles(
        conn, canonical, now_utc=NOW, live_submit_authorized=True,
        position_reader=lambda _token: {"ok": True, "shares": 6.0},
        transport=lambda _path, payload: calls.append(payload) or _resting(_path, payload),
    )
    assert len(calls) == 1
    assert calls[0]["token_id"] == "no_t25"
    assert calls[0]["outcome_side"] == "NO"
    assert calls[0]["side"] == "SELL"
    assert conn.execute(
        "SELECT side FROM basket_target_order_intents WHERE order_intent_id LIKE 'recycle998_%'"
    ).fetchone()[0] == "NO"
    assert _plans(conn, canonical) == []  # the standing sell already offers all six
    conn.execute("UPDATE basket_target_order_reconciliations SET filled_size=2,"
                 " filled_notional_usdc=1.98,state='pending' WHERE order_intent_id LIKE 'recycle998_%'")
    assert _plans(conn, canonical) == []  # unresolved partial: two sold, four still offered


def test_0998_requires_an_exact_fine_tick() -> None:
    assert lane.recycle_price(0.001) == 0.998
    for tick in (0.01, 0.1, None, 0, float("nan")):
        with pytest.raises(ValueError):
            lane.recycle_price(tick)
    conn, canonical = _store()
    conn.execute("DELETE FROM basket_official_ws_snapshot")
    _book(conn, tick=0.01)
    assert _plans(conn, canonical) == []


@pytest.mark.parametrize("status,matched,expected,remaining", [
    ("canceled", 0.0, "cancelled", 10.0),
    ("canceled", 2.0, "cancelled", 8.0),
    ("live", 0.0, "submitted", 0.0),
    ("unknown", 0.0, "submitted", 0.0),
])
def test_old_099_offer_is_released_only_after_exchange_confirmation(
    monkeypatch: pytest.MonkeyPatch, status: str, matched: float,
    expected: str, remaining: float,
) -> None:
    conn, canonical = _store(holdings={"25°C": 10.0})
    _armed(monkeypatch)
    exit_orders.send_exit_sell(
        conn, lane="recycle998", intent_id="recycle998_old_price_test",
        station_id=ST, event_date=ED, settlement_timezone=TZ,
        bucket="25°C", token_id="t25", price=0.99, shares=10.0,
        order_type="GTC", post_only=False, tick_size=0.01,
        intent_body={"rule": "certain_win_recycle_resting_099_v4"},
        transport=lambda _path, _payload: {"ok": True, "orderID": "0xold", "status": "live"},
        live_submit_authorized=True,
    )

    class Client:
        def cancel_order(self, _payload):
            return {"ok": True}

        def lookup_exchange_order(self, _payload):
            return {"ok": True, "status": status,
                    "response": {"id": "0xold", "status": status,
                                 "size_matched": matched}}

    receipt = lane.retire_old_price_orders(conn, now_utc=NOW, client=Client())
    state = conn.execute(
        "SELECT state FROM basket_target_order_intents WHERE order_intent_id='recycle998_old_price_test'"
    ).fetchone()[0]
    assert state == expected
    assert receipt["closed"] == (1 if expected == "cancelled" else 0)
    assert sum(p["shares"] for p in _plans(conn, canonical)) == pytest.approx(remaining)


def test_old_price_cancel_reply_without_terminal_lookup_remains_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, canonical = _store(holdings={"25°C": 10.0})
    _armed(monkeypatch)
    exit_orders.send_exit_sell(
        conn, lane="recycle998", intent_id="recycle998_unknown_cancel_test",
        station_id=ST, event_date=ED, settlement_timezone=TZ,
        bucket="25°C", token_id="t25", price=0.99, shares=10.0,
        order_type="GTC", post_only=False, tick_size=0.01,
        intent_body={"rule": "certain_win_recycle_resting_099_v4"},
        transport=lambda _path, _payload: {"ok": True, "orderID": "0xunknown", "status": "live"},
        live_submit_authorized=True,
    )

    class Client:
        def cancel_order(self, _payload):
            return {"ok": True}

        def lookup_exchange_order(self, _payload):
            return {"ok": True, "status": "cancelled",
                    "status_inferred_from_not_found": True}

    receipt = lane.retire_old_price_orders(conn, now_utc=NOW, client=Client())
    assert receipt["closed"] == 0
    assert receipt["pending"][0]["reason"] == "lookup_not_found_after_cancel"
    assert _plans(conn, canonical) == []


def test_a_market_the_venue_closed_is_not_offered_and_the_newest_registry_row_decides() -> None:
    conn, canonical = _store()
    _registry_row(canonical, "reported_trajectory_market_registry", bucket="25°C", accepting=0, refreshed_at="2026-09-16T16:08:00Z")
    assert [p["bucket"] for p in _plans(conn, canonical)] == ["26°C"]
    conn2, canonical2 = _store()
    _registry_row(canonical2, "reported_trajectory_market_registry", bucket="25°C", accepting=0, refreshed_at="2026-09-15T16:08:00Z")
    assert [p["bucket"] for p in _plans(conn2, canonical2)] == ["25°C", "26°C"]


def test_the_token_survives_the_target_registry_being_pruned() -> None:
    conn, canonical = _store(target_registry=False)
    assert [(p["bucket"], p["token_id"], p["limit_price"]) for p in _plans(conn, canonical)] == [
        ("25°C", "t25", 0.998), ("26°C", "t26", 0.998),
    ]


def test_holdings_under_the_minimum_and_old_station_days_are_left_to_settle() -> None:
    conn, canonical = _store(holdings={"25°C": 4.0, "26°C": 5.0})
    assert [p["bucket"] for p in _plans(conn, canonical)] == ["26°C"]
    # Four days after the date the market is long closed: not read at all.
    assert _plans(conn, canonical, now="2026-09-20T09:30:00.000Z") == []


def test_the_holdings_key_is_filled_by_bucket_and_the_old_name_still_reads() -> None:
    conn, canonical = _store()
    _inventory(conn, {"25°C": 7.0}, observed_at="2026-09-16T09:10:00Z", key="filled_shares_by_bucket")
    assert [(p["bucket"], p["shares"]) for p in _plans(conn, canonical)] == [("25°C", 7.0)]
    # A whole-cent holding is offered whole (12.46 * 100 is 1245.999... in binary).
    _inventory(conn, {"25°C": 12.46}, observed_at="2026-09-16T09:20:00Z")
    assert [(p["bucket"], p["shares"]) for p in _plans(conn, canonical)] == [("25°C", 12.46)]


def test_the_sell_is_a_gtc_limit_at_0998_not_post_only_and_a_standing_bid_fills_it(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store(holdings={"25°C": 6.0})
    _book(conn, bids={"25°C": [(0.999, 400.0)]})
    _armed(monkeypatch)
    sent: list[dict] = []
    receipt = lane.execute_certain_win_recycles(
        conn, canonical, now_utc=NOW, live_submit_authorized=True,
        transport=lambda path, payload: (sent.append(dict(payload)), _matched(path, payload))[1],
    )
    assert receipt["planned"] == 1 and receipt["sent"] == 1 and receipt["submitted"] == 1
    assert sent == [{
        "strategy_id": "weather_basket_yes_v2", "order_intent_id": sent[0]["order_intent_id"],
        "idempotency_key": sent[0]["order_intent_id"], "token_id": "t25", "outcome_side": "YES",
        "side": "SELL", "price": 0.998, "size": 6.0, "order_type": "GTC", "post_only": False,
        "tick_size": 0.001,
    }]
    row = conn.execute(
        "SELECT i.state, r.state, r.filled_size, i.intent_json FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_intent_id LIKE 'recycle998_%'"
    ).fetchone()
    assert row[:3] == ("filled", "resolved_filled", pytest.approx(6.0))
    assert json.loads(row[3])["rule"] == lane.RULE_VERSION
    holdings = ledger_holdings_by_token(conn, station_id=ST, event_date=ED)["holdings"]
    assert "t25" not in holdings or holdings["t25"]["shares"] < 0.01
    # Sold, and the inventory has not caught up: nothing offered again.
    assert _plans(conn, canonical, now="2026-09-16T09:40:00.000Z") == []


def test_a_resting_sell_covers_its_shares_and_new_shares_get_their_own(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store(holdings={"25°C": 6.0})
    _armed(monkeypatch)
    lane.execute_certain_win_recycles(conn, canonical, now_utc=NOW, live_submit_authorized=True, transport=_resting)
    state = conn.execute(
        "SELECT i.state, r.state FROM basket_target_order_intents i JOIN basket_target_order_reconciliations r"
        " ON r.order_intent_id=i.order_intent_id WHERE i.order_intent_id LIKE 'recycle998_%'"
    ).fetchall()
    assert state == [("submitted", "pending")]
    assert _plans(conn, canonical, now="2026-09-16T09:40:00.000Z") == []
    # Six more bought: those six are offered by a second sell, and the first
    # one resting on the same token does not shut the sender's gate.
    _inventory(conn, {"25°C": 12.0}, observed_at="2026-09-16T09:35:00Z")
    _book(conn, observed_at="2026-09-16T09:39:30.000Z", sequence=2)
    plans = _plans(conn, canonical, now="2026-09-16T09:40:00.000Z")
    assert [(p["bucket"], p["shares"], p["offered_before_shares"]) for p in plans] == [("25°C", 6.0, 6.0)]
    _armed(monkeypatch, at="2026-09-16T09:40:10.000Z")
    receipt = lane.execute_certain_win_recycles(
        conn, canonical, now_utc="2026-09-16T09:40:00.000Z", live_submit_authorized=True, transport=_resting,
    )
    assert receipt["submitted"] == 1, receipt


def test_a_sale_the_inventory_has_netted_is_not_taken_off_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store(holdings={"25°C": 16.0})
    _armed(monkeypatch)
    lane.execute_certain_win_recycles(
        conn, canonical, now_utc=NOW, live_submit_authorized=True,
        transport=lambda path, payload: {**_matched(path, payload), "size_matched": 5.0,
                                         "exchange_response": {"takingAmount": 4.995}},
    )
    sold_id = conn.execute(
        "SELECT order_intent_id FROM basket_target_order_intents WHERE order_intent_id LIKE 'recycle998_%'"
    ).fetchone()[0]
    # The inventory caught up: 11 held after the 5 sold, and its lineage names the sale.
    _inventory(conn, {"25°C": 11.0}, observed_at="2026-09-16T09:35:00Z",
               lineage=[{"order_intent_id": sold_id, "order_action": "SELL", "filled_size": 5.0}])
    offered = sum(p["shares"] for p in _plans(conn, canonical, now="2026-09-16T09:40:00.000Z"))
    # Whatever the partial fill left resting is still offered by it; the rest
    # of the 11 is offered anew -- never 11 - 5 again.
    resting_left = conn.execute(
        "SELECT i.requested_size - r.filled_size FROM basket_target_order_intents i"
        " JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id"
        " WHERE i.order_intent_id=? AND i.state IN ('submitted','reconcile_required')", (sold_id,),
    ).fetchone()
    assert offered == pytest.approx(11.0 - (resting_left[0] if resting_left else 0.0))


def test_a_refused_send_is_retried_then_left_and_a_missing_balance_stops_it_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store(holdings={"25°C": 6.0})
    calls: list = []
    refuse = lambda path, payload: (calls.append(payload), {"ok": False, "error": "service_not_ready"})[1]
    for minute in range(lane.MAX_FAILED_SENDS_PER_LEG + 2):
        _armed(monkeypatch, at=f"2026-09-16T09:{30 + minute:02d}:10.000Z")
        _book(conn, observed_at=f"2026-09-16T09:{30 + minute:02d}:00.000Z", sequence=minute + 2)
        lane.execute_certain_win_recycles(
            conn, canonical, now_utc=f"2026-09-16T09:{30 + minute:02d}:00.000Z",
            live_submit_authorized=True, transport=refuse,
        )
    assert len(calls) == lane.MAX_FAILED_SENDS_PER_LEG
    # The account does not hold what the ledger says (RKPK 27C 2026-09-18).
    conn2, canonical2 = _store(holdings={"25°C": 34.0})
    calls2: list = []
    no_balance = lambda path, payload: (calls2.append(payload), {
        "ok": False, "error": "not enough balance / allowance: the balance is not enough -> balance: 0, order amount: 34000000",
    })[1]
    for minute in range(3):
        _armed(monkeypatch, at=f"2026-09-16T09:{30 + minute:02d}:10.000Z")
        _book(conn2, observed_at=f"2026-09-16T09:{30 + minute:02d}:00.000Z", sequence=minute + 2)
        lane.execute_certain_win_recycles(
            conn2, canonical2, now_utc=f"2026-09-16T09:{30 + minute:02d}:00.000Z",
            live_submit_authorized=True, transport=no_balance,
        )
    assert len(calls2) == 1


def test_a_cycle_sends_at_most_its_cap_highest_bid_first(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store()
    _book(conn)
    _armed(monkeypatch)
    sent: list = []
    receipt = lane.execute_certain_win_recycles(
        conn, canonical, now_utc=NOW, live_submit_authorized=True, max_sends=1,
        transport=lambda path, payload: (sent.append(payload["token_id"]), _resting(path, payload))[1],
    )
    assert sent == ["t25"] and receipt["deferred_to_next_cycle"] == 1
    assert lane.MAX_SENDS_PER_CYCLE == 8


def test_disarmed_never_reaches_the_exchange(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, canonical = _store(holdings={"25°C": 6.0})
    monkeypatch.setattr(exit_orders, "live_intent_armed", lambda: False)
    calls: list = []
    before = conn.total_changes
    receipt = lane.execute_certain_win_recycles(conn, canonical, transport=lambda p, payload: calls.append(payload), now_utc=NOW, live_submit_authorized=True)
    assert calls == [] and receipt["submitted"] == 0
    assert receipt["state"] == "live_execution_paused" and receipt["receipts"] == []
    assert conn.total_changes == before


def test_a_resting_recycle_sell_does_not_shut_its_token_but_an_unknown_one_does(monkeypatch: pytest.MonkeyPatch) -> None:
    assert RESTING_RECYCLE_SELL_PREFIX == lane.INTENT_PREFIX + "_"
    conn, canonical = _store(holdings={"25°C": 6.0})
    _armed(monkeypatch)
    lane.execute_certain_win_recycles(conn, canonical, now_utc=NOW, live_submit_authorized=True, transport=_resting)
    assert can_submit_next_order(conn, bucket="25°C", token_id="t25")["allowed"] is True
    # Nobody knows whether it is at the venue: it blocks like anything else.
    conn2, canonical2 = _store(holdings={"25°C": 6.0})
    lane.execute_certain_win_recycles(
        conn2, canonical2, now_utc=NOW, live_submit_authorized=True,
        transport=lambda path, payload: {"ok": False, "transport_ambiguous": True, "error": "timeout"},
    )
    gate = can_submit_next_order(conn2, bucket="25°C", token_id="t25")
    assert gate["allowed"] is False, gate


def test_the_daemon_runs_the_lane_live_on_the_shared_sell_transport() -> None:
    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text(encoding="utf-8")
    hook = source.index("execute_certain_win_recycles(")
    assert 'if PRODUCTION_EXECUTION_MODE == "production_live":' in source[hook - 1200:hook]
    assert "default_transport as _sell_transport" in source[hook - 1200:hook]
    assert "transport=_sell_transport" in source[hook:hook + 400]
    assert '"certain_win_recycle": recycle_receipt' in source


def test_each_sell_is_sized_to_what_the_account_holds_outside_the_ledgers_knowledge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The account is traded by hand too (2026-09-18: all eleven ledger-venue
    differences since 09-01 were trades no lane made -- RKPK 27C's 34 shares,
    bought by the ladder on 09-16, were sold by hand at 0.999 at 08:51Z)."""

    monkeypatch.setattr(lane, "_ACCOUNT_SHORT", {})
    _armed(monkeypatch)
    # The ledger holds 12; the account 7 of them: 7 offered.
    conn, canonical = _store(holdings={"25°C": 12.0})
    sent: list = []
    receipt = lane.execute_certain_win_recycles(
        conn, canonical, now_utc=NOW, live_submit_authorized=True,
        position_reader=lambda token: {"ok": True, "shares": 7.0},
        transport=lambda path, payload: (sent.append(payload["size"]), _resting(path, payload))[1],
    )
    assert sent == [7.0] and receipt["submitted"] == 1
    plan = json.loads(conn.execute(
        "SELECT intent_json FROM basket_target_order_intents WHERE order_intent_id LIKE 'recycle998_%'"
    ).fetchone()[0])["plan"]
    assert plan["ledger_shares"] == 12.0 and plan["capped_by_account_shares"] == 7.0
    # The account holds none: nothing is sent, and the leg is not asked again for a while.
    conn2, canonical2 = _store(holdings={"25°C": 6.0})
    reads: list = []
    calls: list = []
    for minute in range(2):
        receipt = lane.execute_certain_win_recycles(
            conn2, canonical2, now_utc=f"2026-09-16T09:{30 + minute:02d}:00.000Z", live_submit_authorized=True,
            position_reader=lambda token: (reads.append(token), {"ok": True, "shares": 0.0})[1],
            transport=lambda path, payload: calls.append(payload),
        )
        assert receipt["sent"] == 0 and receipt["receipts"][0]["state"] == "skipped"
    assert calls == [] and reads == ["t25"]
    # A position read that fails leaves the venue to check the balance itself.
    monkeypatch.setattr(lane, "_ACCOUNT_SHORT", {})
    conn3, canonical3 = _store(holdings={"25°C": 6.0})
    sent3: list = []
    lane.execute_certain_win_recycles(
        conn3, canonical3, now_utc=NOW, live_submit_authorized=True,
        position_reader=lambda token: {"ok": False, "error": "gateway_transport:TimeoutError"},
        transport=lambda path, payload: (sent3.append(payload["size"]), _resting(path, payload))[1],
    )
    assert sent3 == [6.0]
