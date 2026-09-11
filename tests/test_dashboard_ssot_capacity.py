"""The board's view of the one limit that stops everything.

When the target store passes ``MAX_TARGET_DB_BYTES`` every writer
fail-closes on ``target_database_bytes_exceeded`` and every model run reads
stale from that moment.  The ceiling has been raised four times and each
raise bought days -- and none of it was on the page: the number lived in
``status.json``, and the time axis that makes it actionable reported
``null`` for three days because it measured the file while free pages
absorbed the growth.

Two things this section has to get right:

* The gauge is READ, not recomputed.  A second implementation on the board
  would eventually disagree with the guard that actually fail-closes the
  writers, and then the page argues with the thing it is reporting.
* "Writes a lot" is not "is growing".  A table a retention lane deletes
  from again is churn; counting it as growth produces a ceiling date that
  never arrives.  ``basket_target_market_registry`` writes ~52 MB a day and
  has been flat since its retention shipped on 2026-09-07 -- it went from
  1.47M rows to 261k -- so it is named apart rather than summed in.
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

GUARD = {
    "database_bytes": 38_184_861_696,
    "max_database_bytes": 51_539_607_552,
    "remaining_bytes": 13_354_745_856,
    "warn_remaining_bytes": 1_073_741_824,
    "warn_days_to_ceiling": 2.0,
    "growth_bytes_per_day": 710_000_000.0,
    "days_to_ceiling": 18.8,
    "free_page_bytes": 77_078_528,
    "live_bytes": 38_107_783_168,
    "growth_axis": "live_bytes",
    "state": "within_budget",
    "warnings": [],
}


@pytest.fixture()
def board(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(
        C, "_now", lambda: datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    )
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({"target_ssot_capacity": GUARD}), encoding="utf-8"
    )
    monkeypatch.setattr(C, "STATUS_JSON", status)

    conn = sqlite3.connect(":memory:")
    # One append-only table and one a retention lane deletes from.
    conn.execute(
        "CREATE TABLE basket_official_ws_snapshot ("
        " snapshot_id TEXT PRIMARY KEY, observed_at TEXT, buckets_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_official_ws_snapshot VALUES (?,?,?)",
        [(f"s{i}", "2026-09-11T06:00:00Z", "x" * 2000) for i in range(50)]
        + [(f"o{i}", "2026-09-01T06:00:00Z", "x" * 2000) for i in range(500)],
    )
    conn.execute(
        "CREATE TABLE basket_target_market_registry ("
        " snapshot_sha256 TEXT PRIMARY KEY, refreshed_at TEXT,"
        " registry_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_target_market_registry VALUES (?,?,?)",
        [(f"r{i}", "2026-09-11T06:00:00Z", "y" * 4000) for i in range(80)],
    )
    conn.commit()
    return C.collect_ssot_capacity(conn)


def test_the_gauge_is_read_from_the_guard_not_recomputed(board):
    for key, value in GUARD.items():
        assert board[key] == value, (
            f"{key} must arrive from the guard verbatim; a second"
            " implementation on the board would drift from the one that"
            " actually fail-closes the writers"
        )
    assert board["available"] is True


def test_churn_is_named_apart_from_growth(board):
    by_name = {row["table"]: row for row in board["tables"]}
    ws = by_name["basket_official_ws_snapshot"]
    registry = by_name["basket_target_market_registry"]
    assert ws["retained"] is False
    assert registry["retained"] is True, (
        "a table its retention lane deletes from again is not growth"
    )
    # 50 rows in the last day for ws, 80 for the registry -- the registry
    # writes MORE and contributes nothing to the ceiling.
    assert ws["rows_per_day"] == 50
    assert registry["rows_per_day"] == 80
    assert registry["bytes_per_day"] > ws["bytes_per_day"]
    # And the net figure excludes it entirely.
    assert board["net_payload_bytes_per_day"] == pytest.approx(
        ws["bytes_per_day"]
    )
    assert board["churn_payload_bytes_per_day"] == pytest.approx(
        registry["bytes_per_day"]
    )
    assert board["largest_table"] == "basket_official_ws_snapshot"
    assert board["largest_share"] == pytest.approx(1.0)


def test_row_width_comes_from_the_newest_rows(board):
    """The next day is written at today's width, not the table average.

    The ws snapshot's rows grew from 8.9 KB to 20.3 KB as the books
    deepened; averaging the whole table describes a day that is not coming
    back.
    """

    ws = next(
        row for row in board["tables"]
        if row["table"] == "basket_official_ws_snapshot"
    )
    # Every row in the fixture is the same width, so this only pins that a
    # width is measured at all and that bytes follow from it.
    assert ws["row_bytes"] > 2000
    assert ws["bytes_per_day"] == pytest.approx(
        ws["rows_per_day"] * ws["row_bytes"]
    )


def test_a_board_without_a_guard_says_so_rather_than_inventing_one(
    tmp_path, monkeypatch
):
    status = tmp_path / "status.json"
    status.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(C, "STATUS_JSON", status)
    out = C.collect_ssot_capacity(sqlite3.connect(":memory:"))
    assert out["available"] is False
    assert out["tables"] == []


def test_the_reading_reaches_the_health_lamp():
    """The section is a lamp now (user ruling, 2026-09-11).

    It used to be a gauge, three tiles and a per-table growth table; the
    ruling cut the health group to status lights, so what is pinned here is
    that the collector still ships and the lamp board still reads it -- not
    that a section by that name is registered.
    """

    page = (ROOT / "scripts" / "calibration_dashboard.py").read_text()
    assert '[healthLights,"系统状态灯"]' in page
    assert "D.ssot_capacity" in page
    assert '"ssot_capacity": ssot_capacity,' in page
    # The composition walk counts rows over a day on ten tables; it is the
    # only part that is cached, and the gauge above it never is.
    assert '"ssot_capacity": 45 * 60,' in page
