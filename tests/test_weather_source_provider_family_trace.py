from weather_source_acquisition_manager import (
    observation_from_source_payload,
    source_acquisition_catalog,
    source_latency_row,
)


def test_forecast_trace_exposes_provider_family_and_as_of_eligible_time():
    observation = observation_from_source_payload(
        {
            "source_id": "wu_forecast_peak",
            "valid_time": "2026-07-11T06:00:00Z",
            "fetched_at": "2026-07-11T00:00:03Z",
            "provider_available_at": "2026-07-11T00:00:01Z",
            "as_of_eligible_at": "2026-07-11T00:00:03Z",
            "forecast_peak_c": 31.0,
            "raw_ref": "research://sha",
            "forecast_snapshot_id": "sha-1",
            "lineage_quality": "immutable_envelope",
        }
    )
    row = source_latency_row(observation)
    assert row["provider_family"] == "twc_provider"
    assert row["as_of_eligible_at"] == "2026-07-11T00:00:03Z"
    assert observation.payload["forecast_snapshot_id"] == "sha-1"
    assert observation.payload["lineage_quality"] == "immutable_envelope"


def test_catalog_requires_provider_family_and_causal_availability_trace():
    catalog = source_acquisition_catalog()
    forecast = next(row for row in catalog["sources"] if row["source_class"] == "forecast_weak_source")
    assert "provider_family" in forecast["required_latency_fields"]
    assert "as_of_eligible_at" in forecast["required_latency_fields"]
