from __future__ import annotations

from official_nwp_daily_peak_revision_runtime import build_official_nwp_daily_peak_revision
from joint_forecast_weak_likelihood_training_runtime import build_joint_weak_training_dataset


def _snapshot(hour: int, temperature_c: float, *, run: str = "2026-07-01T00:00:00Z", tier_a: bool = True, payload_hash: str = "", physical: dict | None = None) -> dict:
    valid = f"2026-07-01T{hour:02d}:00:00Z"
    return {"schema": "official_nwp_normalized_snapshot_v1", "provider_id": "gfs", "run_at_utc": run, "valid_time_utc": valid, "requested_at": f"2026-07-01T00:{hour:02d}:00Z", "response_started_at": f"2026-07-01T00:{hour:02d}:01Z" if tier_a else "", "response_headers_observed": tier_a, "response_completed_at": f"2026-07-01T00:{hour:02d}:02Z", "raw_object_ref": f"/immutable/raw-{hour}", "raw_sha256": f"raw-{hour}", "raw_payload_sha256s": [payload_hash or f"payload-{hour}"], "snapshot_sha256": f"snapshot-{hour}", "normalized_features": {"temperature_2m_c": temperature_c, **(physical or {})}}


def test_complete_same_run_curve_becomes_daily_peak_revision() -> None:
    points = [_snapshot(4, 28.0), _snapshot(8, 31.5), _snapshot(12, 30.0)]
    revision = build_official_nwp_daily_peak_revision(provider_id="gfs", point_snapshots=points, target_event_date="2026-07-01", target_timezone="Asia/Shanghai", expected_valid_times_utc=[point["valid_time_utc"] for point in points])

    assert revision["ok"] is True
    assert revision["forecast_peak_c"] == 31.5
    assert revision["source_modes_schema"] == "forecast_source_modes_v1"
    assert revision["source_modes"][0]["mode_id"] == "deterministic_official_curve"
    assert revision["source_modes"][0]["mode_probability"] == 1.0
    assert revision["source_modes"][0]["hourly_curve"] == revision["hourly_curve"]
    assert revision["forecast_peak_valid_time"] == "2026-07-01T08:00:00Z"
    assert revision["raw_sha256"]
    assert revision["strict_asof_tier_a"] is True
    assert revision["boundary"]["interpolation_or_backfill_used"] is False
    assert revision["basin_regime"] == "unknown"
    assert revision["boundary"]["temperature_curve_weather_type_inference_forbidden"] is True
    assert revision["forecast_weather_regime"] == "unknown_forecast_regime"

    dataset = build_joint_weak_training_dataset(
        events=[{
            "event_id": "curve-training-1",
            "event_date": "2026-07-01",
            "decision_as_of": "2026-07-01T00:12:03Z",
            "forecast_context": {"season": "summer", "weather_regime": "clear", "lead_bucket": "0_6h"},
            "physical_x_final": {"label_kind": "physical_x_final", "final": True, "temperature_c": 31.0, "raw_sha256": "physical-x-1"},
            "source_revisions": [revision],
        }],
        source_catalog={"gfs": {"provider_family": "nwp"}},
    )
    assert dataset["state"] == "available"
    assert dataset["samples"][0]["source_set_key"] == "gfs"


def test_missing_point_or_mixed_run_fails_closed_and_missing_header_is_tier_c() -> None:
    points = [_snapshot(4, 28.0), _snapshot(8, 31.5, tier_a=False)]
    expected = ["2026-07-01T04:00:00Z", "2026-07-01T08:00:00Z", "2026-07-01T12:00:00Z"]
    incomplete = build_official_nwp_daily_peak_revision(provider_id="gfs", point_snapshots=points, target_event_date="2026-07-01", target_timezone="Asia/Shanghai", expected_valid_times_utc=expected)
    assert incomplete["ok"] is False
    assert "official_nwp_daily_curve_expected_valid_times_missing" in incomplete["block_reasons"]

    points.append(_snapshot(12, 30.0))
    tier_c = build_official_nwp_daily_peak_revision(provider_id="gfs", point_snapshots=points, target_event_date="2026-07-01", target_timezone="Asia/Shanghai", expected_valid_times_utc=expected)
    assert tier_c["ok"] is True
    assert tier_c["strict_asof_tier_a"] is False

    mixed = [
        _snapshot(4, 28.0),
        _snapshot(8, 31.5),
        _snapshot(12, 30.0, run="2026-07-01T06:00:00Z"),
    ]
    mixed_result = build_official_nwp_daily_peak_revision(provider_id="gfs", point_snapshots=mixed, target_event_date="2026-07-01", target_timezone="Asia/Shanghai", expected_valid_times_utc=expected)
    assert mixed_result["ok"] is False
    assert mixed_result["block_reasons"] == ["official_nwp_daily_curve_mixed_model_runs"]


def test_conflicting_duplicate_point_fails_closed_but_identical_provider_retry_keeps_earliest_arrival() -> None:
    points = [_snapshot(4, 28.0), _snapshot(8, 31.5), _snapshot(12, 30.0)]
    conflict = {**_snapshot(8, 32.0, payload_hash="changed"), "snapshot_sha256": "different-snapshot"}
    blocked = build_official_nwp_daily_peak_revision(provider_id="gfs", point_snapshots=[*points, conflict], target_event_date="2026-07-01", target_timezone="Asia/Shanghai", expected_valid_times_utc=[point["valid_time_utc"] for point in points])
    assert blocked["ok"] is False
    assert "official_nwp_point_duplicate_valid_time_conflict" in blocked["block_reasons"]

    retry = {**_snapshot(8, 31.5), "snapshot_sha256": "retry-snapshot", "response_completed_at": "2026-07-01T00:59:00Z"}
    accepted = build_official_nwp_daily_peak_revision(provider_id="gfs", point_snapshots=[*points, retry], target_event_date="2026-07-01", target_timezone="Asia/Shanghai", expected_valid_times_utc=[point["valid_time_utc"] for point in points])
    assert accepted["ok"] is True
    assert accepted["available_at"] == "2026-07-01T00:12:02Z"


def test_decoder_schema_enrichment_can_supersede_an_identical_field_subset() -> None:
    old = _snapshot(4, 28.0)
    enriched = {
        **_snapshot(4, 28.0, physical={"total_cloud_cover_pct": 70.0}),
        "snapshot_sha256": "enriched-snapshot",
        "response_completed_at": "2026-07-01T01:04:02Z",
    }
    result = build_official_nwp_daily_peak_revision(
        provider_id="gfs",
        point_snapshots=[old, enriched],
        target_event_date="2026-07-01",
        target_timezone="Asia/Shanghai",
        expected_valid_times_utc=[old["valid_time_utc"]],
    )
    assert result["ok"] is True
    assert result["available_at"] == "2026-07-01T01:04:02Z"
    assert result["physical_regime"]["direct_cloud_point_count"] == 1


def test_later_tier_a_transport_proof_supersedes_identical_tier_c_point() -> None:
    tier_c = _snapshot(4, 28.0, tier_a=False)
    tier_a = {
        **_snapshot(4, 28.0, tier_a=True),
        "snapshot_sha256": "tier-a-proof-snapshot",
        "response_completed_at": "2026-07-01T02:04:02Z",
    }
    result = build_official_nwp_daily_peak_revision(
        provider_id="gfs",
        point_snapshots=[tier_c, tier_a],
        target_event_date="2026-07-01",
        target_timezone="Asia/Shanghai",
        expected_valid_times_utc=[tier_a["valid_time_utc"]],
    )
    assert result["strict_asof_tier_a"] is True
    assert result["available_at"] == "2026-07-01T02:04:02Z"


def test_transport_timestamps_out_of_order_cannot_be_tier_a() -> None:
    point = {
        **_snapshot(4, 28.0, tier_a=True),
        "requested_at": "2026-07-01T00:04:02Z",
        "response_started_at": "2026-07-01T00:04:01Z",
        "response_completed_at": "2026-07-01T00:04:03Z",
    }
    result = build_official_nwp_daily_peak_revision(
        provider_id="gfs",
        point_snapshots=[point],
        target_event_date="2026-07-01",
        target_timezone="UTC",
        expected_valid_times_utc=[point["valid_time_utc"]],
    )
    assert result["ok"] is True
    assert result["strict_asof_tier_a"] is False
    assert result["tier_a_na_reason"] == "one_or_more_point_snapshot_header_arrival_evidence_missing"


def test_complete_curve_carries_direct_physical_regime_without_temperature_guessing() -> None:
    humid = {
        "relative_humidity_850_pct": 82.0,
        "relative_humidity_700_pct": 88.0,
        "u_wind_850_mps": 1.0,
        "v_wind_850_mps": 1.0,
        "u_wind_700_mps": 2.0,
        "v_wind_700_mps": 1.0,
    }
    points = [
        _snapshot(4, 28.0, physical=humid),
        _snapshot(8, 31.5, physical=humid),
        _snapshot(12, 30.0, physical=humid),
    ]

    revision = build_official_nwp_daily_peak_revision(
        provider_id="gfs",
        point_snapshots=points,
        target_event_date="2026-07-01",
        target_timezone="Asia/Shanghai",
        expected_valid_times_utc=[point["valid_time_utc"] for point in points],
    )

    assert revision["basin_regime"] == "moist_column_cloud_favorable"
    assert revision["physical_regime"]["known_point_share"] == 1.0
    assert all(point["physical_regime"]["boundary"]["temperature_fields_consumed"] is False for point in revision["hourly_curve"])
    assert all(point["physical_regime"]["precipitation_regime"].startswith("unknown_") for point in revision["hourly_curve"])
