import json
import sqlite3
from pathlib import Path

from lineage_identity_runtime import build_fact_snapshot_id
from scripts import production_identity_duplicate_fact_snapshot_operator_runbook as runbook


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


def _fixture(db: Path, *, latest_on_noncanonical: bool = True):
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
    latest_id = 2 if latest_on_noncanonical else 1
    conn.execute(
        "INSERT INTO event_fact_latest (slug, event_date, fact_id, fact_snapshot_id, updated_at) VALUES (?, ?, ?, ?, ?)",
        (slug, event_date, latest_id, duplicate_id, "2026-07-06T02:01:00Z"),
    )
    conn.commit()
    conn.close()


def test_duplicate_operator_runbook_default_manual_policy_is_read_only_and_blocking(tmp_path: Path):
    db = tmp_path / "archive.db"
    _fixture(db, latest_on_noncanonical=True)

    report = runbook.build_operator_runbook(db, max_items_output=10)

    assert report["read_only"] is True
    assert report["guards"]["writes_archive_db"] is False
    assert report["duplicate_row_count"] == 2
    assert report["reviewed_manifest_candidate"]["latest_anchor_policy"] == "manual_review_required"
    assert report["reviewed_manifest_would_clear_duplicate_apply_contract"] is False
    steps = {item["step"]: item for item in report["checklist"]}
    assert steps["apply_duplicate_fact_snapshot_repair"]["writes_archive_db"] is True
    assert steps["apply_duplicate_fact_snapshot_repair"]["blocked_by_runbook"] is True


def test_duplicate_operator_runbook_with_explicit_latest_policy_clears_contract(tmp_path: Path):
    db = tmp_path / "archive.db"
    _fixture(db, latest_on_noncanonical=True)

    report = runbook.build_operator_runbook(
        db,
        latest_anchor_policy="regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor",
        max_items_output=10,
    )

    assert report["reviewed_manifest_would_clear_duplicate_apply_contract"] is True
    assert report["reviewed_apply_contract_summary"]["ok"] is True
    assert report["reviewed_apply_contract_summary"]["operation_count"] == 3
    steps = {item["step"]: item for item in report["checklist"]}
    assert "--confirm APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR" in steps["apply_duplicate_fact_snapshot_repair"]["command"]
    assert "controlled_apply_maintenance_window_packet.py" in steps["verify_writer_quiescence"]["command"]


def test_duplicate_operator_runbook_emit_reviewed_manifest_json(tmp_path: Path, capsys):
    db = tmp_path / "archive.db"
    _fixture(db, latest_on_noncanonical=False)

    exit_code = runbook.main(
        [
            "--db",
            str(db),
            "--emit-reviewed-manifest-json",
            "--latest-anchor-policy",
            "regenerate_latest_row_fact_snapshot_id_and_keep_latest_anchor",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"].endswith("_decision_manifest_v1")
    assert payload["decision_count"] == 2
    assert payload["decisions"][0]["operator_review_required"] is True
