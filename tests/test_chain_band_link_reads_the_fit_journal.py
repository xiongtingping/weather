"""The chain card's band link reads the lane's journal, not its table.

Measured 2026-09-14: the card printed "④ 概率分档校准 ｜3.2 天没有新记录"
in red.  The refit had run the previous morning and journalled all three
stages ACCUMULATING (P0 3, P0-M 3, P1 6 of 10 days) -- a stage that has not
yet earned new factors writes no table row, on purpose, and the served table
stands.  The card was reading the table, so a lane doing exactly what it
should looked stopped.  It is still red when the lane really stops running.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "scripts", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import calibration_dashboard as C  # noqa: E402
from bayes_basket_target import band_reliability_store as band  # noqa: E402


def _stamp(hours_ago: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    ).isoformat().replace("+00:00", "Z")


def _chain_db(*, table_hours_ago: float, journal_hours_ago: float | None):
    conn = sqlite3.connect(":memory:")
    # The other links' sources, as small as the chain reads them.
    conn.execute("CREATE TABLE calibration_native_ensemble_historical_member_run(archive_query_at_utc TEXT)")
    conn.execute("CREATE TABLE calibration_station_daily_high_truth(local_date TEXT)")
    conn.execute("CREATE TABLE calibration_native_ensemble_activation(valid_from_utc TEXT)")
    conn.execute("CREATE TABLE basket_probability_bridge_history(decision_at_utc TEXT, stage TEXT)")
    conn.execute(band._TABLE_DDL)
    conn.execute(
        "INSERT INTO calibration_band_reliability_table VALUES(?,?,?,?,?,?,?,?,?)",
        ("a" * 64, "P0", "[]", "{}", _stamp(table_hours_ago), 41, 1, _stamp(table_hours_ago), None),
    )
    if journal_hours_ago is not None:
        conn.execute(band._JOURNAL_DDL)
        for index, (stage, days, outcome) in enumerate(
            (("P0", 3, "ACCUMULATING"), ("P0-M", 3, "ACCUMULATING"), ("P1", 6, "ACCUMULATING"))
        ):
            # An older row per stage too, so "latest per stage" is what is read.
            conn.execute(
                "INSERT INTO calibration_band_reliability_fit_journal VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"old-{index}", stage, _stamp(journal_hours_ago + 30), "b" * 64, 40, 900, 3,
                 "ACTIVATED", "[]", json.dumps({})),
            )
            conn.execute(
                "INSERT INTO calibration_band_reliability_fit_journal VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"new-{index}", stage, _stamp(journal_hours_ago), "b" * 64, days, 90, 0,
                 outcome, "[]", json.dumps({})),
            )
    return conn


def _band_link(conn):
    (link,) = [item for item in C.collect_chain(conn) if item["name"].startswith("④")]
    return link


def test_an_accumulating_lane_that_ran_this_morning_is_not_reported_stopped() -> None:
    link = _band_link(_chain_db(table_hours_ago=77, journal_hours_ago=21.5))
    assert link["health"] == "green"
    assert 21 < link["age_hours"] < 22
    assert "攒证据中" in link["volume"]
    assert "P0 3/10" in link["volume"] and "P0-M 3/10" in link["volume"] and "P1 6/10" in link["volume"]


def test_a_lane_that_stopped_running_is_still_red() -> None:
    link = _band_link(_chain_db(table_hours_ago=200, journal_hours_ago=80))
    assert link["health"] == "red"
    assert link["note"].endswith("天没有新记录")


def test_without_a_journal_the_table_is_still_read() -> None:
    link = _band_link(_chain_db(table_hours_ago=77, journal_hours_ago=None))
    assert link["health"] == "red"
    assert link["volume"] == "1 个版本"
