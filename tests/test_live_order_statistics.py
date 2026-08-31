"""The live book's own arithmetic, before there is a live book.

Every number in the board's 实盘订单 panel is unexercised code: the
reconciliation table is empty, so the first time this arithmetic runs for
real is the first time real money is on it.  Synthetic fills are the only
way to know it works at all, and the three things asserted here are the
three ways a live PnL panel lies: it counts the paper book, it scores an
unsettled day as a loss, or it reports 0% when it means "no sample".
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "calibration_dashboard", ROOT / "scripts" / "calibration_dashboard.py"
)
board = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(board)


def _book(rows: list[tuple], fills: list[tuple],
          settled: list[tuple]) -> sqlite3.Connection:
    """A book with just the columns the panel reads."""

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE basket_target_order_intents("
        " order_intent_id TEXT PRIMARY KEY, decision_id TEXT, cycle_id TEXT,"
        " bucket TEXT, side TEXT, limit_price REAL, requested_size REAL,"
        " requested_notional_usdc REAL, state TEXT,"
        " live_submit_authorized INTEGER, created_at TEXT);"
        "CREATE TABLE auto_trade_decisions("
        " decision_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT);"
        "CREATE TABLE basket_target_order_reconciliations("
        " order_intent_id TEXT PRIMARY KEY, state TEXT, filled_size REAL,"
        " filled_notional_usdc REAL, blocking_reason TEXT);"
        "CREATE TABLE paper_trade_settlement("
        " settlement_id INTEGER PRIMARY KEY, station_id TEXT,"
        " event_date TEXT, settled_bucket TEXT);"
    )
    for intent, decision, station, day, bucket, live in rows:
        conn.execute(
            "INSERT INTO basket_target_order_intents VALUES"
            " (?,?,?,?, 'YES', 0.3, 10, 3.0, 'prepared', ?, '2026-08-31T00:00:00Z')",
            (intent, decision, "cycle", bucket, live),
        )
        conn.execute(
            "INSERT OR IGNORE INTO auto_trade_decisions VALUES (?,?,?)",
            (decision, station, day),
        )
    for intent, size, notional in fills:
        conn.execute(
            "INSERT INTO basket_target_order_reconciliations VALUES"
            " (?, 'resolved_filled', ?, ?, NULL)",
            (intent, size, notional),
        )
    for index, (station, day, bucket) in enumerate(settled):
        conn.execute(
            "INSERT INTO paper_trade_settlement VALUES (?,?,?,?)",
            (index + 1, station, day, bucket),
        )
    conn.commit()
    return conn


def test_no_fills_reports_no_sample_rather_than_zero_percent() -> None:
    """An empty live book has no ROI -- 0% would be a measured claim."""

    conn = _book(
        rows=[("i1", "d1", "KMIA", "2026-08-30", "31°C", 1)],
        fills=[], settled=[("KMIA", "2026-08-30", "31°C")],
    )
    pnl = board._collect_live_pnl(conn)
    assert pnl["available"] is False
    assert pnl["roi"] is None
    assert pnl["filled_legs"] == 0
    assert pnl["pnl_usdc"] == 0.0


def test_the_paper_book_never_reaches_the_live_numbers() -> None:
    """A filled row with live_submit_authorized=0 is not a live fill.

    Both books are settled winners here, so a collector that ignored the
    authorisation column would report a healthy live ROI off paper rows.
    """

    conn = _book(
        rows=[("live", "d1", "KMIA", "2026-08-30", "31°C", 1),
              ("paper", "d2", "KLGA", "2026-08-30", "24°C", 0)],
        fills=[("live", 10.0, 3.0), ("paper", 40.0, 12.0)],
        settled=[("KMIA", "2026-08-30", "31°C"),
                 ("KLGA", "2026-08-30", "24°C")],
    )
    pnl = board._collect_live_pnl(conn)
    assert pnl["filled_legs"] == 1
    assert pnl["cost_usdc"] == 3.0
    # Ten shares on the bucket truth landed in: ten USDC, less the three paid.
    assert pnl["payout_usdc"] == 10.0
    assert pnl["pnl_usdc"] == 7.0
    assert pnl["roi"] == round(7.0 / 3.0, 6)
    assert [r["station_id"] for r in pnl["by_station"]] == ["KMIA"]


def test_a_miss_is_a_loss_and_an_unsettled_day_is_neither() -> None:
    """Pending legs stay out of the ratio instead of reading as zero payout.

    Counting them as losses is what would make the panel look worse every
    afternoon and recover overnight, which is not a thing the book did.
    """

    conn = _book(
        rows=[("hit", "d1", "KMIA", "2026-08-30", "31°C", 1),
              ("miss", "d2", "KLGA", "2026-08-30", "24°C", 1),
              ("open", "d3", "EGLL", "2026-08-31", "19°C", 1)],
        fills=[("hit", 10.0, 3.0), ("miss", 10.0, 4.0), ("open", 10.0, 5.0)],
        settled=[("KMIA", "2026-08-30", "31°C"),
                 ("KLGA", "2026-08-30", "25°C")],
    )
    pnl = board._collect_live_pnl(conn)
    assert pnl["filled_legs"] == 2
    assert pnl["won_legs"] == 1
    assert pnl["pending_legs"] == 1
    # The unsettled five USDC is in neither the cost nor the payout.
    assert pnl["cost_usdc"] == 7.0
    assert pnl["payout_usdc"] == 10.0
    assert pnl["pnl_usdc"] == 3.0
    assert pnl["station_days"] == 2
    assert pnl["available"] is True


def test_the_intent_book_counts_both_authorisations_separately() -> None:
    """The book panel is the whole table; the live split is per row."""

    conn = _book(
        rows=[("live", "d1", "KMIA", "2026-08-30", "31°C", 1),
              ("p1", "d2", "KLGA", "2026-08-30", "24°C", 0),
              ("p2", "d3", "KLGA", "2026-08-29", "23°C", 0)],
        fills=[], settled=[],
    )
    book = board._collect_order_book(conn)
    assert book["total"] == 3
    assert book["live"] == 1
    assert book["paper"] == 2
    assert book["stations"] == 2
    assert book["notional_usdc"] == 9.0
    assert book["live_notional_usdc"] == 3.0
    by_day = {r["event_date"]: r for r in book["by_day"]}
    assert by_day["2026-08-30"]["count"] == 2
    assert by_day["2026-08-30"]["live_count"] == 1
    assert by_day["2026-08-29"]["live_count"] == 0
    by_station = {r["station_id"]: r for r in book["by_station"]}
    assert by_station["KLGA"]["count"] == 2
    assert by_station["KLGA"]["days"] == 2


def test_a_fail_closed_receipt_turns_the_production_light_red() -> None:
    """The shape that got past every check: no lanes, because it never ran.

    A worker that cannot open the SSOT publishes a receipt with no lane
    maps at all, no ``worker_checked_at``, and a ``cycle_total`` of a
    fraction of a millisecond -- so the lane scan found nothing blocked,
    the staleness check had nothing to read, and the slow-cycle check
    passed.  The card printed "周期正常产出" while the pipeline had
    produced no decision for nineteen hours.
    """

    receipt = {
        "production": {
            "ok": False,
            "state": "blocked_fail_closed",
            "reason": "target_ssot_read_only_open_failed:ResourceBoundError:"
                      "target_database_wal_checkpoint_busy",
            "execution_mode": "production_live",
            "orders_submitted": 0,
            "stage_timings_ms": {"cycle_total": 0.1},
        }
    }
    health = board._production_health(receipt)
    assert health["production_health"] == "red"
    assert health["production_state"] == "blocked_fail_closed"
    assert "fail-closed" in health["production_note"]
    assert "wal_checkpoint_busy" in health["production_note"]


def test_a_producing_receipt_is_still_green() -> None:
    """The fail-closed check must not swallow the ordinary healthy case."""

    receipt = {
        "production": {
            "ok": True,
            "state": "produced",
            "stage_timings_ms": {"cycle_total": 240_000},
        }
    }
    health = board._production_health(receipt)
    assert health["production_health"] == "green"
    assert health["production_note"] == "周期正常产出"
