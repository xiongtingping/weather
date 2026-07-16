import json

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from scripts.normalize_open_meteo_source_skill_runs import normalize_exact_run_envelope


def _envelope(*, available="2026-07-10T12:10:00Z", eligible=True):
    payload = {
        "hourly": {
            "time": ["2026-07-10T23:00", "2026-07-11T00:00", "2026-07-11T01:00", "2026-07-12T00:00"],
            "temperature_2m": [25.0, 26.0, 30.0, 27.0],
        }
    }
    return build_external_forecast_raw_envelope(
        source_id="gfs_model_peak",
        provider_family="nwp_gfs",
        response_body_utf8=json.dumps(payload),
        requested_at="2026-07-12T00:00:00Z",
        response_started_at="2026-07-12T00:00:01Z",
        response_completed_at="2026-07-12T00:00:02Z",
        provider_available_at=available,
        http_status=200,
        parser_version="test",
        request_url="https://single-runs-api.open-meteo.com/v1/forecast",
        request_parameters={
            "target_event_date_local": "2026-07-11",
            "cutoff_id": "event_day_start_local",
            "cutoff_at": "2026-07-10T16:00:00Z",
            "run": "2026-07-10T06:00",
            "run_initialized_at": "2026-07-10T06:00:00Z",
            "availability_quality": "conservative",
            "eligible_for_exact_cutoff_pairing": eligible,
        },
        availability_mode="historical_archive_replay",
        provider_available_at_quality="conservative_documented_upper_bound",
    )


def test_exact_run_normalizer_extracts_only_target_local_day_peak():
    result = normalize_exact_run_envelope(_envelope())
    assert result["state"] == "available"
    row = result["row"]
    assert row["forecast_peak_c"] == 30.0
    assert len(row["hourly_curve"]) == 2
    assert row["lineage_quality"] == "immutable_envelope"
    assert row["as_of_eligible_at"] <= row["cutoff_at"]


def test_nonexact_previous_run_and_future_available_run_are_rejected():
    assert normalize_exact_run_envelope(_envelope(eligible=False))["state_reason"] == "request_not_exact_cutoff_eligible"
    assert normalize_exact_run_envelope(_envelope(available="2026-07-10T18:00:00Z"))["state_reason"] == "run_not_available_at_cutoff"
