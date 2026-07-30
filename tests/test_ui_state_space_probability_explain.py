from __future__ import annotations

from ui import server


def _state_space_ssot(**overrides):
    value = {
        "schema": "sequential_temperature_posterior_ssot_v4_reported_x",
        "physical_state_posterior_schema": "physical_state_bayes_v6_production_successor_v1",
        "fact_snapshot_id": "fact_1",
        "posterior_run_id": "run_1",
        "posterior_checkpoint_id": "cp_2",
        "previous_checkpoint_id": "cp_1",
        "prior_context_id": "prior_1",
        "physical_driver_generation_sha256": "f" * 64,
        "physical_state_model_artifact_sha256": "t" * 64,
        "physical_observation_artifact_sha256": "o" * 64,
        "physical_reporting_operator_sha256": "r" * 64,
        "physical_path_projection_sha256": "p" * 64,
        "physical_current_event_consumption_count": 1,
        "current_observation_consumed_once": True,
        "forecast_revision_used_as_likelihood": False,
        "nwp_hourly_path_likelihood_reapplied": False,
        "hard_condition_applied": True,
        "reported_x_lower_bound_c": 35,
        "physical_last_weak_advance_json": '{"event_id":"event_1","forcing_generation_sha256":"old"}',
        "physical_forcing_rebind_json": '{"effective_from":"2026-07-21T08:00:00Z","revised_forcing_generation_sha256":"new"}',
        "reported_physical_x_distribution": {"41": 0.276},
        "committed_at": "2026-07-21T08:01:00Z",
    }
    value.update(overrides)
    return value


def test_state_space_lineage_is_read_only_and_does_not_make_forecast_likelihood_buckets():
    ssot = _state_space_ssot()
    compact = server._compact_posterior_ssot_fact_for_ui_snapshot(ssot)
    lineage = server._build_ui_snapshot_state_space_lineage(compact)
    weak = server._build_ui_snapshot_forecast_weak_section({"posterior_ssot_fact": ssot})

    assert lineage["schema"] == "ui_state_space_lineage_v1"
    assert lineage["previous_checkpoint_id"] == "cp_1"
    assert lineage["forcing"]["revision_used_as_likelihood"] is False
    assert lineage["observation_update"]["consumed"] is True
    assert lineage["hard_condition"]["applied"] is True
    assert weak["schema"] == "ui_state_transition_forcing_section_v1"
    assert weak["bucket_probabilities"] == []
    assert weak["role"] == "state_transition_forcing"
    assert weak["boundary"] == "state_transition_only_forecast_revision_is_not_an_observation_likelihood"


def test_physical_posterior_section_carries_state_space_lineage():
    section = server._build_ui_snapshot_physical_high_posterior_section(
        {"posterior_ssot_fact": _state_space_ssot()}
    )

    assert section["state"] == "available"
    assert section["top_temperature_c"] == 41.0
    assert section["state_space_lineage"]["reporting_operator"]["path_projection_sha256"] == "p" * 64


def test_frontend_uses_state_space_labels_and_explicit_invariants():
    html = server.load_html()

    assert "状态转移 / forcing" in html
    assert "forecast revision 作为似然" in html
    assert "ui_state_space_lineage_v1" in html
    assert "当前状态空间后验不展示旧式温度动量 / 峰值牵引 / 逐桶 logLR" in html
    assert "state-space lineage" in html or "状态空间 lineage" in html
