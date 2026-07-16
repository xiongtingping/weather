from scripts.audit_open_meteo_realized_coverage import build_realized_coverage_audit


def test_provider_hole_is_explicit_na_and_not_model_error():
    requests = [
        {"url": "https://example/ok", "source_id": "ecmwf_model_peak", "target_event_date_local": "2026-07-01", "cutoff_id": "event_day_start_local"},
        {"url": "https://example/missing", "source_id": "gfs_model_peak", "target_event_date_local": "2026-07-01", "cutoff_id": "event_day_start_local", "run": "2026-06-30T06:00"},
    ]
    report = build_realized_coverage_audit(
        coverage={"single_runs_exact_requests": requests},
        fetch_report={
            "authorized": True,
            "results": [
                {"url": "https://example/ok", "write": {"ok": True}},
                {"url": "https://example/missing", "attempts": [{"error": "HTTP Error 400: Bad Request"}]},
            ],
        },
        forecast_snapshots=[
            {"state": "available", "source_id": "ecmwf_model_peak", "target_event_date_local": "2026-07-01", "cutoff_id": "event_day_start_local"}
        ],
    )

    assert report["request_accounting_complete"] is True
    assert report["explicit_unavailable_count"] == 1
    assert report["explicit_unavailable"][0]["state_reason"] == "provider_run_unavailable_http_400"
    assert report["availability_policy"]["provider_unavailable_never_receives_zero_score"] is True
