from __future__ import annotations

import json
import sqlite3

from ui import server


def test_research_readiness_materializes_to_feedback_snapshot_and_ui_reads_archive_only(tmp_path, monkeypatch) -> None:
    root = tmp_path / "bias_pipeline"
    reports = root / "reports"
    reports.mkdir(parents=True)
    pipeline_sha = "a" * 64
    report_path = reports / f"{pipeline_sha}.json"
    report = {
        "schema": "joint_weak_bias_research_pipeline_v1", "pipeline_sha256": pipeline_sha,
        "pipeline_contract_version": "joint_weak_bias_research_pipeline_contract_v2",
        "pipeline_contract_sha256": "c" * 64,
        "dataset_sha256": "b" * 64,
        "boundary": {"research_only": True, "writes_posterior": False, "places_orders": False},
        "readiness": {"schema": "joint_weak_bias_research_readiness_v1", "state": "pending_formal_samples", "research_only": True, "production_weight_input": False, "training_event_day_count": 12, "minimum_independent_event_days": 30, "frozen_alpha_oos": {}, "rolling_origin": {"state": "not_run"}, "promotion_state": "not_eligible_research_only", "promotion_blockers": ["insufficient_independent_event_days"]},
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    (root / "latest.json").write_text(json.dumps({"schema": "joint_weak_bias_research_pipeline_v1", "pipeline_sha256": pipeline_sha, "pipeline_contract_version": "joint_weak_bias_research_pipeline_contract_v2", "pipeline_contract_sha256": "c" * 64, "report_path": str(report_path)}), encoding="utf-8")
    monkeypatch.setattr(server, "JOINT_WEAK_BIAS_RESEARCH_ROOT", root)
    conn = sqlite3.connect(":memory:")

    materialized = server.materialize_joint_weak_bias_research_readiness(conn)
    ui_payload = server._safe_joint_weak_bias_research_snapshot_only(conn)

    assert materialized["schema"] == "joint_weak_bias_research_snapshot_v1"
    assert materialized["pipeline_contract_sha256"] == "c" * 64
    assert materialized["research_only"] is True
    assert ui_payload["schema"] == "ui_joint_weak_bias_research_v1"
    assert ui_payload["pipeline_contract_version"] == "joint_weak_bias_research_pipeline_contract_v2"
    assert ui_payload["state"] == "pending_formal_samples"
    assert ui_payload["training_event_day_count"] == 12
    assert ui_payload["production_weight_input"] is False
