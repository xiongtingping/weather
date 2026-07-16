import sqlite3
from pathlib import Path

from scripts import production_identity_index_apply_contract as contract


def _create_db(path: Path, *, with_column: bool = True, with_index: bool = False) -> None:
    conn = sqlite3.connect(path)
    correlation = ", correlation_id TEXT" if with_column else ""
    conn.execute(
        f"""
        CREATE TABLE auto_trade_decisions (
          id INTEGER PRIMARY KEY,
          decision_id TEXT{correlation}
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE auto_trade_jobs (
          id INTEGER PRIMARY KEY,
          order_intent_id TEXT{correlation}
        )
        """
    )
    if with_index:
        conn.execute(
            """
            CREATE INDEX idx_phase0_auto_trade_decisions_correlation_id_nonempty_scan
            ON auto_trade_decisions(correlation_id)
            WHERE correlation_id IS NOT NULL AND correlation_id <> ''
            """
        )
        conn.execute(
            """
            CREATE INDEX idx_phase0_auto_trade_jobs_correlation_id_nonempty_scan
            ON auto_trade_jobs(correlation_id)
            WHERE correlation_id IS NOT NULL AND correlation_id <> ''
            """
        )
    conn.commit()
    conn.close()


def test_index_apply_contract_reports_required_index_without_writing(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_db(db)

    report = contract.build_contract(db, backup_dir=tmp_path / "backups")

    assert report["ok"] is False
    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["needs_apply"] is True
    assert report["ddl_count"] == 2
    assert "CREATE INDEX IF NOT EXISTS" in report["ddl"][0]
    assert report["apply_blockers"] == [
        "apply_flag",
        "confirmation_phrase",
        "fresh_archive_db_backup",
        "writer_quiescence",
    ]
    assert report["operator_contract"]["confirmation_phrase"] == contract.CONFIRMATION_PHRASE
    assert report["operator_contract"]["backup_command"]
    assert report["operator_contract"]["backup_command_blocked_reason"] == ""


def test_index_apply_contract_blocks_broken_backup_symlink(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_db(db)
    backup_link = tmp_path / "backups"
    missing_external_target = tmp_path / "missing-volume" / "backups"
    backup_link.symlink_to(missing_external_target)

    report = contract.build_contract(db, backup_dir=backup_link)

    assert report["needs_apply"] is True
    assert "backup_dir_symlink_target_missing" in report["apply_blockers"]
    assert report["operator_contract"]["backup_command"] == ""
    assert report["operator_contract"]["backup_command_blocked_reason"] == (
        "backup_dir_symlink_target_missing"
    )
    assert report["operator_contract"]["backup_dir_state"]["is_symlink"] is True
    assert report["operator_contract"]["backup_dir_state"]["symlink_target_exists"] is False


def test_index_apply_contract_passes_when_index_exists(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_db(db, with_index=True)

    report = contract.build_contract(db)

    assert report["ok"] is True
    assert report["needs_apply"] is False
    assert report["ddl_count"] == 0
    assert report["required_indexes"][0]["index_exists"] is True


def test_index_apply_contract_blocks_missing_column(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_db(db, with_column=False)

    report = contract.build_contract(db)

    assert report["ok"] is False
    assert report["needs_apply"] is False
    assert "column_missing" in report["blockers"]
    assert report["required_indexes"][0]["blocked"] is True
