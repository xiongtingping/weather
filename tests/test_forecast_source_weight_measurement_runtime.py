from forecast_source_weight_measurement_runtime import build_source_weight_measurement


def _rows(days, *, challenger_nll=0.8, challenger_rps=0.08):
    return [
        {
            "event_date_local": f"2026-01-{day:02d}",
            "champion": {"nll": 1.0, "rps": 0.1},
            "challenger": {"nll": challenger_nll, "rps": challenger_rps},
        }
        for day in range(1, days + 1)
    ]


def test_measurement_registers_7_30_90_and_recommends_keep_when_conclusive():
    report = build_source_weight_measurement(
        _rows(25),
        applied_at="2026-01-01T00:00:00Z",
        as_of_date="2026-01-25",
        minimum_conclusive_event_days=5,
    )
    assert list(report["windows"]) == ["7d", "30d", "90d"]
    assert report["windows"]["7d"]["recommendation"] == "keep"
    assert report["recommendation"] == "keep"
    assert report["rollback_requires_retraining"] is False


def test_measurement_recommends_rollback_on_conclusive_nll_rps_regression():
    report = build_source_weight_measurement(
        _rows(10, challenger_nll=1.2, challenger_rps=0.12),
        applied_at="2026-01-01",
        as_of_date="2026-01-10",
        minimum_conclusive_event_days=5,
    )
    assert report["recommendation"] == "rollback"
    assert report["rollback_reasons"]
    assert report["guards"]["performs_rollback"] is False
