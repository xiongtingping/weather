import sqlite3
from pathlib import Path

from scripts import production_identity_index_apply as apply_script


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
    conn.execute(
        """
        INSERT INTO auto_trade_decisions(id, decision_id, correlation_id)
        VALUES (1, 'decision_1', 'corr_1')
        """
    )
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(id, order_intent_id, correlation_id)
        VALUES (1, 'intent_1', 'corr_job_1')
        """
    )
    conn.commit()
    conn.close()


def _backup(db: Path, backup: Path) -> None:
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dest = sqlite3.connect(backup)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


def _index_count(db: Path) -> int:
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
    return int(row[0] or 0)


def test_index_apply_default_no_apply_does_not_write(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_db(db)

    result = apply_script.build_cli_result(
        db_path=db,
        backup_path=None,
        apply=False,
        confirm="",
        writer_quiescent=False,
        max_items_output=5,
    )

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["read_only"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert _index_count(db) == 0


def test_index_apply_blocks_without_confirmation(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_db(db)

    result = apply_script.build_cli_result(
        db_path=db,
        backup_path=None,
        apply=True,
        confirm="wrong",
        writer_quiescent=True,
        max_items_output=5,
        maintenance_packet={"ok": True, "writer_quiescent": True},
    )

    assert result["ok"] is False
    assert result["block_reason"] == "confirmation_phrase_missing_or_mismatched"
    assert _index_count(db) == 0


def test_index_apply_blocks_when_maintenance_not_quiescent(tmp_path: Path):
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.bak"
    _create_db(db)
    _backup(db, backup)

    result = apply_script.build_cli_result(
        db_path=db,
        backup_path=backup,
        apply=True,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        max_items_output=5,
        maintenance_packet={"ok": False, "writer_quiescent": False, "blockers": ["writer"]},
    )

    assert result["ok"] is False
    assert result["block_reason"] == "maintenance_window_not_quiescent"
    assert _index_count(db) == 0


def test_index_apply_creates_index_after_confirmation_backup_and_quiescence(tmp_path: Path):
    db = tmp_path / "archive.db"
    backup = tmp_path / "archive.bak"
    _create_db(db)
    _backup(db, backup)

    result = apply_script.build_cli_result(
        db_path=db,
        backup_path=backup,
        apply=True,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        max_items_output=5,
        maintenance_packet={"ok": True, "writer_quiescent": True, "blockers": []},
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["apply_result"]["applied_ddl_count"] == 2
    assert result["post_apply_contract"]["ok"] is True
    assert result["guards"]["writes_archive_db"] is True
    assert _index_count(db) == 2
