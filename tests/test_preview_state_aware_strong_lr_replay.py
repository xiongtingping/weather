from scripts import preview_state_aware_strong_lr_replay as preview


def test_preview_peak_timing_uses_latest_replay_update_and_initial_weak_prior(monkeypatch):
    captured = {}

    def fake_trajectory(**kwargs):
        captured["trajectory_args"] = kwargs
        return {"ok": True, "temporal_role": "post_event"}

    def fake_peak_timing(**kwargs):
        captured["peak_args"] = kwargs
        return {
            "schema": "peak_timing_state_l3_v1",
            "ok": True,
            "state": "available",
            "entry_peak_at": "2026-07-14T09:00:00Z",
            "does_not_update_physical_x": True,
        }

    monkeypatch.setattr(preview, "build_temperature_trajectory_feature_state", fake_trajectory)
    monkeypatch.setattr(preview, "build_peak_timing_state", fake_peak_timing)
    result = preview._preview_peak_timing_state(
        events=[{"canonical_observation_event_id": "event-1"}],
        replay={"updates": [{"strong_observation_update_id": "update-1"}]},
        initial_lineage={
            "forecast_peak_timing_prior": {
                "schema": "forecast_peak_timing_prior_v1",
                "forecast_evidence_snapshot_id": "forecast-1",
            }
        },
        decision_as_of="2026-07-14T08:31:00Z",
    )

    assert result["state"] == "available"
    assert result["does_not_update_physical_x"] is True
    assert captured["trajectory_args"]["temporal_role"] == "post_event"
    assert captured["trajectory_args"]["temperature_quantization_c"] == 0.25
    assert captured["peak_args"]["canonical_observation_event"]["canonical_observation_event_id"] == "event-1"
    assert captured["peak_args"]["strong_observation_update"]["strong_observation_update_id"] == "update-1"
    assert captured["peak_args"]["forecast_peak_timing_prior"]["forecast_evidence_snapshot_id"] == "forecast-1"


def test_preview_peak_timing_failure_is_na_and_does_not_block_physical_x(monkeypatch):
    monkeypatch.setattr(
        preview,
        "build_temperature_trajectory_feature_state",
        lambda **_kwargs: {"ok": False, "temporal_role": "post_event"},
    )
    monkeypatch.setattr(
        preview,
        "build_peak_timing_state",
        lambda **_kwargs: {"ok": False, "block_reasons": ["trajectory_invalid"]},
    )

    result = preview._preview_peak_timing_state(
        events=[{"canonical_observation_event_id": "event-1"}],
        replay={
            "updates": [
                {
                    "strong_observation_update_id": "update-1",
                    "forecast_evidence_snapshot_id": "forecast-1",
                }
            ]
        },
        initial_lineage={},
        decision_as_of="2026-07-14T08:31:00Z",
    )

    assert result["ok"] is True
    assert result["state"] == "na"
    assert result["timing_failure_does_not_block_probability"] is True
    assert result["source_block_reasons"] == ["trajectory_invalid"]

