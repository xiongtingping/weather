from __future__ import annotations

import json
import sqlite3

from sequential_bayes_schema_contract import apply_forward_schema
from scripts.materialize_probability_replay_equivalence_report import (
    LATEST_SCHEMA_VERSION,
    _latest_pointer_name,
    build_replay_equivalence_report,
    load_verified_latest_report,
)
from tests.test_sequential_bayes_shadow_pipeline import (
    NOW,
    TARGET,
    _artifact,
    _canonical_event_id,
    _previous_checkpoint,
    _resolution_contract,
    _run,
    _seed_previous_checkpoint,
)


def test_real_replay_report_refuses_target_without_incremental_event(tmp_path) -> None:
    db = tmp_path / "shadow.db"
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE resident_p10_prepared_cycles_shadow("
            "target_identity TEXT, prepared_at TEXT, cycle_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE posterior_checkpoints("
            "posterior_checkpoint_id TEXT,joint_posterior_sha256 TEXT,"
            "physical_high_distribution_json TEXT,latent_post_state_json TEXT,"
            "observed_max_lower_bound_c REAL,consumed_event_ids_json TEXT,"
            "forecast_evidence_snapshot_id TEXT,prior_context_id TEXT,"
            "resolution_contract_id TEXT,model_artifact_sha256 TEXT,"
            "canonical_sequence_sha256 TEXT,checkpoint_kind TEXT,committed_at TEXT,"
            "target_identity TEXT)"
        )
        conn.execute(
            "INSERT INTO resident_p10_prepared_cycles_shadow VALUES(?,?,?)",
            ("target", "2026-07-14T00:00:00Z", json.dumps({"target_identity": "target"})),
        )
    report = build_replay_equivalence_report(shadow_db=db, target_identity="target")
    assert report["ok"] is False
    assert report["blockers"] == ["replay_real_incremental_checkpoint_missing"]
    assert report["boundary"]["uses_synthetic_events"] is False


def test_target_specific_latest_pointer_missing_is_explicit(tmp_path) -> None:
    _payload, _path, blockers = load_verified_latest_report(
        shadow_db=tmp_path / "missing.db",
        target_identity="physical_weather_day_v1:ZUUU:2026-07-14",
        report_root=tmp_path / "reports",
        root=tmp_path,
    )
    assert blockers == ["replay_equivalence_report_missing"]
    assert _latest_pointer_name("a") != _latest_pointer_name("b")
    assert LATEST_SCHEMA_VERSION.endswith("_v1")


def test_real_incremental_checkpoint_matches_full_replay(tmp_path) -> None:
    db = tmp_path / "shadow.db"
    with sqlite3.connect(db) as conn:
        apply_forward_schema(conn)
        _seed_previous_checkpoint(conn, _previous_checkpoint())
        event_id = _canonical_event_id(conn)
        result = _run(conn, event_id)
        assert result["ok"] is True
        conn.execute(
            "CREATE TABLE resident_p10_prepared_cycles_shadow("
            "target_identity TEXT,prepared_at TEXT,cycle_json TEXT)"
        )
        cycle = {
            "prepared_cycle_id": "p10pc_replay_test",
            "prepared_cycle_sha256": "cycle-sha-replay-test",
            "target_identity": TARGET,
            "previous_checkpoint": _previous_checkpoint(),
            "posterior_context": {
                "forecast_evidence_snapshot_id": "forecast-current",
                "prior_context_id": "prior-current",
                "approved_lr_artifact": _artifact(),
                "resolution_contract": _resolution_contract(),
                "source_reliability_before": {"AMSC": {"state": "unknown"}},
                "temperature_quantization_c": 1.0,
            },
        }
        conn.execute(
            "INSERT INTO resident_p10_prepared_cycles_shadow VALUES(?,?,?)",
            (TARGET, NOW, json.dumps(cycle)),
        )
        conn.commit()

    report = build_replay_equivalence_report(shadow_db=db, target_identity=TARGET)
    assert report["ok"] is True
    assert report["state"] == "equivalent_current_target_snapshot"
    assert report["bound_snapshot"]["canonical_event_ids"] == [event_id]
    assert all(report["comparisons"].values())
