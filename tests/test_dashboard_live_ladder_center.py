"""What the live panel may say about a basket, and where it reads it.

Two things are shown per basket and neither is derived: the centre rung it
seated on, and the shape of the day that placed it.  The centre is not the
middle of the window -- charter 75 seats on the mean landing bucket and
charter 55 lets the window extend to one side of it -- so the board reads
the ``center_bucket`` of the compiled plan the decision names by
``production_plan_sha256``, and marks nothing when that plan is gone.

The tempting substitute is the ledger's own seat row.  It is a different
book: measured across the whole live order book on 2026-09-06, fifteen of
fifty-three baskets carried a ledger seat decided at another moment, and six
of those named a centre that was not one of the basket's legs.  A green ring
on the wrong rung is worse than no ring, so that fallback is pinned out.

The shape reading (``skew_reading``) reaches plans from 2026-09-06.  Older
plans fall back to the plan's own ``direction``, which is the same skew sign
under charter 75 except on a day whose evidence was not fresh -- that reads
NEUTRAL whatever its shape -- so the two are labelled apart.
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


def _plan_row(
    station: str,
    day: str,
    plan_sha: str,
    center: str,
    *,
    direction: str | None = None,
    skew: dict | None = None,
) -> tuple:
    plan: dict = {"center_bucket": center}
    if direction is not None:
        plan["direction"] = direction
    if skew is not None:
        plan["skew_reading"] = skew
    payload = {
        "compiled_plan": {
            "production_plan_sha256": plan_sha,
            "plan": plan,
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


def _plan(conn, *rows) -> None:
    for row in rows:
        conn.execute(
            "INSERT INTO basket_p0_preselection_plan VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )


def _rows(*decisions: str) -> list[dict]:
    return [{"decision_id": d} for d in decisions]


def test_centre_is_the_plan_the_decision_names(conn):
    _decide(conn, "d1", "KATL", "2026-09-07", "a" * 64)
    _plan(conn, _plan_row("KATL", "2026-09-07", "a" * 64, "86-87°F"))
    read = C._live_basket_plan_state(conn, _rows("d1"))
    assert read["d1"]["center"] == "86-87°F"


def test_two_baskets_on_one_station_day_keep_their_own_centres(conn):
    # A station-day carries one decision per moment and they do not share a
    # centre; keying by station-day would hand one basket the other's rung.
    _decide(conn, "d1", "ZHHH", "2026-09-07", "a" * 64)
    _decide(conn, "d2", "ZHHH", "2026-09-07", "b" * 64)
    _plan(
        conn,
        _plan_row("ZHHH", "2026-09-07", "a" * 64, "34°C"),
        _plan_row("ZHHH", "2026-09-07", "b" * 64, "35°C"),
    )
    read = C._live_basket_plan_state(conn, _rows("d1", "d2"))
    assert read["d1"]["center"] == "34°C"
    assert read["d2"]["center"] == "35°C"


def test_the_shape_is_the_plans_own_reading_when_it_has_one(conn):
    _decide(conn, "d1", "CYYZ", "2026-09-07", "a" * 64)
    _plan(conn, _plan_row(
        "CYYZ", "2026-09-07", "a" * 64, "24-25°C",
        direction="UP",
        skew={
            "state": "RIGHT_SKEWED", "sign": 1, "gap_buckets": 0.75754,
            "threshold_buckets": 0.5,
        },
    ))
    read = C._live_basket_plan_state(conn, _rows("d1"))["d1"]
    assert read["skew"] == "RIGHT_SKEWED"
    assert read["skew_source"] == "reading"
    assert read["gap_buckets"] == pytest.approx(0.75754)
    assert read["threshold_buckets"] == pytest.approx(0.5)


def test_an_older_plan_falls_back_to_the_direction_it_acted_on(conn):
    # Charter 75 reads the direction off the skew sign, so it is the same
    # word -- but a day whose evidence was stale reads NEUTRAL whatever its
    # shape, which is why the source is carried and the label differs.
    for decision, direction, expected in (
        ("d1", "UP", "RIGHT_SKEWED"),
        ("d2", "DOWN", "LEFT_SKEWED"),
        ("d3", "NEUTRAL", "SYMMETRIC"),
    ):
        sha = decision * 64
        _decide(conn, decision, "EDDM", "2026-09-07", sha)
        _plan(conn, _plan_row(
            "EDDM", "2026-09-07", sha, "23°C", direction=direction,
        ))
    read = C._live_basket_plan_state(conn, _rows("d1", "d2", "d3"))
    assert read["d1"]["skew"] == "RIGHT_SKEWED"
    assert read["d2"]["skew"] == "LEFT_SKEWED"
    assert read["d3"]["skew"] == "SYMMETRIC"
    for key in ("d1", "d2", "d3"):
        assert read[key]["skew_source"] == "direction"
        assert read[key]["gap_buckets"] is None


def test_a_plan_with_neither_reading_nor_direction_says_nothing(conn):
    _decide(conn, "d1", "KATL", "2026-09-07", "a" * 64)
    _plan(conn, _plan_row("KATL", "2026-09-07", "a" * 64, "86-87°F"))
    read = C._live_basket_plan_state(conn, _rows("d1"))["d1"]
    assert read["skew"] == ""
    assert read["skew_source"] == ""


def test_a_missing_plan_marks_nothing(conn):
    # Ten of the fifty-three baskets on the book predate the plan store.
    # They get no centre rather than the nearest one.
    _decide(conn, "d1", "KATL", "2026-08-31", "c" * 64)
    assert C._live_basket_plan_state(conn, _rows("d1")) == {}


def test_a_plan_from_another_decision_is_not_borrowed(conn):
    # Same station-day, different plan hash: the row exists but it is not
    # this basket's, so it is not this basket's centre.
    _decide(conn, "d1", "KATL", "2026-09-07", "a" * 64)
    _plan(conn, _plan_row("KATL", "2026-09-07", "b" * 64, "84-85°F"))
    assert C._live_basket_plan_state(conn, _rows("d1")) == {}


def test_no_plan_store_is_not_an_error(conn):
    conn.execute("DROP TABLE basket_p0_preselection_plan")
    _decide(conn, "d1", "KATL", "2026-09-07", "a" * 64)
    assert C._live_basket_plan_state(conn, _rows("d1")) == {}
