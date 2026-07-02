from __future__ import annotations

import json
from pathlib import Path

from scripts import settlement_closed_loop_audit as audit


def test_closed_loop_waits_for_actual_confirmation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        audit.settlement_validation_pipeline,
        "build_report",
        lambda *args, **kwargs: {
            "next_action": "confirm_and_apply_settlement_actuals_then_rebuild_labels",
            "settlement_actual_apply_dry_run": {
                "ready_count": 2,
                "flow_confirm_command": "apply-actuals",
                "flow_confirm_and_label_apply_command": "apply-both",
                "flow_confirm_label_apply_and_validate_command": "apply-both-and-validate",
                "review_packet": {
                    "ready_count": 2,
                    "would_insert_event_run_rows": 2,
                    "requires_manual_review": True,
                    "items": [{"slug": "highest-temperature-in-nyc-on-may-31-2026"}],
                    "confirm_command": "apply-actuals-from-packet",
                },
            },
            "settlement_label_dry_run": {"planned_labels": 0},
            "after_actual_apply_simulation": {
                "label_planned_after_actual_apply": 88,
                "actual_inserted_count": 2,
                "label_apply_inserted_count": 88,
                "label_planned_after_label_apply": 0,
                "label_apply_closed_loop_ok": True,
            },
            "post_label_apply_validation": {
                "commands": {
                    "rolling_validation": "run-rolling",
                    "objective_audit": "run-objective",
                }
            },
            "write_safety": {
                "pre_apply_backup_required": True,
                "backup_default": "enabled",
                "bypass_effect": "confirmed_apply_blocked",
            },
        },
    )
    rolling = tmp_path / "rolling.json"
    objective = tmp_path / "objective.json"
    rolling.write_text(json.dumps({"ok": False, "blockers": ["not_enough_rows"]}))
    objective.write_text(json.dumps({"ok": False, "blockers": ["not_complete"]}))

    report = audit.build_report(
        tmp_path / "archive.db",
        rolling_report_path=rolling,
        objective_report_path=objective,
    )

    assert report["ok"] is False
    assert report["phase"] == "waiting_actual_confirmation"
    assert report["actual_ready"] == 2
    assert report["sim_labels_after_actual_apply"] == 88
    assert report["sim_label_apply_inserted"] == 88
    assert report["sim_label_planned_after_label_apply"] == 0
    assert report["sim_label_apply_closed_loop_ok"] is True
    assert "settlement_actuals_waiting_confirmation" in report["blockers"]
    assert "settlement_labels_predicted_after_actual_apply" in report["blockers"]
    assert report["commands"]["apply_actuals"] == "apply-actuals"
    assert report["commands"]["apply_actuals_and_labels"] == "apply-both"
    assert report["commands"]["apply_actuals_labels_and_validate"] == "apply-both-and-validate"
    assert report["write_safety"]["pre_apply_backup_required"] is True
    assert report["write_safety"]["bypass_effect"] == "confirmed_apply_blocked"
    assert report["actual_review_packet"]["would_insert_event_run_rows"] == 2
    assert report["actual_review_packet"]["items"][0]["slug"] == (
        "highest-temperature-in-nyc-on-may-31-2026"
    )
    assert report["actual_review_packet"]["confirm_command"] == "apply-actuals-from-packet"
    assert report["next_actions"] == [
        "review_and_confirm_settlement_actual_apply",
        "optional_double_confirm_actuals_and_labels_apply",
        "optional_double_confirm_apply_and_refresh_validation",
        "after_label_apply_run_rolling_validation",
        "after_label_apply_run_objective_audit",
    ]


def test_closed_loop_waits_for_label_confirmation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        audit.settlement_validation_pipeline,
        "build_report",
        lambda *args, **kwargs: {
            "next_action": "review_and_apply_settlement_calibration_labels",
            "settlement_actual_apply_dry_run": {"ready_count": 0},
            "settlement_label_dry_run": {
                "planned_labels": 5,
                "flow_apply_command": "apply-labels",
            },
        },
    )

    report = audit.build_report(
        tmp_path / "archive.db",
        rolling_report_path=tmp_path / "missing-rolling.json",
        objective_report_path=tmp_path / "missing-objective.json",
    )

    assert report["phase"] == "waiting_label_confirmation"
    assert report["labels_planned"] == 5
    assert report["commands"]["apply_labels"] == "apply-labels"
    assert report["next_actions"] == ["review_and_confirm_settlement_label_apply"]


def test_closed_loop_validated_after_reports_pass(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        audit.settlement_validation_pipeline,
        "build_report",
        lambda *args, **kwargs: {
            "next_action": "continue_collecting_paper_observation_samples",
            "settlement_actual_apply_dry_run": {"ready_count": 0},
            "settlement_label_dry_run": {"planned_labels": 0},
            "after_actual_apply_simulation": {"label_planned_after_actual_apply": 0},
        },
    )
    rolling = tmp_path / "rolling.json"
    objective = tmp_path / "objective.json"
    rolling.write_text(json.dumps({"ok": True, "next_mainline_task": "small_capital_scale_up_review"}))
    objective.write_text(json.dumps({"ok": True, "blockers": []}))

    report = audit.build_report(
        tmp_path / "archive.db",
        rolling_report_path=rolling,
        objective_report_path=objective,
    )

    assert report["ok"] is True
    assert report["phase"] == "settlement_closed_loop_validated"
    assert report["blockers"] == []
