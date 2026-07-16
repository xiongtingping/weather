from scripts import daily_review_production_apply_preflight_packet as packet


def _fake_master_summary() -> dict:
    return {
        "schema": "daily_review_master_controlled_apply_summary_no_apply",
        "ok": True,
        "phase0": {
            "operator_apply_gate_summary": {
                "gates": [
                    {
                        "gate": "phase0_schema_apply",
                        "ready_after_operator_preflight": True,
                        "ready_now": False,
                        "unexpected_blockers": [],
                    },
                    {
                        "gate": "lineage_repair_apply",
                        "ready_after_operator_preflight": True,
                        "ready_now": False,
                        "blockers_now": [],
                    },
                    {
                        "gate": "daily_review_persistence_apply",
                        "ready_after_operator_preflight": False,
                        "ready_now": False,
                        "blockers_now": [
                            "daily_review_persistence_schema_missing",
                            "batch_plan_lineage_incomplete",
                        ],
                    },
                ]
            }
        },
    }


def _quiet_maintenance() -> dict:
    return {
        "schema": "controlled_apply_maintenance_window_packet_v1",
        "ok": True,
        "writer_quiescent": True,
        "required_stop_labels": [],
        "counts": {
            "writer_quiescence_blocker_count": 0,
            "write_capable_handle_process_count": 0,
        },
        "stop_authorization": {
            "system_monitor_stop_required": False,
            "noninteractive_sudo_available": False,
            "can_pause_required_services_now": True,
            "blockers": [],
        },
        "operator_commands": {
            "verify_writer_quiescence": "python3 scripts/controlled_apply_maintenance_window_packet.py --summary",
            "suggested_pause_commands": [],
            "suggested_resume_commands": [],
            "manual_wait_or_stop": [],
        },
    }


def _blocked_maintenance() -> dict:
    return {
        "schema": "controlled_apply_maintenance_window_packet_v1",
        "ok": False,
        "writer_quiescent": False,
        "required_stop_labels": ["system/com.weather.monitor.daemon"],
        "counts": {
            "writer_quiescence_blocker_count": 1,
            "write_capable_handle_process_count": 1,
        },
        "stop_authorization": {
            "system_monitor_stop_required": True,
            "noninteractive_sudo_available": False,
            "can_pause_required_services_now": False,
            "blockers": ["system_monitor_daemon_stop_requires_interactive_sudo"],
        },
        "operator_commands": {
            "verify_writer_quiescence": "python3 scripts/controlled_apply_maintenance_window_packet.py --summary",
            "suggested_pause_commands": ["sudo launchctl disable system/com.weather.monitor.daemon && sudo launchctl stop system/com.weather.monitor.daemon"],
            "suggested_resume_commands": [
                "sudo launchctl enable system/com.weather.monitor.daemon && sudo launchctl kickstart -k system/com.weather.monitor.daemon  # resume after apply"
            ],
            "manual_wait_or_stop": ["wait_for_backup_process_to_finish(pid=4505)"],
        },
    }


def _current_latest_posterior_ready() -> dict:
    return {
        "schema": "current_latest_posterior_materialization_preflight_v1",
        "read_only": True,
        "candidate_count": 3,
        "projected_ok_count": 3,
        "projected_issue_count": 0,
        "candidate_fact_ids": [106743, 104326, 103658],
        "candidate_count_matches_fact_ids": True,
        "apply_confirmation_command": (
            "python3 scripts/posterior_ssot_apply_confirmation_packet.py "
            "--fact-ids 106743,104326,103658 --summary"
        ),
        "single_fact_preflight_command_template": (
            "python3 scripts/posterior_ssot_apply_preflight_risk_packet.py --fact-id <fact_id>"
        ),
        "confirmation_phrase": "APPLY_POSTERIOR_SSOT_FACT_REPAIR",
    }


def _current_latest_posterior_none() -> dict:
    return {
        "schema": "current_latest_posterior_materialization_preflight_v1",
        "read_only": True,
        "candidate_count": 0,
        "projected_ok_count": 0,
        "projected_issue_count": 0,
        "candidate_fact_ids": [],
        "candidate_count_matches_fact_ids": True,
        "apply_confirmation_command": "",
    }


def test_current_latest_posterior_report_queries_latest_only(monkeypatch, tmp_path):
    calls = []

    def fake_build_plan(*args, **kwargs):
        calls.append(kwargs)
        return {
            "schema": "posterior_ssot_rematerialization_plan_v1",
            "guards": {"writes_archive_db": False},
            "historical_rematerialization_plan": {
                "current_latest_issue_count": 1,
                "current_latest_projected_ok_count": 1,
                "current_latest_projected_issue_count": 0,
                "issue_family_counts": {"runtime_policy_materialization_gap": 1},
                "current_issue_counts": {"posterior_runtime_policy_stale": 1},
            },
            "candidates": [
                {
                    "fact_id": 106888,
                    "is_latest_fact": True,
                }
            ],
        }

    monkeypatch.setattr(packet.rematerialization_plan, "build_plan", fake_build_plan)

    result = packet._current_latest_posterior_report(tmp_path / "archive.db")

    assert calls and calls[0]["latest_only"] is True
    assert result["candidate_count"] == 1
    assert result["candidate_fact_ids"] == [106888]
    assert result["candidate_count_matches_fact_ids"] is True


def _fake_resolution_packet(
    *,
    legacy_resolved: bool,
    posterior_candidate_count: int = 29,
    posterior_ready_for_apply: bool = True,
) -> dict:
    legacy = {
        "candidate_count": 2,
        "decision_required": not legacy_resolved,
        "operator_route_complete": legacy_resolved,
        "acceptance_exception_complete": legacy_resolved,
        "decision_manifest_schema": "legacy_awc_signal_anchor_decision_manifest_v1",
        "decision_manifest_audit": {
            "decision_counts": {"mark_reviewed_legacy_no_signal_anchor": 2}
            if legacy_resolved
            else {"undecided": 2},
            "hard_blockers": []
            if legacy_resolved
            else [
                "legacy_awc_signal_anchor_decision_manifest_missing",
                "legacy_awc_signal_anchor_decision_missing",
            ],
        },
    }
    return {
        "schema": "daily_review_post_apply_resolution_confirmation_packet_v1",
        "read_only": True,
        "dry_run": True,
        "posterior_ssot_repair_control": {
            "candidate_count": posterior_candidate_count,
            "ready_for_operator_controlled_apply": posterior_ready_for_apply,
            "apply_command_count": posterior_candidate_count if posterior_ready_for_apply else 0,
            "probe": {"projected_issue_count": 0},
        },
        "legacy_awc_signal_anchor_control": legacy,
        "daily_review_master_control_summary": _fake_master_summary(),
        "control_sequence": [
            {"step": "apply_posterior_ssot_repairs_per_fact", "writes_archive_db": True},
            {"step": "continue_with_daily_review_controlled_apply_sequence", "writes_archive_db": False},
        ],
    }


def test_production_preflight_blocks_start_when_legacy_awc_manifest_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        packet.resolution_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _fake_resolution_packet(legacy_resolved=False),
    )
    monkeypatch.setattr(
        packet.resolution_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "read_only": True},
    )

    result = packet.build_preflight_packet(
        tmp_path / "archive.db",
        maintenance_window_packet=_quiet_maintenance(),
        current_latest_posterior_packet=_current_latest_posterior_ready(),
    )

    assert result["schema"] == "daily_review_production_apply_preflight_packet_v1"
    assert result["read_only"] is True
    assert result["guards"]["writes_archive_db"] is False
    assert result["ready_to_start_controlled_sequence"] is False
    assert "legacy_awc_signal_anchor_decision_manifest_missing" in result["start_blockers"]

    stages = {row["key"]: row for row in result["stages"]}
    assert stages["maintenance_window_writer_quiescence"]["ready"] is True
    assert stages["legacy_awc_signal_anchor_decision_manifest"]["ready"] is False
    assert stages["current_latest_posterior_materialization"]["ready"] is True
    assert stages["current_latest_posterior_materialization"]["writes_archive_db_when_executed"] is True
    assert stages["current_latest_posterior_materialization"]["detail"]["candidate_fact_ids"] == [
        106743,
        104326,
        103658,
    ]
    assert stages["daily_review_posterior_ssot_fact_repair"]["ready"] is True
    assert stages["daily_review_posterior_ssot_fact_repair"]["writes_archive_db_when_executed"] is True
    assert stages["phase0_schema_apply"]["ready"] is True
    assert stages["lineage_repair_apply"]["ready"] is True
    assert stages["daily_review_persistence_apply"]["ready"] is False
    assert "daily_review_persistence_schema_missing" in result["sequence_dependency_blockers"]


def test_production_preflight_can_start_after_legacy_awc_reviewed_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(
        packet.resolution_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _fake_resolution_packet(legacy_resolved=True),
    )
    monkeypatch.setattr(
        packet.resolution_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "read_only": True},
    )

    result = packet.build_preflight_packet(
        tmp_path / "archive.db",
        maintenance_window_packet=_quiet_maintenance(),
        current_latest_posterior_packet=_current_latest_posterior_ready(),
    )

    assert result["ok"] is True
    assert result["ready_to_start_controlled_sequence"] is True
    assert result["start_blockers"] == []
    stages = {row["key"]: row for row in result["stages"]}
    assert stages["maintenance_window_writer_quiescence"]["ready"] is True
    assert stages["legacy_awc_signal_anchor_decision_manifest"]["ready"] is True
    assert stages["current_latest_posterior_materialization"]["ready"] is True
    assert stages["daily_review_persistence_apply"]["blocked"] is True
    assert result["current_latest_posterior_materialization"]["candidate_fact_ids"] == [
        106743,
        104326,
        103658,
    ]
    assert result["next_boundary"] == (
        "operator_can_start_controlled_sequence_with_explicit_confirmations_backups_and_writer_quiescence"
    )


def test_production_preflight_treats_zero_daily_review_posterior_candidates_as_ready(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        packet.resolution_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _fake_resolution_packet(
            legacy_resolved=True,
            posterior_candidate_count=0,
            posterior_ready_for_apply=False,
        ),
    )
    monkeypatch.setattr(
        packet.resolution_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "read_only": True},
    )

    result = packet.build_preflight_packet(
        tmp_path / "archive.db",
        maintenance_window_packet=_quiet_maintenance(),
        current_latest_posterior_packet=_current_latest_posterior_none(),
    )

    assert result["ready_to_start_controlled_sequence"] is True
    assert result["start_blockers"] == []
    stages = {row["key"]: row for row in result["stages"]}
    assert stages["current_latest_posterior_materialization"]["ready"] is True
    assert stages["current_latest_posterior_materialization"]["writes_archive_db_when_executed"] is False
    assert stages["daily_review_posterior_ssot_fact_repair"]["ready"] is True
    assert stages["daily_review_posterior_ssot_fact_repair"]["writes_archive_db_when_executed"] is False
    assert stages["daily_review_posterior_ssot_fact_repair"]["next_action"] == (
        "no_posterior_ssot_repairs_required"
    )


def test_production_preflight_blocks_start_when_writer_quiescence_needs_interactive_sudo(monkeypatch, tmp_path):
    monkeypatch.setattr(
        packet.resolution_packet,
        "build_confirmation_packet",
        lambda *_args, **_kwargs: _fake_resolution_packet(legacy_resolved=True),
    )
    monkeypatch.setattr(
        packet.resolution_packet,
        "compact_confirmation_packet",
        lambda value: {"schema": value["schema"], "read_only": True},
    )

    result = packet.build_preflight_packet(
        tmp_path / "archive.db",
        maintenance_window_packet=_blocked_maintenance(),
        current_latest_posterior_packet=_current_latest_posterior_ready(),
    )

    assert result["ok"] is False
    assert result["ready_to_start_controlled_sequence"] is False
    assert "writer_quiescence_not_satisfied" in result["start_blockers"]
    assert "system_monitor_daemon_stop_requires_interactive_sudo" in result["start_blockers"]
    stages = {row["key"]: row for row in result["stages"]}
    assert stages["maintenance_window_writer_quiescence"]["ready"] is False
    assert stages["maintenance_window_writer_quiescence"]["blocked"] is True
    assert stages["current_latest_posterior_materialization"]["ready"] is True
    assert result["maintenance_window"]["stop_authorization"]["can_pause_required_services_now"] is False
    assert result["maintenance_window"]["operator_commands"]["suggested_pause_commands"] == [
        "sudo launchctl disable system/com.weather.monitor.daemon && sudo launchctl stop system/com.weather.monitor.daemon"
    ]
    assert result["maintenance_window"]["operator_commands"]["manual_wait_or_stop"] == [
        "wait_for_backup_process_to_finish(pid=4505)"
    ]


def test_compact_preflight_packet_keeps_stage_and_guard_summary() -> None:
    compact = packet.compact_preflight_packet(
        {
            "schema": "daily_review_production_apply_preflight_packet_v1",
            "ok": False,
            "read_only": True,
            "dry_run": True,
            "db": "/tmp/archive.db",
            "scope": {"since_days": 45},
            "ready_to_start_controlled_sequence": False,
            "start_blockers": ["legacy_awc_signal_anchor_decision_missing"],
            "sequence_dependency_blockers": ["daily_review_persistence_schema_missing"],
            "stages": [
                {
                    "order": 0,
                    "key": "maintenance_window_writer_quiescence",
                    "ready": False,
                    "blocked": True,
                    "blockers": ["writer_quiescence_not_satisfied"],
                    "next_action": "pause_writers",
                    "writes_archive_db_when_executed": False,
                },
                {
                    "order": 2,
                    "key": "current_latest_posterior_materialization",
                    "ready": True,
                    "blocked": False,
                    "blockers": [],
                    "next_action": "run_exact_fact_id_confirmation",
                    "writes_archive_db_when_executed": True,
                },
                {
                    "order": 1,
                    "key": "legacy_awc_signal_anchor_decision_manifest",
                    "ready": False,
                    "blocked": True,
                    "blockers": ["legacy_awc_signal_anchor_decision_missing"],
                    "next_action": "provide_manifest",
                    "writes_archive_db_when_executed": False,
                }
            ],
            "maintenance_window": _blocked_maintenance(),
            "current_latest_posterior_materialization": _current_latest_posterior_ready(),
            "guards": {"writes_archive_db": False},
            "boundary": {"no_production_write": True},
            "next_boundary": "resolve_start_blockers_before_controlled_apply",
        }
    )

    assert compact["schema"] == "daily_review_production_apply_preflight_packet_v1_summary"
    assert compact["read_only"] is True
    assert compact["guards"]["writes_archive_db"] is False
    assert compact["stages"][0]["key"] == "maintenance_window_writer_quiescence"
    assert compact["maintenance_window"]["writer_quiescent"] is False
    assert compact["maintenance_window"]["required_stop_labels"] == ["system/com.weather.monitor.daemon"]
    assert compact["maintenance_window"]["operator_commands"]["verify_writer_quiescence"].endswith("--summary")
    assert compact["maintenance_window"]["operator_commands"]["manual_wait_or_stop"] == [
        "wait_for_backup_process_to_finish(pid=4505)"
    ]
    assert compact["current_latest_posterior_materialization"]["candidate_count"] == 3
    assert compact["current_latest_posterior_materialization"]["candidate_fact_ids"] == [
        106743,
        104326,
        103658,
    ]
