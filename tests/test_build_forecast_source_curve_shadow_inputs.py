from scripts.build_forecast_source_curve_shadow_inputs import build_curve_shadow_inputs


def test_every_external_observation_becomes_a_causal_checkpoint():
    report = build_curve_shadow_inputs(
        market_events=[{"event_id": "event-1", "event_date_local": "2026-07-10", "resolved_at": "2026-07-10T12:00:00Z"}],
        forecast_snapshots=[
            {
                "state": "available",
                "source_id": "gfs_model_peak",
                "target_event_date_local": "2026-07-10",
                "as_of_eligible_at": "2026-07-09T12:10:00Z",
                "forecast_snapshot_id": "sha-1",
                "hourly_curve": [
                    {"valid_time_local": "2026-07-10T09:00", "temperature_c": 30.0},
                    {"valid_time_local": "2026-07-10T10:00", "temperature_c": 31.0},
                ],
            }
        ],
        observations=[
            {"valid_time": "2026-07-10T01:00:00Z", "first_seen_at": "2026-07-10T01:02:00Z", "arrival_time_quality": "observed_first_seen", "temperature_c": 30.2},
            {"valid_time": "2026-07-10T02:00:00Z", "first_seen_at": "2026-07-10T02:03:00Z", "arrival_time_quality": "observed_first_seen", "temperature_c": 31.1},
        ],
    )

    assert report["curve_input_count"] == 2
    assert len(report["curve_inputs"][0]["observations"]) == 1
    assert len(report["curve_inputs"][1]["observations"]) == 2
    assert report["curve_inputs"][0]["curve_cutoff_at"] == "2026-07-10T01:02:00Z"
    assert report["guards"]["hour_specific_thresholds"] is False


def test_observation_first_seen_after_settlement_is_not_oos_evidence():
    report = build_curve_shadow_inputs(
        market_events=[{"event_id": "event-1", "event_date_local": "2026-07-10", "resolved_at": "2026-07-10T12:00:00Z"}],
        forecast_snapshots=[],
        observations=[
            {
                "valid_time": "2026-07-10T01:00:00Z",
                "first_seen_at": "2026-07-11T01:00:00Z",
                "arrival_time_quality": "observed_first_seen",
                "temperature_c": 30.0,
            }
        ],
    )

    assert report["curve_input_count"] == 0
    assert report["excluded_counts"]["observation_available_after_market_settlement"] == 1


def test_curve_checkpoint_uses_latest_causal_forecast_availability_cutoff():
    report = build_curve_shadow_inputs(
        market_events=[{"event_id": "event-1", "event_date_local": "2026-07-10", "resolved_at": "2026-07-10T12:00:00Z"}],
        forecast_snapshots=[
            {
                "state": "available",
                "source_id": "gfs_model_peak",
                "target_event_date_local": "2026-07-10",
                "as_of_eligible_at": "2026-07-09T12:00:00Z",
                "forecast_snapshot_id": "sha",
                "hourly_curve": [{"valid_time_local": "2026-07-10T09:00", "temperature_c": 30.0}],
            }
        ],
        observations=[
            {"valid_time": "2026-07-10T01:00:00Z", "first_seen_at": "2026-07-10T01:04:00Z", "arrival_time_quality": "observed_first_seen", "temperature_c": 30.0}
        ],
        cutoffs=[
            {"event_id": "event-1", "cutoff_id": "event_day_start_local", "cutoff_at": "2026-07-09T16:00:00Z"},
            {"event_id": "event-1", "cutoff_id": "forecast_availability_new", "cutoff_at": "2026-07-10T01:03:00Z"},
        ],
    )

    assert report["curve_inputs"][0]["cutoff_id"] == "forecast_availability_new"
