"""Charter 177: a cycle says which SQL it spent its time in.

The statement probe already sees every statement a thread runs.  It now also
keeps, per statement text, a count and the time spent in execute and in the
fetches, and each production cycle publishes its costliest texts into the
cycle history.  On 2026-09-21 three sampled stacks in a row landed on
queries that cost 1-2ms, while the two costing 75ms and 72ms a call were
found only by profiling one stage offline.
"""

import ast
import inspect
import sqlite3
import textwrap
import threading

from bayes_basket_target import production_worker as pw
from scripts import weather_basket_yes_daemon as daemon


def _conn():
    conn = sqlite3.connect(":memory:", isolation_level=None, factory=pw._ProbedConnection)
    conn.execute("CREATE TABLE t(x INTEGER PRIMARY KEY, y TEXT)")
    conn.executemany("INSERT INTO t(y) VALUES(?)", [(str(i),) for i in range(200)])
    return conn


def test_each_text_is_counted_and_charged_execute_plus_fetch() -> None:
    conn = _conn()
    pw.begin_statement_ledger()
    for i in range(30):
        conn.execute("SELECT y FROM t WHERE x=?", (i + 1,)).fetchone()
    rows = conn.execute("SELECT y FROM t WHERE x>?", (0,)).fetchall()
    assert len(rows) == 200
    for _row in conn.execute("SELECT   x\n  FROM t"):
        pass
    top = {row["sql"]: row for row in pw.statement_ledger_top(10)}
    assert top["SELECT y FROM t WHERE x=?"]["count"] == 30
    assert top["SELECT y FROM t WHERE x>?"]["count"] == 1
    # whitespace is folded, so one statement is one key however it is laid out
    assert top["SELECT x FROM t"]["count"] == 1
    for row in top.values():
        assert row["total_ms"] >= row["execute_ms"] >= 0
        assert abs(row["total_ms"] - row["execute_ms"] - row["fetch_ms"]) <= 0.2


def test_the_ledger_is_per_thread() -> None:
    conn_here = _conn()
    pw.begin_statement_ledger()
    conn_here.execute("SELECT 1").fetchone()
    seen_there = {}

    def other():
        conn = sqlite3.connect(":memory:", isolation_level=None, factory=pw._ProbedConnection)
        pw.begin_statement_ledger()
        conn.execute("SELECT 2").fetchone()
        seen_there["top"] = pw.statement_ledger_top(5)

    worker = threading.Thread(target=other)
    worker.start()
    worker.join(5)
    here = {row["sql"] for row in pw.statement_ledger_top(20)}
    there = {row["sql"] for row in seen_there["top"]}
    assert "SELECT 1" in here and "SELECT 2" not in here
    assert there == {"SELECT 2"}


def test_a_thread_without_a_ledger_pays_nothing_and_records_nothing() -> None:
    pw._probe_slot().pop("ledger", None)
    conn = _conn()
    conn.execute("SELECT 3").fetchone()
    assert pw.statement_ledger_top(5) == []


def test_the_ledger_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(pw, "_STATEMENT_LEDGER_MAX_KEYS", 5)
    conn = _conn()
    pw.begin_statement_ledger()
    for i in range(12):
        conn.execute(f"SELECT {i}").fetchone()
    ledger = pw._probe_slot()["ledger"]
    assert len(ledger) <= 6
    assert ledger[pw._STATEMENT_LEDGER_OTHER][0] == 12 - 5


def test_every_cycle_starts_a_fresh_ledger_and_publishes_its_top() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(pw.TargetProductionWorker.run)))
    calls = {
        getattr(node.func, "id", getattr(node.func, "attr", ""))
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert {"begin_statement_ledger", "statement_ledger_top"} <= calls


def test_the_cycle_history_keeps_the_top_statements() -> None:
    result = {
        "stage_timings_ms": {"cycle_total": 1000.0, "decision_loop": 400.0},
        "stage_span_ms": {},
        "statement_ledger_top": [
            {"sql": "SELECT a FROM b WHERE c=?", "count": 86, "total_ms": 17500.0,
             "execute_ms": 1000.0, "fetch_ms": 16500.0},
        ],
    }
    row = daemon.production_cycle_history_row(
        result, cycle_index=3, process_started_at_utc="t0", completed_at_utc="t1",
    )
    assert row["sql_top"] == [
        {"sql": "SELECT a FROM b WHERE c=?", "count": 86, "total_ms": 17500.0, "fetch_ms": 16500.0}
    ]
