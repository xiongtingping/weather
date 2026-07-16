import json
import sqlite3
from pathlib import Path

from lineage_identity_runtime import build_fact_snapshot_id
from scripts import production_identity_duplicate_fact_snapshot_apply_contract as contract
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


def _reviewed_manifest(db: Path, *, latest_anchor_policy: str):
    template = packet.build_packet(db, max_items_output=100)["operator_manifest_template"]
    return {
        "schema": packet.MANIFEST_SCHEMA_VERSION,
        "latest_anchor_policy": latest_anchor_policy,
        "decisions": template["decisions"],
    }


def _fixture_with_latest_on_noncanonical(db: Path) -> None:
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


def test_apply_contract_blocks_without_reviewed_manifest(tmp_path: Path):
    db = tmp_path / "archive.db"
    _fixture_with_latest_on_noncanonical(db)

    report = contract.build_contract(db, max_items_output=10)

    assert report["ok"] is False
    assert "duplicate_decision_manifest_required" in report["blockers"]
    assert "duplicate_decision_manifest_audit_not_ready" in report["blockers"]
    assert report["decision_manifest_audit"]["expected_decision_count"] == 2


def test_apply_contract_is_noop_ready_when_no_duplicates(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_db(db)
    slug = "highest-temperature-in-chengdu-on-july-6-2026"
    event_date = "2026-07-06"
    conn = sqlite3.connect(db)
    _insert_fact(
        conn,
        row_id=1,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-07-06T01:00:00Z",
        fact_snapshot_id="fact_one",
    )
    _insert_fact(
        conn,
        row_id=2,
        slug=slug,
        event_date=event_date,
        fact_as_of="2026-07-06T02:00:00Z",
        fact_snapshot_id="fact_two",
    )
    conn.commit()
    conn.close()

    report = contract.build_contract(db, max_items_output=10)

    assert report["ok"] is True
    assert report["duplicate_row_count"] == 0
    assert report["operation_count"] == 0
    assert report["next_boundary"] == "duplicate_fact_snapshot_apply_not_required"


def test_apply_contract_projects_history_and_latest_updates_for_reviewed_manifest(tmp_path: Path):
    db = tmp_path / "archive.db"
    _fixture_with_latest_on_noncanonical(db)
    manifest = _reviewed_manifest(
        db,
        latest_anchor_policy="regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor",
    )

    report = contract.build_contract(db, decision_manifest=manifest, max_items_output=10)

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["decision_manifest_audit"]["ready_decision_count"] == 2
    assert report["operation_count"] == 3
    history_ops = [item for item in report["operations"] if item["table"] == "event_fact_history"]
    latest_ops = [item for item in report["operations"] if item["table"] == "event_fact_latest"]
    assert len(history_ops) == 2
    assert len(latest_ops) == 1
    noncanonical_history = [item for item in history_ops if item["primary_key"]["id"] == 2][0]
    assert noncanonical_history["set_fields"]["fact_json"]["old_value"] != noncanonical_history["set_fields"]["fact_json"]["new_value"]
    assert latest_ops[0]["operation"] == "align_latest_identity_column_with_existing_fact_anchor"
    assert latest_ops[0]["set_fields"]["fact_snapshot_id"]["new_value"] == noncanonical_history["proposed_fact_snapshot_id"]


def test_apply_contract_can_project_latest_repoint_to_natural_owner(tmp_path: Path):
    db = tmp_path / "archive.db"
    _fixture_with_latest_on_noncanonical(db)
    manifest = _reviewed_manifest(
        db,
        latest_anchor_policy="repoint_latest_to_natural_fact_snapshot_owner_after_duplicate_apply",
    )

    report = contract.build_contract(db, decision_manifest=manifest, max_items_output=10)

    assert report["ok"] is True
    latest_ops = [item for item in report["operations"] if item["table"] == "event_fact_latest"]
    assert latest_ops[0]["operation"] == "repoint_latest_to_natural_fact_snapshot_owner"
    assert latest_ops[0]["set_fields"]["fact_id"] == {"old_value": 2, "new_value": 1}
