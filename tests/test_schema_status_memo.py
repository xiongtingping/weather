"""The schema status is remembered per connection and per schema version.

Four hot loaders ask for it on their way in, about five hundred times a
cycle, and it was recomputed each time: 43 schema statements whose answer
could not have changed.  Now one ``PRAGMA schema_version`` decides whether
the remembered answer still stands; any DDL bumps it.
"""

import sqlite3

from bayes_basket_target import ssot


class _Counting:
    """A connection that counts the statements it is asked to run."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.statements = 0

    def execute(self, *args, **kwargs):
        self.statements += 1
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _store() -> _Counting:
    conn = sqlite3.connect(":memory:")
    ssot.apply_production_ssot_schema(conn)
    return _Counting(conn)


def test_the_second_ask_costs_one_statement_and_says_the_same() -> None:
    conn = _store()
    ssot.forget_schema_status()
    first = ssot.production_ssot_schema_status(conn)
    cost_first = conn.statements
    conn.statements = 0
    second = ssot.production_ssot_schema_status(conn)
    assert first["ok"] is True
    assert second == first
    assert cost_first >= 20
    assert conn.statements == 1  # PRAGMA schema_version


def test_a_ddl_on_the_connection_is_seen() -> None:
    conn = _store()
    ssot.forget_schema_status()
    assert ssot.production_ssot_schema_status(conn)["ok"] is True
    conn.execute("DROP TABLE basket_regime_artifact")
    conn.statements = 0
    status = ssot.production_ssot_schema_status(conn)
    assert status["ok"] is False
    assert conn.statements >= 20  # recomputed, not remembered


def test_two_connections_never_share_an_answer() -> None:
    full = _store()
    empty = _Counting(sqlite3.connect(":memory:"))
    ssot.forget_schema_status()
    assert ssot.production_ssot_schema_status(full)["ok"] is True
    assert ssot.production_ssot_schema_status(empty)["ok"] is False
    assert ssot.production_ssot_schema_status(full)["ok"] is True


def test_the_caller_gets_its_own_copy() -> None:
    conn = _store()
    ssot.forget_schema_status()
    status = ssot.production_ssot_schema_status(conn)
    status["ok"] = False
    status.setdefault("missing_tables", []).append("invented")
    again = ssot.production_ssot_schema_status(conn)
    assert again["ok"] is True
    assert "invented" not in (again.get("missing_tables") or [])


def test_forgetting_one_connection_recomputes_it() -> None:
    conn = _store()
    ssot.forget_schema_status()
    ssot.production_ssot_schema_status(conn)
    ssot.forget_schema_status(conn)
    conn.statements = 0
    ssot.production_ssot_schema_status(conn)
    assert conn.statements >= 20


def test_the_memo_stays_bounded() -> None:
    ssot.forget_schema_status()
    keep = []
    for _ in range(ssot._SCHEMA_STATUS_MEMO_LIMIT + 5):
        conn = sqlite3.connect(":memory:")
        keep.append(conn)
        ssot.production_ssot_schema_status(conn)
    assert len(ssot._SCHEMA_STATUS_MEMO) == ssot._SCHEMA_STATUS_MEMO_LIMIT
