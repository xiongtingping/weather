"""Charter 234: the 远尾做空线 -- the market's own far tails, bought NO as a taker.

User rulings 2026-09-28 「上线：只按市场自己的价格结构做空远尾桶」 and
「上线：H1 的纯市场版。并测试持续监控，直至下单成功！」: on a P0 station-day
more than 36 hours from settlement, a bucket the market prices at or under
5%, three or more rungs from its own modal bucket, with a YES bid of at least
2 cents, is bought NO by FOK at 1 - bid.  The model is not read.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import strategy_lanes  # noqa: E402
from bayes_basket_target import tail_short_lane as lane  # noqa: E402
from bayes_basket_target import tiebreak_lane  # noqa: E402
from tests.test_tiebreak_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    ADMIT,
    ST,
    TZ,
    _insert,
    store,
)

ED = "2026-09-20"            # settles 2026-09-20T16:00Z (Asia/Shanghai midnight)
DECIDED = "2026-09-18T12:02:00.000Z"
NOW = "2026-09-18T12:02:30.000Z"   # ~52h to settlement
SETTINGS = {"live_ws_book": False}
#: bucket: (ordinal, bid, ask).  Mode 28°C; 24/25 and 31/32 sit 3+ rungs out.
LADDER = {
    "24°C": (24, 0.02, 0.04), "25°C": (25, 0.03, 0.05), "26°C": (26, 0.07, 0.09),
    "27°C": (27, 0.18, 0.20), "28°C": (28, 0.30, 0.32), "29°C": (29, 0.18, 0.20),
    "30°C": (30, 0.08, 0.10), "31°C": (31, 0.03, 0.05), "32°C": (32, 0.01, 0.03),
}


def _book(ladder=LADDER, bid_size=100.0):
    return [{"bucket": b, "ordinal": o, "ask_available": True, "price_tick_size": 0.01,
             "ask_levels": [{"price": a, "shares": 100.0}], "bid_levels": [{"price": bid, "shares": bid_size}]}
            for b, (o, bid, a) in ladder.items()]


def _seed(conn, *, stage="P0", decided=DECIDED, observed=None, book=None, no_token=True):
    observed = observed or decided
    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": "snap1", "market_id": "m", "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "sequence": 1, "observed_at": observed, "quote_version": "v",
        "quote_observed_at": observed, "quote_expires_at": "2026-09-18T13:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(book if book is not None else _book()), "state": "COMMITTED",
        "created_at": observed,
    })
    _insert(conn, "basket_policy_training_fact", {
        "fact_id": "f1", "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "decision_at_utc": decided, "decision_scope_key": "scope_f1", "captured_at_utc": decided,
        "source_sequence": 1, "quote_observed_at": decided, "quote_expires_at": "2026-09-18T13:00:00Z",
        "stage": stage, "probability_envelope_id": "env_f1", "ws_snapshot_id": "snap1",
        "intent_count": 0, "authority_scope": "PRODUCTION_NO_LIVE_FORWARD", "created_at_utc": decided,
        "cycle_id": "cyc_f1", "decision_id": "dec_f1",
    })
    registry_sha = "regf1".ljust(64, "0")
    _insert(conn, "basket_target_market_registry_snapshot",
            {"snapshot_sha256": registry_sha, "source_scope": "shared_live_registry_export_only"})
    for bucket in LADDER:
        raw = {"order_price_min_tick_size": 0.01, "order_min_size": 5.0}
        if no_token:
            raw["no_token"] = f"no_{bucket}"
        _insert(conn, "basket_target_market_registry", {
            "snapshot_sha256": registry_sha, "slug": f"{ST}-{bucket}", "event_date": ED,
            "outcome": bucket, "city_slug": "shanghai", "station_id": ST, "settlement_timezone": TZ,
            "yes_token": f"yes_{bucket}", "no_token": f"no_{bucket}", "market_active": 1,
            "market_accepting_orders": 1, "registry_json": json.dumps(raw), "refreshed_at": decided,
            "source_eligible": 1, "source_lane": "awc",
        })


def _fill_transport(sent: list, *, fill: bool = True):
    def transport(path, payload):
        sent.append((path, payload))
        if not fill:
            return {"ok": True, "orderID": f"0xkill{len(sent)}", "status": "unmatched"}
        return {"ok": True, "orderID": f"0xfill{len(sent)}", "status": "matched",
                "size_matched": payload["size"],
                "exchange_response": {"makingAmount": round(payload["size"] * payload["price"], 2),
                                      "takingAmount": payload["size"]}}
    return transport


@pytest.fixture(autouse=True)
def _cash(monkeypatch, tmp_path):
    # Hermetic: the operator's own flags on the machine running the tests
    # (a paused book, a disarmed intent, a red egress watch) are not this
    # suite's subject.
    monkeypatch.setattr(lane, "_cash_refusal", lambda conn, now_utc, needed: None)
    monkeypatch.setattr(lane, "PAUSE_FLAG_PATH", tmp_path / "live_paused.flag")
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: True)
    monkeypatch.setattr("bayes_basket_target.lane_orders.egress_send_gate", lambda: {"hold": False})


def _run(conn, sent, *, now=NOW, fill=True, **overrides):
    return lane.execute_tail_short(
        conn, transport=_fill_transport(sent, fill=fill), now_utc=now, live_submit_authorized=True,
        settings={**strategy_lanes.TAIL_SHORT_DEFAULT_SETTINGS, **SETTINGS, **overrides}, admission=ADMIT,
    )


def _intents(conn):
    return conn.execute(
        "SELECT bucket, token_id, side, limit_price, requested_size, state, intent_json"
        " FROM basket_target_order_intents ORDER BY bucket"
    ).fetchall()


# ---------------------------------------------------------------------------
# The rule on one ladder.


def test_the_rule_fires_on_far_cheap_tails_with_a_bid() -> None:
    reading = lane.far_tail_legs(_book(), max_market_price=0.05, min_rungs_from_mode=3, min_yes_bid=0.02)
    assert reading["state"] == "complete" and reading["mode_bucket"] == "28°C"
    # 26°C and 30°C are two rungs out; 32°C bids a cent.
    assert [leg["bucket"] for leg in reading["legs"]] == ["24°C", "25°C", "31°C"]
    assert {leg["bucket"]: leg["rungs_from_mode"] for leg in reading["legs"]} == {"24°C": 4, "25°C": 3, "31°C": 3}


def test_a_tail_the_market_prices_over_five_percent_is_not_shorted() -> None:
    ladder = {**LADDER, "24°C": (24, 0.06, 0.08)}
    reading = lane.far_tail_legs(_book(ladder), max_market_price=0.05, min_rungs_from_mode=3, min_yes_bid=0.02)
    assert "24°C" not in [leg["bucket"] for leg in reading["legs"]]


def test_an_incomplete_book_fires_nothing() -> None:
    book = _book()
    book[0]["bid_levels"], book[0]["ask_levels"] = [], []
    reading = lane.far_tail_legs(book, max_market_price=0.05, min_rungs_from_mode=3, min_yes_bid=0.02)
    assert reading["state"] == "market_incomplete" and reading["legs"] == []


def test_the_no_take_is_priced_at_one_minus_the_bid_within_its_depth() -> None:
    take = lane.no_take(0.03, 100.0, 10.0, tick_size=0.01, min_shares=5.0)
    assert take["price"] == pytest.approx(0.97)
    assert take["shares"] == 10.0 and take["cost_usdc"] == pytest.approx(9.7)
    # A thin bid caps the size with the P1 tail lane's headroom (min of 80%, depth - 1).
    thin = lane.no_take(0.03, 8.0, 10.0, tick_size=0.01, min_shares=5.0)
    assert thin["shares"] == 6.0
    assert lane.no_take(0.03, 5.0, 10.0, tick_size=0.01, min_shares=5.0)["refusal"].startswith("thin_bid")
    assert lane.no_take(0.035, 100.0, 10.0, tick_size=0.01, min_shares=5.0)["refusal"] == "no_price_off_tick"


def test_a_bid_off_the_cent_grid_is_refused_on_a_finer_tick() -> None:
    # Under 4 cents the venue ticks the market in tenths of a cent; the
    # gateway signs on cents, so 0.975 would go out as a price nobody quoted.
    assert lane.no_take(0.025, 100.0, 10.0, tick_size=0.001, min_shares=5.0)["refusal"] == "no_price_off_tick"
    assert lane.no_take(0.02, 100.0, 10.0, tick_size=0.001, min_shares=5.0)["price"] == pytest.approx(0.98)


def test_the_taker_fee_is_polymarkets_weather_formula() -> None:
    assert lane.taker_fee_usdc(0.97, 10.0, 0.05) == pytest.approx(10 * 0.05 * 0.97 * 0.03)


# ---------------------------------------------------------------------------
# The lane end to end.


def test_the_lane_buys_no_by_fok_at_one_minus_the_bid(store) -> None:
    _seed(store)
    sent: list = []
    receipt = _run(store, sent)
    assert receipt["planned"] == 3 and receipt["submitted"] == 3 and receipt["filled"] == 3
    rows = _intents(store)
    assert [r[0] for r in rows] == ["24°C", "25°C", "31°C"]
    for bucket, token, side, price, size, state, body in rows:
        intent = json.loads(body)
        assert side == "NO" and token == f"no_{bucket}" and state == "filled"
        assert intent["execution_candidate"] == "TAKER_FOK" and intent["order_type"] == "FOK"
        assert intent["post_only"] is False and intent["fok_price_cap"] == pytest.approx(price)
        assert price == pytest.approx(1.0 - LADDER[bucket][1])
    assert all(payload["order_type"] == "FOK" and payload["outcome_side"] == "NO" for _p, payload in sent)
    filled = store.execute(
        f"SELECT bucket, sent, filled_shares, fee_usdc_est FROM {lane.EVALUATION_TABLE} WHERE order_intent_id IS NOT NULL"
    ).fetchall()
    assert all(s == 1 and f > 0 and fee > 0 for _b, s, f, fee in filled)


def test_a_filled_bucket_is_not_bought_again(store) -> None:
    _seed(store)
    _run(store, [])
    sent: list = []
    receipt = _run(store, sent, now="2026-09-18T12:02:40.000Z")
    assert receipt["planned"] == 0 and sent == []


def test_a_killed_take_is_retried_up_to_the_attempt_cap(store) -> None:
    _seed(store)
    for i in range(4):
        _run(store, [], fill=False, now=f"2026-09-18T12:02:3{i}.000Z", max_attempts_per_bucket=2)
    sends = store.execute(
        f"SELECT bucket, SUM(sent) FROM {lane.EVALUATION_TABLE} GROUP BY bucket"
    ).fetchall()
    assert dict(sends) == {"24°C": 2, "25°C": 2, "31°C": 2}
    refusals = {r[0] for r in store.execute(f"SELECT refusal FROM {lane.EVALUATION_TABLE}")}
    assert "max_attempts_reached" in refusals


def test_a_killed_take_spends_none_of_the_budget(store) -> None:
    _seed(store)
    _run(store, [], fill=False)
    assert lane.lane_cost_today(store, now_utc=NOW) == 0.0
    receipt = _run(store, [], now="2026-09-18T12:02:40.000Z", max_daily_cost_usdc=15.0)
    assert receipt["planned"] == 1 and receipt["filled"] == 1
    # 10 shares at 0.98 plus the fee estimate.
    assert lane.lane_cost_today(store, now_utc=NOW) == pytest.approx(9.8 + 10 * 0.05 * 0.98 * 0.02)


def test_a_fill_reconciled_after_the_fact_holds_the_bucket(store) -> None:
    _seed(store)
    _run(store, [], fill=False)
    store.execute("UPDATE basket_target_order_intents SET state='filled' WHERE bucket='24°C'")
    receipt = _run(store, [], now="2026-09-18T12:02:40.000Z")
    assert [r["bucket"] for r in receipt["receipts"]] == ["25°C", "31°C"]


def test_nothing_is_written_while_paused_or_disarmed(store, monkeypatch) -> None:
    _seed(store)
    lane.PAUSE_FLAG_PATH.write_text("")
    assert _run(store, [])["state"] == "live_paused"
    lane.PAUSE_FLAG_PATH.unlink()
    monkeypatch.setattr("bayes_basket_target.lane_orders.live_intent_armed", lambda: False)
    assert _run(store, [])["state"] == "live_intent_disarmed"
    assert store.execute(f"SELECT COUNT(*) FROM {lane.EVALUATION_TABLE}").fetchone()[0] == 0
    assert store.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0] == 0


def test_a_send_that_never_reached_the_venue_waits_and_is_bounded(store) -> None:
    _seed(store)
    gateway: list = []

    def refusing(path, payload):
        gateway.append(payload)
        return {"ok": False, "error": "exchange_min_order_size_not_met:5", "exchange_post_started": False}

    def run(now):
        return lane.execute_tail_short(
            store, transport=refusing, now_utc=now, live_submit_authorized=True, admission=ADMIT,
            settings={**strategy_lanes.TAIL_SHORT_DEFAULT_SETTINGS, **SETTINGS,
                      "send_book_fresh_seconds": 86400.0, "max_orders_per_bucket": 2},
        )

    assert run(NOW)["planned"] == 3 and len(gateway) == 3
    waiting = run("2026-09-18T12:02:40.000Z")
    assert waiting["planned"] == 0 and waiting["skipped"] == {"unsent_retry_wait": 3}
    assert run("2026-09-18T12:07:31.000Z")["planned"] == 3
    capped = run("2026-09-18T12:14:10.000Z")
    assert capped["planned"] == 0 and capped["refusals"] == ["max_orders_reached"]
    assert len(gateway) == 6
    assert store.execute("SELECT COUNT(*) FROM basket_target_order_intents").fetchone()[0] == 6


@pytest.mark.parametrize("now,expected", [(NOW, 3), ("2026-09-19T04:30:00.000Z", 0)])
def test_only_more_than_thirty_six_hours_out(store, now, expected) -> None:
    _seed(store, decided=now, observed=now)
    assert _run(store, [], now=now)["planned"] == expected


def test_only_while_the_newest_decision_is_p0(store) -> None:
    _seed(store, stage="P0-M")
    assert _run(store, [])["planned"] == 0


def test_a_stale_book_is_never_taken(store) -> None:
    _seed(store, observed="2026-09-18T12:00:00.000Z")
    receipt = _run(store, [])
    assert receipt["planned"] == 0 and receipt["skipped"] == {"book_stale": 1}


def test_a_missing_no_token_is_refused_once(store) -> None:
    _seed(store, no_token=False)
    _run(store, [])
    _run(store, [], now="2026-09-18T12:02:40.000Z")
    rows = store.execute(
        f"SELECT bucket, refusal FROM {lane.EVALUATION_TABLE} ORDER BY bucket"
    ).fetchall()
    assert rows == [("24°C", "registry_no_token_missing"), ("25°C", "registry_no_token_missing"),
                    ("31°C", "registry_no_token_missing")]


def test_the_daily_budget_bounds_the_lane(store) -> None:
    _seed(store)
    receipt = _run(store, [], max_daily_cost_usdc=15.0)
    assert receipt["planned"] == 1
    assert any(r.startswith("daily_budget:") for r in receipt["refusals"])


def test_the_station_day_leg_cap_bounds_the_lane(store) -> None:
    _seed(store)
    assert _run(store, [], max_legs_per_station_day=2)["planned"] == 2


def test_an_unadmitted_station_is_refused(store) -> None:
    _seed(store)
    receipt = lane.execute_tail_short(
        store, transport=_fill_transport([]), now_utc=NOW, live_submit_authorized=True,
        settings={**strategy_lanes.TAIL_SHORT_DEFAULT_SETTINGS, **SETTINGS},
        admission=lambda station, conn: {"admitted": False, "reason": "live_station_barred"},
    )
    assert receipt["planned"] == 0
    assert receipt["refusals"] == ["station_not_admitted:live_station_barred"]


# ---------------------------------------------------------------------------
# Registration and wiring.


def test_the_lane_is_registered_and_shipped_on() -> None:
    assert strategy_lanes.LANE_TAIL_SHORT in strategy_lanes.KNOWN_LANES
    served = strategy_lanes.TAIL_SHORT_DEFAULT_SETTINGS
    assert served["enabled"] is True and served["rule"] == lane.RULE_V1
    assert (served["max_market_price"], served["min_rungs_from_mode"], served["min_yes_bid"]) == (0.05, 3, 0.02)
    assert served["min_hours_to_settlement"] == 36.0 and served["opening_stages"] == ["P0"]
    assert served["taker_fee_rate"] == 0.05
    assert (served["max_attempts_per_bucket"], served["max_orders_per_bucket"]) == (3, 8)


def test_the_daemon_runs_the_lane_at_the_cycle_tail() -> None:
    source = (ROOT / "scripts" / "weather_basket_yes_daemon.py").read_text()
    called = [node.func.id for node in ast.walk(ast.parse(source))
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert called.count("execute_tail_short") == 1
    assert '"tail_short_lane": tail_short_receipt' in source


def test_stuck_cycles_of_this_lane_are_settled_with_the_others() -> None:
    assert "tail_short_cycle_" in tiebreak_lane.LANE_CYCLE_PREFIXES


def test_the_charter_carries_the_rule() -> None:
    text = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text()
    body = text[text.index("\n234. "):]
    for phrase in ("远尾做空线", "1 − YES 买一", "0.05 × p × (1 − p)", "tail_short_lane"):
        assert phrase in body


def test_the_monitor_reads_the_first_fill_and_stops(store, tmp_path, capsys, monkeypatch) -> None:
    import tail_short_monitor as monitor

    _seed(store)
    before = monitor.one_pass(store)
    assert before["table"] is False and before["fills"] == []
    _run(store, [])
    after = monitor.one_pass(store)
    assert after["table"] is True and after["sends"] == 3 and after["filled_rows"] == 3
    assert [f[2] for f in after["fills"]] == ["24°C", "25°C", "31°C"]
    store.close()
    monkeypatch.setattr(sys, "argv", ["tail_short_monitor.py", "--ssot", str(tmp_path / "target.db"), "--once"])
    assert monitor.main() == 0
    assert "FIRST FILL" in capsys.readouterr().out


def test_the_board_pays_a_no_leg_on_every_bucket_but_its_own() -> None:
    import calibration_dashboard as cd

    def leg(intent, bucket, side, truth):
        return {"order_intent_id": intent, "decision_id": f"tail_short_decision_{ST}_{ED}",
                "bucket": bucket, "side": side, "order_action": "BUY", "state": "filled",
                "filled_size": 10.0, "filled_notional_usdc": 9.8, "requested_size": 10.0,
                "requested_notional_usdc": 9.8, "limit_price": 0.98, "station_id": ST,
                "event_date": ED, "reason": "", "settled_bucket": truth,
                "settled_bucket_source": "paper_ledger", "lane": "tail_short"}

    # The tail did not settle: each NO leg pays its ten shares.
    missed = cd._live_station_day_money([leg("a", "24°C", "NO", "28°C"), leg("b", "31°C", "NO", "28°C")])
    row = missed["rows"][0]
    assert (row["cost_usdc"], row["settlement_usdc"], row["pnl_usdc"]) == (19.6, 20.0, 0.4)
    assert row["basket_covered"] is False
    # The tail settled: that leg pays nothing, the other still pays.
    hit = cd._live_station_day_money([leg("a", "24°C", "NO", "24°C"), leg("b", "31°C", "NO", "24°C")])
    assert (hit["rows"][0]["settlement_usdc"], hit["rows"][0]["pnl_usdc"]) == (10.0, -9.6)
