"""The ledger's fill simulation has to answer to something measured.

Every money number the paper ledger produces comes from one simulation: did
the book's ask ever trade through the resting rung.  Since charter 94 made
the order family a resting ladder, that simulation IS the ledger's P&L shape,
and it had never been held against a measurement.

There is exactly one measurement available -- ``maker_shadow_order``, four
million post-only intents placed against the real book since 2026-08-17,
filled by the same ``ask <= limit`` rule -- and it does not agree.  The one
number that matters is the asymmetry: how much less often a rung on the
bucket that WINS fills than a rung on a bucket that loses, because that ratio
is what turns a calibrated claim into a losing book.  The ledger reads 0.17.
The shadow lane reads 0.96 at the ladder's own price rule over an hour, and
stratified by distance from settlement it is ABOVE one everywhere earlier
than three hours out, reaching 0.63 only inside the final hour.  Nothing in
four million orders is within a factor of three of the ledger.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "calibrate_maker_fill_model",
    ROOT / "scripts" / "calibrate_maker_fill_model.py",
)
calib = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(calib)


def _book(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "shadow.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE maker_shadow_order("
        " order_id TEXT, station_id TEXT, event_date TEXT, bucket TEXT,"
        " price_rule TEXT, horizon_minutes INTEGER, lead_hours REAL,"
        " state TEXT, limit_price REAL, fill_limit_price REAL,"
        " fill_kind TEXT)"
    )
    conn.execute(
        "CREATE TABLE maker_shadow_settlement("
        " order_id TEXT, station_id TEXT, event_date TEXT,"
        " settled_bucket TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_position("
        " position_id TEXT, stage TEXT, intended_legs_json TEXT,"
        " leg_shares_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE paper_trade_settlement("
        " position_id TEXT, settled_bucket TEXT)"
    )
    conn.execute(
        "INSERT INTO maker_shadow_settlement VALUES"
        " ('x','ZSQD','2026-09-01','30°C')"
    )
    return conn


def _orders(conn, *, bucket, lead, filled, missed, rule="bid+1t"):
    n = 0
    for state, count in (("FILLED", filled), ("EXPIRED", missed)):
        for _ in range(count):
            n += 1
            conn.execute(
                "INSERT INTO maker_shadow_order("
                " order_id, station_id, event_date, bucket, price_rule,"
                " horizon_minutes, lead_hours, state)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"o{bucket}{lead}{state}{n}", "ZSQD", "2026-09-01",
                    bucket, rule, 60, lead, state,
                ),
            )


def test_the_shadow_reading_is_keyed_on_the_bucket_that_won(tmp_path):
    """Winner and loser are the two sides, and the ratio is the reading."""

    conn = _book(tmp_path)
    # Two hours out: winners fill 6/10, losers 3/10 -> asymmetry 2.0.
    _orders(conn, bucket="30°C", lead=2.0, filled=6, missed=4)
    _orders(conn, bucket="31°C", lead=2.0, filled=3, missed=7)
    conn.commit()
    reading = calib.shadow_reading(conn)
    band = next(
        cell for cell in reading["by_lead_hours"] if cell["lead_band"] == "1-3h"
    )
    assert band["winner_orders"] == 10 and band["loser_orders"] == 10
    assert band["winner_fill_rate"] == pytest.approx(0.6)
    assert band["loser_fill_rate"] == pytest.approx(0.3)
    assert band["asymmetry"] == pytest.approx(2.0)
    conn.close()


def test_the_ledger_reading_counts_posted_legs_not_kept_ones(tmp_path):
    """The denominator is what was POSTED.  Counting only the legs that
    filled would make every fill rate 100% and the whole comparison
    vacuous -- the same mistake the dashboard thermometer made."""

    conn = _book(tmp_path)
    conn.execute(
        "INSERT INTO paper_trade_position VALUES('p','P0',?,?)",
        ('{"29°C": 5, "30°C": 5, "31°C": 5}', '{"29°C": 5, "31°C": 5}'),
    )
    conn.execute("INSERT INTO paper_trade_settlement VALUES('p','30°C')")
    conn.commit()
    reading = calib.ledger_reading(conn)
    # One winning leg posted and unfilled; two losing legs posted and filled.
    assert reading["winner_legs"] == 1 and reading["loser_legs"] == 2
    assert reading["winner_fill_rate"] == pytest.approx(0.0)
    assert reading["loser_fill_rate"] == pytest.approx(1.0)
    assert reading["asymmetry"] == pytest.approx(0.0)
    conn.close()


def test_an_older_database_reports_the_new_arms_as_accumulating(tmp_path):
    """A replay extract or a copy taken before the arms existed carries the
    order table without ``fill_kind``.  Reading it must not raise -- the
    honest answer is that the arm has nothing yet, and a calibration that
    crashes on an old database is a calibration nobody runs on one."""

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE maker_shadow_order("
        " order_id TEXT, station_id TEXT, event_date TEXT, bucket TEXT,"
        " price_rule TEXT, horizon_minutes INTEGER, lead_hours REAL,"
        " state TEXT, limit_price REAL)"
    )
    conn.execute(
        "CREATE TABLE maker_shadow_settlement("
        " order_id TEXT, station_id TEXT, event_date TEXT,"
        " settled_bucket TEXT)"
    )
    reading = calib.shadow_reading(conn)
    assert reading["take_after"]["crossed"]["fills"] == 0
    assert reading["take_after"]["crossed"]["mean_price_paid"] is None
    conn.close()


def test_the_ladder_rule_is_the_one_charter_94_posts():
    """bid+1 tick is the ladder's first rung and its first fill, so it is
    the rule the simulation is compared at.  Comparing at ``ask`` would
    flatter the simulation with a number the ladder never posts."""

    assert calib.LADDER_RULE == "bid+1t"


def test_the_receipt_says_when_the_simulation_is_outside_what_was_observed():
    """A calibration that cannot fail is a decoration.  The verdict names
    the observed interval, the simulation's value, and the factor between
    them -- so a reader does not have to hold both tables in their head."""

    source = (
        ROOT / "scripts" / "calibrate_maker_fill_model.py"
    ).read_text(encoding="utf-8")
    assert '"inside_observed_range"' in source
    assert '"factor_outside"' in source
    assert '"shadow_asymmetry_range"' in source
