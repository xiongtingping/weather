from scripts.evaluate_external_forecast_source_skill import evaluate_external_source_skill


def test_source_skill_report_keeps_hit_rate_diagnostic_only():
    rows = [
        {
            "state": "available",
            "event_date_local": "2026-07-01",
            "source_id": "gfs",
            "provider_family": "nwp_gfs",
            "lead_hours": 12,
            "context": {"segments": ["humid"]},
            "nll": 0.5,
            "rps": 0.1,
            "brier_multiclass": 0.2,
            "top1_hit": True,
            "top3_contains": True,
            "interval_mae": 0.0,
            "bias_interval": 0.0,
            "lineage_quality": "immutable_envelope",
        }
    ]
    report = evaluate_external_source_skill(rows)
    assert report["by_source"]["gfs"]["nll"] == 0.5
    assert report["by_weather_context"]["humid"]["sample_count"] == 1
    assert report["metric_policy"]["promotion_primary"] == ["nll", "rps"]
    assert report["metric_policy"]["hit_rate_diagnostic_only"] is True
    assert report["provider_error_correlations"] == []
