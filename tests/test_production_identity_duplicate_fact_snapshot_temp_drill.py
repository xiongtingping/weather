import json
import sqlite3
from pathlib import Path

from lineage_identity_runtime import build_fact_snapshot_id
from scripts import production_identity_duplicate_fact_snapshot_temp_drill as drill
from tests.test_production_identity_duplicate_fact_snapshot_apply import _fixture as _duplicate_fixture


def _fact_state(db: Path) -> list[tuple[int, str, str]]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT id, fact_snapshot_id, fact_json FROM event_fact_history ORDER BY id"
        ).fetchall()
    return [(int(row[0]), str(row[1] or ""), json.loads(row[2])["fact_snapshot_id"]) for row in rows]


def _create_no_duplicate_db(db: Path) -> str:
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
    slug = "highest-temperature-in-chengdu-on-july-6-2026"
    event_date = "2026-07-06"
    fact_as_of = "2026-07-06T01:00:00Z"
    fact_snapshot_id = build_fact_snapshot_id(
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        source_event_ids=["srcevt_test"],
    )
    conn.execute(
        """
        INSERT INTO event_fact_history (id, slug, event_date, fact_as_of, fact_snapshot_id, fact_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            1,
            slug,
            event_date,
            fact_as_of,
            fact_snapshot_id,
            json.dumps(
                {
                    "fact_snapshot_id": fact_snapshot_id,
                    "posterior_run_id": "post_test",
                    "source_event_ids": ["srcevt_test"],
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO event_fact_latest (slug, event_date, fact_id, fact_snapshot_id, updated_at) VALUES (?, ?, ?, ?, ?)",
        (slug, event_date, 1, fact_snapshot_id, "2026-07-06T01:01:00Z"),
    )
    conn.commit()
    conn.close()
    return fact_snapshot_id


def test_duplicate_fact_snapshot_temp_drill_applies_on_temp_only(tmp_path: Path):
    db = tmp_path / "archive.db"
    _duplicate_fixture(db)
    before_state = _fact_state(db)

    result = drill.run_drill(db_path=db, max_items_output=10, keep_temp=False)

    assert result["ok"] is True
    assert result["read_only_for_production"] is True
    assert result["copy_mode"] == "focused"
    assert result["production_counts_unchanged"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_temp_archive_db"] is True
    assert result["source_duplicate_packet"]["duplicate_row_count"] == 2
    assert result["source_runbook"]["reviewed_manifest_would_clear_duplicate_apply_contract"] is True
    assert result["reviewed_manifest_summary"]["decision_count"] == 2
    assert result["reviewed_manifest_summary"]["latest_anchor_policy"] == drill.DEFAULT_LATEST_ANCHOR_POLICY
    assert result["pre_apply_contract"]["ok"] is True
    assert result["pre_apply_contract"]["operation_count"] == 3
    assert result["apply_result"]["ok"] is True
    assert result["apply_result"]["applied"] is True
    assert result["apply_result"]["operation_count"] == 3
    assert result["apply_result"]["maintenance_window_writer_quiescent"] is True
    assert result["post_apply_duplicate_packet"]["duplicate_row_count"] == 0
    assert result["post_apply_contract"]["ok"] is True
    assert result["post_apply_identity_coverage"]["skipped"] is True
    assert result["next_boundary"] == "operator_may_run_controlled_duplicate_apply_in_writer_quiescent_window_after_reviewing_drill"
    assert _fact_state(db) == before_state


def test_duplicate_fact_snapshot_temp_drill_noops_when_no_duplicates(tmp_path: Path):
    db = tmp_path / "archive.db"
    fact_snapshot_id = _create_no_duplicate_db(db)
    before_state = _fact_state(db)

    result = drill.run_drill(db_path=db, max_items_output=10, keep_temp=False)

    assert result["ok"] is True
    assert result["source_duplicate_packet"]["duplicate_row_count"] == 0
    assert result["reviewed_manifest_summary"]["decision_count"] == 0
    assert result["pre_apply_contract"]["ok"] is True
    assert result["pre_apply_contract"]["operation_count"] == 0
    assert result["apply_result"]["ok"] is True
    assert result["apply_result"]["applied"] is False
    assert result["post_apply_duplicate_packet"]["duplicate_row_count"] == 0
    assert result["next_boundary"] == "duplicate_fact_snapshot_apply_not_required"
    assert _fact_state(db) == before_state
    assert before_state == [(1, fact_snapshot_id, fact_snapshot_id)]
