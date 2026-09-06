"""Which rung the live panel paints green, and where that number comes from.

The centre is not the middle of the window.  Charter 75 seats the ladder on
the mean landing bucket and charter 55 lets the window extend to one side of
it, so a basket's centre can sit off-centre among its own legs -- which is
exactly why the board is forbidden from deriving one.  It reads back the
``center_bucket`` of the compiled plan the decision names by
``production_plan_sha256``, and marks nothing when that plan is gone.

The tempting substitute is the ledger's own seat row.  It is a different
book: measured across the whole live order book on 2026-09-06, fifteen of
fifty-three baskets carried a ledger seat decided at another moment, and six
of those named a centre that was not one of the basket's legs.  A green ring
on the wrong rung is worse than no ring, so that fallback is pinned out here.
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

import calibration_dashboard as C  # noqa: E402


def _plan_row(station: str, day: str, plan_sha: str, center: str) -> tuple:
    payload = {
        "compiled_plan": {
            "production_plan_sha256": plan_sha,
            "plan": {"center_bucket": center},
        }
    }
    return (
        "p0_" + plan_sha, station, day, "cp", "P0", day + "T00:00:00Z",
        day + "T00:00:00Z", "q" * 64, "act", "PRODUCTION_NO_LIVE_CANDIDATE",
        json.dumps(payload), "p" * 64, "PRELIMINARY_NO_LIVE", "P1",
        0, 0, 0, day + "T00:00:00Z",
    )


@pytest.fixture()
def conn():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE auto_trade_decisions ("
        " decision_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " details_json TEXT)"
    )
    db.execute(
        "CREATE TABLE basket_p0_preselection_plan ("
        " preselection_id TEXT PRIMARY KEY, station_id TEXT, event_date TEXT,"
        " checkpoint_id TEXT, probability_stage TEXT, decision_at_utc TEXT,"
        " quote_observed_at_utc TEXT, quote_manifest_sha256 TEXT,"
        " policy_activation_id TEXT, policy_authority_scope TEXT,"
        " plan_json TEXT, plan_sha256 TEXT, state TEXT, final_authority TEXT,"
        " live_submit_authorized INTEGER, calls_order_gateway INTEGER,"
        " orders_submitted INTEGER, created_at TEXT)"
    )
    return db


def _decide(conn, decision_id: str, station: str, day: str, plan_sha: str):
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES (?,?,?,?)",
        (decision_id, station, day,
         json.dumps({"production_plan_sha256": plan_sha})),
    )


def _rows(*decisions: str) -> list[dict]:
    return [{"decision_id": d} for d in decisions]


def test_centre_is_the_plan_the_decision_names(conn):
    _decide(conn, "d1", "KATL", "2026-09-07", "a" * 64)
    conn.execute(
        "INSERT INTO basket_p0_preselection_plan VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _plan_row("KATL", "2026-09-07", "a" * 64, "86-87°F"),
    )
    assert C._live_ladder_centers(conn, _rows("d1")) == {"d1": "86-87°F"}


def test_two_baskets_on_one_station_day_keep_their_own_centres(conn):
    # A station-day carries one decision per moment and they do not share a
    # centre; keying by station-day would hand one basket the other's rung.
    _decide(conn, "d1", "ZHHH", "2026-09-07", "a" * 64)
    _decide(conn, "d2", "ZHHH", "2026-09-07", "b" * 64)
    for sha, center in (("a" * 64, "34°C"), ("b" * 64, "35°C")):
        conn.execute(
            "INSERT INTO basket_p0_preselection_plan VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _plan_row("ZHHH", "2026-09-07", sha, center),
        )
    assert C._live_ladder_centers(conn, _rows("d1", "d2")) == {
        "d1": "34°C", "d2": "35°C",
    }


def test_a_missing_plan_marks_nothing(conn):
    # Ten of the fifty-three baskets on the book predate the plan store.
    # They get no centre rather than the nearest one.
    _decide(conn, "d1", "KATL", "2026-08-31", "c" * 64)
    assert C._live_ladder_centers(conn, _rows("d1")) == {}


def test_a_plan_from_another_decision_is_not_borrowed(conn):
    # Same station-day, different plan hash: the row exists but it is not
    # this basket's, so it is not this basket's centre.
    _decide(conn, "d1", "KATL", "2026-09-07", "a" * 64)
    conn.execute(
        "INSERT INTO basket_p0_preselection_plan VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _plan_row("KATL", "2026-09-07", "b" * 64, "84-85°F"),
    )
    assert C._live_ladder_centers(conn, _rows("d1")) == {}


def test_no_plan_store_is_not_an_error(conn):
    conn.execute("DROP TABLE basket_p0_preselection_plan")
    _decide(conn, "d1", "KATL", "2026-09-07", "a" * 64)
    assert C._live_ladder_centers(conn, _rows("d1")) == {}
