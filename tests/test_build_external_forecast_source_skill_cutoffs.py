from scripts.build_external_forecast_source_skill_cutoffs import build_event_day_start_cutoffs


def test_cutoff_is_local_event_day_start_without_outcome_features():
    report = build_event_day_start_cutoffs(
        [{"event_id": "event-1", "event_date_local": "2026-07-10", "settlement_actual": 36.0}]
    )

    cutoff = report["cutoffs"][0]
    assert cutoff["cutoff_at"] == "2026-07-09T16:00:00Z"
    assert cutoff["cutoff_id"] == "event_day_start_local"
    assert cutoff["context"] == {}
    assert report["guards"]["uses_settlement_outcome_as_feature"] is False


def test_forecast_first_availability_adds_smooth_intraday_cutoff_without_fixed_hours():
    report = build_event_day_start_cutoffs(
        [{"event_id": "event-1", "event_date_local": "2026-07-10", "resolved_at": "2026-07-10T12:00:00Z"}],
        forecast_snapshots=[
            {"target_event_date_local": "2026-07-10", "as_of_eligible_at": "2026-07-10T01:03:00Z"},
            {"target_event_date_local": "2026-07-10", "as_of_eligible_at": "2026-07-10T01:03:00Z"},
        ],
    )

    assert report["cutoff_count"] == 2
    intraday = report["cutoffs"][1]
    assert intraday["cutoff_at"] == "2026-07-10T01:03:00Z"
    assert intraday["cutoff_id"].startswith("forecast_availability_")
    assert intraday["lead_hours"] == 14.95
