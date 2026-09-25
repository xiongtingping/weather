"""The METAR physical noon prefix: opens at the local-midnight report (v3),
holds only what had arrived by the issue time (v5).

v2 selected [local midnight, local noon] as text, and the archive spells
``...T22:00:00.000Z`` where the bound read ``...T22:00:00Z``: '.' sorts before
'Z', so each day's first report fell outside its own day (1,229 of 2,090
station-days; the noon running high wrong on 20, e.g. LEMD 2026-08-27 read 21
where the reports said 23).  User ruling 2026-09-25: fix and recompute since
08-01 under a new method version.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from bayes_basket_target import metar_physical_forward as mpf
from bayes_basket_target.metar_physical_tmax import METHOD_VERSION

UTC = timezone.utc
# LEMD 2026-08-27 (Europe/Madrid, UTC+2): the local day opens 08-26T22:00Z,
# local noon is 08-27T10:00Z.
DAY_START = datetime(2026, 8, 26, 22, tzinfo=UTC)
ANCHOR = datetime(2026, 8, 27, 10, tzinfo=UTC)
ISSUE = ANCHOR + mpf.PREFIX_ARRIVAL_GRACE


def _archive(rows) -> sqlite3.Connection:
    """Rows without a first-seen stamp arrived four minutes after their time."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE reported_trajectory_observations(observation_id TEXT, station_id TEXT,"
        " source_lane TEXT, valid_at TEXT, reported_temperature_c REAL, raw_text TEXT,"
        " first_seen_at TEXT)"
    )
    for row in rows:
        if len(row) == 6:
            valid = datetime.fromisoformat(row[3].replace("Z", "+00:00"))
            row = (*row, (valid + timedelta(minutes=4)).isoformat().replace("+00:00", "Z"))
        conn.execute("INSERT INTO reported_trajectory_observations VALUES(?,?,?,?,?,?,?)", row)
    return conn


def _prefix(conn) -> list[dict]:
    return mpf._noon_prefix_rows(
        conn, station_id="LEMD", day_start=DAY_START, anchor=ANCHOR, arrived_by=ISSUE
    )


def test_the_local_midnight_report_opens_the_prefix_and_noon_closes_it() -> None:
    conn = _archive([
        ("o0", "LEMD", "awc", "2026-08-26T21:30:00.000Z", 24.0, "previous day"),
        ("o1", "LEMD", "awc", "2026-08-26T22:00:00.000Z", 23.0, "local midnight"),
        ("o2", "LEMD", "awc", "2026-08-27T06:00:00.000Z", 20.0, "dawn"),
        ("o3", "LEMD", "awc", "2026-08-27T10:00:00.000Z", 21.0, "local noon"),
        ("o4", "LEMD", "awc", "2026-08-27T10:30:00.000Z", 25.0, "after noon"),
        ("o5", "LEMD", "asos_1min", "2026-08-27T08:00:00.000Z", 22.0, "other lane"),
        ("o6", "LEMD", "awc", "2026-08-27T09:00:00.000Z", None, "no temperature"),
    ])
    rows = _prefix(conn)
    assert [row["observation_id"] for row in rows] == ["o1", "o2", "o3"]
    assert max(row["temperature_c"] for row in rows) == 23.0


def test_mixed_spellings_are_one_window_in_instant_order() -> None:
    conn = _archive([
        ("b", "LEMD", "awc", "2026-08-27T06:00:00Z", 20.0, "seconds spelling"),
        ("a", "LEMD", "awc", "2026-08-26T22:00:00Z", 23.0, "seconds spelling"),
        ("c", "LEMD", "awc", "2026-08-27T10:00:00.000Z", 21.0, "millisecond spelling"),
        ("d", "LEMD", "awc", "2026-08-27T10:00:01Z", 26.0, "one second past noon"),
    ])
    rows = _prefix(conn)
    assert [row["observation_id"] for row in rows] == ["a", "b", "c"]


def test_a_report_first_seen_after_the_issue_time_is_left_out() -> None:
    conn = _archive([
        ("o1", "LEMD", "awc", "2026-08-26T22:00:00.000Z", 18.0, "on time"),
        # A backfill of a feed gap: stamped at dawn, first seen at 13:00Z.
        ("o2", "LEMD", "awc", "2026-08-27T06:00:00.000Z", 27.0, "backfilled",
         "2026-08-27T13:00:00.000Z"),
        # The noon report, twenty minutes late: still there by the issue time.
        ("o3", "LEMD", "awc", "2026-08-27T10:00:00.000Z", 21.0, "noon, late",
         "2026-08-27T10:20:00Z"),
        # One second past the issue time is too late.
        ("o4", "LEMD", "awc", "2026-08-27T09:00:00.000Z", 22.0, "just missed",
         "2026-08-27T10:30:01.000Z"),
    ])
    rows = _prefix(conn)
    assert [row["observation_id"] for row in rows] == ["o1", "o3"]
    assert max(row["temperature_c"] for row in rows) == 21.0


def test_the_issue_time_is_half_an_hour_after_noon() -> None:
    assert mpf.PREFIX_ARRIVAL_GRACE == timedelta(minutes=30)
    assert ISSUE == datetime(2026, 8, 27, 10, 30, tzinfo=UTC)


def test_the_recompute_lives_under_its_own_method_version() -> None:
    assert METHOD_VERSION == "metar_physical_tmax_from_conditioned_remaining_warming_v5"
