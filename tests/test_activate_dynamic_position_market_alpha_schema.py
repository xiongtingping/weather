from __future__ import annotations

import sqlite3

from scripts.activate_dynamic_position_market_alpha_schema import (
    CONFIRMATION,
    apply,
    inspect,
)


def _source_and_backup(tmp_path):
    source = tmp_path / "archive.db"
    backup = tmp_path / "archive.backup.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE base(id INTEGER PRIMARY KEY,value TEXT)")
        conn.execute("INSERT INTO base(value) VALUES('current')")
    with sqlite3.connect(source) as left, sqlite3.connect(backup) as right:
        left.backup(right)
    return source, backup


def test_default_inspection_is_read_only_and_apply_requires_explicit_guards(tmp_path) -> None:
    source, backup = _source_and_backup(tmp_path)
    before = inspect(source)
    assert before["state"] == "apply_required"
    assert before["market_alpha_schema"]["ok"] is False
    assert before["read_only"] is True

    blocked = apply(
        source, backup=backup, writer_quiescent=False, confirmation=CONFIRMATION
    )
    assert blocked["state"] == "blocked"
    assert "writer_quiescent_assertion_required" in blocked["block_reasons"]

    result = apply(
        source, backup=backup, writer_quiescent=True, confirmation=CONFIRMATION
    )
    assert result["ok"] is True
    assert result["changes_activation"] is False
    with sqlite3.connect(source) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "dynamic_three_leg_position_lots",
        "dynamic_three_leg_position_lot_events",
        "market_alpha_take_profit_cycles",
        "dynamic_three_leg_continuation_events",
        "dynamic_three_leg_continuation_transition_plans",
    } <= tables
