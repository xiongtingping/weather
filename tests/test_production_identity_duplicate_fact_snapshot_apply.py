import json
import sqlite3
from pathlib import Path

from lineage_identity_runtime import build_fact_snapshot_id
from scripts import production_identity_duplicate_fact_snapshot_apply as apply_script
from scripts import production_identity_duplicate_fact_snapshot_packet as packet


def _create_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_snapshot_id TEXT,
            fact_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            fact_snapshot_id TEXT,
            updated_at TEXT,
            PRIMARY KEY (slug, event_date)
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_fact(conn, *, row_id: int, slug: str, event_date: str, fact_as_of: str, fact_snapshot_id: str):
    conn.execute(
        """
        INSERT INTO event_fact_history (id, slug, event_date, fact_as_of, fact_snapshot_id, fact_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            slug,
            event_date,
            fact_as_of,
            "",
            json.dumps(
                {
                    "fact_snapshot_id": fact_snapshot_id,
                    "posterior_run_id": "post_test",
                    "source_event_ids": ["srcevt_test"],
                }
            ),
        ),
    )


def _fixture(db: Path) -> tuple[str, str, str]:
    _create_db(db)
    slug = "highest-temperature-in-chengdu-on-july-6-2026"
    event_date = "2026-07-06"
    natural_fact_as_of = "2026-07-06T01:00:00Z"
    duplicate_id = build_fact_snapshot_id(
        slug=slug,
        event_date=event_date,
        fact_as_of=natural_fact_as_of,
        source_event_ids=["srcevt_test"],
    )
    conn = sqlite3.connect(db)
    _insert_fact(conn, row_id=1, slug=slug, event_date=event_date, fact_as_of=natural_fact_as_of, fact_snapshot_id=duplicate_id)
    _insert_fact(conn, row_id=2, slug=slug, event_date=event_date, fact_as_of="2026-07-06T02:00:00Z", fact_snapshot_id=duplicate_id)
    conn.execute(
        "INSERT INTO event_fact_latest (slug, event_date, fact_id, fact_snapshot_id, updated_at) VALUES (?, ?, ?, ?, ?)",
        (slug, event_date, 2, duplicate_id, "2026-07-06T02:01:00Z"),
    )
    conn.commit()
    conn.close()
    return slug, event_date, duplicate_id


def _manifest(db: Path) -> dict:
    template = packet.build_packet(db, max_items_output=100)["operator_manifest_template"]
    return {
        "schema": packet.MANIFEST_SCHEMA_VERSION,
        "latest_anchor_policy": "regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor",
        "decisions": template["decisions"],
    }


def _backup(db: Path, backup: Path) -> None:
    source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dest = sqlite3.connect(backup)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()


def test_duplicate_apply_default_no_apply_does_not_write(tmp_path: Path):
    db = tmp_path / "archive.db"
    _, _, duplicate_id = _fixture(db)
    manifest = _manifest(db)

    result = apply_script.build_cli_result(
        db_path=db,
        decision_manifest=manifest,
        backup_path=None,
        apply=False,
        confirm="",
        writer_quiescent=False,
        max_items_output=10,
    )

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["read_only"] is True
    conn = sqlite3.connect(db)
    payload = json.loads(conn.execute("SELECT fact_json FROM event_fact_history WHERE id=2").fetchone()[0])
    conn.close()
    assert payload["fact_snapshot_id"] == duplicate_id


def test_duplicate_apply_blocks_without_required_confirmation_and_backup(tmp_path: Path):
    db = tmp_path / "archive.db"
    _fixture(db)
    manifest = _manifest(db)

    result = apply_script.build_cli_result(
        db_path=db,
        decision_manifest=manifest,
        backup_path=None,
        apply=True,
        confirm="wrong",
        writer_quiescent=True,
        max_items_output=10,
        maintenance_packet={"ok": True, "writer_quiescent": True},
    )

    assert result["ok"] is False
    assert result["block_reason"] == "confirmation_phrase_missing_or_mismatched"


def test_duplicate_apply_blocks_when_maintenance_not_quiescent(tmp_path: Path):
    db = tmp_path / "archive.db"
    _fixture(db)
    manifest = _manifest(db)
    backup = tmp_path / "archive.bak"
    _backup(db, backup)

    result = apply_script.build_cli_result(
        db_path=db,
        decision_manifest=manifest,
        backup_path=backup,
        apply=True,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        max_items_output=10,
        maintenance_packet={"ok": False, "writer_quiescent": False, "blockers": ["writer"]},
    )

    assert result["ok"] is False
    assert result["block_reason"] == "maintenance_window_not_quiescent"


def test_duplicate_apply_updates_only_contract_rows_in_transaction(tmp_path: Path):
    db = tmp_path / "archive.db"
    slug, event_date, duplicate_id = _fixture(db)
    manifest = _manifest(db)
    backup = tmp_path / "archive.bak"
    _backup(db, backup)

    result = apply_script.build_cli_result(
        db_path=db,
        decision_manifest=manifest,
        backup_path=backup,
        apply=True,
        confirm=apply_script.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        max_items_output=10,
        maintenance_packet={"ok": True, "writer_quiescent": True, "blockers": []},
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["apply_result"]["operation_count"] == 3
    conn = sqlite3.connect(db)
    row1 = conn.execute("SELECT fact_snapshot_id, fact_json FROM event_fact_history WHERE id=1").fetchone()
    row2 = conn.execute("SELECT fact_snapshot_id, fact_json FROM event_fact_history WHERE id=2").fetchone()
    latest = conn.execute(
        "SELECT fact_id, fact_snapshot_id FROM event_fact_latest WHERE slug=? AND event_date=?",
        (slug, event_date),
    ).fetchone()
    conn.close()
    payload1 = json.loads(row1[1])
    payload2 = json.loads(row2[1])
    assert payload1["fact_snapshot_id"] == duplicate_id
    assert row1[0] == duplicate_id
    assert payload2["fact_snapshot_id"] != duplicate_id
    assert row2[0] == payload2["fact_snapshot_id"]
    assert latest[0] == 2
    assert latest[1] == payload2["fact_snapshot_id"]
