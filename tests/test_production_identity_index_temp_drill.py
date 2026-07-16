import sqlite3
from pathlib import Path

from scripts import production_identity_index_temp_drill as drill


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
          id INTEGER PRIMARY KEY,
          decision_id TEXT,
          correlation_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE auto_trade_jobs (
          id INTEGER PRIMARY KEY,
          order_intent_id TEXT,
          correlation_id TEXT
        )
        """
    )
    for idx in range(3):
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(id, decision_id, correlation_id)
            VALUES (?, ?, ?)
            """,
            (idx + 1, f"decision_{idx}", f"corr_{idx}"),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_jobs(id, order_intent_id, correlation_id)
            VALUES (?, ?, ?)
            """,
            (idx + 1, f"intent_{idx}", f"job_corr_{idx}"),
        )
    conn.commit()
    conn.close()


def test_identity_index_temp_drill_applies_index_only_to_temp_db(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_db(db)

    result = drill.run_drill(db_path=db, row_limit=2)

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_temp_archive_db"] is True
    assert result["pre_contract"]["needs_apply"] is True
    assert result["apply_result"]["applied"] is True
    assert result["post_contract"]["ok"] is True

    conn = sqlite3.connect(db)
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM sqlite_master
        WHERE type='index'
          AND name IN (
            'idx_phase0_auto_trade_decisions_correlation_id_nonempty_scan',
            'idx_phase0_auto_trade_jobs_correlation_id_nonempty_scan'
          )
        """
    ).fetchone()
    conn.close()
    assert int(row[0] or 0) == 0
