from __future__ import annotations

from datetime import date

from scripts.external_forecast_coverage_manifest import build_external_forecast_coverage_manifest


def test_coverage_manifest_is_external_only_and_bounded_without_confirmation_rule():
    discovery = {
        "events": [
            {"event_date_local": "2026-06-20"},
            {"event_date_local": "2026-07-10"},
        ]
    }
    report = build_external_forecast_coverage_manifest(
        discovery,
        as_of_date=date(2026, 7, 11),
        twc_api_key_available=False,
    )

    assert report["source_data_policy"] == "external_only"
    assert report["market_event_day_count"] == 2
    assert report["date_chunk_count"] == 2
    assert report["estimated_impact"]["previous_runs_request_count"] == 6
    assert report["estimated_impact"]["single_runs_exact_request_count"] == 6
    assert report["causal_contract"]["previous_runs_exact_cutoff_eligible"] is False
    assert report["causal_contract"]["single_runs_exact_cutoff_required"] is True
    assert all(row["eligible_for_exact_cutoff_pairing"] is False for row in report["previous_runs_requests"])
    assert all(row["eligible_for_exact_cutoff_pairing"] is True for row in report["single_runs_exact_requests"])
    assert report["confirmation"]["historical_download_requires_confirmation"] is False
    assert report["confirmation"]["bounded_fetch_flags_required"] is True
    assert report["source_coverage"]["taf"]["official_window_event_days"] == 1
    assert report["source_coverage"]["taf"]["older_event_days_forward_only"] == 1
    assert report["source_coverage"]["twc_forecast"]["state"] == "blocked_missing_api_credential"
    assert not any(report["guards"].values())


def test_exact_triplet_coverage_respects_single_run_archive_start_on_run_date():
    report = build_external_forecast_coverage_manifest(
        {
            "events": [
                {"event_date_local": "2026-04-02"},
                {"event_date_local": "2026-04-03"},
            ]
        },
        as_of_date=date(2026, 7, 11),
    )
    by_day = {}
    for row in report["single_runs_exact_requests"]:
        by_day.setdefault(row["target_event_date_local"], set()).add(row["source_id"])
    assert by_day["2026-04-02"] == {"ecmwf_model_peak"}
    assert by_day["2026-04-03"] == {"ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"}
    assert report["source_coverage"]["single_runs_replay"]["exact_triplet_event_day_count"] == 1
