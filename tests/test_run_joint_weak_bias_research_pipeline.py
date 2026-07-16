from __future__ import annotations

import json
from pathlib import Path

from scripts import run_joint_weak_bias_research_pipeline as pipeline
from scripts.build_official_forecast_source_accuracy_report import _load_candidate_events
from tests.test_joint_forecast_weak_likelihood_training_runtime import _event


def test_pipeline_materializes_only_research_artifacts_from_station_day_candidates(tmp_path, monkeypatch) -> None:
    research_root = tmp_path / "research"
    event_root = research_root / "station_day_candidates"
    output_root = research_root / "bias_pipeline"
    event_root.mkdir(parents=True)
    monkeypatch.setattr(pipeline, "RESEARCH_ROOT", research_root)
    for index in range(31):
        event = _event(index, ["gfs", "taf"])
        (event_root / f"{event['event_date']}.json").write_text(json.dumps({
            "event_date": event["event_date"],
            "weak_training_event_envelope": {"events": [event]},
        }), encoding="utf-8")

    result = pipeline.run_joint_weak_bias_research_pipeline(
        event_root=event_root, output_root=output_root,
        minimum_event_days_per_surface=30, minimum_pair_samples=20,
        allow_research_write=True,
    )

    assert result["state"] == "materialized_shadow_research"
    assert result["pipeline_contract_version"] == "joint_weak_bias_research_pipeline_contract_v2"
    assert len(result["pipeline_contract_sha256"]) == 64
    assert result["write_authorized"] is True
    assert set(result["write_outcomes"]) == {"artifact_alpha_0.03", "artifact_alpha_0.05", "report"}
    assert result["boundary"]["writes_runtime_artifact_pointer"] is False
    assert result["boundary"]["writes_posterior"] is False
    assert result["boundary"]["places_orders"] is False
    assert (output_root / "latest.json").exists()
    assert len(list((output_root / "artifacts").glob("*.json"))) == 2
    assert result["rolling_origin_research"]["promotion_eligible"] is False
    assert result["readiness"]["schema"] == "joint_weak_bias_research_readiness_v1"
    assert result["readiness"]["training_event_day_count"] == 31
    assert result["readiness"]["promotion_state"] == "not_eligible_research_only"

    repeated = pipeline.run_joint_weak_bias_research_pipeline(
        event_root=event_root, output_root=output_root,
        minimum_event_days_per_surface=30, minimum_pair_samples=20,
        allow_research_write=True,
    )
    assert repeated["state"] == "skipped_unchanged_dataset"
    assert repeated["pipeline_contract_sha256"] == result["pipeline_contract_sha256"]

    changed_contract = pipeline.run_joint_weak_bias_research_pipeline(
        event_root=event_root, output_root=output_root,
        minimum_event_days_per_surface=30, minimum_pair_samples=21,
        allow_research_write=True,
    )
    assert changed_contract["state"] == "materialized_shadow_research"
    assert changed_contract["dataset_sha256"] == result["dataset_sha256"]
    assert changed_contract["pipeline_contract_sha256"] != result["pipeline_contract_sha256"]
    assert changed_contract["pipeline_sha256"] != result["pipeline_sha256"]


def test_candidate_loader_rejects_unbound_high_revision(tmp_path: Path) -> None:
    root = tmp_path / "station_day_candidates"
    root.mkdir()
    base = {
        "event_date": "2026-07-12",
        "materialization_sha256": "a" * 64,
        "weak_training_event_envelope": {"events": [{"event_date": "2026-07-12"}]},
    }
    (root / "2026-07-12.json").write_text(json.dumps(base), encoding="utf-8")
    rogue = {
        **base,
        "candidate_revision": 99,
        "materialization_sha256": "b" * 64,
        "supersedes": {
            "path": str(root / "does-not-exist.json"),
            "materialization_sha256": "c" * 64,
            "candidate_revision": 98,
        },
    }
    (root / "2026-07-12.v99-rogue.json").write_text(json.dumps(rogue), encoding="utf-8")

    events, excluded = _load_candidate_events(root)

    assert events == [{"event_date": "2026-07-12"}]
    assert excluded["candidate_supersede_chain_invalid"] == 1
