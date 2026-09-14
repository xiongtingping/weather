"""Regime's fresh-scope probe reads only the envelopes appended since it last looked.

``SELECT DISTINCT station_id,event_date FROM basket_probability_envelope WHERE
state='COMMITTED' AND fresh_until_utc>?`` could not seek (the freshness column
is ninth in the only covering index) and walked ~448,000 envelopes every cycle
for ~100 scopes: 7-8s on 2026-09-14.  The table is append-only by trigger, so
a scope's newest freshness only grows and only by rows past the last rowid.
The answer must be that statement's, at any cutoff, after any appends.
"""

from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.production_worker import TargetProductionWorker

SQL = (
    "SELECT DISTINCT station_id,event_date FROM basket_probability_envelope"
    " WHERE state='COMMITTED' AND fresh_until_utc>? ORDER BY station_id,event_date"
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope(envelope_id TEXT PRIMARY KEY, station_id TEXT,"
        " event_date TEXT, fresh_until_utc TEXT, state TEXT)"
    )
    return conn


def _append(conn, rows) -> None:
    start = conn.execute("SELECT COUNT(*) FROM basket_probability_envelope").fetchone()[0]
    conn.executemany(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?)",
        [(f"env-{start + i}", *row) for i, row in enumerate(rows)],
    )
    conn.commit()


def _worker(conn) -> TargetProductionWorker:
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    return worker


CUTOFFS = ("2026-09-14T00:00:00Z", "2026-09-14T06:00:00Z", "2026-09-14T09:30:00.500000Z", "2026-09-15T00:00:00Z")


def _expect(conn, cutoff):
    return [tuple(row) for row in conn.execute(SQL, (cutoff,))]


def test_the_answer_is_the_statements_through_appends(monkeypatch) -> None:
    conn = _conn()
    _append(conn, [
        ("KMIA", "2026-09-14", "2026-09-14T05:00:00Z", "COMMITTED"),
        ("KMIA", "2026-09-14", "2026-09-14T08:00:00Z", "COMMITTED"),
        ("RJTT", "2026-09-14", "2026-09-14T09:30:00.400000Z", "COMMITTED"),
        ("EGLC", "2026-09-13", None, "COMMITTED"),
        ("ZUUU", "2026-09-14", "2026-09-14T23:00:00Z", "STAGED"),
    ])
    worker = _worker(conn)
    for cutoff in CUTOFFS:
        assert worker._fresh_committed_envelope_scopes(fresh_after_utc=cutoff) == _expect(conn, cutoff)

    _append(conn, [
        ("RJTT", "2026-09-14", "2026-09-14T11:00:00Z", "COMMITTED"),     # fresher than held
        ("KMIA", "2026-09-14", "2026-09-14T01:00:00Z", "COMMITTED"),     # older than held
        ("EGLC", "2026-09-13", "2026-09-14T07:00:00Z", "COMMITTED"),     # first freshness
        ("ZUUU", "2026-09-14", "2026-09-15T01:00:00Z", "BLOCKED"),       # never counts
        ("CYYZ", "2026-08-24", "2026-09-14T12:00:00Z", "COMMITTED"),     # a replay of an old day
    ])
    expected = {cutoff: _expect(conn, cutoff) for cutoff in CUTOFFS}
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    answers = {
        cutoff: worker._fresh_committed_envelope_scopes(fresh_after_utc=cutoff)
        for cutoff in CUTOFFS
    }
    conn.set_trace_callback(None)
    assert answers == expected
    envelope_reads = [sql for sql in statements if "basket_probability_envelope" in sql]
    assert envelope_reads and all("rowid>" in sql for sql in envelope_reads), envelope_reads


def test_a_replaced_connection_is_read_from_the_start() -> None:
    first = _conn()
    _append(first, [("KMIA", "2026-09-14", "2026-09-14T08:00:00Z", "COMMITTED")])
    worker = _worker(first)
    assert worker._fresh_committed_envelope_scopes(fresh_after_utc=CUTOFFS[0]) == [("KMIA", "2026-09-14")]
    second = _conn()
    _append(second, [("RJTT", "2026-09-14", "2026-09-14T08:00:00Z", "COMMITTED")])
    worker.conn = second
    assert worker._fresh_committed_envelope_scopes(fresh_after_utc=CUTOFFS[0]) == [("RJTT", "2026-09-14")]
