import json
import sqlite3
from pathlib import Path

from scripts import production_identity_merge_fill_temp_drill as drill
from tests.test_production_identity_merge_fill_plan import _create_fixture


def _seed_duplicate_and_missing_identity(db: Path) -> None:
    duplicate_payload = json.dumps(
        {
            "fact_snapshot_id": "fact_duplicate",
            "posterior_run_id": "post_duplicate",
            "source_event_ids": ["srcevt_duplicate"],
        }
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE event_fact_history SET fact_snapshot_id='', correlation_id='', posterior_run_id='', source_event_id='', fact_json=? WHERE id=1",
        (duplicate_payload,),
    )
    conn.execute(
        """
        INSERT INTO event_fact_history (
            id, run_date, slug, event_date, fact_as_of, fact_json, created_at,
            correlation_id, source_event_id, fact_snapshot_id, posterior_run_id, signal_id, review_id
        )
        VALUES (2, '2026-07-06', 'slug2', '2026-07-06', '2026-07-06T02:00:00Z', ?, 'now',
            '', '', '', '', '', '')
        """,
        (duplicate_payload,),
    )
    conn.execute(
        "UPDATE event_fact_latest SET fact_id=2, fact_snapshot_id='', correlation_id='' WHERE slug='slug' OR slug='slug_event_fact_latest'"
    )
    conn.execute("UPDATE auto_trade_decisions SET decision_id='', correlation_id='' WHERE id=1")
    conn.commit()
    conn.close()


def _source_state(db: Path) -> tuple[int, int]:
    conn = sqlite3.connect(db)
    history_missing = int(
        conn.execute(
            "SELECT COUNT(*) FROM event_fact_history WHERE fact_snapshot_id IS NULL OR fact_snapshot_id=''"
        ).fetchone()[0]
    )
    decision_missing = int(
        conn.execute(
            "SELECT COUNT(*) FROM auto_trade_decisions WHERE decision_id IS NULL OR decision_id=''"
        ).fetchone()[0]
    )
    conn.close()
    return history_missing, decision_missing


def test_identity_merge_fill_temp_drill_runs_duplicate_then_merge_fill_on_temp_only(tmp_path: Path):
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_duplicate_and_missing_identity(db)
    before = _source_state(db)

    result = drill.run_drill(db_path=db, limit=100, max_items_output=10, keep_temp=False)

    assert result["ok"] is True
    assert result["read_only_for_production"] is True
    assert result["copy_mode"] == "focused"
    assert result["production_counts_unchanged"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_temp_archive_db"] is True
    assert result["source_duplicate_packet"]["duplicate_row_count"] == 2
    assert result["reviewed_manifest_summary"]["decision_count"] == 2
    assert result["duplicate_apply_result"]["ok"] is True
    assert result["duplicate_apply_result"]["operation_count"] >= 2
    assert result["post_duplicate_packet"]["duplicate_row_count"] == 0
    assert result["pre_merge_contract"]["ok"] is True
    assert result["pre_merge_contract"]["operation_count"] >= 2
    assert result["merge_apply_result"]["ok"] is True
    assert result["merge_apply_result"]["applied"] is True
    assert result["post_merge_contract"]["ok"] is True
    assert result["post_merge_contract"]["operation_count"] == 0
    assert result["next_boundary"] == "operator_may_run_duplicate_then_identity_merge_fill_apply_in_writer_quiescent_window"
    assert _source_state(db) == before

    summary = drill.compact_drill(result, max_items_output=2)
    assert summary["schema"] == "production_identity_merge_fill_temp_drill_v1_summary"
    assert summary["source_schema"] == result["schema"]
    assert summary["ok"] is True
    assert summary["read_only_for_production"] is True
    assert summary["source_duplicate_packet"]["duplicate_row_count"] == 2
    assert summary["post_duplicate_packet"]["duplicate_row_count"] == 0
    assert summary["pre_merge_contract"]["operation_count"] >= 2
    assert summary["post_merge_contract"]["operation_count"] == 0
    assert summary["guards"]["writes_archive_db"] is False
