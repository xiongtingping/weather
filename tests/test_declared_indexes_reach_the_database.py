"""Declared in ssot.py is not the same as present in the database, and nothing said so.

The resident does not apply this schema; only the controlled migration does.
So an index added to the DDL reaches production when that migration next
runs -- and the schema status, which is what anyone would look at, compares
tables and columns and had never looked at an index at all.

Measured 2026-09-11 on the live SSOT: 89 indexes declared, 87 present.  One of
the two missing, ``basket_probability_envelope_committed_scope_idx``, had been
declared since 2026-08-16 carrying its own measurement in the comment above it
-- 13.0-15.6s per cycle, "also retires the temp b-tree" -- and had never
existed in production for a single cycle.  ``production_ssot_schema.ok`` read
true throughout, and so did the migration plan.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.ssot import (  # noqa: E402
    apply_production_ssot_schema,
    declared_index_names,
    missing_declared_indexes,
    production_ssot_schema_status,
)


def _fresh(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "ssot.db")
    apply_production_ssot_schema(conn)
    return conn


def test_the_declaration_scan_finds_a_real_number_of_indexes():
    """A regex that matched nothing would make every test here vacuous."""

    declared = declared_index_names()

    assert len(declared) >= 80, len(declared)
    assert (
        declared["basket_probability_envelope_scope_distinct_idx"]
        == "basket_probability_envelope"
    )


def test_an_index_for_a_table_this_database_does_not_have_is_not_missing(
    tmp_path,
):
    """The republisher schema declares eleven indexes that the production
    apply deliberately does not create.  Reporting those as missing would
    make the check cry wolf on every fresh database."""

    conn = _fresh(tmp_path)
    declared = declared_index_names()
    absent_tables = {
        table for table in declared.values()
        if table not in {
            str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    }

    assert absent_tables, "fixture no longer exercises the case"
    assert missing_declared_indexes(conn) == []


def test_a_freshly_applied_schema_is_missing_none(tmp_path):
    assert missing_declared_indexes(_fresh(tmp_path)) == []


def test_an_index_the_database_lacks_is_named(tmp_path):
    conn = _fresh(tmp_path)
    conn.execute("DROP INDEX basket_probability_envelope_scope_distinct_idx")
    conn.commit()

    assert missing_declared_indexes(conn) == [
        "basket_probability_envelope_scope_distinct_idx"
    ]


def test_the_status_names_it_without_refusing_to_start(tmp_path):
    """Reported, not fatal -- and that is a decision, so it is pinned.

    The daemon reads this status.  Folding a missing index into ``ok`` would
    refuse to start the resident over a performance index, and two were
    missing from the live database the day this was written.
    """

    conn = _fresh(tmp_path)
    conn.execute("DROP INDEX basket_probability_envelope_scope_distinct_idx")
    conn.commit()

    status = production_ssot_schema_status(conn)

    assert status["missing_indexes"] == [
        "basket_probability_envelope_scope_distinct_idx"
    ]
    assert status["ok"] is True, "a missing index must not refuse the resident"
