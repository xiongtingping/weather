"""Registry history keeps its identity and loses its row copies.

Every refresh writes a complete copy of the fleet under a new snapshot sha
and nothing deleted the previous one: 1,176 copies by 2026-09-07, 1,471,712
rows for about 2,810 markets, 2.58 GB, 270-320 MB a day, in a database 4.78
GB from its ceiling and 6.1 days from reaching it at that rate.

The copies were not only disk.  No reader scopes a query to a snapshot, so
every registry read saw the union of all of them: one
``SELECT DISTINCT settlement_timezone WHERE station_id=? AND event_date=?``
scanned 2,233 rows for one answer, and that lookup runs once per scope in
the TAF ingest -- 98.8s of a 197.2s stage on cold pages.

Two things must survive: the snapshot headers, because a decision names the
registry it was made against, and the newest snapshot's rows, because a
fleet that stops refreshing must not lose its current registry to a clock.
"""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.market_registry_retention import (
    apply_market_registry_retention,
    plan_market_registry_retention,
)

NOW = "2026-09-07T16:00:00Z"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE basket_target_market_registry_snapshot ("
        " snapshot_sha256 TEXT PRIMARY KEY, ingested_at_utc TEXT NOT NULL,"
        " row_count INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE basket_target_market_registry ("
        " snapshot_sha256 TEXT NOT NULL, station_id TEXT, event_date TEXT,"
        " settlement_timezone TEXT)"
    )
    return connection


def _snapshot(conn: sqlite3.Connection, sha: str, ingested: str, rows: int = 3) -> None:
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot("
        "snapshot_sha256,ingested_at_utc,row_count) VALUES(?,?,?)",
        (sha, ingested, rows),
    )
    conn.executemany(
        "INSERT INTO basket_target_market_registry("
        "snapshot_sha256,station_id,event_date,settlement_timezone)"
        " VALUES(?,?,?,?)",
        [(sha, f"K{i:03d}", "2026-09-07", "America/Chicago") for i in range(rows)],
    )


def _rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM basket_target_market_registry"
    ).fetchone()[0])


def _headers(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM basket_target_market_registry_snapshot"
    ).fetchone()[0])


def test_old_copies_go_and_every_header_stays(conn: sqlite3.Connection) -> None:
    """The sha a decision references must outlive the rows nobody reads."""

    _snapshot(conn, "a" * 64, "2026-09-01T00:00:00Z")
    _snapshot(conn, "b" * 64, "2026-09-02T00:00:00Z")
    _snapshot(conn, "c" * 64, "2026-09-07T12:00:00Z")
    assert _rows(conn) == 9

    plan = plan_market_registry_retention(conn, as_of_utc=NOW)
    assert plan["stale_snapshot_count"] == 2
    receipt = apply_market_registry_retention(conn, plan)

    assert receipt["deleted_snapshot_count"] == 2
    assert receipt["deleted_row_count"] == 6
    assert _rows(conn) == 3
    assert _headers(conn) == 3, "a header carries identity and never expires"


def test_the_newest_snapshot_survives_its_own_age(conn: sqlite3.Connection) -> None:
    """A fleet that stopped refreshing must not lose its current registry.

    Without this the retention turns a stalled publisher into an empty
    registry, which is a far worse failure than a large one.
    """

    _snapshot(conn, "a" * 64, "2026-08-01T00:00:00Z")
    _snapshot(conn, "b" * 64, "2026-08-02T00:00:00Z")

    plan = plan_market_registry_retention(conn, as_of_utc=NOW)
    assert plan["newest_snapshot_sha256"] == "b" * 64
    apply_market_registry_retention(conn, plan)
    assert _rows(conn) == 3
    assert conn.execute(
        "SELECT DISTINCT snapshot_sha256 FROM basket_target_market_registry"
    ).fetchone()[0] == "b" * 64


def test_a_pass_with_nothing_stale_plans_no_write(conn: sqlite3.Connection) -> None:
    """The common pass must not open a write transaction, for the same
    reason the metar projection was split into plan and apply."""

    _snapshot(conn, "c" * 64, "2026-09-07T12:00:00Z")
    plan = plan_market_registry_retention(conn, as_of_utc=NOW)
    assert plan["stale_snapshots"] == []
    assert plan["stale_snapshot_count"] == 0


def test_one_pass_is_bounded_and_says_what_it_deferred(
    conn: sqlite3.Connection,
) -> None:
    """One writer serves everything here, so a sweep that frees 900,000
    rows in one transaction is an outage.  Whole snapshots, oldest first,
    so an interrupted pass leaves complete copies rather than half of one.
    """

    for index in range(5):
        _snapshot(conn, f"{index:064d}", f"2026-09-0{index + 1}T00:00:00Z")
    _snapshot(conn, "f" * 64, "2026-09-07T12:00:00Z")

    plan = plan_market_registry_retention(conn, as_of_utc=NOW)
    receipt = apply_market_registry_retention(conn, plan, max_rows=4)

    assert receipt["deleted_snapshot_count"] == 2
    assert receipt["deferred_snapshot_count"] == 2
    # Whole snapshots only: no snapshot is left half deleted.
    remaining = {
        str(row[0]) for row in conn.execute(
            "SELECT snapshot_sha256, COUNT(*) FROM basket_target_market_registry"
            " GROUP BY snapshot_sha256 HAVING COUNT(*) <> 3"
        )
    }
    assert not remaining, remaining


def test_each_recent_station_day_keeps_the_registry_it_resolves_to(
    conn: sqlite3.Connection,
) -> None:
    """The paper ledger replays production's own decisions, and a compile
    cannot run without its station-day's registry (user ruling 2026-09-24:
    「账本对齐」).  Old snapshots still go -- except, for each station-day
    of the last REPLAY_RETENTION_DAYS, the newest snapshot that lists it.
    On 2026-09-25 no event date before 09-21 could be recompiled at all."""

    def rows(sha: str, day: str, stations: range) -> None:
        conn.executemany(
            "INSERT INTO basket_target_market_registry("
            "snapshot_sha256,station_id,event_date,settlement_timezone) VALUES(?,?,?,?)",
            [(sha, f"K{i:03d}", day, "America/Chicago") for i in stations],
        )

    def header(sha: str, ingested: str) -> None:
        conn.execute(
            "INSERT INTO basket_target_market_registry_snapshot("
            "snapshot_sha256,ingested_at_utc,row_count) VALUES(?,?,0)",
            (sha, ingested),
        )

    # a: lists 09-02 and 09-03; b: lists 09-03 only, later -- the newest for
    # 09-03; c: the current fleet (09-07).  07-01 is past the replay window.
    header("a" * 64, "2026-09-01T00:00:00Z")
    rows("a" * 64, "2026-09-02", range(3))
    rows("a" * 64, "2026-09-03", range(3))
    rows("a" * 64, "2026-07-01", range(2))
    header("b" * 64, "2026-09-02T00:00:00Z")
    rows("b" * 64, "2026-09-03", range(3))
    header("c" * 64, "2026-09-07T12:00:00Z")
    rows("c" * 64, "2026-09-07", range(3))

    plan = plan_market_registry_retention(conn, as_of_utc=NOW)
    assert set(plan["stale_snapshots"]) == {"a" * 64}  # b only keeps 09-03
    assert plan["protected_scopes"]["a" * 64] == [f"K{i:03d}|2026-09-02" for i in range(3)]
    receipt = apply_market_registry_retention(conn, plan)
    assert receipt["deleted_row_count"] == 5  # a's 09-03 copies and 07-01

    left = conn.execute(
        "SELECT snapshot_sha256, event_date, COUNT(*) FROM basket_target_market_registry"
        " GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    assert left == [
        ("a" * 64, "2026-09-02", 3),
        ("b" * 64, "2026-09-03", 3),
        ("c" * 64, "2026-09-07", 3),
    ]
    # A kept scope is not re-planned every pass: nothing is left to delete.
    assert plan_market_registry_retention(conn, as_of_utc=NOW)["stale_snapshots"] == []
