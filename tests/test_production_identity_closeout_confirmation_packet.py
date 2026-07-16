import json
import sqlite3
from pathlib import Path

from scripts import production_identity_closeout_confirmation_packet as packet
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


def test_identity_closeout_confirmation_packet_sequences_duplicate_then_merge_fill(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_duplicate_and_missing_identity(db)

    result = packet.build_confirmation_packet(
        db,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-07T00:00:00Z",
        max_items_output=5,
        limit=100,
    )
    summary = packet.compact_confirmation_packet(result)

    assert result["read_only"] is True
    assert result["dry_run"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["creates_backup"] is False
    assert result["guards"]["writes_manifest_file"] is False
    assert result["ok"] is True
    assert result["duplicate"]["row_count"] == 2
    assert result["duplicate"]["reviewed_apply_contract_ok"] is True
    assert result["duplicate"]["reviewed_apply_operation_count"] >= 2
    assert result["identity_audit_indexes"]["contract_ok"] is True
    assert result["identity_audit_indexes"]["already_satisfied"] is False
    assert result["identity_audit_indexes"]["needs_apply"] is True
    assert result["identity_audit_indexes"]["ddl_count"] >= 1
    assert result["identity_audit_indexes"]["contract_blocker_count"] == 0
    assert result["merge_fill"]["contract_ok"] is False
    assert result["merge_fill"]["contract_stage"] == "pre_duplicate_repair_diagnostic"
    assert result["merge_fill"]["expected_to_be_ready_after_duplicate_repair_apply"] is True
    assert result["merge_fill"]["operation_count"] >= 2
    assert result["maintenance_window"]["writer_quiescent"] is True
    assert Path(result["shared_backup_path"]).exists() is False
    assert Path(result["duplicate_manifest_path"]).exists() is False

    steps = {row["step"]: row for row in result["control_sequence"]}
    assert steps["review_identity_audit_index_contract"]["order"] < steps["apply_identity_audit_indexes"]["order"]
    assert steps["run_identity_audit_index_temp_drill"]["order"] < steps["apply_identity_audit_indexes"]["order"]
    assert steps["run_identity_closeout_temp_drill"]["order"] < steps["create_shared_fresh_backup"]["order"]
    assert "production_identity_merge_fill_temp_drill.py" in steps["run_identity_closeout_temp_drill"]["command"]
    assert "--copy-mode focused" in steps["run_identity_closeout_temp_drill"]["command"]
    assert steps["apply_identity_audit_indexes"]["order"] < steps["apply_duplicate_fact_snapshot_repair"]["order"]
    assert steps["apply_duplicate_fact_snapshot_repair"]["order"] < steps["review_identity_merge_fill_contract_after_duplicate_repair"]["order"]
    assert steps["review_identity_merge_fill_contract_after_duplicate_repair"]["order"] < steps["apply_identity_merge_fill"]["order"]
    assert steps["apply_identity_audit_indexes"]["confirmation_phrase"] == "APPLY_PRODUCTION_IDENTITY_AUDIT_INDEXES"
    assert steps["apply_duplicate_fact_snapshot_repair"]["confirmation_phrase"] == "APPLY_PRODUCTION_DUPLICATE_FACT_SNAPSHOT_REPAIR"
    assert steps["apply_identity_merge_fill"]["confirmation_phrase"] == "APPLY_PRODUCTION_IDENTITY_MERGE_FILL"
    assert result["shared_backup_path"] in steps["apply_identity_audit_indexes"]["command"]
    assert result["shared_backup_path"] in steps["apply_duplicate_fact_snapshot_repair"]["command"]
    assert result["shared_backup_path"] in steps["apply_identity_merge_fill"]["command"]
    assert result["duplicate_manifest_path"] in steps["apply_duplicate_fact_snapshot_repair"]["command"]
    assert result["duplicate_manifest_path"] in steps["apply_identity_merge_fill"]["command"]
    assert summary["boundary"]["temp_drill_must_precede_production_apply"] is True
    assert summary["boundary"]["identity_audit_index_apply_must_precede_identity_coverage_verification"] is True
    assert summary["identity_audit_indexes"]["contract_ok"] is True
    assert summary["identity_audit_indexes"]["already_satisfied"] is False
    assert summary["identity_audit_indexes"]["needs_apply"] is True
    assert summary["identity_audit_indexes"]["contract_blocker_count"] == 0
    assert summary["control_sequence"][6]["step"] == "run_identity_closeout_temp_drill"
    assert summary["control_sequence"][9]["step"] == "apply_identity_audit_indexes"
    assert summary["control_sequence"][10]["step"] == "apply_duplicate_fact_snapshot_repair"
    assert summary["control_sequence"][13]["step"] == "apply_identity_merge_fill"


def test_identity_closeout_confirmation_packet_blocks_when_writers_are_active(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "archive.db"
    _create_fixture(db)
    _seed_duplicate_and_missing_identity(db)

    def fake_maintenance_packet(*args, **kwargs):
        return {
            "schema": "controlled_apply_maintenance_window_packet_v1",
            "ok": False,
            "writer_quiescent": False,
            "required_stop_labels": ["system/com.weather.monitor.daemon"],
            "blockers": [{"owner_class": "hard_source_direct_poller"}],
            "operator_commands": {"suggested_pause_commands": ["sudo launchctl disable system/com.weather.monitor.daemon && sudo launchctl stop system/com.weather.monitor.daemon"]},
            "stop_authorization": {"system_monitor_stop_required": True},
            "counts": {"writer_quiescence_blocker_count": 1},
            "next_boundary": "obtain_interactive_sudo_for_system_monitor_pause_then_rerun_packet",
        }

    monkeypatch.setattr(packet.maintenance_window, "build_maintenance_window_packet", fake_maintenance_packet)

    result = packet.build_confirmation_packet(
        db,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-07T00:00:00Z",
        max_items_output=5,
        limit=100,
    )

    assert result["read_only"] is True
    assert result["ok"] is False
    assert "writer_quiescence_not_satisfied" in result["hard_blockers"]
    assert result["maintenance_window"]["writer_quiescent"] is False
    assert result["control_sequence"][0]["step"] == "pause_writer_services"
    assert result["control_sequence"][0]["required"] is True
