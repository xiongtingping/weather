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
                    "schema": "forecast_background_context_v1",
                    "transition_control_active": False,
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
    assert json.loads(row["snapshot_json"])["forecast_background_context"]["schema"] == "forecast_background_context_v1"
    assert row["state"] == "active"
