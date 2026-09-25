"""The lane that asks whether a resting leg fills less often when it wins.

The measurement it replaces was a shell one-liner, and the shell one-liner
was wrong twice in one sitting: it silently filed a leg whose bucket the
current ladder cannot price as a loss, and a filter nobody wrote down
dropped six maker legs and two baskets.  Both are what a fixture is for.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import measure_live_resting_fill_by_outcome as lane  # noqa: E402


def _database() -> sqlite3.Connection:
    """The four tables the lane joins, shaped as production shapes them."""

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_target_order_intents (
          order_intent_id TEXT PRIMARY KEY, decision_id TEXT, bucket TEXT,
          limit_price REAL, requested_size REAL, live_submit_authorized INT,
          intent_json TEXT, created_at TEXT);
        CREATE TABLE basket_target_order_reconciliations (
          order_intent_id TEXT PRIMARY KEY, state TEXT, filled_size REAL,
          filled_notional_usdc REAL);
        CREATE TABLE auto_trade_decisions (
          decision_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT);
        CREATE TABLE basket_probability_bridge_history (
          history_id TEXT, station_id TEXT, event_date TEXT);
        CREATE TABLE basket_probability_bridge_bucket_probability (
          history_id TEXT, bucket TEXT, ordinal INT, lower_c REAL,
          upper_c REAL, probability REAL);
        CREATE TABLE calibration_station_daily_high_truth (
          station_id TEXT, local_date TEXT, official_tmax_c REAL,
          revision_no INT);
        """
    )
    return conn


def _scope(
    conn: sqlite3.Connection,
    *,
    station: str,
    date: str,
    buckets: list[tuple[str, float, float]],
    truth: float | None,
    history: str = "h1",
) -> None:
    conn.execute(
        "INSERT INTO basket_probability_bridge_history VALUES (?,?,?)",
        (history, station, date),
    )
    for ordinal, (bucket, low, high) in enumerate(buckets):
        conn.execute(
            "INSERT INTO basket_probability_bridge_bucket_probability"
            " VALUES (?,?,?,?,?,?)",
            (history, bucket, ordinal, low, high, 0.3),
        )
    if truth is not None:
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth"
            " VALUES (?,?,?,?)", (station, date, truth, 0)
        )


def _leg(
    conn: sqlite3.Connection,
    *,
    intent: str,
    station: str,
    date: str,
    bucket: str,
    family: str = "MAKER",
    state: str = "resolved_filled",
    filled: float = 5.0,
    cash: float = 1.5,
    limit: float = 0.30,
    size: float = 5.0,
    created: str = "2026-09-06T00:00:00Z",
) -> None:
    decision = "d_" + intent
    conn.execute(
        "INSERT OR IGNORE INTO auto_trade_decisions VALUES (?,?,?)",
        (decision, station, date),
    )
    conn.execute(
        "INSERT INTO basket_target_order_intents VALUES (?,?,?,?,?,?,?,?)",
        (intent, decision, bucket, limit, size, 1,
         '{"execution_candidate":"%s"}' % family, created),
    )
    conn.execute(
        "INSERT INTO basket_target_order_reconciliations VALUES (?,?,?,?)",
        (intent, state, filled, cash),
    )


def test_a_leg_counts_once_however_many_times_it_was_posted() -> None:
    conn = _database()
    _scope(conn, station="KMIA", date="2026-09-06",
           buckets=[("30C", 30.0, 31.0), ("31C", 31.0, 32.0)], truth=30.5)
    # One leg, three postings: the first two expired, the third filled.
    _leg(conn, intent="i1", station="KMIA", date="2026-09-06", bucket="30C",
         state="resolved_cancelled", filled=0.0, cash=0.0)
    _leg(conn, intent="i2", station="KMIA", date="2026-09-06", bucket="30C",
         state="resolved_cancelled", filled=0.0, cash=0.0)
    _leg(conn, intent="i3", station="KMIA", date="2026-09-06", bucket="30C")
    conn.commit()

    report = lane.measure(conn, since="")
    per_leg = {(r["family"], r["outcome"]): r for r in report["per_leg"]}
    assert per_leg[("MAKER", "WIN")]["legs"] == 1
    assert per_leg[("MAKER", "WIN")]["filled"] == 1
    assert per_leg[("MAKER", "WIN")]["intents_per_leg"] == 3.0
    # Per intent, the same leg is three rows and one fill -- which is why
    # the two readings must not be quoted as one number.
    per_intent = {
        (r["family"], r["outcome"]): r for r in report["per_intent"]
    }
    assert per_intent[("MAKER", "WIN")]["intents"] == 3
    assert per_intent[("MAKER", "WIN")]["filled"] == 1


def test_the_winning_leg_and_the_losing_legs_are_counted_apart() -> None:
    conn = _database()
    _scope(conn, station="KMIA", date="2026-09-06",
           buckets=[("30C", 30.0, 31.0), ("31C", 31.0, 32.0),
                    ("32C", 32.0, 33.0)], truth=30.5)
    _leg(conn, intent="w", station="KMIA", date="2026-09-06", bucket="30C",
         state="resolved_cancelled", filled=0.0, cash=0.0)
    _leg(conn, intent="l1", station="KMIA", date="2026-09-06", bucket="31C")
    _leg(conn, intent="l2", station="KMIA", date="2026-09-06", bucket="32C")
    conn.commit()

    report = lane.measure(conn, since="")
    per_leg = {(r["family"], r["outcome"]): r for r in report["per_leg"]}
    assert per_leg[("MAKER", "WIN")]["fill_rate_pct"] == 0.0
    assert per_leg[("MAKER", "LOSE")]["fill_rate_pct"] == 100.0
    # And the basket is the one the split calls gapped: the winner was
    # posted and never bought, so the loss is the execution's.
    baskets = report["baskets"]
    assert baskets["winner_gapped"]["baskets"] == 1
    assert baskets["winner_filled"]["baskets"] == 0
    assert baskets["winner_missing"]["baskets"] == 0


def test_a_winner_never_posted_is_the_models_loss_not_the_orders() -> None:
    conn = _database()
    _scope(conn, station="KLGA", date="2026-09-07",
           buckets=[("30C", 30.0, 31.0), ("31C", 31.0, 32.0),
                    ("35C", 35.0, 36.0)], truth=35.5)
    _leg(conn, intent="a", station="KLGA", date="2026-09-07", bucket="30C")
    _leg(conn, intent="b", station="KLGA", date="2026-09-07", bucket="31C")
    conn.commit()

    baskets = lane.measure(conn, since="")["baskets"]
    assert baskets["winner_missing"]["baskets"] == 1
    assert baskets["winner_missing"]["roi_pct"] == -100.0
    assert baskets["winner_gapped"]["baskets"] == 0


def test_an_order_that_never_reached_the_venue_is_not_an_unfilled_order() -> None:
    """The gateway, the cash phase and the dollar floor are not the book."""

    conn = _database()
    _scope(conn, station="KMIA", date="2026-09-06",
           buckets=[("30C", 30.0, 31.0)], truth=30.5)
    _leg(conn, intent="ghost", station="KMIA", date="2026-09-06",
         bucket="30C", state="resolved_not_placed", filled=0.0, cash=0.0)
    conn.commit()

    report = lane.measure(conn, since="")
    assert report["intents_not_placed"]["intents"] == 1
    assert report["per_leg"] == []
    assert report["baskets"]["settled"] == 0


def test_a_bucket_the_current_ladder_cannot_price_is_not_filed_as_a_loss() -> None:
    conn = _database()
    _scope(conn, station="KMIA", date="2026-09-06",
           buckets=[("30C", 30.0, 31.0)], truth=30.5)
    # Posted on a bucket the newest history no longer carries.
    _leg(conn, intent="gone", station="KMIA", date="2026-09-06",
         bucket="29C")
    conn.commit()

    report = lane.measure(conn, since="")
    assert report["legs_unpriceable_by_current_ladder"]["intents"] == 1
    assert report["per_leg"] == []


def test_a_day_with_no_truth_is_open_not_lost() -> None:
    conn = _database()
    _scope(conn, station="KMIA", date="2026-09-12",
           buckets=[("30C", 30.0, 31.0)], truth=None)
    _leg(conn, intent="open", station="KMIA", date="2026-09-12",
         bucket="30C", created="2026-09-12T00:00:00Z")
    conn.commit()

    report = lane.measure(conn, since="")
    assert report["legs_unsettled"]["intents"] == 1
    assert report["baskets"]["settled"] == 0


def test_the_since_floor_is_applied_to_the_intent_not_the_event() -> None:
    conn = _database()
    _scope(conn, station="KMIA", date="2026-09-06",
           buckets=[("30C", 30.0, 31.0)], truth=30.5)
    _leg(conn, intent="old", station="KMIA", date="2026-09-06", bucket="30C",
         created="2026-09-01T00:00:00Z")
    conn.commit()

    assert lane.measure(conn, since="2026-09-05")["baskets"]["settled"] == 0
    assert lane.measure(conn, since="")["baskets"]["settled"] == 1


def test_the_settled_bucket_rule_is_productions_not_a_second_copy() -> None:
    """Imported, so a change to the interval rule reaches this lane.

    Asserted by where the function comes from, not by object identity: a
    sibling test that puts the repository root on the path first imports
    the ledger under a second module name, and then two names for one
    function read as two functions.
    """

    assert lane._settled_bucket.__module__.endswith("live_book")
    assert lane._settled_bucket.__qualname__ == "_settled_bucket"
    # And the lane does not define its own.
    source = (
        ROOT / "scripts" / "measure_live_resting_fill_by_outcome.py"
    ).read_text(encoding="utf-8")
    assert "def _settled_bucket" not in source


