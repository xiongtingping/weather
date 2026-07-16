import json
import sqlite3
from pathlib import Path

from scripts import production_identity_closeout_operator_runbook as runbook
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


def test_identity_closeout_operator_runbook_is_read_only_and_orders_human_steps(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_duplicate_and_missing_identity(db)

    result = runbook.build_operator_runbook(
        db,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-07T00:00:00Z",
        max_items_output=5,
        limit=100,
    )
    summary = runbook.compact_operator_runbook(result)

    assert result["schema"] == "production_identity_closeout_operator_runbook_v1"
    assert result["read_only"] is True
    assert result["dry_run"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_manifest_file"] is False
    assert result["guards"]["creates_backup"] is False
    assert result["ready_for_controlled_apply_now"] is True
    assert result["identity_scope"]["identity_audit_index_contract_ok"] is True
    assert result["identity_scope"]["identity_audit_index_already_satisfied"] is False
    assert result["identity_scope"]["identity_audit_index_needs_apply"] is True
    assert result["identity_scope"]["identity_audit_index_ddl_count"] >= 1
    assert result["identity_scope"]["identity_audit_index_contract_blocker_count"] == 0
    assert "writer_quiescence" in result["identity_scope"]["identity_audit_index_apply_blockers"]

    steps = {row["step"]: row for row in result["operator_sequence"]}
    assert steps["run_identity_closeout_temp_drill"]["order"] < steps["create_shared_fresh_backup"]["order"]
    assert steps["create_shared_fresh_backup"]["order"] < steps["verify_writer_quiescence"]["order"]
    assert steps["verify_writer_quiescence"]["order"] < steps["apply_identity_audit_indexes"]["order"]
    assert steps["apply_identity_audit_indexes"]["order"] < steps["apply_duplicate_fact_snapshot_repair"]["order"]
    assert steps["verify_writer_quiescence"]["order"] < steps["apply_duplicate_fact_snapshot_repair"]["order"]
    assert steps["apply_duplicate_fact_snapshot_repair"]["order"] < steps["apply_identity_merge_fill"]["order"]
    assert steps["resume_writer_services"]["order"] > steps["verify_full_joint_gate"]["order"]

    index_apply = steps["apply_identity_audit_indexes"]
    duplicate_apply = steps["apply_duplicate_fact_snapshot_repair"]
    merge_apply = steps["apply_identity_merge_fill"]
    assert index_apply["writes_archive_db"] is True
    assert index_apply["requires_operator_confirmation_phrase"] is True
    assert index_apply["confirmation_phrase"] == "APPLY_PRODUCTION_IDENTITY_AUDIT_INDEXES"
    assert duplicate_apply["writes_archive_db"] is True
    assert duplicate_apply["requires_operator_confirmation_phrase"] is True
    assert duplicate_apply["confirmation_phrase"] == "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR"
    assert "fresh_backup_exists" in duplicate_apply["blocked_until"]
    assert "writer_quiescence_passes_immediately_before_apply" in duplicate_apply["blocked_until"]
    assert merge_apply["confirmation_phrase"] == "APPLY_PRODUCTION_IDENTITY_MERGE_FILL"
    assert summary["operator_sequence"][-1]["step"] == "resume_writer_services"


def test_identity_closeout_operator_runbook_surfaces_current_writer_pause_and_resume(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_duplicate_and_missing_identity(db)

    def fake_maintenance_packet(*args, **kwargs):
        return {
            "schema": "controlled_apply_maintenance_window_packet_v1",
            "ok": False,
            "writer_quiescent": False,
            "required_stop_labels": ["gui/$(id -u)/com.xiong.weather.ui"],
            "blockers": [{"owner_class": "ui_server"}],
            "operator_commands": {
                "suggested_pause_commands": ["scripts/weatherctl.sh stop"],
                "suggested_resume_commands": ["scripts/weatherctl.sh start  # resume GUI LaunchAgents after apply"],
                "verify_writer_quiescence": "python3 scripts/controlled_apply_maintenance_window_packet.py --summary",
            },
            "stop_authorization": {"blockers": [], "system_monitor_stop_required": False},
            "counts": {"writer_quiescence_blocker_count": 1},
            "next_boundary": "pause_required_writer_services_then_rerun_this_packet",
        }

    monkeypatch.setattr(
        runbook.closeout_packet.maintenance_window,
        "build_maintenance_window_packet",
        fake_maintenance_packet,
    )

    result = runbook.build_operator_runbook(
        db,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-07T00:00:00Z",
        max_items_output=5,
        limit=100,
    )

    steps = {row["step"]: row for row in result["operator_sequence"]}
    assert result["ready_for_controlled_apply_now"] is False
    assert result["start_blockers"] == ["writer_quiescence_not_satisfied"]
    assert steps["pause_writer_services"]["command"] == "scripts/weatherctl.sh stop"
    assert steps["pause_writer_services"]["execution_allowed_now"] is True
    assert steps["resume_writer_services"]["command"].startswith("scripts/weatherctl.sh start")
    assert "writer_quiescence_not_satisfied" in steps["apply_duplicate_fact_snapshot_repair"]["blocked_until"]
