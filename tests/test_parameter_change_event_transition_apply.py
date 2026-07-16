import importlib.util
import json
import shutil
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "parameter_change_event_transition_apply.py"
spec = importlib.util.spec_from_file_location("parameter_change_event_transition_apply", SCRIPT_PATH)
transition_mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(transition_mod)


def _create_db(tmp_path: Path, *, initial_state: str = "applied") -> Path:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE parameter_change_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              calibration_candidate_id TEXT,
              state TEXT,
              changed_by TEXT,
              change_json TEXT,
              created_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO parameter_change_events(
              calibration_candidate_id, state, changed_by, change_json, created_at
            )
            VALUES ('cal_123', ?, 'unit', '{}', '2026-07-01T00:00:00Z')
            """,
            (initial_state,),
        )
    return db


def _manifest(*items: dict) -> dict:
    return {
        "schema": transition_mod.MANIFEST_SCHEMA,
        "transitions": list(items),
    }


def test_parameter_change_transition_contract_accepts_applied_to_measured(tmp_path: Path) -> None:
    db = _create_db(tmp_path, initial_state="applied")
    manifest = _manifest(
        {
            "calibration_candidate_id": "cal_123",
            "from_state": "applied",
            "to_state": "measured",
            "changed_by": "operator",
            "measurement_evidence": {
                "primary_metric": "brier_delta",
                "windows": {
                    "7d": {"delta": -0.01},
                    "30d": {"delta": -0.02},
                    "90d": {"delta": -0.03},
                },
            },
        }
    )

    contract = transition_mod.build_contract(db, manifest=manifest, max_items_output=5)

    assert contract["ok"] is True
    assert contract["operation_count"] == 1
    assert contract["operations"][0]["from_state"] == "applied"
    assert contract["operations"][0]["to_state"] == "measured"
    assert contract["guards"]["changes_parameters"] is False


def test_parameter_change_transition_blocks_missing_measurement_windows(tmp_path: Path) -> None:
    db = _create_db(tmp_path, initial_state="applied")
    manifest = _manifest(
        {
            "calibration_candidate_id": "cal_123",
            "from_state": "applied",
            "to_state": "measured",
            "measurement_evidence": {
                "primary_metric": "brier_delta",
                "windows": {"7d": {"delta": -0.01}},
            },
        }
    )

    contract = transition_mod.build_contract(db, manifest=manifest)

    assert contract["ok"] is False
    assert "transition_items_blocked" in contract["blockers"]
    blockers = contract["blocked_items"][0]["blockers"]
    assert "measurement_window_missing:30d" in blockers
    assert "measurement_window_missing:90d" in blockers


def test_parameter_change_transition_blocks_state_machine_skip(tmp_path: Path) -> None:
    db = _create_db(tmp_path, initial_state="applied")
    manifest = _manifest(
        {
            "calibration_candidate_id": "cal_123",
            "from_state": "applied",
            "to_state": "kept",
            "decision_evidence": {
                "keep_reason": "metric_improved",
                "measurement_summary": "all windows improved",
            },
        }
    )

    contract = transition_mod.build_contract(db, manifest=manifest)

    assert contract["ok"] is False
    assert "transition_not_allowed_by_state_machine" in contract["blocked_items"][0]["blockers"]


def test_parameter_change_transition_apply_appends_events_without_parameter_changes(tmp_path: Path) -> None:
    db = _create_db(tmp_path, initial_state="applied")
    backup = tmp_path / "archive.db.bak"
    shutil.copy2(db, backup)
    measured_manifest = _manifest(
        {
            "calibration_candidate_id": "cal_123",
            "from_state": "applied",
            "to_state": "measured",
            "changed_by": "operator",
            "measurement_evidence": {
                "primary_metric": "brier_delta",
                "windows": {
                    "7d": {"delta": -0.01},
                    "30d": {"delta": -0.02},
                    "90d": {"delta": -0.03},
                },
            },
        }
    )

    measured = transition_mod.apply_transitions(
        db,
        manifest=measured_manifest,
        backup_path=backup,
        allow_apply=True,
        confirmation_phrase=transition_mod.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        created_at="2026-07-08T00:00:00Z",
    )

    assert measured["ok"] is True
    assert measured["applied"] is True
    assert measured["guards"]["writes_parameter_change_events"] is True
    assert measured["guards"]["changes_parameters"] is False

    kept_manifest = _manifest(
        {
            "calibration_candidate_id": "cal_123",
            "from_state": "measured",
            "to_state": "kept",
            "changed_by": "operator",
            "decision_evidence": {
                "keep_reason": "metric_improved_without_side_effects",
                "measurement_summary": "7d/30d/90d windows improved",
            },
        }
    )
    kept = transition_mod.apply_transitions(
        db,
        manifest=kept_manifest,
        backup_path=backup,
        allow_apply=True,
        confirmation_phrase=transition_mod.CONFIRMATION_PHRASE,
        writer_quiescent=True,
        created_at="2026-07-09T00:00:00Z",
    )

    assert kept["ok"] is True
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT state, change_json FROM parameter_change_events ORDER BY id"
        ).fetchall()
    assert [row[0] for row in rows] == ["applied", "measured", "kept"]
    payload = json.loads(rows[-1][1])
    assert payload["schema"] == transition_mod.EVENT_SCHEMA
    assert payload["to_state"] == "kept"


def test_parameter_change_transition_apply_requires_confirmation(tmp_path: Path) -> None:
    db = _create_db(tmp_path, initial_state="measured")
    backup = tmp_path / "archive.db.bak"
    shutil.copy2(db, backup)
    manifest = _manifest(
        {
            "calibration_candidate_id": "cal_123",
            "from_state": "measured",
            "to_state": "rolled_back",
            "changed_by": "operator",
            "decision_evidence": {
                "rollback_reason": "side_effect_detected",
                "rollback_plan": {"restore": "previous_snapshot"},
            },
        }
    )

    result = transition_mod.apply_transitions(
        db,
        manifest=manifest,
        backup_path=backup,
        allow_apply=True,
        confirmation_phrase="WRONG",
        writer_quiescent=True,
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert "confirmation_phrase" in result["contract"]["apply_blockers"]
