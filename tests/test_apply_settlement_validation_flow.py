from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from scripts import apply_settlement_validation_flow as flow


def _fake_actual_report(*args, **kwargs):
    apply_allowed = bool(kwargs.get("apply")) and (
        kwargs.get("confirm_apply")
        == flow.apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE
    )
    return {
        "apply_allowed": apply_allowed,
        "apply_confirmation_ok": (
            kwargs.get("confirm_apply")
            == flow.apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE
        ),
        "candidate_count": 2,
        "ready_count": 2,
        "inserted_count": 2 if apply_allowed else 0,
        "next_action": (
            "rebuild_settlement_calibration_labels"
            if apply_allowed
            else "rerun_with_confirm_apply_after_review"
        ),
        "results": [
            {
                "city": "nyc",
                "slug": "highest-temperature-in-nyc-on-may-31-2026",
                "status": "settlement_actual_inserted" if apply_allowed else "dry_run_ready",
                "actual": 77.8,
            },
            {
                "city": "toronto",
                "slug": "highest-temperature-in-toronto-on-may-31-2026",
                "status": "settlement_actual_inserted" if apply_allowed else "dry_run_ready",
                "actual": 24.4,
            },
        ],
        "review_packet": {
            "ready_count": 2,
            "would_insert_event_run_rows": 2,
            "items": [
                {"slug": "highest-temperature-in-nyc-on-may-31-2026"},
                {"slug": "highest-temperature-in-toronto-on-may-31-2026"},
            ],
        },
    }


def _fake_label_plan(*args, **kwargs):
    return {
        "planned_labels": 88,
        "decisions_scanned": 88,
        "skipped": {},
        "settlement_skipped": {},
        "labels": [
            {
                "decision_id": 1,
                "slug": "highest-temperature-in-nyc-on-may-31-2026",
                "event_date": "2026-05-31",
                "label": 1,
            }
        ],
    }


def test_flow_default_is_read_only(monkeypatch, tmp_path: Path) -> None:
    label_apply_calls: list[object] = []
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: label_apply_calls.append(args) or {"inserted": 1},
    )
    monkeypatch.setattr(
        flow.settlement_validation_pipeline,
        "build_report",
        lambda *args, **kwargs: {
            "after_actual_apply_simulation": {
                "simulated": True,
                "label_planned_after_actual_apply": 88,
            }
        },
    )

    report = flow.build_report(tmp_path / "archive.db")

    assert report["read_only"] is True
    assert report["actual_apply"]["inserted_count"] == 0
    assert report["actual_apply"]["review_packet"]["ready_count"] == 2
    assert report["label_plan"]["planned_labels"] == 88
    assert report["label_apply"]["status"] == "dry_run_only"
    assert report["after_actual_apply_simulation"]["label_planned_after_actual_apply"] == 88
    assert report["post_label_apply_validation"]["required"] is False
    assert report["post_label_apply_validation"]["commands"]["rolling_validation"] == (
        ".venv/bin/python scripts/paper_observation_rolling_validation.py "
        "--since-days 42 --market-scope highest_temperature --json"
    )
    assert report["pre_apply_backup"]["created"] is False
    assert report["pre_apply_backup"]["reason"] == "no_confirmed_apply_requested"
    assert label_apply_calls == []


def test_flow_blocks_actual_apply_without_confirmation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )

    report = flow.build_report(tmp_path / "archive.db", apply_actuals=True)

    assert report["ok"] is False
    assert report["read_only"] is True
    assert "actual_apply_not_allowed" in report["blockers"]
    assert report["actual_apply"]["inserted_count"] == 0
    assert report["pre_apply_backup"]["created"] is False
    assert report["pre_apply_backup"]["reason"] == "no_confirmed_apply_requested"


def test_flow_can_apply_actuals_without_applying_labels(monkeypatch, tmp_path: Path) -> None:
    label_apply_calls: list[object] = []
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: label_apply_calls.append(args) or {"inserted": 1},
    )

    db_path = tmp_path / "archive.db"
    sqlite3.connect(str(db_path)).close()
    report = flow.build_report(
        db_path,
        apply_actuals=True,
        confirm_actuals=flow.apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE,
        backup_dir=tmp_path / "backups",
    )

    assert report["ok"] is True
    assert report["read_only"] is False
    assert report["actual_apply"]["inserted_count"] == 2
    assert report["label_apply"]["status"] == "dry_run_only"
    assert report["pre_apply_backup"]["created"] is True
    assert label_apply_calls == []
    assert report["next_action"] == "review_label_plan_then_apply_labels"


def test_flow_blocks_confirmed_actual_apply_when_backup_missing(
    monkeypatch, tmp_path: Path
) -> None:
    label_apply_calls: list[object] = []
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: label_apply_calls.append(args) or {"inserted": 1},
    )

    report = flow.build_report(
        tmp_path / "missing.db",
        apply_actuals=True,
        confirm_actuals=flow.apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE,
    )

    assert report["ok"] is False
    assert report["read_only"] is True
    assert "pre_apply_backup_not_created" in report["blockers"]
    assert "actual_apply_not_allowed" in report["blockers"]
    assert report["actual_apply"]["inserted_count"] == 0
    assert report["pre_apply_backup"]["reason"] == "source_db_missing"
    assert label_apply_calls == []


def test_flow_requires_separate_label_confirmation(monkeypatch, tmp_path: Path) -> None:
    label_apply_calls: list[object] = []
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: label_apply_calls.append(args) or {"inserted": 1},
    )

    report = flow.build_report(tmp_path / "archive.db", apply_labels=True)

    assert report["ok"] is False
    assert "label_apply_not_allowed" in report["blockers"]
    assert report["label_apply"]["status"] == "missing_apply_confirmation"
    assert label_apply_calls == []


def test_flow_applies_labels_with_second_confirmation(monkeypatch, tmp_path: Path) -> None:
    label_apply_calls: list[object] = []
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: label_apply_calls.append(args) or {
            "inserted": 88,
            "skipped_existing": 0,
        },
    )

    db_path = tmp_path / "archive.db"
    sqlite3.connect(str(db_path)).close()
    report = flow.build_report(
        db_path,
        apply_labels=True,
        confirm_labels=flow.build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE,
        backup_dir=tmp_path / "backups",
    )

    assert report["ok"] is True
    assert report["read_only"] is False
    assert report["label_apply"]["status"] == "labels_applied"
    assert report["label_apply"]["inserted"] == 88
    assert report["pre_apply_backup"]["created"] is True
    assert report["post_label_apply_validation"]["required"] is True
    assert report["post_label_apply_validation"]["status"] == "run_after_labels_applied"
    assert report["post_label_apply_validation"]["commands"]["objective_audit"] == (
        ".venv/bin/python scripts/strategy_objective_completion_audit.py "
        "--since-days 42 --market-scope highest_temperature --json"
    )
    assert len(label_apply_calls) == 1


def test_flow_creates_sqlite_backup_before_confirmed_apply(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO marker(value) VALUES ('before')")
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: {"inserted": 88, "skipped_existing": 0},
    )

    report = flow.build_report(
        db_path,
        apply_actuals=True,
        confirm_actuals=flow.apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE,
        backup_dir=tmp_path / "backups",
    )

    backup = report["pre_apply_backup"]
    assert backup["created"] is True
    backup_path = Path(backup["path"])
    assert backup_path.exists()
    backup_conn = sqlite3.connect(str(backup_path))
    try:
        value = backup_conn.execute("SELECT value FROM marker").fetchone()[0]
    finally:
        backup_conn.close()
    assert value == "before"


def test_flow_can_disable_pre_apply_backup(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    sqlite3.connect(str(db_path)).close()
    label_apply_calls: list[object] = []
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: label_apply_calls.append(args) or {
            "inserted": 88,
            "skipped_existing": 0,
        },
    )

    report = flow.build_report(
        db_path,
        apply_labels=True,
        confirm_labels=flow.build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE,
        backup_before_apply=False,
        backup_dir=tmp_path / "backups",
    )

    assert report["ok"] is False
    assert report["read_only"] is True
    assert "pre_apply_backup_not_created" in report["blockers"]
    assert "label_apply_not_allowed" in report["blockers"]
    assert report["label_apply"]["status"] == "dry_run_only"
    assert report["pre_apply_backup"]["requested"] is True
    assert report["pre_apply_backup"]["created"] is False
    assert report["pre_apply_backup"]["reason"] == "disabled_by_no_backup_before_apply"
    assert label_apply_calls == []
    assert not (tmp_path / "backups").exists()


def test_flow_can_apply_actuals_and_labels_with_two_confirmations(monkeypatch, tmp_path: Path) -> None:
    label_apply_calls: list[object] = []
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: label_apply_calls.append(args) or {
            "inserted": 88,
            "skipped_existing": 0,
        },
    )

    db_path = tmp_path / "archive.db"
    sqlite3.connect(str(db_path)).close()
    report = flow.build_report(
        db_path,
        apply_actuals=True,
        confirm_actuals=flow.apply_settlement_actual_repair_plan.APPLY_CONFIRM_PHRASE,
        apply_labels=True,
        confirm_labels=flow.build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE,
        backup_dir=tmp_path / "backups",
    )

    assert report["ok"] is True
    assert report["actual_apply"]["inserted_count"] == 2
    assert report["label_apply"]["inserted"] == 88
    assert report["next_action"] == "refresh_rolling_validation_and_objective_audit"
    assert report["post_label_apply_validation"]["required"] is True
    assert report["post_validation_run"]["status"] == "not_requested"
    assert len(label_apply_calls) == 1


def test_flow_does_not_run_post_validation_until_labels_apply(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )

    report = flow.build_report(tmp_path / "archive.db", run_post_validation=True)

    assert report["read_only"] is True
    assert report["post_validation_run"]["run_requested"] is True
    assert report["post_validation_run"]["run_allowed"] is False
    assert report["post_validation_run"]["status"] == "not_run_until_labels_applied"


def test_flow_runs_post_validation_after_label_apply(monkeypatch, tmp_path: Path) -> None:
    label_apply_calls: list[object] = []
    monkeypatch.setattr(
        flow.apply_settlement_actual_repair_plan,
        "build_report",
        _fake_actual_report,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "build_label_plan",
        _fake_label_plan,
    )
    monkeypatch.setattr(
        flow.build_settlement_calibration_labels,
        "apply_label_plan",
        lambda *args, **kwargs: label_apply_calls.append(args) or {
            "inserted": 88,
            "skipped_existing": 0,
        },
    )

    class FakeRolling:
        DEFAULT_OUT = tmp_path / "rolling.json"

        @staticmethod
        def build_report(*args, **kwargs):
            return {"ok": True, "blockers": []}

    class FakeObjective:
        DEFAULT_OUT = tmp_path / "objective.json"

        @staticmethod
        def build_report(*args, **kwargs):
            return {"ok": False, "blockers": ["paper_small_cap_scale_validation_before_scaling"]}

    monkeypatch.setitem(sys.modules, "scripts.paper_observation_rolling_validation", FakeRolling)
    monkeypatch.setitem(sys.modules, "scripts.strategy_objective_completion_audit", FakeObjective)

    db_path = tmp_path / "archive.db"
    sqlite3.connect(str(db_path)).close()
    report = flow.build_report(
        db_path,
        apply_labels=True,
        confirm_labels=flow.build_settlement_calibration_labels.APPLY_CONFIRM_PHRASE,
        run_post_validation=True,
        backup_dir=tmp_path / "backups",
    )

    assert report["next_action"] == "review_post_validation_results"
    assert report["post_validation_run"]["status"] == "post_validation_refreshed"
    assert report["post_validation_run"]["rolling_ok"] is True
    assert report["post_validation_run"]["objective_ok"] is False
    assert FakeRolling.DEFAULT_OUT.exists()
    assert FakeObjective.DEFAULT_OUT.exists()
    assert len(label_apply_calls) == 1
