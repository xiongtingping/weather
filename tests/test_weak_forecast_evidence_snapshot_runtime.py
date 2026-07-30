import hashlib
import json

from weak_forecast_evidence_snapshot_runtime import build_weak_forecast_evidence_snapshot_row


def test_snapshot_freezes_exact_weak_surface() -> None:
    likelihood = [
        {"temperature_c": 33.0, "likelihood": 0.4},
        {"temperature_c": 34.0, "likelihood": 0.6},
    ]
    sha = hashlib.sha256(
        json.dumps([[33.0, 0.4], [34.0, 0.6]], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row = build_weak_forecast_evidence_snapshot_row(
        {
            "ok": True,
            "forecast_evidence": {
                "source_revisions": [
                    {"source_id": "taf", "source_revision_id": "tafrev-1"}
                ]
            },
            "joint_weak_likelihood": {
                "ok": True,
                "forecast_evidence_snapshot_id": "forev-1",
                "artifact_sha256": "a" * 64,
                "joint_forecast_weak_likelihood_sha256": sha,
                "joint_forecast_weak_likelihood": likelihood,
                "weak_source_set": {"source_set_key": "taf"},
                "forecast_peak_timing_prior": {"state": "na"},
                "forecast_background_context": {
                    "schema": "forecast_background_context_v2",
                    "transition_control_active": False,
                },
                "source_expert_trace": [
                    {
                        "source_id": "taf",
                        "forecast_peak_c": 35.0,
                        "calibration_bias_c": 0.0,
                        "calibrated_mean_c": 35.0,
                        "information_weight": 0.35,
                        "source_modes_schema": "forecast_source_modes_v1",
                        "source_mode_count": 1,
                        "source_modes": [{
                            "mode_id": "taf_tx_primary",
                            "mode_probability": 1.0,
                            "forecast_peak_c": 35.0,
                            "calibrated_mean_c": 35.0,
                        }],
                    }
                ],
                "forecast_multimodality_trace": {
                    "schema": "forecast_multimodality_trace_v1",
                    "mode_count": 1,
                    "multimodality_state": "unimodal",
                },
                "formula": "correlation_aware_tempered_mixture_of_context_calibrated_forecast_experts",
                "context_key": "season=summer|weather_regime=clear_dry|lead_bucket=d1",
                "source_weight_attribution": {
                    "schema": "weak_source_weight_attribution_v1",
                    "frontend_recompute_allowed": False,
                    "source_rows": [{"source_id": "taf", "final_information_index": 0.35}],
                    "bucket_rows": [
                        {
                            "reported_x_c": 35,
                            "local_responsibility_by_source": {"taf": 1.0},
                        }
                    ],
                },
            },
        },
        target_identity="physical_weather_day_v1:ZUUU:2026-07-15",
        event_date="2026-07-15",
        decision_as_of="2026-07-15T00:30:01Z",
    )
    assert json.loads(row["likelihood_by_x_json"]) == {"33": 0.4, "34": 0.6}
    assert json.loads(row["source_revision_ids_json"]) == ["tafrev-1"]
    assert json.loads(row["forecast_background_context_json"])["transition_control_active"] is False
    assert json.loads(row["snapshot_json"])["forecast_background_context"]["schema"] == "forecast_background_context_v2"
    expert = json.loads(row["source_expert_trace_json"])[0]
    assert expert["calibrated_mean_c"] == 35.0
    assert expert["source_modes"][0]["mode_id"] == "taf_tx_primary"
    assert json.loads(row["forecast_multimodality_trace_json"])["mode_count"] == 1
    assert json.loads(row["weak_model_diagnostics_json"])["context_key"].endswith("lead_bucket=d1")
    attribution = json.loads(row["weak_model_diagnostics_json"])["source_weight_attribution"]
    assert attribution["frontend_recompute_allowed"] is False
    assert attribution["bucket_rows"][0]["local_responsibility_by_source"] == {"taf": 1.0}
    assert row["state"] == "active"
