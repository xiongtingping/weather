"""The live book: the one loader every live money reading shares.

User ruling 2026-09-16: the paper ledger is a rule comparator, and every
reading of whether the account, a station or a day made money reads the live
fills.  Three things here would quietly make those readings wrong:

* the claim.  Each leg's claim is the probability the minting decision was
  bound to, read back from the exact envelope its forward evidence binding
  names.  The binding and the envelope spell one instant two ways
  (``.697Z`` / ``.697000Z``), and an equality seek found 16 of 100 live
  decisions on the store; a lookup on the hash alone scans a 38 GB table.
* the uneven basket.  Live legs fill unevenly, and the portfolio arithmetic
  rests on ``stake x (claim/cost - 1)`` being the claimed EV.
* the automatic bar, which bars stations from live trading and must judge
  them on live money, not on the paper book.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import auto_station_exclusions as auto  # noqa: E402
import live_book  # noqa: E402

SHA = "a" * 64
OTHER = "b" * 64


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_target_order_intents("
        " order_intent_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,"
        " cycle_id TEXT NOT NULL DEFAULT 'c', bucket TEXT NOT NULL,"
        " limit_price REAL NOT NULL, requested_size REAL NOT NULL,"
        " requested_notional_usdc REAL NOT NULL, state TEXT NOT NULL,"
        " live_submit_authorized INTEGER NOT NULL, created_at TEXT NOT NULL);"
        "CREATE TABLE auto_trade_decisions("
        " decision_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,"
        " event_date TEXT NOT NULL, details_json TEXT NOT NULL);"
        "CREATE TABLE basket_target_order_reconciliations("
        " order_intent_id TEXT PRIMARY KEY, state TEXT NOT NULL,"
        " filled_size REAL NOT NULL, filled_notional_usdc REAL NOT NULL);"
        "CREATE TABLE basket_probability_envelope("
        " envelope_id TEXT PRIMARY KEY, stage TEXT NOT NULL,"
        " station_id TEXT NOT NULL, event_date TEXT NOT NULL,"
        " decision_at_utc TEXT NOT NULL, probabilities_json TEXT NOT NULL,"
        " content_sha256 TEXT NOT NULL, state TEXT NOT NULL);"
        "CREATE TABLE paper_trade_settlement("
        " settlement_id TEXT PRIMARY KEY, position_id TEXT NOT NULL,"
        " station_id TEXT NOT NULL, event_date TEXT NOT NULL,"
        " stage TEXT NOT NULL, settled_bucket TEXT NOT NULL,"
        " hit INTEGER NOT NULL, stake_usdc REAL NOT NULL,"
        " pnl_usdc REAL NOT NULL);"
        "CREATE TABLE paper_trade_position("
        " position_id TEXT PRIMARY KEY, station_id TEXT NOT NULL,"
        " win_probability REAL, all_in_cost REAL, stake_usdc REAL);"
    )
    return conn


def _decision(conn, decision, station, day, *, stage="P0",
              bound_at="2026-09-10T05:08:48.697Z", sha=SHA,
              decided_at="2026-09-10T05:18:29.926Z"):
    details = {
        "decision_signature": {"stage": stage},
        "forward_evidence_binding": {
            "probability_stage": stage,
            "probability_decision_at_utc": bound_at,
            "probability_artifact_sha256": sha,
            "decision_at_utc": decided_at,
        },
    }
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES (?,?,?,?)",
        (decision, station, day, json.dumps(details)),
    )


def _envelope(conn, station, day, probabilities, *, stage="P0",
              at="2026-09-10T05:08:48.697000Z", sha=SHA, name="e1"):
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?)",
        (name, stage, station, day, at, json.dumps(probabilities), sha,
         "COMMITTED"),
    )


def _leg(conn, intent, decision, bucket, *, size=10.0, notional=3.0,
         filled=None, filled_notional=None):
    conn.execute(
        "INSERT INTO basket_target_order_intents"
        "(order_intent_id, decision_id, bucket, limit_price, requested_size,"
        " requested_notional_usdc, state, live_submit_authorized, created_at)"
        " VALUES (?,?,?,0.3,?,?, 'filled', 1, '2026-09-10T05:18:30Z')",
        (intent, decision, bucket, size, notional),
    )
    if filled is not None:
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations"
            " VALUES (?, 'resolved_filled', ?, ?)",
            (intent, filled, filled_notional),
        )


def _settle(conn, station, day, bucket):
    conn.execute(
        "INSERT INTO paper_trade_settlement VALUES"
        " (?, 'p', ?, ?, 'P0', ?, 0, 0, 0)",
        (f"s-{station}-{day}", station, day, bucket),
    )


def test_the_claim_is_the_bound_envelopes_even_when_the_instant_is_spelt_apart():
    conn = _store()
    _decision(conn, "d1", "KMIA", "2026-09-10")
    # Same instant, microsecond spelling, and a decoy at the same instant
    # whose hash is not the one the binding names.
    _envelope(conn, "KMIA", "2026-09-10", {"80-81°F": 0.9}, sha=OTHER,
              name="decoy")
    _envelope(conn, "KMIA", "2026-09-10", {"80-81°F": 0.62})
    _leg(conn, "i1", "d1", "80-81°F", filled=10.0, filled_notional=3.0)
    _leg(conn, "i2", "d1", "82-83°F")

    legs = {leg["order_intent_id"]: leg for leg in live_book.load_legs(conn)}
    assert legs["i1"]["claimed_probability"] == pytest.approx(0.62)
    assert legs["i1"]["stage"] == "P0"
    # A bucket the envelope gives no mass is a claim of zero, not unknown.
    assert legs["i2"]["claimed_probability"] == 0.0
    assert legs["i2"]["filled_size"] == 0.0


def test_a_binding_whose_envelope_is_missing_has_no_claim_rather_than_a_guess():
    conn = _store()
    _decision(conn, "d1", "KMIA", "2026-09-10", sha=OTHER)
    _envelope(conn, "KMIA", "2026-09-10", {"80-81°F": 0.62})
    _leg(conn, "i1", "d1", "80-81°F", filled=10.0, filled_notional=3.0)
    _settle(conn, "KMIA", "2026-09-10", "80-81°F")

    (leg,) = live_book.load_legs(conn)
    assert leg["claimed_probability"] is None
    (row,) = live_book.build(conn)["settlements"]
    # Money is still counted; the expectation is not invented.
    assert row["pnl_usdc"] == pytest.approx(7.0)
    assert row["claimed_win_probability"] is None
    assert row["basket_all_in_cost"] is None
    assert row["claimed_ev_usdc"] is None


def test_an_uneven_basket_keeps_the_claimed_ev_identity():
    """stake x (claim / cost - 1) == sum(p x shares) - stake, exactly."""

    conn = _store()
    _decision(conn, "d1", "ZBAA", "2026-09-10")
    _envelope(conn, "ZBAA", "2026-09-10", {"30°C": 0.4, "31°C": 0.2})
    _leg(conn, "i1", "d1", "30°C", filled=10.0, filled_notional=3.0)
    _leg(conn, "i2", "d1", "31°C", filled=5.0, filled_notional=1.0)
    _leg(conn, "i3", "d1", "32°C")  # posted, never filled
    _settle(conn, "ZBAA", "2026-09-10", "32°C")

    (row,) = live_book.build(conn)["settlements"]
    stake = 4.0
    claimed = 0.4 * 10.0 + 0.2 * 5.0 - stake
    assert row["stake_usdc"] == pytest.approx(stake)
    assert row["claimed_win_probability"] == pytest.approx(0.6)
    assert row["claimed_ev_usdc"] == pytest.approx(claimed)
    assert stake * (row["claimed_win_probability"]
                    / row["basket_all_in_cost"] - 1.0) == pytest.approx(claimed)
    # Held misses, the posted basket held the truth.
    assert row["hit"] == 0 and row["intended_hit"] == 1
    assert row["pnl_usdc"] == pytest.approx(-stake)


def test_a_basket_two_decisions_bought_is_claimed_on_the_last_ones_envelope():
    """KLAX 2026-09-15: three buckets from two envelopes summed to 1.35.

    One decision bought 30°C believing it 0.5; a later one bought 31°C
    believing it 0.7 while holding the first leg.  Added up, the basket "won"
    1.2 of the time.  The later decision is the one that judged the pair: its
    envelope puts 0.9 on it, and the EV, the cost and each held bucket read
    that same envelope.
    """

    conn = _store()
    # "late" sorts before "early" by id and its intent row comes first, so
    # only the decision instant can say which one judged the pair.
    _decision(conn, "early", "ZBAA", "2026-09-10", sha=SHA)
    _decision(conn, "late", "ZBAA", "2026-09-10", sha=OTHER,
              bound_at="2026-09-10T07:00:00.000Z",
              decided_at="2026-09-10T07:05:00.000Z")
    _envelope(conn, "ZBAA", "2026-09-10",
              {"30°C": 0.5, "31°C": 0.45, "32°C": 0.05}, sha=SHA, name="e1")
    _envelope(conn, "ZBAA", "2026-09-10",
              {"30°C": 0.2, "31°C": 0.7, "32°C": 0.1}, sha=OTHER, name="e2",
              at="2026-09-10T07:00:00.000000Z")
    _leg(conn, "i2", "late", "31°C", filled=10.0, filled_notional=4.0)
    _leg(conn, "i1", "early", "30°C", filled=10.0, filled_notional=3.0)
    _settle(conn, "ZBAA", "2026-09-10", "31°C")

    book = live_book.build(conn)
    (row,) = book["settlements"]
    stake = 7.0
    assert row["claimed_win_probability"] == pytest.approx(0.9)
    assert row["claimed_ev_usdc"] == pytest.approx(0.2 * 10 + 0.7 * 10 - stake)
    assert stake * (row["claimed_win_probability"]
                    / row["basket_all_in_cost"] - 1.0) == pytest.approx(
        row["claimed_ev_usdc"])
    # The held buckets are drawn on the same envelope, so they add up to it.
    (day,) = live_book.station_days(book)
    held = [b for b in day["stages"]["P0"]["buckets"] if b["filled_size"] > 0]
    assert sum(b["claimed_probability"] for b in held) == pytest.approx(0.9)


def test_one_unknown_envelope_leaves_a_mixed_basket_unclaimed():
    conn = _store()
    _decision(conn, "early", "ZBAA", "2026-09-10", sha=SHA)
    _decision(conn, "late", "ZBAA", "2026-09-10", sha="c" * 64,  # no envelope
              bound_at="2026-09-10T07:00:00.000Z",
              decided_at="2026-09-10T07:05:00.000Z")
    _envelope(conn, "ZBAA", "2026-09-10", {"30°C": 0.5, "31°C": 0.45})
    _leg(conn, "i1", "early", "30°C", filled=10.0, filled_notional=3.0)
    _leg(conn, "i2", "late", "31°C", filled=10.0, filled_notional=4.0)
    _settle(conn, "ZBAA", "2026-09-10", "31°C")

    book = live_book.build(conn)
    (row,) = book["settlements"]
    assert row["claimed_win_probability"] is None
    assert row["basket_all_in_cost"] is None
    # A reading that tolerates it steps back to the newest envelope it has.
    assert live_book.basket_belief(book["legs"], strict=False) == {
        "30°C": 0.5, "31°C": 0.45}
    assert live_book.basket_belief(book["legs"]) is None


def test_an_even_basket_prices_like_the_paper_seat():
    conn = _store()
    _decision(conn, "d1", "ZBAA", "2026-09-10")
    _envelope(conn, "ZBAA", "2026-09-10", {"30°C": 0.4, "31°C": 0.2})
    _leg(conn, "i1", "d1", "30°C", filled=10.0, filled_notional=3.0)
    _leg(conn, "i2", "d1", "31°C", filled=10.0, filled_notional=1.5)
    _settle(conn, "ZBAA", "2026-09-10", "30°C")

    (row,) = live_book.build(conn)["settlements"]
    # One share count: the all-in cost is stake / shares, the ladder's price.
    assert row["basket_all_in_cost"] == pytest.approx(4.5 / 10.0)
    assert row["payout_usdc"] == pytest.approx(10.0)
    assert row["hit"] == 1


def test_the_build_is_not_handed_to_the_next_connection():
    """Keyed on id() alone, a closed connection's book leaked to the next."""

    first = _store()
    _decision(first, "d1", "KMIA", "2026-09-10")
    _leg(first, "i1", "d1", "80-81°F", filled=10.0, filled_notional=3.0)
    assert len(live_book.build(first)["legs"]) == 1
    del first
    second = _store()
    assert live_book.build(second)["legs"] == []


def test_the_automatic_bar_judges_live_money_and_ignores_the_paper_book(
    monkeypatch,
):
    """User ruling 2026-09-16: criterion unchanged, book switched to live."""

    conn = _store()
    days = [f"2026-09-{d:02d}" for d in range(1, 7)]
    for index, day in enumerate(days):
        decision = f"d{index}"
        _decision(conn, decision, "KSFO", day,
                  bound_at=f"{day}T05:08:48.697Z")
        _envelope(conn, "KSFO", day, {"60-61°F": 0.5},
                  at=f"{day}T05:08:48.697000Z", name=f"e{index}")
        _leg(conn, f"i{index}", decision, "60-61°F",
             filled=10.0, filled_notional=2.0)
        _settle(conn, "KSFO", day, "62-63°F")
    # A paper book for a station that never traded live, losing heavily:
    # a layer still reading paper would bar it.
    for index in range(8):
        conn.execute(
            "INSERT INTO paper_trade_position VALUES (?, 'EGLL', 0.9, 0.1, 10)",
            (f"pp{index}",),
        )
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES"
            " (?, ?, 'EGLL', '2026-09-01', 'P0', 'x', 0, 10, -10)",
            (f"ps{index}", f"pp{index}"),
        )

    keys = auto.station_keys(conn)
    assert set(keys) == {"KSFO"}
    row = keys["KSFO"]
    assert row["settlements"] == 6
    # Each day claimed 0.5 x 10 - 2 = +3 and lost the 2 it paid.
    assert row["expected_usdc"] == pytest.approx(18.0)
    assert row["realized_usdc"] == pytest.approx(-12.0)
    assert row["fulfilment_ratio"] == pytest.approx(-12.0 / 18.0)
    verdict = auto.decide(keys, currently_auto_barred=set(), operator_ruled=set())
    assert verdict["bar"] == [
        {"station": "KSFO", "on": "fulfilment_ratio", "value": -0.667}
    ]


def _store_with_centres() -> sqlite3.Connection:
    """The store, with the envelope's lineage and the truth table.

    ``_store`` leaves both out on purpose: a store without the lineage column
    must still read its claims, and the tests above pin that.
    """

    conn = _store()
    conn.executescript(
        "DROP TABLE basket_probability_envelope;"
        "CREATE TABLE basket_probability_envelope("
        " envelope_id TEXT PRIMARY KEY, stage TEXT NOT NULL,"
        " station_id TEXT NOT NULL, event_date TEXT NOT NULL,"
        " decision_at_utc TEXT NOT NULL, probabilities_json TEXT NOT NULL,"
        " lineage_json TEXT, content_sha256 TEXT NOT NULL,"
        " state TEXT NOT NULL);"
        "CREATE TABLE calibration_station_daily_high_truth("
        " truth_id TEXT PRIMARY KEY, station_id TEXT, local_date TEXT,"
        " official_tmax_c REAL, revision_no INTEGER, content_sha256 TEXT);"
    )
    return conn


def _centred_day(conn, station, day, index, *, centre, truth, bucket,
                 claim=0.5, settle=None, stage="P0", revision_truth=None):
    decision = f"d{station}{index}{stage}"
    sha = f"{index:02d}{stage}".ljust(64, "c")
    _decision(conn, decision, station, day, stage=stage,
              bound_at=f"{day}T05:08:48.697Z", sha=sha)
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES (?,?,?,?,?,?,?,?,?)",
        (f"e{decision}", stage, station, day, f"{day}T05:08:48.697000Z",
         json.dumps({bucket: claim}), json.dumps({"model_center_c": centre}),
         sha, "COMMITTED"),
    )
    _leg(conn, f"i{decision}", decision, bucket,
         filled=10.0, filled_notional=10.0 * claim)
    if settle is not None and stage == "P0":
        _settle(conn, station, day, settle)
        # An older revision first: the standing one is what scores.
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth VALUES"
            " (?,?,?,?,0,'x')", (f"t{station}{day}r0", station, day, 99.0),
        )
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth VALUES"
            " (?,?,?,?,1,'y')", (f"t{station}{day}r1", station, day, truth),
        )


def test_a_settled_live_basket_carries_its_centre_against_the_standing_truth():
    """The paper settlement's centre columns, off the envelope the leg was bound to."""

    conn = _store_with_centres()
    _centred_day(conn, "ZBAA", "2026-09-10", 0, centre=31.4, truth=30.9,
                 bucket="31°C", settle="31°C")

    (leg,) = live_book.load_legs(conn)
    assert leg["center_c"] == pytest.approx(31.4)
    (row,) = live_book.build(conn)["settlements"]
    assert row["center_c"] == pytest.approx(31.4)
    assert row["settled_tmax_c"] == pytest.approx(30.9)
    assert row["center_error_c"] == pytest.approx(0.5)


def test_station_quality_reads_claim_per_basket_and_truth_per_day():
    """Three stages holding one day are one observation of its truth.

    KAAA: day 1 held by P0 and P1, both hit at a claim of 0.5, centres 1.0C
    and 3.0C warm -> one day at 2.0C.  Day 2 held by P0 only, a miss, centre
    0C.  Claim is per basket (2 hits of 3 over a 0.5 claim), centre and
    basket hit per day.
    """

    conn = _store_with_centres()
    _centred_day(conn, "KAAA", "2026-09-01", 0, centre=31.0, truth=30.0,
                 bucket="30°C", settle="30°C")
    _centred_day(conn, "KAAA", "2026-09-01", 1, centre=33.0, truth=30.0,
                 bucket="30°C", stage="P1")
    _centred_day(conn, "KAAA", "2026-09-02", 2, centre=30.0, truth=30.0,
                 bucket="31°C", settle="30°C")
    book = live_book.build(conn)
    quality = live_book.station_quality(
        book["settlements"], live_book.station_days(book)
    )
    kaaa = quality["stations"]["KAAA"]
    assert kaaa["settled_count"] == 3 and kaaa["settled_days"] == 2
    assert kaaa["calibration_ratio"] == pytest.approx((2 / 3) / 0.5)
    assert kaaa["centre_residual_c"] == pytest.approx((2.0 + 0.0) / 2)
    assert (kaaa["basket_hit_days"], kaaa["settled_days"]) == (1, 2)
    assert kaaa["basket_hit_rate"] == pytest.approx(0.5)
    # Each basket paid 5 for 10 shares at 0.5: +5, +5, then -5.
    assert kaaa["realized_usdc"] == pytest.approx(5.0)
    assert kaaa["flags"]["centre"] is True
    assert kaaa["flags"]["claim"] is False
    assert quality["split_event_date"] == "2026-09-02"
    assert kaaa["early_pnl_usdc"] == pytest.approx(10.0)
    assert kaaa["late_pnl_usdc"] == pytest.approx(-5.0)
    assert kaaa["flags"]["persistence"] is False
    # A station that never settled a live basket is absent, not zero.
    assert "KBBB" not in quality["stations"]


def _with_sells(conn) -> None:
    """The columns and table the exit lanes' sells are read through."""

    conn.execute(
        "ALTER TABLE basket_target_order_intents"
        " ADD COLUMN order_action TEXT NOT NULL DEFAULT 'BUY'"
    )
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        " cycle_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT)"
    )


def _sell(conn, intent, station, day, bucket, *, filled, proceeds):
    """A sale by an exit lane: its own cycle row, no decision row."""

    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES (?,?,?)",
        (f"cy_{intent}", station, day),
    )
    conn.execute(
        "INSERT INTO basket_target_order_intents"
        "(order_intent_id, decision_id, cycle_id, bucket, limit_price, requested_size,"
        " requested_notional_usdc, state, live_submit_authorized, created_at, order_action)"
        " VALUES (?,?,?,?,0.99,?,?, 'filled', 1, '2026-09-10T09:00:00Z', 'SELL')",
        (intent, f"recycle998_decision_{station}_{day}", f"cy_{intent}", bucket, filled, filled * 0.99),
    )
    conn.execute(
        "INSERT INTO basket_target_order_reconciliations VALUES (?, 'resolved_filled', ?, ?)",
        (intent, filled, proceeds),
    )


def test_a_sale_is_realised_when_made_and_counted_in_the_settlement():
    """User, 2026-09-18: 「没有计算主动卖出的吗？」.  The exit lanes write
    no decision row, so the book never saw their sales: a leg sold at 0.99
    was counted as held to settlement, one sold back at the bid as held to
    zero."""

    conn = _store()
    _with_sells(conn)
    _decision(conn, "d1", "KMIA", "2026-09-10")
    _envelope(conn, "KMIA", "2026-09-10", {"80-81°F": 0.6, "82-83°F": 0.3})
    _leg(conn, "i1", "d1", "80-81°F", filled=10.0, filled_notional=3.0)
    _leg(conn, "i2", "d1", "82-83°F", filled=10.0, filled_notional=2.0)
    _sell(conn, "recycle998_a", "KMIA", "2026-09-10", "80-81°F", filled=10.0, proceeds=9.9)

    book = live_book.build(conn)
    assert book["settlements"] == []
    pending = live_book.pending_sales(book)
    assert pending["proceeds_usdc"] == pytest.approx(9.9)
    assert pending["cost_usdc"] == pytest.approx(3.0)
    assert pending["realised_pnl_usdc"] == pytest.approx(6.9)
    assert pending["station_days"] == 1

    _settle(conn, "KMIA", "2026-09-10", "80-81°F")
    book = live_book.build(conn)
    (row,) = book["settlements"]
    # The won shares were sold at 0.99, not held to 1.00: 9.9 in, 5.0 staked.
    assert row["pnl_usdc"] == pytest.approx(9.9 - 5.0)
    assert row["sold_shares"] == pytest.approx(10.0)
    assert row["hit"] == 1
    assert live_book.pending_sales(book)["proceeds_usdc"] == 0.0
    (day,) = live_book.station_days(book)
    assert day["pnl_usdc"] == pytest.approx(9.9 - 5.0)
    assert day["sell_proceeds_usdc"] == pytest.approx(9.9)


def test_a_sale_is_shared_over_the_stages_that_held_the_bucket_and_never_beyond():
    conn = _store()
    _with_sells(conn)
    _decision(conn, "d0", "KMIA", "2026-09-10", stage="P0")
    _decision(conn, "d1", "KMIA", "2026-09-10", stage="P1")
    _envelope(conn, "KMIA", "2026-09-10", {"80-81°F": 0.6}, stage="P0")
    _envelope(conn, "KMIA", "2026-09-10", {"80-81°F": 0.7}, stage="P1", name="e2")
    _leg(conn, "i0", "d0", "80-81°F", filled=6.0, filled_notional=1.8)
    _leg(conn, "i1", "d1", "80-81°F", filled=2.0, filled_notional=1.0)
    # Twelve sold, eight held by the book: the other four were never its.
    _sell(conn, "recycle998_a", "KMIA", "2026-09-10", "80-81°F", filled=12.0, proceeds=12.0)
    sold = live_book.build(conn)["sold_by_stage"]
    assert sold[("KMIA", "2026-09-10", "P0")]["80-81°F"] == pytest.approx((6.0, 6.0))
    assert sold[("KMIA", "2026-09-10", "P1")]["80-81°F"] == pytest.approx((2.0, 2.0))
