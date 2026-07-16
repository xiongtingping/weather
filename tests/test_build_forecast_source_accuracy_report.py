from scripts.build_forecast_source_accuracy_report import build_forecast_source_accuracy_report


def _row(*, source: str, date: str, lead: float, bias: float, mae: float) -> dict:
    return {
        "state": "available",
        "source_id": source,
        "provider_family": "nwp_test",
        "event_date_local": date,
        "lead_hours": lead,
        "bias_interval": bias,
        "interval_mae": mae,
        "nll": 1.0,
        "rps": 0.1,
        "top1_hit": True,
        "top3_contains": True,
        "lineage_quality": "immutable_envelope",
    }


def test_groups_d1_d0_and_season_with_signed_bias():
    report = build_forecast_source_accuracy_report(
        {
            "source_data_policy": "external_only",
            "score_rows": [
                _row(source="ecmwf", date="2026-04-03", lead=24, bias=-1.0, mae=1.0),
                _row(source="ecmwf", date="2026-04-03", lead=3, bias=2.0, mae=2.0),
            ],
        }
    )

    assert report["research_only"] is True
    rows = {(row["horizon"], row["season"]): row for row in report["rows"]}
    assert rows[("D1", "春")]["mean_bias_c"] == -1.0
    assert rows[("D0", "春")]["mean_bias_c"] == 2.0
    assert rows[("D0", "春")]["event_day_count"] == 1
    assert rows[("D0", "春")]["ranking_state"] == "insufficient_independent_event_days"


def test_rejects_invalid_lead_from_grouped_rows():
    report = build_forecast_source_accuracy_report(
        {"score_rows": [_row(source="ecmwf", date="2026-06-01", lead=-1, bias=0.0, mae=0.0)]}
    )

    assert report["rows"] == []
    assert report["coverage"]["excluded_counts"] == {"lead_hours_missing_or_invalid": 1}
