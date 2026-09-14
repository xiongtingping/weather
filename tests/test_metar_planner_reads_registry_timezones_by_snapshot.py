"""Registry timezone reads are the full DISTINCT, snapshot by snapshot.

``plan_metar_physical_member_runs`` ran ``SELECT DISTINCT station_id,
settlement_timezone FROM basket_target_market_registry`` once a production
cycle.  No index carries the timezone, so it read ~390,000 rows back from the
table for 49 pairs: 19.3s cold on 2026-09-14, and ``metar_proj_plan`` spans of
29s and 75s in consecutive cycles for a planner that nearly always wrote
nothing.  The TAF inbox ingest asked the same table per scope: 23.7s for 457
scopes.  Snapshots are written whole and removed whole, and a re-ingested
snapshot inserts nothing, so each is read once.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import market_registry as mr
from bayes_basket_target import metar_physical_forward as mpf


def _db(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE basket_target_market_registry_snapshot(snapshot_sha256 TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        "snapshot_sha256 TEXT, slug TEXT, station_id TEXT, event_date TEXT, settlement_timezone TEXT)"
    )
    conn.execute(
        "CREATE INDEX basket_target_market_registry_latest_idx"
        " ON basket_target_market_registry(snapshot_sha256, station_id)"
    )
    return conn


def _ingest(conn, sha, rows) -> None:
    conn.execute("INSERT INTO basket_target_market_registry_snapshot VALUES(?)", (sha,))
    conn.executemany(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?)",
        [(sha, f"{station}-{i}", station, "2026-09-14", zone) for i, (station, zone) in enumerate(rows)],
    )
    conn.commit()


def _full(conn) -> set[tuple[str, str]]:
    return set(conn.execute(
        "SELECT DISTINCT station_id,event_date,settlement_timezone FROM basket_target_market_registry"
    ).fetchall())


@pytest.fixture(autouse=True)
def _fresh_memo():
    mr._SNAPSHOT_SCOPE_TIMEZONES.clear()
    yield
    mr._SNAPSHOT_SCOPE_TIMEZONES.clear()


def test_the_answer_is_the_full_distinct_through_ingest_and_retention(tmp_path) -> None:
    conn = _db(tmp_path / "ssot.db")
    _ingest(conn, "s1", [("EHAM", "Europe/Amsterdam"), ("KLAX", "America/Los_Angeles"), ("KLAX", "")])
    _ingest(conn, "s2", [("EHAM", "Europe/Amsterdam"), ("RJTT", "Asia/Tokyo")])
    assert mr.registry_scope_timezone_triples(conn) == _full(conn)

    # A new snapshot names a second zone for a station: the planner's
    # ambiguity guard must see it.
    _ingest(conn, "s3", [("KLAX", "America/Denver")])
    assert mr.registry_scope_timezone_triples(conn) == _full(conn)
    assert ("KLAX", "2026-09-14", "America/Denver") in _full(conn)

    # Retention removes a snapshot's rows and keeps its snapshot row.
    conn.execute("DELETE FROM basket_target_market_registry WHERE snapshot_sha256='s3'")
    conn.execute("DELETE FROM basket_target_market_registry WHERE snapshot_sha256='s1'")
    conn.commit()
    assert mr.registry_scope_timezone_triples(conn) == _full(conn)
    assert ("KLAX", "2026-09-14", "America/Denver") not in mr.registry_scope_timezone_triples(conn)


def test_a_remembered_snapshot_is_not_read_again(tmp_path) -> None:
    conn = _db(tmp_path / "ssot.db")
    _ingest(conn, "s1", [("EHAM", "Europe/Amsterdam")])
    mr.registry_scope_timezone_triples(conn)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    mr.registry_scope_timezone_triples(conn)
    conn.set_trace_callback(None)
    assert not any("SELECT DISTINCT" in sql for sql in statements), statements


def test_an_in_memory_database_is_never_remembered() -> None:
    first = _db(":memory:")
    _ingest(first, "same", [("EHAM", "Europe/Amsterdam")])
    second = _db(":memory:")
    _ingest(second, "same", [("EHAM", "Europe/London")])
    assert mr.registry_scope_timezone_triples(first) == {("EHAM", "2026-09-14", "Europe/Amsterdam")}
    assert mr.registry_scope_timezone_triples(second) == {("EHAM", "2026-09-14", "Europe/London")}
    assert not mr._SNAPSHOT_SCOPE_TIMEZONES


def test_empty_and_null_timezones_are_part_of_the_answer_as_they_were(tmp_path) -> None:
    conn = _db(tmp_path / "ssot.db")
    _ingest(conn, "s1", [("KLAX", ""), ("KLAX", None), ("KLAX", "America/Los_Angeles")])
    assert mr.registry_scope_timezone_triples(conn) == _full(conn)
    assert len(_full(conn)) == 3


def test_the_planner_no_longer_issues_the_whole_table_distinct(tmp_path) -> None:
    from bayes_basket_target.market_registry import apply_target_market_registry_schema
    from bayes_basket_target.ssot import apply_production_ssot_schema

    conn = sqlite3.connect(str(tmp_path / "ssot.db"))
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    archive = sqlite3.connect(":memory:")
    archive.execute(
        "CREATE TABLE reported_trajectory_observations(observation_id TEXT, station_id TEXT,"
        " source_lane TEXT, valid_at TEXT, reported_temperature_c REAL, raw_text TEXT)"
    )
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    mpf.plan_metar_physical_member_runs(conn, archive_conn=archive, as_of_utc="2026-09-14T08:00:00Z")
    conn.set_trace_callback(None)
    registry_reads = [sql for sql in statements if "basket_target_market_registry" in sql]
    assert any("FROM basket_target_market_registry_snapshot" in sql for sql in registry_reads), registry_reads
    assert not any(
        "DISTINCT" in sql and "snapshot_sha256=?" not in sql for sql in registry_reads
    ), registry_reads
