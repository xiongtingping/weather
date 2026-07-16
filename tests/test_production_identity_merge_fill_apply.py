import json
import sqlite3
from pathlib import Path

from scripts import production_identity_merge_fill_apply as apply_script
from tests.test_production_identity_merge_fill_plan import _create_fixture


def _seed_missing_history_identity(db: Path) -> None:
    payload = json.dumps(
        {
            "fact_snapshot_id": "fact_merge_fill",
            "posterior_run_id": "post_merge_fill",
            "source_event_ids": ["srcevt_merge_fill"],
        }
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE event_fact_history SET fact_snapshot_id='', correlation_id='', posterior_run_id='', source_event_id='', fact_json=? WHERE id=1",
        (payload,),
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


def _history_identity(db: Path) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT fact_snapshot_id, correlation_id, posterior_run_id, source_event_id FROM event_fact_history WHERE id=1"
    ).fetchone()
    conn.close()
    return dict(row)


def test_identity_merge_fill_apply_default_no_apply_does_not_write(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_missing_history_identity(db)

    result = apply_script.build_cli_result(
        db_path=db,
        duplicate_decision_manifest=None,
        backup_path=None,
        apply=False,
        confirm="",
        writer_quiescent=False,
        limit=100,
        max_items_output=10,
    )

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["read_only"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert _history_identity(db)["fact_snapshot_id"] == ""


def test_identity_merge_fill_apply_blocks_without_confirmation(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_missing_history_identity(db)

    result = apply_script.build_cli_result(
        db_path=db,
        duplicate_decision_manifest=None,
        backup_path=None,
        apply=True,
        confirm="wrong",
        writer_quiescent=True,
        limit=100,
        max_items_output=10,
        maintenance_packet={"ok": True, "writer_quiescent": True},
    )

    assert result["ok"] is False
    assert result["block_reason"] == "confirmation_phrase_missing_or_mismatched"


def test_identity_merge_fill_apply_blocks_when_maintenance_not_quiescent(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_missing_history_identity(db)
    backup = tmp_path / "archive.bak"
    _backup(db, backup)

    result = apply_script.build_cli_result(
        db_path=db,
        duplicate_decision_manifest=None,
        backup_path=backup,
        apply=True,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        limit=100,
        max_items_output=10,
        maintenance_packet={"ok": False, "writer_quiescent": False, "blockers": ["writer"]},
    )

    assert result["ok"] is False
    assert result["block_reason"] == "maintenance_window_not_quiescent"


def test_identity_merge_fill_apply_updates_empty_fields_in_transaction(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_missing_history_identity(db)
    backup = tmp_path / "archive.bak"
    _backup(db, backup)

    result = apply_script.build_cli_result(
        db_path=db,
        duplicate_decision_manifest=None,
        backup_path=backup,
        apply=True,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        limit=100,
        max_items_output=10,
        maintenance_packet={"ok": True, "writer_quiescent": True, "blockers": []},
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["apply_result"]["operation_count"] == 1
    row = _history_identity(db)
    assert row["fact_snapshot_id"] == "fact_merge_fill"
    assert row["posterior_run_id"] == "post_merge_fill"
    assert row["source_event_id"] == "srcevt_merge_fill"
    assert row["correlation_id"].startswith("corr_")
