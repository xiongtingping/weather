import json
import sqlite3
from pathlib import Path

from lineage_identity_runtime import build_fact_snapshot_id
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
        INSERT INTO event_fact_history (id, slug, event_date, fact_as_of, fact_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            row_id,
            slug,
            event_date,
            fact_as_of,
            json.dumps(
                {
                    "fact_snapshot_id": fact_snapshot_id,
                    "posterior_run_id": "post_test",
                    "source_event_ids": ["srcevt_test"],
                }
            ),
        ),
    )


def test_duplicate_packet_prefers_natural_owner_as_canonical(tmp_path: Path):
    db = tmp_path / "archive.db"
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
    conn.commit()
    conn.close()

    report = packet.build_packet(db, max_items_output=10)

    assert report["duplicate_group_count"] == 1
    group = report["groups"][0]
    assert group["canonical_row_id"] == 1
    assert group["canonical_reason"] == "natural_key_regenerates_existing_fact_snapshot_id"
    decisions = {row["row_id"]: row for row in report["operator_manifest_template"]["decisions"]}
    assert decisions[1]["decision"] == "keep_existing_fact_snapshot_id"
    assert decisions[2]["decision"] == "regenerate_fact_snapshot_id_required"
    assert report["ok"] is False
    assert "duplicate_fact_snapshot_decision_manifest_missing" in report["hard_blockers"]


def test_duplicate_packet_blocks_when_latest_is_not_natural_owner(tmp_path: Path):
    db = tmp_path / "archive.db"
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
        "INSERT INTO event_fact_latest (slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        (slug, event_date, 2, "2026-07-06T02:01:00Z"),
    )
    conn.commit()
    conn.close()

    report = packet.build_packet(db, max_items_output=10)

    assert report["ok"] is False
    assert "latest_row_is_not_natural_fact_snapshot_owner" in report["blockers"]
    assert report["groups"][0]["canonical_row_id"] == 1


def test_duplicate_decision_manifest_can_clear_latest_anchor_policy(tmp_path: Path):
    db = tmp_path / "archive.db"
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
        "INSERT INTO event_fact_latest (slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        (slug, event_date, 2, "2026-07-06T02:01:00Z"),
    )
    conn.commit()
    conn.close()

    template = packet.build_packet(db, max_items_output=10)["operator_manifest_template"]
    manifest = {
        "schema": packet.MANIFEST_SCHEMA_VERSION,
        "latest_anchor_policy": "regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor",
        "decisions": template["decisions"],
    }

    report = packet.build_packet(db, max_items_output=10, decision_manifest=manifest)

    assert report["ok"] is True
    assert report["blockers"] == ["latest_row_is_not_natural_fact_snapshot_owner"]
    assert report["manifest_resolved_blockers"] == ["latest_row_is_not_natural_fact_snapshot_owner"]
    assert report["hard_blockers"] == []
    audit = report["decision_manifest_audit"]
    assert audit["blocking_for_current_apply"] is False
    assert audit["latest_anchor_policy_ready"] is True
    assert audit["ready_decision_count"] == 2


def test_duplicate_decision_manifest_rejects_invalid_or_mismatched_rows(tmp_path: Path):
    db = tmp_path / "archive.db"
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
    conn.commit()
    conn.close()

    manifest = {
        "schema": "wrong_schema",
        "decisions": [
            {"row_id": 1, "decision": "regenerate_fact_snapshot_id_required"},
            {"row_id": 1, "decision": "keep_existing_fact_snapshot_id"},
            {"row_id": 999, "decision": "not_a_decision"},
        ],
    }

    report = packet.build_packet(db, max_items_output=10, decision_manifest=manifest)

    assert report["ok"] is False
    audit = report["decision_manifest_audit"]
    assert audit["duplicate_decision_count"] == 1
    assert audit["unexpected_decision_count"] == 1
    assert audit["missing_decision_count"] == 1
    assert audit["mismatch_decision_count"] == 1
    assert "duplicate_fact_snapshot_decision_manifest_schema_invalid" in report["hard_blockers"]
    assert "duplicate_fact_snapshot_decision_manifest_invalid" in report["hard_blockers"]
    assert "duplicate_fact_snapshot_decision_mismatch" in report["hard_blockers"]
