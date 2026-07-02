from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import check_temperature_path_decision_traceability as audit


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER,
                run_date TEXT,
                slug TEXT,
                event_date TEXT,
                title TEXT,
                outcome TEXT,
                side TEXT,
                action TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                reasons_json TEXT,
                details_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _insert_entry(path: Path, details: dict) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
                run_date, slug, event_date, title, outcome, side, action,
                decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-06-21",
                "chengdu-test",
                "2026-06-21",
                "Chengdu",
                "25°C or below",
                "YES",
                "entry",
                "blocked",
                0,
                json.dumps(["temperature_path_trade_follow_not_allowed"]),
                "[]",
                json.dumps(details),
                "2026-06-21T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_traceability_audit_accepts_complete_temperature_path_block(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_db(db)
    _insert_entry(
        db,
        {
            "posterior_settlement_entry_allowed": False,
            "posterior_settlement_entry_block_reason": "temperature_path_trade_follow_not_allowed",
            "target_realization_trade_follow_allowed": False,
            "target_realization_trade_follow_reason": "blocked_path_reliability_low",
            "temperature_path_trade_follow_allowed": False,
            "temperature_path_trade_follow_block_yes": True,
            "temperature_path_trade_follow_reason": "blocked_path_reliability_low",
            "temperature_path_trade_follow_gate_source": "temperature_path_distribution.trade_follow",
        },
    )

    result = audit.check_db(db)

    assert result["ok"] is True
    assert result["temperature_path_block_rows"] == 1
    assert result["blockers"] == []


def test_traceability_audit_blocks_compacted_temperature_path_gap(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_db(db)
    _insert_entry(
        db,
        {
            "posterior_settlement_entry_allowed": False,
            "posterior_settlement_entry_block_reason": "temperature_path_trade_follow_not_allowed",
        },
    )

    result = audit.check_db(db)

    assert result["ok"] is False
    assert result["temperature_path_block_rows"] == 1
    assert "decision_1:temperature_path_trade_follow_allowed_missing" in result["blockers"]
    assert "decision_1:temperature_path_trade_follow_reason_missing" in result["blockers"]
