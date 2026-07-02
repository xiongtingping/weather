from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts import settlement_validation_pipeline as pipeline


def test_pipeline_prioritizes_ready_actual_repairs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pipeline.settlement_actual_repair_plan,
        "build_report",
        lambda *args, **kwargs: {
            "missing_settlement_actual_count": 1,
            "next_action": "review_repair_plan_and_confirm_before_any_settlement_actual_write",
            "rows": [
                {
                    "city": "toronto",
                    "slug": "highest-temperature-in-toronto-on-may-31-2026",
                    "event_date": "2026-05-31",
                    "current_status": "closed_without_settlement_actual",
                    "fetch_preview": {"candidate_settlement_actual": 24.4},
                }
            ],
        },
    )
    monkeypatch.setattr(
        pipeline.apply_settlement_actual_repair_plan,
        "build_report",
        lambda *args, **kwargs: {
            "candidate_count": 1,
            "ready_count": 1,
            "read_only": True,
            "next_action": "rerun_with_confirm_apply_after_review",
            "results": [
                {
                    "city": "toronto",
                    "slug": "highest-temperature-in-toronto-on-may-31-2026",
                    "event_date": "2026-05-31",
                    "status": "dry_run_ready",
                    "actual": 24.4,
                    "blockers": [],
                }
            ],
            "review_packet": {
                "ready_count": 1,
                "would_insert_event_run_rows": 1,
                "items": [{"slug": "highest-temperature-in-toronto-on-may-31-2026"}],
            },
        },
    )
    monkeypatch.setattr(
        pipeline.build_settlement_calibration_labels,
        "build_label_plan",
        lambda *args, **kwargs: {
            "planned_labels": 0,
            "decisions_scanned": 10,
            "skipped": {"settlement_missing": 10},
            "apply_command": None,
        },
    )

    report = pipeline.build_report(tmp_path / "archive.db")

    assert report["ok"] is False
    assert report["read_only"] is True
    assert report["next_action"] == "confirm_and_apply_settlement_actuals_then_rebuild_labels"
    assert report["settlement_actual_apply_dry_run"]["ready_count"] == 1
    assert report["write_safety"]["pre_apply_backup_required"] is True
    assert report["write_safety"]["bypass_effect"] == "confirmed_apply_blocked"
    assert report["settlement_actual_apply_dry_run"]["pre_apply_backup_required"] is True
    assert report["settlement_actual_apply_dry_run"]["review_packet"]["ready_count"] == 1
    assert "APPLY_SETTLEMENT_ACTUALS" in report["settlement_actual_apply_dry_run"]["confirm_command"]
    assert report["settlement_actual_apply_dry_run"]["flow_confirm_command"] == (
        ".venv/bin/python scripts/apply_settlement_validation_flow.py "
        "--since-days 42 --market-scope highest_temperature "
        "--apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS"
    )
    assert report["settlement_actual_apply_dry_run"]["flow_confirm_and_label_apply_command"] == (
        ".venv/bin/python scripts/apply_settlement_validation_flow.py "
        "--since-days 42 --market-scope highest_temperature "
        "--apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS "
        "--apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS"
    )
    assert report["settlement_actual_apply_dry_run"]["flow_confirm_label_apply_and_validate_command"] == (
        ".venv/bin/python scripts/apply_settlement_validation_flow.py "
        "--since-days 42 --market-scope highest_temperature "
        "--apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS "
        "--apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS --run-post-validation"
    )
    assert report["flow_commands"]["apply_actuals"] == (
        ".venv/bin/python scripts/apply_settlement_validation_flow.py "
        "--since-days 42 --market-scope highest_temperature "
        "--apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS"
    )
    assert report["flow_commands"]["apply_actuals_and_labels"] == (
        ".venv/bin/python scripts/apply_settlement_validation_flow.py "
        "--since-days 42 --market-scope highest_temperature "
        "--apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS "
        "--apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS"
    )
    assert report["flow_commands"]["apply_actuals_labels_and_validate"] == (
        ".venv/bin/python scripts/apply_settlement_validation_flow.py "
        "--since-days 42 --market-scope highest_temperature "
        "--apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS "
        "--apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS --run-post-validation"
    )
    assert report["settlement_actual_apply_dry_run"]["after_apply_label_dry_run_command"] == (
        ".venv/bin/python scripts/build_settlement_calibration_labels.py "
        "--since-days 42 --market-scope highest_temperature"
    )
    assert "APPLY_SETTLEMENT_LABELS" in (
        report["settlement_actual_apply_dry_run"]["after_apply_label_apply_command"]
    )
    assert report["settlement_label_dry_run"]["skipped"] == {"settlement_missing": 10}


def test_pipeline_prioritizes_label_apply_when_labels_planned(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        pipeline.settlement_actual_repair_plan,
        "build_report",
        lambda *args, **kwargs: {"missing_settlement_actual_count": 0, "rows": []},
    )
    monkeypatch.setattr(
        pipeline.apply_settlement_actual_repair_plan,
        "build_report",
        lambda *args, **kwargs: {
            "candidate_count": 0,
            "ready_count": 0,
            "read_only": True,
            "results": [],
        },
    )
    monkeypatch.setattr(
        pipeline.build_settlement_calibration_labels,
        "build_label_plan",
        lambda *args, **kwargs: {
            "planned_labels": 3,
            "decisions_scanned": 12,
            "skipped": {},
            "apply_command": "apply-labels",
        },
    )

    report = pipeline.build_report(tmp_path / "archive.db")

    assert report["next_action"] == "review_and_apply_settlement_calibration_labels"
    assert report["settlement_label_dry_run"]["planned_labels"] == 3
    assert report["settlement_label_dry_run"]["pre_apply_backup_required"] is True
    assert report["settlement_label_dry_run"]["apply_command"] == "apply-labels"
    assert report["flow_commands"]["apply_labels"] == (
        ".venv/bin/python scripts/apply_settlement_validation_flow.py "
        "--since-days 42 --market-scope highest_temperature "
        "--apply-labels --confirm-labels APPLY_SETTLEMENT_LABELS"
    )
    assert report["settlement_label_dry_run"]["dry_run_command"] == (
        ".venv/bin/python scripts/build_settlement_calibration_labels.py "
        "--since-days 42 --market-scope highest_temperature"
    )
    assert report["settlement_label_dry_run"]["after_apply_validation_commands"] == {
        "rolling_validation": (
            ".venv/bin/python scripts/paper_observation_rolling_validation.py "
            "--since-days 42 --market-scope highest_temperature --json"
        ),
        "objective_audit": (
            ".venv/bin/python scripts/strategy_objective_completion_audit.py "
            "--since-days 42 --market-scope highest_temperature --json"
        ),
    }
    assert report["post_label_apply_validation"]["required"] is True
    assert report["flow_commands"]["after_labels_applied"]["rolling_validation"] == (
        ".venv/bin/python scripts/paper_observation_rolling_validation.py "
        "--since-days 42 --market-scope highest_temperature --json"
    )


def test_pipeline_simulates_after_actual_apply_on_temp_db(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE marker (value TEXT)")
    conn.execute("INSERT INTO marker(value) VALUES ('real')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        pipeline.settlement_actual_repair_plan,
        "build_report",
        lambda *args, **kwargs: {"missing_settlement_actual_count": 1, "rows": []},
    )
    monkeypatch.setattr(
        pipeline.apply_settlement_actual_repair_plan,
        "build_report",
        lambda *args, **kwargs: {
            "candidate_count": 1,
            "ready_count": 1,
            "read_only": not kwargs.get("apply"),
            "inserted_count": 1 if kwargs.get("apply") else 0,
            "next_action": "rebuild_settlement_calibration_labels",
            "results": [
                {
                    "city": "toronto",
                    "slug": "highest-temperature-in-toronto-on-may-31-2026",
                    "event_date": "2026-05-31",
                    "status": (
                        "settlement_actual_inserted"
                        if kwargs.get("apply")
                        else "dry_run_ready"
                    ),
                    "actual": 24.4,
                    "blockers": [],
                }
            ],
        },
    )

    label_calls: list[Path] = []

    def fake_label_plan(db_arg: Path, **kwargs):
        label_calls.append(Path(db_arg))
        return {
            "planned_labels": 5 if len(label_calls) == 2 else 0,
            "decisions_scanned": 12,
            "skipped": {"settlement_missing": 7 if len(label_calls) == 1 else 2},
            "labels": [{"decision_id": i, "label": 1} for i in range(5)]
            if len(label_calls) == 2
            else [],
            "apply_command": "apply-labels" if len(label_calls) == 2 else None,
        }

    monkeypatch.setattr(
        pipeline.build_settlement_calibration_labels,
        "build_label_plan",
        fake_label_plan,
    )
    monkeypatch.setattr(
        pipeline.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: {"inserted": 5, "skipped_existing": 0},
    )

    report = pipeline.build_report(
        db_path,
        simulate_after_actual_apply=True,
    )

    simulation = report["after_actual_apply_simulation"]
    assert simulation["ok"] is True
    assert simulation["read_only"] is True
    assert simulation["simulated"] is True
    assert simulation["actual_inserted_count"] == 1
    assert simulation["label_planned_after_actual_apply"] == 5
    assert simulation["label_apply_inserted_count"] == 5
    assert simulation["label_planned_after_label_apply"] == 0
    assert simulation["label_apply_closed_loop_ok"] is True
    assert label_calls[0] == db_path
    assert label_calls[1] != db_path
    assert label_calls[2] != db_path
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "real"
    finally:
        conn.close()


def test_pipeline_simulation_uses_subset_for_large_db(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE event_runs (id INTEGER PRIMARY KEY, slug TEXT, event_date TEXT)")
    conn.execute(
        "CREATE TABLE auto_trade_decisions (id INTEGER PRIMARY KEY, slug TEXT, event_date TEXT)"
    )
    conn.execute("CREATE TABLE calibration_samples (id INTEGER PRIMARY KEY, decision_id INTEGER)")
    conn.execute("CREATE TABLE calibration_labels (id INTEGER PRIMARY KEY, decision_id INTEGER)")
    conn.execute("CREATE TABLE filler (payload BLOB)")
    conn.execute(
        "INSERT INTO event_runs(id, slug, event_date) VALUES (1, ?, ?)",
        ("highest-temperature-in-toronto-on-may-31-2026", "2026-05-31"),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions(id, slug, event_date) VALUES (10, ?, ?)",
        ("highest-temperature-in-toronto-on-may-31-2026", "2026-05-31"),
    )
    conn.execute("INSERT INTO calibration_labels(id, decision_id) VALUES (20, 10)")
    conn.execute("INSERT INTO calibration_samples(id, decision_id) VALUES (30, 10)")
    conn.execute("INSERT INTO filler(payload) VALUES (?)", (b"0" * 4096,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        pipeline.settlement_actual_repair_plan,
        "build_report",
        lambda *args, **kwargs: {"missing_settlement_actual_count": 1, "rows": []},
    )
    monkeypatch.setattr(
        pipeline.apply_settlement_actual_repair_plan,
        "build_report",
        lambda *args, **kwargs: {
            "candidate_count": 1,
            "ready_count": 1,
            "read_only": True,
            "inserted_count": 1 if kwargs.get("apply") else 0,
            "results": [
                {
                    "city": "toronto",
                    "slug": "highest-temperature-in-toronto-on-may-31-2026",
                    "event_date": "2026-05-31",
                    "status": "dry_run_ready",
                    "actual": 24.4,
                    "blockers": [],
                }
            ],
        },
    )
    monkeypatch.setattr(
        pipeline.build_settlement_calibration_labels,
        "build_label_plan",
        lambda *args, **kwargs: {
            "planned_labels": 0,
            "decisions_scanned": 0,
            "skipped": {},
        },
    )

    report = pipeline.build_report(
        db_path,
        simulate_after_actual_apply=True,
        simulate_max_db_mb=0.001,
    )

    simulation = report["after_actual_apply_simulation"]
    assert simulation["simulated"] is True
    assert simulation["simulation_db_mode"] == "subset"
    assert simulation["subset_copy"]["copied"]["event_runs"] == 1
    assert simulation["subset_copy"]["copied"]["auto_trade_decisions"] == 1
    assert simulation["subset_copy"]["copied"]["calibration_samples"] == 1
    assert simulation["subset_copy"]["copied"]["calibration_labels"] == 1
