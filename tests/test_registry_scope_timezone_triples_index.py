"""Charter 177: scope timezones are read from the covering index when it exists.

The union over retained snapshots was built to cost less than a whole-table
DISTINCT that had to visit every row's page.  With the timezone in an index
(charter 176) the whole-table DISTINCT never touches the table: 226ms cold
and 55ms warm on the live store, against 9,671ms cold for the union over
2,572 snapshots -- paid by three callers on every restart.  Same answer by
definition; a store without the index keeps the union.
"""

import sqlite3

import pytest

from bayes_basket_target import market_registry as mr

INDEX = mr.SCOPE_TIMEZONE_INDEX


def _store(*, with_index: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE basket_target_market_registry_snapshot(snapshot_sha256 TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        "snapshot_sha256 TEXT, slug TEXT, station_id TEXT, event_date TEXT, settlement_timezone TEXT)"
    )
    conn.execute(
        "CREATE INDEX basket_target_market_registry_latest_idx"
        " ON basket_target_market_registry(snapshot_sha256, station_id)"
    )
    if with_index:
        conn.execute(
            f"CREATE INDEX {INDEX} ON basket_target_market_registry"
            "(station_id,event_date,settlement_timezone)"
        )
    for snap, rows in enumerate((
        [("KLAX", "America/Los_Angeles"), ("KLAX", ""), ("EHAM", "Europe/Amsterdam")],
        [("KLAX", "America/Los_Angeles"), ("KLAX", None)],
    )):
        sha = f"{snap:064x}"
        conn.execute("INSERT INTO basket_target_market_registry_snapshot VALUES(?)", (sha,))
        conn.executemany(
            "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?)",
            [(sha, f"{st}-{i}", st, f"2026-09-2{snap}", zone) for i, (st, zone) in enumerate(rows)],
        )
    return conn


def _distinct(conn):
    return set(conn.execute(
        "SELECT DISTINCT station_id,event_date,settlement_timezone FROM basket_target_market_registry"
    ).fetchall())


@pytest.fixture(autouse=True)
def _fresh_memo():
    mr._SNAPSHOT_ANSWERS.clear()
    yield
    mr._SNAPSHOT_ANSWERS.clear()


def test_with_the_index_the_answer_is_the_whole_table_distinct() -> None:
    conn = _store(with_index=True)
    answer = mr.registry_scope_timezone_triples(conn)
    assert answer == _distinct(conn)
    # blank and NULL timezones are part of the answer, exactly as before
    assert {("KLAX", "2026-09-20", ""), ("KLAX", "2026-09-21", None)} <= answer
    assert not mr._SNAPSHOT_ANSWERS  # the union was never consulted


def test_without_the_index_the_union_answers_the_same() -> None:
    conn = _store(with_index=False)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    answer = mr.registry_scope_timezone_triples(conn)
    conn.set_trace_callback(None)
    assert answer == _distinct(conn)
    # ...and it was the per-snapshot union that answered, not a table walk.
    assert any("snapshot_sha256=" in sql for sql in statements), statements
    assert not any(f"INDEXED BY {INDEX}" in sql for sql in statements), statements


def test_the_indexed_read_is_answered_from_the_index_alone() -> None:
    conn = _store(with_index=True)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    mr.registry_scope_timezone_triples(conn)
    conn.set_trace_callback(None)
    reads = [sql for sql in statements if "FROM basket_target_market_registry" in sql]
    assert reads and all(f"INDEXED BY {INDEX}" in sql for sql in reads), reads
    plan = " | ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + reads[0]))
    assert f"COVERING INDEX {INDEX}" in plan, plan
