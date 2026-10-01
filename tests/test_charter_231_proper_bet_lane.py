"""Charter 231: the Proper 下注线 replaces the basket's betting rule.

User rulings 2026-09-26 「调整下注规则」 (arXiv 2607.06166, LogScore),
「实盘上线，p＝天气模型」, then v2: 「吃单改为挂单，post-only，best bid+1，挂单
周期最短，重新挂单，直至成交，但是挂单价格要符合下注规则」, 「proper线还是按照
>36h，仅p0阶段下单」, 「预算提高到最低要求」: the weather model's served vector
``p`` against the market's normalised mid ladder ``q`` gives
``s = log(p / q)`` on every bucket; each leg rests post-only at bid+1t of
its own side's book, re-posted at the venue's minimum horizon until it
fills, only at a price under its claim; ``lambda`` is the smallest the
venue allows for the whole position.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from bayes_basket_target import order_submission as sub  # noqa: E402
from bayes_basket_target import proper_bet_lane as lane  # noqa: E402
from bayes_basket_target import strategy_lanes  # noqa: E402
from bayes_basket_target.execution_reconciliation import record_order_event  # noqa: E402
from bayes_basket_target.inventory_producer import _ledger_fill_rows  # noqa: E402
from bayes_basket_target.resting_ladder_lifecycle import (  # noqa: E402
    DEAD_BUCKET_CANCEL_RULE_VERSION,
    mirror_book,
    resting_rung_keep_or_cancel,
)
from bayes_basket_target.ssot import apply_production_ssot_schema  # noqa: E402
from tests.test_tiebreak_lane import (  # noqa: E402,F401 -- ``store`` is a fixture
    ADMIT,
    ORDS,
    ST,
    TZ,
    _insert,
    store,
)

ED = "2026-09-20"
DECIDED = "2026-09-18T12:02:00.000Z"
NOW = "2026-09-18T12:02:30.000Z"
TOKENS = {"27°C": ("t27", "n27"), "28°C": ("t28", "n28"), "29°C": ("t29", "n29")}
#: Both sides: this suite pins the two-sided rule as charter 231 wrote it.  The
#: YES side ships stopped (charter 235, ``sides: ["NO"]``); its own suite pins
#: that.
SETTINGS = {"opening_stages": ["P0"], "live_ws_book": False, "min_hours_to_settlement": 36.0,
            "send_book_fresh_seconds": 60.0, "first_decision_fresh_seconds": 3600.0,
            "sides": ["YES", "NO"]}


def _row(bucket, ordinal, bids, asks, tick=0.01):
    return {"bucket": bucket, "ordinal": ordinal, "ask_available": True, "price_tick_size": tick,
            "ask_levels": [{"price": p, "shares": s} for p, s in asks],
            "bid_levels": [{"price": p, "shares": s} for p, s in bids]}


def _ladder(bid_29=0.18, ask_29=0.20):
    """27°C 0.48/0.50, 28°C 0.30/0.32, 29°C 0.18/0.20."""

    return [
        _row("27°C", 27, [(0.48, 200.0)], [(0.50, 200.0)]),
        _row("28°C", 28, [(0.30, 200.0)], [(0.32, 200.0)]),
        _row("29°C", 29, [(bid_29, 200.0)], [(ask_29, 200.0)]),
    ]


MODEL = {"27°C": 0.10, "28°C": 0.30, "29°C": 0.60}


def _snapshot(conn, snapshot_id, observed, book, sequence):
    _insert(conn, "basket_official_ws_snapshot", {
        "snapshot_id": snapshot_id, "market_id": "m", "station_id": ST, "event_date": ED,
        "settlement_timezone": TZ, "sequence": sequence, "observed_at": observed, "quote_version": "v",
        "quote_observed_at": observed, "quote_expires_at": "2026-09-18T13:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "remaining_budget_usdc": 100.0,
        "buckets_json": json.dumps(book), "state": "COMMITTED", "created_at": observed,
    })


def _fact(conn, *, stage, fact_id, decided, snapshot_id):
    _insert(conn, "basket_probability_envelope", {
        "envelope_id": f"env_{fact_id}", "stage": stage, "checkpoint_id": f"cp_{fact_id}", "station_id": ST,
        "event_date": ED, "settlement_timezone": TZ, "decision_at_utc": decided,
        "fresh_until_utc": "2026-09-18T13:00:00Z", "bucket_ordinals_json": json.dumps(ORDS),
        "probabilities_json": json.dumps(MODEL), "content_sha256": (fact_id * 64)[:64],
        "state": "COMMITTED", "created_at": decided, "lineage_json": "{}",
    })
    _insert(conn, "basket_policy_training_fact", {
        "fact_id": fact_id, "station_id": ST, "event_date": ED, "settlement_timezone": TZ,
        "decision_at_utc": decided, "decision_scope_key": f"scope_{fact_id}", "captured_at_utc": decided,
        "source_sequence": 1, "quote_observed_at": decided, "quote_expires_at": "2026-09-18T13:00:00Z",
        "stage": stage, "probability_envelope_id": f"env_{fact_id}", "ws_snapshot_id": snapshot_id,
        "intent_count": 0, "authority_scope": "PRODUCTION_NO_LIVE_FORWARD", "created_at_utc": decided,
        "cycle_id": f"cyc_{fact_id}", "decision_id": f"dec_{fact_id}",
    })


def _seed(conn, *, stage="P0", decided=DECIDED, observed=None, no_token=True, book=None):
    _snapshot(conn, "snap_f1", observed or decided, book if book is not None else _ladder(), 1)
    _fact(conn, stage=stage, fact_id="f1", decided=decided, snapshot_id="snap_f1")
    registry_sha = "regf1".ljust(64, "0")
    _insert(conn, "basket_target_market_registry_snapshot",
            {"snapshot_sha256": registry_sha, "source_scope": "shared_live_registry_export_only"})
    for bucket, (yes, no) in TOKENS.items():
        raw = {"order_price_min_tick_size": 0.01, "order_min_size": 5.0}
        if no_token:
            raw["no_token"] = no
        _insert(conn, "basket_target_market_registry", {
            "snapshot_sha256": registry_sha, "slug": f"{ST}-{bucket}", "event_date": ED,
            "outcome": bucket, "city_slug": "shanghai", "station_id": ST, "settlement_timezone": TZ,
            "yes_token": yes, "no_token": no, "market_active": 1, "market_accepting_orders": 1,
            "registry_json": json.dumps(raw), "refreshed_at": decided, "source_eligible": 1,
            "source_lane": "awc",
        })


def _resting_transport(sent: list):
    def transport(path, payload):
        sent.append((path, payload))
        return {"ok": True, "accepted": True, "exchange_order_id": f"0xrest{len(sent)}", "status": "live"}
    return transport


def _run(conn, sent, now=NOW, **overrides):
    return lane.execute_proper_bets(
        conn, transport=_resting_transport(sent), now_utc=now, live_submit_authorized=True,
        settings={**SETTINGS, **overrides}, admission=ADMIT,
    )


def _intents(conn):
    return conn.execute(
        "SELECT order_intent_id, token_id, side, limit_price, requested_size, state, intent_json"
        " FROM basket_target_order_intents ORDER BY created_at, order_intent_id"
    ).fetchall()


def _end(conn, intent_id, *, filled=0.0, price=0.0, at="2026-09-18T12:10:00.000Z"):
    event = "FILL" if filled else "EXPIRED"
    order_id = conn.execute(
        "SELECT exchange_order_id FROM basket_target_order_reconciliations WHERE order_intent_id=?",
        (intent_id,),
    ).fetchone()[0]
    record_order_event(conn, order_intent_id=intent_id, event={
        "event_type": event, "exchange_order_id": order_id, "observed_at": at,
        "matched_size": filled, "matched_notional_usdc": round(filled * price, 6),
        "event_payload": {"reason": "test"},
    })


# ---------------------------------------------------------------------------
# The rule.


def test_the_log_proper_position_and_divergence_on_the_paper_example() -> None:
    p = {"<=32": 0.10, "33": 0.30, "34": 0.40, ">=35": 0.20}
    q = {"<=32": 0.20, "33": 0.25, "34": 0.30, ">=35": 0.25}
    s = lane.proper_position(p, q)
    assert [round(s[b], 3) for b in p] == [-0.693, 0.182, 0.288, -0.223]
    d = lane.divergence_kl(p, q)
    assert d == pytest.approx(sum(q[b] * math.log(q[b] / p[b]) for b in q)) and d > 0.0
    for y in p:  # s.(1_y - q) = log(p_y/q_y) + D, pointwise
        lhs = sum(s[b] * ((1.0 if b == y else 0.0) - q[b]) for b in q)
        assert lhs == pytest.approx(math.log(p[y] / q[y]) + d)


def test_the_no_book_is_the_yes_book_mirrored() -> None:
    row = _row("x", 0, [(0.23, 10.0)], [(0.24, 10.0)])
    assert lane.side_book(row, lane.SIDE_YES, 0.01) == (0.23, 0.24)
    assert lane.side_book(row, lane.SIDE_NO, 0.01) == (0.76, 0.77)
    assert mirror_book({"bid": 0.23, "ask": 0.24, "tick": 0.01, "observed_at": "t"}) == {
        "bid": 0.76, "ask": 0.77, "tick": 0.01, "observed_at": "t"}


def test_a_rung_rests_at_bid_plus_one_and_only_under_its_claim() -> None:
    wide = _row("x", 0, [(0.18, 50.0)], [(0.22, 50.0)])
    yes = lane.leg_quote("x", 0.60, 1.1, wide, 0.01)
    assert (yes["side"], yes["price"], yes["reason"]) == ("YES", 0.19, None)
    # YES at or above p breaks the rule.
    assert lane.leg_quote("x", 0.19, 0.2, wide, 0.01)["reason"].startswith("price_breaks_rule")
    # NO rests at the NO bid + 1t (1 - 0.22 + 0.01) and must sit under 1 - p.
    no = lane.leg_quote("x", 0.10, -1.0, wide, 0.01)
    assert (no["side"], no["price"], no["reason"]) == ("NO", 0.79, None)
    assert lane.leg_quote("x", 0.21, -0.1, wide, 0.01)["reason"].startswith("price_breaks_rule")
    # The pasted rule (NO iff p < 1 - bid) is not the rule: p = 0.30 under a
    # 0.20/0.22 book would rest NO at 0.79 for a 0.70 claim.
    assert lane.leg_quote("x", 0.30, -0.3, wide, 0.01)["reason"].startswith("price_breaks_rule")
    # A one-tick spread clamps under the ask; no bid is no anchor.
    tight = _row("x", 0, [(0.18, 50.0)], [(0.19, 50.0)])
    assert lane.leg_quote("x", 0.60, 1.0, tight, 0.01)["price"] == 0.18
    empty = _row("x", 0, [], [(0.19, 50.0)])
    assert lane.leg_quote("x", 0.60, 1.0, empty, 0.01)["reason"] == "maker_anchor_unavailable"


def test_lambda_is_the_venue_minimum_for_the_whole_position() -> None:
    legs = [
        {"bucket": "a", "s": -1.6, "side": "NO", "price": 0.51, "reason": None},
        {"bucket": "b", "s": 1.1, "side": "YES", "price": 0.19, "reason": None},
        {"bucket": "c", "s": 0.4, "side": "YES", "price": 0.30, "reason": None},
    ]
    mins = {"a": 5.0, "b": 5.0, "c": 5.0}
    full = lane.size_minimum(legs, cash_usdc=None, min_shares=mins)
    shares = {leg["bucket"]: leg["target_shares"] for leg in full["legs"]}
    # The smallest |s| sits exactly at the minimum; the rest in ratio.
    assert full["lambda_shares"] == pytest.approx(5.0 / 0.4)
    assert shares["c"] == 5.0
    assert shares["a"] == pytest.approx(12.5 * 1.6, abs=0.01)
    assert shares["b"] == pytest.approx(12.5 * 1.1, abs=0.01)
    assert full["capped_by_cash"] is False and full["dropped"] == {}
    need = full["need_full_usdc"]
    # Cash short of the whole position: the longest |s| prefix it can carry,
    # at that prefix's own minimum.
    capped = lane.size_minimum(legs, cash_usdc=need - 0.01, min_shares=mins)
    assert [leg["bucket"] for leg in capped["legs"]] == ["a", "b"]
    assert capped["lambda_shares"] == pytest.approx(5.0 / 1.1)
    assert capped["dropped"] == {"c": "budget_capped_by_cash"} and capped["capped_by_cash"] is True
    none = lane.size_minimum(legs, cash_usdc=1.0, min_shares=mins)
    assert none["legs"] == [] and none["lambda_shares"] is None


def test_the_ladder_must_be_the_markets_own() -> None:
    reading = lane.evaluate_ladder({"27°C": 0.5, "28°C": 0.5}, _ladder(), cash_usdc=None,
                                   min_shares={}, ticks={})
    assert reading["refusal"] == "ladder_mismatch" and reading["market_only"] == ["29°C"]


def test_evaluate_ladder_prices_every_leg_off_one_book() -> None:
    reading = lane.evaluate_ladder(MODEL, _ladder(), cash_usdc=None,
                                   min_shares={b: 5.0 for b in MODEL}, ticks={b: 0.01 for b in MODEL})
    assert reading["refusal"] is None
    assert reading["q"]["27°C"] == pytest.approx(0.49 / 0.99)
    quotes = {q["bucket"]: q for q in reading["quotes"]}
    assert (quotes["27°C"]["side"], quotes["27°C"]["price"]) == ("NO", 0.51)
    assert (quotes["28°C"]["side"], quotes["28°C"]["price"]) == ("NO", 0.69)
    assert (quotes["29°C"]["side"], quotes["29°C"]["price"]) == ("YES", 0.19)
    smallest = min(abs(reading["s"][b]) for b in MODEL)
    assert reading["lambda_shares"] == pytest.approx(5.0 / smallest)


# ---------------------------------------------------------------------------
# End to end.


def test_every_leg_rests_post_only_at_bid_plus_one_on_its_own_token(store) -> None:
    _seed(store)
    sent: list = []
    receipt = _run(store, sent)
    assert receipt["evaluated"] == 1 and receipt["planned"] == 1
    assert receipt["rungs"] == 3 and receipt["submitted"] == 3
    payloads = {payload["token_id"]: payload for _path, payload in sent}
    assert set(payloads) == {"n27", "n28", "t29"}
    for payload in payloads.values():
        assert payload["side"] == "BUY" and payload["order_type"] == "GTD"
        assert payload["post_only"] is True and "fok_price_cap" not in payload
    assert payloads["n27"]["outcome_side"] == "NO" and payloads["n27"]["price"] == pytest.approx(0.51)
    assert payloads["n28"]["outcome_side"] == "NO" and payloads["n28"]["price"] == pytest.approx(0.69)
    assert payloads["t29"]["outcome_side"] == "YES" and payloads["t29"]["price"] == pytest.approx(0.19)
    # The shortest horizon: the venue's minimum plus the signing margin.
    from bayes_basket_target.production import (  # noqa: PLC0415
        GTD_SIGNING_MARGIN_SECONDS,
        VENUE_GTD_MINIMUM_HORIZON_SECONDS,
    )
    now = lane._utc(NOW).timestamp()
    for payload in payloads.values():
        assert payload["expiration"] - now == pytest.approx(
            VENUE_GTD_MINIMUM_HORIZON_SECONDS + GTD_SIGNING_MARGIN_SECONDS, abs=1.0)
    # lambda at the venue minimum: the smallest |s| leg (28°C) is 5 shares.
    assert payloads["n28"]["size"] == 5.0
    sides = {row[1]: row[2] for row in _intents(store)}
    assert sides == {"n27": "NO", "n28": "NO", "t29": "YES"}
    row = store.execute(
        f"SELECT refusal, plan_state, hours_to_settlement, planned_legs FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert row[0] is None and row[1] == lane.PLAN_ACTIVE and row[2] > 36.0 and row[3] == 3


def test_an_unfilled_rung_is_reposted_until_the_leg_fills(store) -> None:
    _seed(store)
    sent: list = []
    _run(store, sent)
    first = {row[1]: row[0] for row in _intents(store)}
    # Nothing new while the three rungs rest.
    again = _run(store, sent, now="2026-09-18T12:03:00.000Z")
    assert again["rungs"] == 0 and len(sent) == 3
    # 29°C expired unfilled, 27°C filled whole, 28°C filled in part.
    target_27 = store.execute(
        "SELECT requested_size FROM basket_target_order_intents WHERE order_intent_id=?", (first["n27"],)
    ).fetchone()[0]
    _end(store, first["t29"])
    _end(store, first["n27"], filled=target_27, price=0.51)
    _end(store, first["n28"], filled=2.0, price=0.69)
    later = "2026-09-18T12:12:00.000Z"
    _snapshot(store, "snap_f2", later, _ladder(), 2)
    third = _run(store, sent, now=later)
    reposted = {payload["token_id"]: payload for _path, payload in sent[3:]}
    # 29°C re-posted whole on the book that stands; 27°C is done; 28°C has
    # 3 shares left, under the venue minimum, so it is done too.
    assert set(reposted) == {"t29"} and third["rungs"] == 1
    assert reposted["t29"]["size"] == store.execute(
        "SELECT requested_size FROM basket_target_order_intents WHERE order_intent_id=?", (first["t29"],)
    ).fetchone()[0]
    _end(store, next(r[0] for r in _intents(store) if r[1] == "t29" and r[0] != first["t29"]),
         filled=reposted["t29"]["size"], price=0.19, at="2026-09-18T12:14:00.000Z")
    last = "2026-09-18T12:20:00.000Z"
    _snapshot(store, "snap_f3", last, _ladder(), 3)
    _run(store, sent, now=last)
    state = store.execute(f"SELECT plan_state, plan_state_reason FROM {lane.EVALUATION_TABLE}").fetchone()
    assert state == (lane.PLAN_DONE, "all_legs_filled")


def test_a_repost_waits_while_its_price_breaks_the_rule(store) -> None:
    _seed(store)
    sent: list = []
    _run(store, sent)
    first = {row[1]: row[0] for row in _intents(store)}
    _end(store, first["t29"])
    later = "2026-09-18T12:12:00.000Z"
    # 29°C now bid 0.62: bid+1 is 0.63, above p = 0.60.
    _snapshot(store, "snap_f2", later, _ladder(bid_29=0.62, ask_29=0.66), 2)
    third = _run(store, sent, now=later)
    assert third["rungs"] == 0 and len(sent) == 3
    legs = json.loads(store.execute(f"SELECT legs_state_json FROM {lane.EVALUATION_TABLE}").fetchone()[0])
    assert legs["YES:29°C"]["status"].startswith("waiting:price_breaks_rule")


def test_the_plan_stops_at_36_hours_and_when_the_day_leaves_p0(store) -> None:
    _seed(store)
    _run(store, [])
    # 2026-09-20 in Shanghai settles 09-20T16:00Z: 35 hours out at 09-19T05:00Z.
    closed = _run(store, [], now="2026-09-19T05:00:00.000Z")
    assert closed["closed_plans"][0]["reason"].startswith("inside_36h")
    assert store.execute(f"SELECT plan_state FROM {lane.EVALUATION_TABLE}").fetchone()[0] == lane.PLAN_CLOSED


def test_a_p0m_decision_ends_the_plan(store) -> None:
    _seed(store)
    _run(store, [])
    _fact(store, stage="P0-M", fact_id="f9", decided="2026-09-18T12:05:00.000Z", snapshot_id="snap_f1")
    closed = _run(store, [], now="2026-09-18T12:06:00.000Z")
    assert closed["closed_plans"][0]["reason"] == "stage_left_p0:P0-M"


def test_a_first_decision_inside_36_hours_is_not_planned(store) -> None:
    _seed(store, decided="2026-09-19T05:00:00.000Z", observed="2026-09-19T05:00:00.000Z")
    sent: list = []
    receipt = _run(store, sent, now="2026-09-19T05:00:10.000Z")
    assert receipt["refusals"][0].startswith("inside_36h") and not sent


def test_only_the_p0_first_decision_is_judged(store) -> None:
    _seed(store, stage="P1")
    receipt = _run(store, [])
    assert receipt["evaluated"] == 0 and receipt["planned"] == 0


def test_a_stale_book_waits_while_the_decision_is_young(store) -> None:
    _seed(store, observed="2026-09-18T11:50:00.000Z")
    sent: list = []
    receipt = _run(store, sent)
    assert receipt["waiting"] == 1 and receipt["evaluated"] == 0 and not sent
    late = _run(store, sent, now="2026-09-18T13:30:00.000Z")
    assert late["evaluated"] == 1 and late["refusals"] == ["first_decision_stale"] and not sent


def _publisher_manifest(tmp_path: Path, *, observed: str) -> Path:
    """The WS publisher's multi-scope manifest for this station-day, with the
    same ladder the committed snapshot carries."""

    import hashlib  # noqa: PLC0415

    from bayes_basket_target.market_ingress import _canonical  # noqa: PLC0415

    quotes = {}
    for row in _ladder():
        token = TOKENS[row["bucket"]][0]
        ask, bid = row["ask_levels"][0]["price"], row["bid_levels"][0]["price"]
        quotes[token] = {
            "token_id": token, "side": "YES",
            "asks": [{"price": ask, "size": 200.0}], "bids": [{"price": bid, "size": 200.0}],
            "best_ask": ask, "best_bid": bid, "book_observed_at": observed,
        }
    core = {
        "schema": "clob_ws_latest_quote_snapshot_v1",
        "source": "official_polymarket_clob_websocket",
        "evidence_only": True, "authorizes_order": False, "state": "connected",
        "active_shards": [0], "expected_shards": 1, "quote_storage": "inline_v1",
        "quotes_by_token": quotes, "scope_key": f"{ST}|{ED}",
        "target_local_date": ED, "unmaterialized_tokens": [],
    }
    nested = {**core, "snapshot_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
    path = tmp_path / "publisher_manifest.json"
    path.write_text(json.dumps({
        "schema": "bayes_basket_target_resident_multi_scope_ws_manifest_v1",
        "state": "connected", "scope_manifests": {f"{ST}|{ED}": nested},
    }), encoding="utf-8")
    return path


def test_the_publishers_live_book_prices_a_day_that_has_not_started(store, tmp_path) -> None:
    """2026-09-26: the lane bets only more than 36 hours out, so the day it
    prices has never started -- and the live book read the started-day
    registry view, found no rows, and gave nothing.  Every v2 first decision
    waited on a committed snapshot 130-140 s old against the 60-second bound
    until it was an hour old and was refused ``first_decision_stale``."""

    # The committed snapshot is two minutes old: past the send bound.
    _seed(store, observed="2026-09-18T12:00:30.000Z")
    manifest = _publisher_manifest(tmp_path, observed="2026-09-18T12:02:20.000Z")
    sent: list = []
    receipt = lane.execute_proper_bets(
        store, transport=_resting_transport(sent), now_utc=NOW, live_submit_authorized=True,
        settings={**SETTINGS, "live_ws_book": True}, admission=ADMIT,
        ws_manifest_path=manifest,
    )
    assert receipt["waiting"] == 0, receipt["waiting_reasons"]
    assert receipt["evaluated"] == 1 and receipt["planned"] == 1
    row = store.execute(
        f"SELECT book_source, refusal FROM {lane.EVALUATION_TABLE}"
    ).fetchone()
    assert tuple(row) == ("ws_publisher_manifest", None)


def test_a_waiting_decision_says_what_it_waits_on(store) -> None:
    _seed(store, observed="2026-09-18T11:50:00.000Z")
    receipt = _run(store, [])
    assert receipt["waiting"] == 1
    assert receipt["waiting_reasons"] == {"book_stale": 1}


def test_a_missing_no_token_is_not_sent(store) -> None:
    _seed(store, no_token=False)
    sent: list = []
    _run(store, sent)
    assert [payload["token_id"] for _path, payload in sent] == ["t29"]


def test_the_yes_inventory_never_reads_a_no_fill(store) -> None:
    _seed(store)
    _run(store, [])
    for intent_id, _token, _side, price, size, _state, _body in _intents(store):
        _end(store, intent_id, filled=size, price=price)
    fills = _ledger_fill_rows(store, station_id=ST, event_date=ED)
    assert [fill["token_id"] for fill in fills] == ["t29"]


def test_the_live_book_pays_a_no_leg_when_its_bucket_does_not_settle(store) -> None:
    _seed(store)
    _run(store, [])
    for intent_id, _token, _side, price, size, _state, _body in _intents(store):
        _end(store, intent_id, filled=size, price=price)
    import live_book  # noqa: PLC0415

    legs = live_book.load_legs(store)
    assert {leg["side"] for leg in legs} == {"NO", "YES"}
    summary = live_book.stage_summary(legs)
    n27, n28 = summary["held_no_shares"]["27°C"], summary["held_no_shares"]["28°C"]
    y29 = summary["held_shares"]["29°C"]
    assert summary["held_buckets"] == ["29°C"] and summary["filled_legs"] == 3
    for truth, expected in (("27°C", n28), ("28°C", n27), ("29°C", n27 + n28 + y29)):
        assert live_book._payout(summary["held_shares"], {}, truth, summary["held_no_shares"]) == pytest.approx(expected)


def test_the_lifecycle_judges_a_no_rung_on_the_mirrored_book() -> None:
    yes_book = {"bid": 0.48, "ask": 0.50, "tick": 0.01, "observed_at": "2026-09-18T12:02:00Z"}
    intent = {"bucket": "27°C", "side": "NO", "order_action": "BUY", "limit_price": 0.51,
              "posted_at_utc": "2026-09-18T12:02:00Z"}
    verdict = resting_rung_keep_or_cancel(
        intent=intent, dead_buckets=(), running_high=None, load_book=lambda: mirror_book(yes_book),
        as_of_utc="2026-09-18T12:03:00Z", stale_book_seconds=3600.0,
    )
    assert verdict["action"] == "keep"
    # A dead bucket makes a NO leg certain, not a loss: the dead-bucket cancel is YES-only.
    dead = resting_rung_keep_or_cancel(
        intent=intent, dead_buckets=("27°C",), running_high=30.0, load_book=lambda: mirror_book(yes_book),
        as_of_utc="2026-09-18T12:03:00Z", stale_book_seconds=3600.0,
    )
    assert dead.get("cancel_rule") != DEAD_BUCKET_CANCEL_RULE_VERSION
    yes_dead = resting_rung_keep_or_cancel(
        intent={**intent, "side": "YES", "limit_price": 0.49}, dead_buckets=("27°C",), running_high=30.0,
        load_book=lambda: yes_book, as_of_utc="2026-09-18T12:03:00Z", stale_book_seconds=3600.0,
    )
    assert yes_dead["cancel_rule"] == DEAD_BUCKET_CANCEL_RULE_VERSION


# ---------------------------------------------------------------------------
# The ledger and the switches.


def test_the_submitter_takes_a_no_buy_and_refuses_a_no_sell() -> None:
    base = {"order_intent_id": "i", "idempotency_key": "i", "token_id": "n", "limit_price": 0.51,
            "requested_size": 10.0, "execution_candidate": "MAKER", "order_type": "GTD",
            "post_only": True, "expiration": 1_900_000_000}
    payload = sub.build_place_order_payload({**base, "side": "NO", "order_action": "BUY"})
    assert payload["outcome_side"] == "NO" and payload["side"] == "BUY" and payload["post_only"] is True
    with pytest.raises(sub.OrderSubmissionError, match="no_leg_sell_invalid"):
        sub.build_place_order_payload({**base, "side": "NO", "order_action": "SELL"})
    with pytest.raises(sub.OrderSubmissionError, match="outcome_side_invalid"):
        sub.build_place_order_payload({**base, "side": "MAYBE", "order_action": "BUY"})


def test_the_retired_no_side_trigger_is_gone_from_the_schema(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "s.db", isolation_level=None)
    apply_production_ssot_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "basket_target_intents_no_no_side_insert" not in names


def test_the_lane_ships_v2_and_the_basket_off() -> None:
    assert strategy_lanes.LANE_PROPER_BET in strategy_lanes.KNOWN_LANES
    defaults = strategy_lanes.PROPER_BET_DEFAULT_SETTINGS
    assert defaults["rule"] == lane.RULE_MAKER_V2 and defaults["opening_stages"] == ["P0"]
    assert defaults["min_hours_to_settlement"] == 36.0
    assert defaults["budget_rule"] == "minimum_full_position" and "stake_usdc" not in defaults
    shipped = strategy_lanes.load_strategy_lanes(ROOT / "config" / "trading" / "strategy_lanes_v1.json")
    assert shipped["source"] == "file"
    proper = shipped["lanes"]["proper_bet"]
    # The on/off switch is the operator's (2026-09-28 ruling stopped Proper);
    # only the rule it runs when on is pinned here.
    assert proper["rule"] == lane.RULE_MAKER_V2
    assert proper["min_hours_to_settlement"] == 36.0 and proper["opening_stages"] == ["P0"]
    assert shipped["lanes"]["basket"]["enabled"] is False


def test_the_charter_and_agents_carry_the_ruling() -> None:
    charter = (ROOT / "docs" / "constitution" / "SYSTEM_CHARTER.md").read_text(encoding="utf-8")
    assert "\n231. **Proper betting" in charter and "log_proper_maker_bid_plus_one_min_horizon_v2" in charter
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "禁止任何自动买 NO 策略" not in agents
    assert "宪章 231" in agents
