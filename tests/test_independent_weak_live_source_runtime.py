import gzip
import json

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from independent_weak_live_source_runtime import _live_wu_auxiliary_observation


def test_live_wu_auxiliary_enters_weak_model_without_tier_a_promotion(tmp_path) -> None:
    body = {
        "schema": "forecast_source_forward_metrics_v1",
        "source_id": "wu_forecast_peak",
        "station_id": "ZUUU",
        "target_event_date_local": "2026-07-18",
        "forecast_peak_c": 33.3,
        "hourly_curve": [{"valid_time_local": "2026-07-18T14:00:00+0800", "temperature_c": 32.8}],
        "metrics": {"peak_at": "2026-07-18T14:00:00+0800"},
    }
    envelope = build_external_forecast_raw_envelope(
        source_id="wu_forecast_peak",
        provider_family="twc_provider",
        response_body_utf8=json.dumps(body),
        requested_at="2026-07-16T16:00:00Z",
        response_started_at="2026-07-16T16:00:00Z",
        response_completed_at="2026-07-16T16:00:01Z",
        provider_available_at="2026-07-16T16:00:01Z",
        http_status=200,
        parser_version="test",
        request_url="https://www.wunderground.com/hourly/cn/chengdu/ZUUU",
        request_parameters={"target_event_date_local": "2026-07-18"},
        source_event_id="forward_wu_forecast_peak_2026-07-18",
        availability_mode="live_fetch",
        provider_available_at_quality="observed_response_completion",
        capture_time_provenance="batch_clock_proxy",
        response_headers_observed=False,
    )
    snapshot = tmp_path / "wu.json.gz"
    with gzip.open(snapshot, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle)
    pointer = tmp_path / "_latest_derived" / "wu_forecast_peak" / "2026-07-18.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({"snapshot_path": str(snapshot)}), encoding="utf-8")

    row = _live_wu_auxiliary_observation(root=tmp_path, event_date="2026-07-18", station_id="ZUUU")

    assert row["source_id"] == "wu"
    assert row["forecast_peak_c"] == 33.3
    assert row["payload"]["strict_asof_training_eligible"] is False
    assert row["payload"]["live_weak_model_eligible"] is True
