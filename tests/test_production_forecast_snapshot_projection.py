import json

from production_forecast_snapshot_projection import project_forecast_snapshot_fields


def test_projection_copies_persisted_model_taf_peaks_without_recomputing():
    row = {
        "forecast_evidence_snapshot_id": "forev-test",
        "source_set_key": "ecmwf+icon+gfs+taf",
        "source_revision_ids_json": json.dumps(["gfs-1", "icon-1", "ecmwf-1", "taf-1"]),
        "source_revisions_json": json.dumps(
            [
                {"source_id": "gfs", "source_revision_id": "gfs-1", "available_at": "2026-07-19T01:00:00Z", "forecast_peak_c": 32.5, "forecast_peak_valid_time": "2026-07-20T08:00:00Z"},
                {"source_id": "icon", "source_revision_id": "icon-1", "available_at": "2026-07-19T02:00:00Z", "forecast_peak_c": 36.2, "forecast_peak_valid_time": "2026-07-20T08:00:00Z"},
                {"source_id": "ecmwf", "source_revision_id": "ecmwf-1", "available_at": "2026-07-19T03:00:00Z", "forecast_peak_c": 37.8, "forecast_peak_valid_time": "2026-07-20T09:00:00Z"},
                {"source_id": "taf", "source_revision_id": "taf-1", "available_at": "2026-07-19T04:00:00Z", "forecast_peak_c": 39.0, "forecast_peak_valid_time": "2026-07-20T07:00:00Z"},
            ]
        ),
        "likelihood_by_x_json": json.dumps({"35": 0.4, "36": 0.6}),
        "weak_likelihood_sha256": "weak-sha",
        "weak_likelihood_artifact_sha256": "artifact-sha",
        "created_at": "2026-07-19T04:00:01Z",
        "decision_as_of": "2026-07-19T04:00:01Z",
    }

    projected = project_forecast_snapshot_fields(row)

    assert projected["forecast_weak_likelihood"] == {"35°C": 0.4, "36°C": 0.6}
    assert projected["forecast_weak_attribution"]["source_revision_ids"] == ["gfs-1", "icon-1", "ecmwf-1", "taf-1"]
    features = projected["weather_likelihood_features_compact"]
    assert features["gfs_forecast_peak_c"] == 32.5
    assert features["icon_forecast_peak_c"] == 36.2
    assert features["ecmwf_forecast_peak_c"] == 37.8
    assert features["taf_forecast_peak_c"] == 39.0
    assert features["taf_forecast_peak_issued_at"] == "2026-07-19T04:00:00Z"
    assert features["weather_likelihood_refreshed_at"] == "2026-07-19T04:00:01Z"
