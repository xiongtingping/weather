from __future__ import annotations

import json
from pathlib import Path

from scripts import strategy_goal_completion_matrix as matrix


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_apply_flow_and_confirmation(
    report_dir: Path,
    *,
    flow_generated_at: str = "2026-06-01T00:00:00+00:00",
    packet_flow_generated_at: str | None = None,
    actual_ready: int = 2,
    would_insert_event_run_rows: int = 2,
) -> None:
    _write(
        report_dir / "settlement_validation_apply_flow_latest.json",
        {
            "ok": True,
            "generated_at": flow_generated_at,
            "actual_apply": {
                "ready_count": actual_ready,
                "review_packet": {
                    "would_insert_event_run_rows": would_insert_event_run_rows
                },
            },
        },
    )
    _write(
        report_dir / "settlement_confirmation_packet_latest.json",
        {
            "ok": True,
            "blockers": [],
            "flow_generated_at": packet_flow_generated_at or flow_generated_at,
            "actual_ready": actual_ready,
            "would_insert_event_run_rows": would_insert_event_run_rows,
        },
    )


def _write_base_closed_loop_inputs(report_dir: Path) -> None:
    _write(
        report_dir / "settlement_closed_loop_audit_latest.json",
        {"ok": True, "phase": "settlement_closed_loop_validated", "blockers": []},
    )
    _write(
        report_dir / "settlement_validation_pipeline_latest.json",
        {
            "ok": True,
            "write_safety": {
                "pre_apply_backup_required": True,
                "bypass_effect": "confirmed_apply_blocked",
            },
        },
    )
    _write(
        report_dir / "recommended_pilot_gap_refresh_health_latest.json",
        {"ok": True, "blockers": [], "queue_count": 0},
    )
    _write_apply_flow_and_confirmation(report_dir)


def test_matrix_blocks_stale_objective_schema_and_waiting_settlement(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": False,
            "requirements": [
                {"name": name, "ok": True, "blockers": []}
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
                if name != "settlement_write_safety_backup_gate"
            ],
        },
    )
    _write(
        report_dir / "settlement_closed_loop_audit_latest.json",
        {
            "ok": False,
            "phase": "waiting_actual_confirmation",
            "actual_ready": 2,
            "sim_labels_after_actual_apply": 88,
            "sim_label_apply_closed_loop_ok": True,
            "blockers": ["settlement_actuals_waiting_confirmation"],
        },
    )
    _write(
        report_dir / "settlement_validation_pipeline_latest.json",
        {"ok": False},
    )
    _write(
        report_dir / "recommended_pilot_gap_refresh_health_latest.json",
        {"ok": True, "blockers": [], "queue_count": 0},
    )
    _write_apply_flow_and_confirmation(report_dir)

    report = matrix.build_report(report_dir)

    assert report["ok"] is False
    assert report["next_mainline_task"] == "refresh_latest_audits_to_current_schema"
    assert (
        "settlement_write_safety_backup_gate:requirement_missing_from_latest_objective_audit"
        in report["blockers"]
    )
    assert "latest_pipeline_write_safety_schema:write_safety_missing_or_not_proved" in report["blockers"]
    settlement = [row for row in report["rows"] if row["name"] == "real_settlement_label_closed_loop"][0]
    assert settlement["phase"] == "waiting_actual_confirmation"
    assert settlement["sim_labels_after_actual_apply"] == 88


def test_matrix_does_not_treat_partial_objective_requirement_as_proved(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": False,
            "requirements": [
                {
                    "name": name,
                    "ok": True,
                    "status": (
                        "partial"
                        if name == "resource_focus_70_structural_30_major_settlement"
                        else "proved"
                    ),
                    "blockers": [],
                    "warnings": (
                        ["no_selected_events_to_measure_share"]
                        if name == "resource_focus_70_structural_30_major_settlement"
                        else []
                    ),
                    "evidence": (
                        {"selected_count": 0}
                        if name == "resource_focus_70_structural_30_major_settlement"
                        else {}
                    ),
                }
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ],
        },
    )
    _write_base_closed_loop_inputs(report_dir)

    report = matrix.build_report(report_dir)

    row = [
        item
        for item in report["rows"]
        if item["name"] == "resource_focus_70_structural_30_major_settlement"
    ][0]
    assert row["status"] == "partial"
    assert row["ok"] is True
    assert row["evidence"] == {"selected_count": 0}
    assert (
        "resource_focus_70_structural_30_major_settlement:objective_requirement_not_proved:partial"
        not in report["blockers"]
    )


def test_matrix_passes_when_all_requirements_and_settlement_closed_loop_pass(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": True,
            "requirements": [
                {"name": name, "ok": True, "status": "proved", "blockers": []}
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ],
        },
    )
    _write(
        report_dir / "settlement_closed_loop_audit_latest.json",
        {
            "ok": True,
            "phase": "settlement_closed_loop_validated",
            "blockers": [],
        },
    )
    _write(
        report_dir / "settlement_validation_pipeline_latest.json",
        {
            "ok": True,
            "write_safety": {
                "pre_apply_backup_required": True,
                "bypass_effect": "confirmed_apply_blocked",
            },
        },
    )
    _write(
        report_dir / "recommended_pilot_gap_refresh_health_latest.json",
        {
            "ok": True,
            "blockers": [],
            "queue_count": 0,
            "priority_slug_count": 0,
        },
    )
    _write_apply_flow_and_confirmation(
        report_dir,
        actual_ready=0,
        would_insert_event_run_rows=0,
    )

    report = matrix.build_report(report_dir)

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["next_mainline_task"] == "strategy_goal_completion_review"


def test_matrix_blocks_when_recommended_pilot_gap_refresh_is_stale(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": True,
            "requirements": [
                {"name": name, "ok": True, "blockers": []}
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ],
        },
    )
    _write(
        report_dir / "settlement_closed_loop_audit_latest.json",
        {"ok": True, "phase": "settlement_closed_loop_validated", "blockers": []},
    )
    _write(
        report_dir / "settlement_validation_pipeline_latest.json",
        {
            "ok": True,
            "write_safety": {
                "pre_apply_backup_required": True,
                "bypass_effect": "confirmed_apply_blocked",
            },
        },
    )
    _write(
        report_dir / "recommended_pilot_gap_refresh_health_latest.json",
        {
            "ok": False,
            "queue_count": 2,
            "priority_slug_count": 4,
            "blockers": ["highest-temperature-in-ankara-on-june-2-2026:tick_stale"],
        },
    )
    _write_apply_flow_and_confirmation(report_dir)

    report = matrix.build_report(report_dir)

    assert report["ok"] is True
    assert report["next_mainline_task"] == "strategy_goal_completion_review"
    row = [
        item
        for item in report["rows"]
        if item["name"] == "recommended_pilot_gap_refresh_health"
    ][0]
    assert row["goal_blocking"] is False
    assert (
        "recommended_pilot_gap_refresh_health:highest-temperature-in-ankara-on-june-2-2026:tick_stale"
        not in report["blockers"]
    )
    assert "highest-temperature-in-ankara-on-june-2-2026:tick_stale" in row["blockers"]


def test_matrix_blocks_when_settlement_confirmation_packet_missing(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": True,
            "requirements": [
                {"name": name, "ok": True, "blockers": []}
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ],
        },
    )
    _write(
        report_dir / "settlement_closed_loop_audit_latest.json",
        {"ok": True, "phase": "settlement_closed_loop_validated", "blockers": []},
    )
    _write(
        report_dir / "settlement_validation_pipeline_latest.json",
        {
            "ok": True,
            "write_safety": {
                "pre_apply_backup_required": True,
                "bypass_effect": "confirmed_apply_blocked",
            },
        },
    )
    _write(
        report_dir / "recommended_pilot_gap_refresh_health_latest.json",
        {"ok": True, "blockers": []},
    )

    report = matrix.build_report(report_dir)

    assert report["ok"] is True
    row = [
        item
        for item in report["rows"]
        if item["name"] == "settlement_confirmation_packet_ready"
    ][0]
    assert row["goal_blocking"] is False
    assert (
        "settlement_confirmation_packet_ready:settlement_confirmation_packet_missing"
        not in report["blockers"]
    )
    assert "settlement_confirmation_packet_missing" in row["blockers"]


def test_matrix_blocks_when_confirmation_packet_missing_latest_apply_flow(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": True,
            "requirements": [
                {"name": name, "ok": True, "blockers": []}
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ],
        },
    )
    _write(
        report_dir / "settlement_closed_loop_audit_latest.json",
        {"ok": True, "phase": "settlement_closed_loop_validated", "blockers": []},
    )
    _write(
        report_dir / "settlement_validation_pipeline_latest.json",
        {
            "ok": True,
            "write_safety": {
                "pre_apply_backup_required": True,
                "bypass_effect": "confirmed_apply_blocked",
            },
        },
    )
    _write(
        report_dir / "recommended_pilot_gap_refresh_health_latest.json",
        {"ok": True, "blockers": []},
    )
    _write(
        report_dir / "settlement_confirmation_packet_latest.json",
        {
            "ok": True,
            "blockers": [],
            "flow_generated_at": "2026-06-01T00:00:00+00:00",
            "actual_ready": 2,
            "would_insert_event_run_rows": 2,
        },
    )

    report = matrix.build_report(report_dir)

    assert report["ok"] is True
    row = [
        item
        for item in report["rows"]
        if item["name"] == "settlement_confirmation_packet_ready"
    ][0]
    assert row["goal_blocking"] is False
    assert (
        "settlement_confirmation_packet_ready:settlement_validation_apply_flow_missing"
        not in report["blockers"]
    )
    assert "settlement_validation_apply_flow_missing" in row["blockers"]


def test_matrix_blocks_when_confirmation_packet_is_stale_or_mismatched(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": True,
            "requirements": [
                {"name": name, "ok": True, "blockers": []}
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ],
        },
    )
    _write(
        report_dir / "settlement_closed_loop_audit_latest.json",
        {"ok": True, "phase": "settlement_closed_loop_validated", "blockers": []},
    )
    _write(
        report_dir / "settlement_validation_pipeline_latest.json",
        {
            "ok": True,
            "write_safety": {
                "pre_apply_backup_required": True,
                "bypass_effect": "confirmed_apply_blocked",
            },
        },
    )
    _write(
        report_dir / "recommended_pilot_gap_refresh_health_latest.json",
        {"ok": True, "blockers": []},
    )
    _write(
        report_dir / "settlement_validation_apply_flow_latest.json",
        {
            "ok": True,
            "generated_at": "2026-06-01T01:00:00+00:00",
            "actual_apply": {
                "ready_count": 3,
                "review_packet": {"would_insert_event_run_rows": 3},
            },
        },
    )
    _write(
        report_dir / "settlement_confirmation_packet_latest.json",
        {
            "ok": True,
            "blockers": [],
            "flow_generated_at": "2026-06-01T00:00:00+00:00",
            "actual_ready": 2,
            "would_insert_event_run_rows": 2,
        },
    )

    report = matrix.build_report(report_dir)

    assert report["ok"] is True
    row = [
        item
        for item in report["rows"]
        if item["name"] == "settlement_confirmation_packet_ready"
    ][0]
    assert row["goal_blocking"] is False
    assert (
        "settlement_confirmation_packet_ready:settlement_confirmation_packet_stale_for_latest_apply_flow"
        not in report["blockers"]
    )
    assert (
        "settlement_confirmation_packet_stale_for_latest_apply_flow"
        in row["blockers"]
    )
    assert (
        "settlement_confirmation_packet_actual_ready_mismatch"
        in row["blockers"]
    )
    assert "settlement_confirmation_packet_insert_rows_mismatch" in row["blockers"]


def test_matrix_blocks_when_paper_samples_needed_but_gap_scan_plan_missing(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": False,
            "requirements": [
                {
                    "name": name,
                    "ok": name != "paper_small_cap_scale_validation_before_scaling",
                    "blockers": (
                        ["rolling:paper_observation_resolved_rows_below_min"]
                        if name == "paper_small_cap_scale_validation_before_scaling"
                        else []
                    ),
                }
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ]
            + [
                {
                    "name": "paper_small_cap_scale_validation_before_scaling",
                    "ok": False,
                    "blockers": ["rolling:paper_observation_resolved_rows_below_min"],
                }
            ],
        },
    )
    _write_base_closed_loop_inputs(report_dir)

    report = matrix.build_report(report_dir)

    assert report["ok"] is True
    row = [
        item
        for item in report["rows"]
        if item["name"] == "paper_observation_gap_scan_plan_ready"
    ][0]
    assert row["goal_blocking"] is False
    assert (
        "paper_observation_gap_scan_plan_ready:paper_observation_gap_scan_plan_missing"
        not in report["blockers"]
    )
    assert "paper_observation_gap_scan_plan_missing" in row["blockers"]


def test_matrix_proves_gap_scan_plan_when_paper_samples_needed_and_slugs_exist(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": False,
            "generated_at": "2026-06-01T00:00:00+00:00",
            "requirements": [
                {
                    "name": name,
                    "ok": name != "paper_small_cap_scale_validation_before_scaling",
                    "blockers": (
                        ["rolling:paper_observation_resolved_rows_below_min"]
                        if name == "paper_small_cap_scale_validation_before_scaling"
                        else []
                    ),
                }
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ]
            + [
                {
                    "name": "paper_small_cap_scale_validation_before_scaling",
                    "ok": False,
                    "blockers": ["rolling:paper_observation_resolved_rows_below_min"],
                }
            ],
        },
    )
    _write_base_closed_loop_inputs(report_dir)
    _write(
        report_dir / "paper_observation_target_package_latest.json",
        {
            "generated_at": "2026-06-01T00:05:00+00:00",
            "city_targets": [{"city": "ankara", "needed_resolved_rows": 7}],
        },
    )
    _write(
        report_dir / "recommended_pilot_gap_scan_plan_latest.json",
        {
            "ok": False,
            "audit_generated_at": "2026-06-01T00:00:00+00:00",
            "target_package_generated_at": "2026-06-01T00:05:00+00:00",
            "queue_count": 1,
            "city_count": 1,
            "strategy_gap_count": 1,
            "priority_slug_count": 1,
            "priority_slugs": ["highest-temperature-in-ankara-on-june-2-2026"],
            "plan": [
                {
                    "city": "ankara",
                    "needed_resolved_rows": 7,
                    "resolved_rows": 3,
                    "pending_rows": 0,
                    "strategy_gaps": [
                        {
                            "target_strategy": "reverse_edge_buy_no_sell_yes",
                            "needed_resolved_rows": 1,
                        }
                    ],
                }
            ],
        },
    )

    report = matrix.build_report(report_dir)

    row = [
        item
        for item in report["rows"]
        if item["name"] == "paper_observation_gap_scan_plan_ready"
    ][0]
    assert row["status"] == "proved"
    assert row["ok"] is True
    assert row["priority_slugs_preview"] == [
        "highest-temperature-in-ankara-on-june-2-2026"
    ]
    assert row["audit_generated_at"] == "2026-06-01T00:00:00+00:00"
    assert row["target_package_generated_at"] == "2026-06-01T00:05:00+00:00"
    assert row["plan_preview"][0]["city"] == "ankara"
    assert (
        "paper_observation_gap_scan_plan_ready:paper_observation_gap_scan_plan_missing"
        not in report["blockers"]
    )


def test_matrix_blocks_stale_gap_scan_plan_source_timestamps(
    tmp_path: Path,
) -> None:
    report_dir = tmp_path / "reports"
    _write(
        report_dir / "strategy_objective_completion_audit_latest.json",
        {
            "ok": False,
            "generated_at": "2026-06-01T01:00:00+00:00",
            "requirements": [
                {
                    "name": name,
                    "ok": name != "paper_small_cap_scale_validation_before_scaling",
                    "blockers": (
                        ["rolling:paper_observation_resolved_rows_below_min"]
                        if name == "paper_small_cap_scale_validation_before_scaling"
                        else []
                    ),
                }
                for name in matrix.REQUIRED_OBJECTIVE_REQUIREMENTS
            ]
            + [
                {
                    "name": "paper_small_cap_scale_validation_before_scaling",
                    "ok": False,
                    "blockers": ["rolling:paper_observation_resolved_rows_below_min"],
                }
            ],
        },
    )
    _write_base_closed_loop_inputs(report_dir)
    _write(
        report_dir / "paper_observation_target_package_latest.json",
        {
            "generated_at": "2026-06-01T01:05:00+00:00",
            "city_targets": [{"city": "buenos-aires", "needed_resolved_rows": 10}],
        },
    )
    _write(
        report_dir / "recommended_pilot_gap_scan_plan_latest.json",
        {
            "ok": False,
            "audit_generated_at": "2026-06-01T00:00:00+00:00",
            "target_package_generated_at": "2026-06-01T00:05:00+00:00",
            "queue_count": 1,
            "city_count": 1,
            "strategy_gap_count": 1,
            "priority_slug_count": 1,
            "priority_slugs": ["highest-temperature-in-chongqing-on-june-2-2026"],
            "plan": [
                {
                    "city": "chongqing",
                    "needed_resolved_rows": 7,
                    "strategy_gaps": [
                        {
                            "target_strategy": "reverse_edge_buy_no_sell_yes",
                            "needed_resolved_rows": 1,
                        }
                    ],
                }
            ],
        },
    )

    report = matrix.build_report(report_dir)

    row = [
        item
        for item in report["rows"]
        if item["name"] == "paper_observation_gap_scan_plan_ready"
    ][0]
    assert row["status"] == "blocked"
    assert row["ok"] is False
    assert row["goal_blocking"] is False
    assert (
        "paper_observation_gap_scan_plan_ready:paper_observation_gap_scan_plan_stale_for_latest_objective_audit"
        not in report["blockers"]
    )
    assert (
        "paper_observation_gap_scan_plan_stale_for_latest_objective_audit"
        in row["blockers"]
    )
    assert "paper_observation_gap_scan_plan_stale_for_latest_target_package" in row["blockers"]
    assert row["latest_objective_audit_generated_at"] == "2026-06-01T01:00:00+00:00"
    assert row["latest_target_package_generated_at"] == "2026-06-01T01:05:00+00:00"
