from pathlib import Path

from scripts import daily_review_post_apply_resolution_confirmation_packet as packet


def _fake_resolution() -> dict:
    return {
        "ok": True,
        "schema": "daily_review_post_apply_resolution_plan_v1",
        "resolution_complete": False,
        "unresolved_counts": {
            "missing_fact_snapshot_excluded": 1,
            "posterior_ssot_fact_candidates": 2,
            "legacy_awc_signal_anchor_candidates": 1,
        },
        "source_counts": {
            "effective_fact_count": 2,
            "excluded_settled_event_count": 1,
        },
        "lineage_resolution": {
            "excluded_sample_count": 1,
            "confirm_repair_count": 0,
            "proposed_manifest_decision_counts": {"exclude_sample": 1},
            "recommended_action": "keep_excluded_sample_out_of_daily_review_persistence_or_manual_rebuild_fact_snapshot",
            "excluded_samples": [
                {
                    "slug": "highest-temperature-in-chengdu-on-may-31-2026",
                    "event_date": "2026-05-30",
                    "event_run_id": 319,
                    "decision": "exclude_sample",
                    "repair_type": "missing_fact_snapshot_for_settled_event",
                    "reason": "unit_missing_fact_snapshot",
                    "state": "operator_decision_recorded_no_apply",
                    "guards": {"writes_archive_db": False},
                }
            ],
        },
        "posterior_ssot_resolution": {
            "candidate_count": 2,
            "candidate_fact_ids": [11, 12],
            "rematerialization_probe": {
                "available": True,
                "checked_fact_count": 2,
                "candidate_count": 2,
                "projected_ok_count": 2,
                "projected_issue_count": 0,
                "can_forward_writer_fix_all_candidates": True,
                "action_counts": {
                    "controlled_append_or_repoint_with_fact_writer_after_operator_confirmation": 2
                },
                "issue_family_counts": {"missing_posterior_ssot_fact": 2},
                "fact_ids": [11, 12],
                "items": [
                    {
                        "fact_id": 11,
                        "slug": "highest-temperature-in-chengdu-on-july-1-2026",
                        "event_date": "2026-07-01",
                    },
                    {"fact_id": 12},
                ],
            },
        },
        "replay_anchor_resolution": {
            "candidate_count": 1,
            "source_counts": {"zuuu_metar_raw_awc": 1},
            "constraint_mode_counts": {"hard_source_lower_bound_constraint": 1},
            "items": [
                {
                    "fact_id": 9,
                    "slug": "highest-temperature-in-chengdu-on-june-21-2026",
                    "event_date": "2026-06-21",
                    "override": {
                        "source": "zuuu_metar_raw_awc",
                        "constraint_mode": "hard_source_lower_bound_constraint",
                    },
                    "no_apply_probe_command": (
                        "python3 scripts/ssot_chain_replay_audit.py --fact-id 9 --max-signals-output 3"
                    ),
                }
            ],
        },
    }


def _fake_master() -> dict:
    return {
        "schema": "daily_review_controlled_apply_confirmation_packet_v1",
        "ok": True,
        "read_only": True,
        "dry_run": True,
        "proposed_manifest_decision_counts": {"confirm_repair": 2, "exclude_sample": 1},
        "boundary": {
            "phase0_schema_apply_included_as_controlled_step": True,
            "posterior_ssot_repair_included": False,
        },
        "confirmation_phrases": {
            "phase0_schema_apply": "APPLY_PHASE0_FORWARD_SCHEMA",
            "lineage_repair_apply": "APPLY_DAILY_REVIEW_LINEAGE_REPAIR",
            "daily_review_persistence_apply": "APPLY_DAILY_REVIEW_PERSISTENCE",
        },
        "control_sequence": [
            {"order": 1, "step": "review_master_packet", "writes": False},
            {"order": 2, "step": "apply_phase0_schema", "writes": True, "writes_archive_db": True},
        ],
        "guards": {"writes_archive_db": False},
    }


def test_resolution_confirmation_packet_stitches_post_apply_work_without_writing(monkeypatch, tmp_path: Path) -> None:
    captured = {}

    def fake_resolution_plan(db_path, **kwargs):
        captured["resolution_db"] = db_path
        captured["resolution_kwargs"] = kwargs
        return _fake_resolution()

    def fake_master_packet(db_path, **kwargs):
        captured["master_db"] = db_path
        captured["master_kwargs"] = kwargs
        return _fake_master()

    monkeypatch.setattr(packet.resolution_plan, "build_resolution_plan", fake_resolution_plan)
    monkeypatch.setattr(packet.master_packet, "build_confirmation_packet", fake_master_packet)
    monkeypatch.setattr(packet.master_packet, "compact_confirmation_packet", lambda value: value)

    result = packet.build_confirmation_packet(
        tmp_path / "archive.db",
        since_days=45,
        limit=100,
        max_items_output=10,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )

    assert result["schema"] == "daily_review_post_apply_resolution_confirmation_packet_v1"
    assert result["read_only"] is True
    assert result["dry_run"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["writes_event_fact_history"] is False
    assert captured["resolution_kwargs"]["max_items_output"] == 100
    assert captured["master_kwargs"]["max_items_output"] == 100
    assert captured["resolution_kwargs"]["master_confirmation_packet"] == _fake_master()

    posterior = result["posterior_ssot_repair_control"]
    assert posterior["ready_for_operator_controlled_apply"] is True
    assert posterior["apply_command_count"] == 2
    assert posterior["all_commands_materialized_in_packet"] is True
    first_command = posterior["apply_commands"][0]["command"]
    assert "--fact-id 11" in first_command
    assert "--slug highest-temperature-in-chengdu-on-july-1-2026" in first_command
    assert "--event-date 2026-07-01" in first_command
    assert "--confirm APPLY_POSTERIOR_SSOT_FACT_REPAIR" in first_command
    assert "--writer-quiescent" in first_command
    assert "--backup-dir" in first_command
    assert "--fact-id 12" in posterior["apply_commands"][1]["command"]
    missing_fact = result["missing_fact_snapshot_control"]
    assert missing_fact["excluded_sample_count"] == 1
    assert missing_fact["excluded_samples"][0]["slug"] == "highest-temperature-in-chengdu-on-may-31-2026"
    assert missing_fact["excluded_samples"][0]["event_date"] == "2026-05-30"
    assert missing_fact["excluded_samples"][0]["decision"] == "exclude_sample"
    assert missing_fact["excluded_samples"][0]["guards"]["writes_archive_db"] is False

    legacy = result["legacy_awc_signal_anchor_control"]
    assert legacy["decision_required"] is True
    assert legacy["ready_for_automatic_apply"] is False
    assert legacy["manual_decision_items"][0]["apply_command"] == ""
    assert "manual_rebuild_signal_anchor_with_reviewed_source_evidence" in legacy["manual_decision_items"][0]["allowed_operator_decisions"]
    assert "legacy_awc_signal_anchor_operator_decision_required" in result["hard_blockers"]
    assert result["ok"] is False

    steps = {str(row["step"]): row for row in result["control_sequence"]}
    assert steps["apply_posterior_ssot_repairs_per_fact"]["writes_archive_db"] is True
    assert steps["apply_posterior_ssot_repairs_per_fact"]["confirmation_phrase"] == "APPLY_POSTERIOR_SSOT_FACT_REPAIR"
    assert steps["resolve_legacy_awc_signal_anchors"]["writes"] is False
    assert steps["continue_with_daily_review_controlled_apply_sequence"]["nested_control_sequence_count"] == 2
    assert result["boundary"]["posterior_ssot_repair_included_as_controlled_step"] is True
    assert result["boundary"]["legacy_awc_signal_anchor_auto_rebuild_included"] is False


def test_resolution_confirmation_packet_can_emit_shared_posterior_backup(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(packet.resolution_plan, "build_resolution_plan", lambda *_args, **_kwargs: _fake_resolution())
    monkeypatch.setattr(packet.master_packet, "build_confirmation_packet", lambda *_args, **_kwargs: _fake_master())
    monkeypatch.setattr(packet.master_packet, "compact_confirmation_packet", lambda value: value)
    backup_path = tmp_path / "backups" / "archive.db.phase0.bak"

    result = packet.build_confirmation_packet(
        tmp_path / "archive.db",
        max_items_output=10,
        backup_dir=tmp_path / "backups",
        posterior_repair_backup=backup_path,
        legacy_awc_anchor_decision_manifest={
            "schema": packet.legacy_awc_anchor_packet.MANIFEST_SCHEMA_VERSION,
            "decisions": [
                {
                    "fact_id": 9,
                    "decision": "mark_reviewed_legacy_no_signal_anchor",
                    "reason": "unit_reviewed_legacy_no_signal",
                    "review_evidence": "unit review accepted no signal ledger anchor",
                }
            ],
        },
    )

    posterior = result["posterior_ssot_repair_control"]
    first_command = posterior["apply_commands"][0]["command"]
    assert posterior["backup_path"] == str(backup_path.resolve())
    assert f"--backup {backup_path.resolve()}" in first_command
    assert "--backup-dir" not in first_command
    steps = {str(row["step"]): row for row in result["control_sequence"]}
    assert steps["apply_posterior_ssot_repairs_per_fact"]["backup_path"] == str(backup_path.resolve())


def test_resolution_confirmation_summary_is_compact(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(packet.resolution_plan, "build_resolution_plan", lambda *_args, **_kwargs: _fake_resolution())
    monkeypatch.setattr(packet.master_packet, "build_confirmation_packet", lambda *_args, **_kwargs: _fake_master())
    monkeypatch.setattr(packet.master_packet, "compact_confirmation_packet", lambda value: value)

    full = packet.build_confirmation_packet(
        tmp_path / "archive.db",
        max_items_output=1,
        backup_dir=tmp_path / "backups",
        generated_at="2026-07-06T00:00:00Z",
    )
    summary = packet.compact_confirmation_packet(full)

    assert summary["schema"] == "daily_review_post_apply_resolution_confirmation_packet_v1_summary"
    assert summary["read_only"] is True
    assert summary["posterior_ssot_repair"]["candidate_count"] == 2
    assert summary["posterior_ssot_repair"]["apply_command_count"] == 2
    assert "apply_commands" not in summary["posterior_ssot_repair"]
    assert summary["legacy_awc_signal_anchor"]["candidate_count"] == 1
    assert summary["missing_fact_snapshot"]["excluded_sample_count"] == 1
    assert summary["missing_fact_snapshot"]["excluded_samples"] == [
        {
            "slug": "highest-temperature-in-chengdu-on-may-31-2026",
            "event_date": "2026-05-30",
            "event_run_id": 319,
            "decision": "exclude_sample",
            "repair_type": "missing_fact_snapshot_for_settled_event",
            "reason": "unit_missing_fact_snapshot",
            "state": "operator_decision_recorded_no_apply",
        }
    ]
    assert summary["daily_review_master"]["proposed_manifest_decision_counts"]["exclude_sample"] == 1
    assert summary["guards"]["writes_archive_db"] is False
    steps = {str(row["step"]): row for row in summary["control_sequence"]}
    assert "apply_posterior_ssot_repairs_per_fact" in steps
    assert len(steps["apply_posterior_ssot_repairs_per_fact"]["command"]) <= 3
    assert steps["apply_posterior_ssot_repairs_per_fact"]["commands_truncated"] is False
