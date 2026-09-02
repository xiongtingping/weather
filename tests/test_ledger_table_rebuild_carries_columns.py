"""A table rebuild must carry every column, not every column the DDL spells.

SQLite cannot drop a constraint in place, so the ledger rebuilds
``paper_trade_position`` by renaming it, recreating it and copying the rows
across.  The copy used to take the intersection of the new table's columns
and the old one's -- which quietly excludes every column added by ALTER
after the DDL, and three of this table's are.

Measured 2026-09-02, after the fact: the rebuild that removed the
one-seat-per-stage constraint blanked ``selection_rule_sha256`` and
``consolidation_json`` on all 460 rows that predated it.  The whole page then
labelled every basket "chosen under a retired rule", because the empty
string matches no rule set at all, and nothing anywhere said a column had
been dropped.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as L  # noqa: E402


def _seeded(tmp_path: Path) -> sqlite3.Connection:
    """A position table with the constraint, and with the ALTER columns
    carrying values -- which is the state the rebuild has to survive."""

    conn = sqlite3.connect(tmp_path / "book.db")
    # The DDL as it was before the constraint came off, so the rebuild has
    # something real to remove.  Spliced in before the closing paren rather
    # than by replacing the first one, which lands inside a CHECK clause.
    body = L._POSITION_DDL.rstrip().rstrip(";").rstrip()
    assert body.endswith(")")
    conn.execute(
        body[:-1] + ", UNIQUE(station_id, event_date, stage))"
    )
    for name in ("content_sha256", "consolidation_json",
                 "selection_rule_sha256"):
        existing = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(paper_trade_position)")
        }
        if name not in existing:
            conn.execute(
                "ALTER TABLE paper_trade_position"
                f" ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )
    columns = [
        str(row[1])
        for row in conn.execute("PRAGMA table_info(paper_trade_position)")
    ]
    values = {
        "position_id": "paper-pos-test",
        "station_id": "AAAA",
        "event_date": "2026-08-20",
        "stage": "P0",
        "content_sha256": "content-sha",
        "consolidation_json": '{"count": 2}',
        "selection_rule_sha256": "rule-sha",
        "selection_rule_label": "some-label",
    }
    conn.execute(
        f"INSERT INTO paper_trade_position({','.join(columns)})"
        f" VALUES({','.join('?' * len(columns))})",
        [values.get(name, 0 if name.endswith("_usdc") else "")
         for name in columns],
    )
    conn.commit()
    return conn


def test_the_rebuild_carries_the_columns_the_ddl_does_not_spell(
    tmp_path,
) -> None:
    conn = _seeded(tmp_path)
    try:
        L._rebuild_position_without_scope_unique(conn)
        row = conn.execute(
            "SELECT content_sha256, consolidation_json,"
            " selection_rule_sha256, selection_rule_label"
            " FROM paper_trade_position"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("content-sha", '{"count": 2}', "rule-sha", "some-label"), (
        "the rebuild dropped a column that only exists because of an ALTER"
    )


def test_the_rebuild_carries_every_row(tmp_path) -> None:
    conn = _seeded(tmp_path)
    try:
        L._rebuild_position_without_scope_unique(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_trade_position"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_rebuild_actually_removes_the_constraint(tmp_path) -> None:
    """The reason the rebuild exists: a stage must be able to seat twice."""

    conn = _seeded(tmp_path)
    try:
        L._rebuild_position_without_scope_unique(conn)
        assert not L._position_scope_unique_present(conn)
    finally:
        conn.close()


def test_a_rebuild_that_would_drop_a_column_raises_instead(tmp_path) -> None:
    """Silence is the failure mode this whole file is about.

    If a column cannot be carried the rebuild must abort and name it, and
    the rollback must leave the old table in place for someone to look at --
    rather than committing a table that is quietly one column short.
    """

    class RefusesOneAdd(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "ADD COLUMN odd_column" in str(sql):
                return super().execute("SELECT 1")
            return super().execute(sql, *args, **kwargs)

    conn = _seeded(tmp_path)
    conn.execute("ALTER TABLE paper_trade_position ADD COLUMN odd_column TEXT")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(tmp_path / "book.db", factory=RefusesOneAdd)
    try:
        with pytest.raises(RuntimeError) as caught:
            L._rebuild_position_without_scope_unique(conn)
        assert "odd_column" in str(caught.value)
        # Rolled back, so the original table is still the one in place.
        names = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "paper_trade_position" in names
        assert conn.execute(
            "SELECT COUNT(*) FROM paper_trade_position"
        ).fetchone()[0] == 1
    finally:
        conn.close()
