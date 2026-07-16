from __future__ import annotations

from scripts.build_external_forecast_source_skill_dataset import (
    build_cutoff_weather_context,
    build_external_forecast_source_skill_dataset,
    select_strict_as_of_source_forecasts,
)


def _market() -> dict:
    return {
        "event_id": "event-1",
        "event_date_local": "2026-07-10",
        "station_id": "ZUUU",
        "temperature_unit": "C",
        "winning_bucket_label": "36°C",
        "bucket_schema": [
            {"label": "35°C or below", "lower": None, "upper": 35.5, "lower_inclusive": False, "upper_inclusive": False, "unit": "C"},
            {"label": "36°C", "lower": 35.5, "upper": 36.5, "lower_inclusive": True, "upper_inclusive": False, "unit": "C"},
            {"label": "37°C or higher", "lower": 36.5, "upper": None, "lower_inclusive": True, "upper_inclusive": False, "unit": "C"},
        ],
    }


def _row(snapshot_id: str, *, available: str, valid: str, temperature: float) -> dict:
    return {
        "forecast_snapshot_id": snapshot_id,
        "source_event_id": f"event-{snapshot_id}",
        "source_id": "gfs_model_peak",
        "provider_family": "nwp_gfs",
        "source_class": "forecast_weak_source",
        "station_id": "ZUUU",
        "target_event_date_local": "2026-07-10",
        "as_of_eligible_at": available,
        "issued_at": "2026-07-09T00:00:00Z",
        "valid_time_utc": valid,
        "temperature_c": temperature,
        "temperature_unit": "C",
        "lineage_quality": "immutable_envelope",
        "residual_sigma": 0.8,
    }


def test_pairing_selects_latest_past_snapshot_and_excludes_future_snapshot():
    snapshots = [
        _row("past", available="2026-07-09T12:00:00Z", valid="2026-07-10T00:00:00Z", temperature=31.0),
        _row("past", available="2026-07-09T12:00:00Z", valid="2026-07-10T06:00:00Z", temperature=36.0),
        _row("future", available="2026-07-09T18:00:00Z", valid="2026-07-10T06:00:00Z", temperature=38.0),
    ]
    selected, trace = select_strict_as_of_source_forecasts(
        market_event=_market(),
        cutoff_at="2026-07-09T16:00:00Z",
        forecast_snapshots=snapshots,
    )

    assert len(selected) == 1
    assert selected[0]["forecast_snapshot_id"] == "past"
    assert selected[0]["forecast_peak_c"] == 36.0
    assert trace["excluded_counts"] == {"forecast_future_at_cutoff": 1}
    assert trace["used_sources"]["gfs_model_peak"]["lineage_quality"] == "immutable_envelope"


def test_dataset_builds_one_score_per_event_cutoff_source_without_local_history():
    snapshots = [
        _row("past", available="2026-07-09T12:00:00Z", valid="2026-07-10T00:00:00Z", temperature=31.0),
        _row("past", available="2026-07-09T12:00:00Z", valid="2026-07-10T06:00:00Z", temperature=36.0),
    ]
    report = build_external_forecast_source_skill_dataset(
        market_events=[_market()],
        forecast_snapshots=snapshots,
        cutoffs=[{"event_id": "event-1", "cutoff_id": "d0", "cutoff_at": "2026-07-09T16:00:00Z"}],
    )

    assert report["available_score_row_count"] == 1
    assert report["paired_event_count"] == 1
    assert report["score_rows"][0]["source_id"] == "gfs_model_peak"
    assert report["score_rows"][0]["top1_hit"] is True
    assert not any(report["guards"].values())


def test_exact_run_snapshot_cannot_silently_cross_a_different_named_cutoff():
    snapshot = _row(
        "past",
        available="2026-07-09T12:00:00Z",
        valid="2026-07-10T06:00:00Z",
        temperature=36.0,
    )
    snapshot["cutoff_id"] = "event_day_start_local"
    selected, trace = select_strict_as_of_source_forecasts(
        market_event=_market(),
        cutoff_at="2026-07-09T16:00:00Z",
        cutoff_id="midday_update",
        forecast_snapshots=[snapshot],
    )
    assert selected == []
    assert trace["excluded_counts"] == {"forecast_cutoff_id_mismatch": 1}


def test_declared_dynamic_cutoff_can_inherit_a_causally_prior_named_snapshot():
    snapshot = _row(
        "past",
        available="2026-07-09T12:00:00Z",
        valid="2026-07-10T06:00:00Z",
        temperature=36.0,
    )
    snapshot["cutoff_id"] = "event_day_start_local"
    snapshot["cutoff_at"] = "2026-07-09T16:00:00Z"
    selected, trace = select_strict_as_of_source_forecasts(
        market_event=_market(),
        cutoff_at="2026-07-10T01:03:00Z",
        cutoff_id="forecast_availability_abc",
        forecast_snapshots=[snapshot],
        allow_prior_named_cutoff=True,
    )

    assert len(selected) == 1
    assert trace["excluded_counts"] == {}


def test_weather_context_is_smooth_and_derived_only_from_selected_forecasts():
    selected = [
        {
            "forecast_peak_c": 36.0,
            "source_rows": [{"hourly_curve": [{"temperature_c": 30.0}, {"temperature_c": 36.0}, {"temperature_c": 34.0}]}],
        },
        {
            "forecast_peak_c": 38.0,
            "source_rows": [{"hourly_curve": [{"temperature_c": 31.0}, {"temperature_c": 38.0}, {"temperature_c": 35.0}]}],
        },
    ]

    context = build_cutoff_weather_context(selected)

    assert context["forecast_consensus_peak_c"] == 37.0
    assert context["forecast_peak_spread_c"] == 1.0
    assert context["forecast_consensus_diurnal_range_c"] == 6.5
    assert context["forecast_consensus_warming_from_day_start_c"] == 6.5
