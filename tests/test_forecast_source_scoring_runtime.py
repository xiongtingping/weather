from __future__ import annotations

import math

from forecast_source_scoring_runtime import score_forecast_source_event


def _market() -> dict:
    return {
        "event_id": "event-1",
        "event_date_local": "2026-07-10",
        "station_id": "ZUUU",
        "winning_bucket_label": "36°C",
        "bucket_schema": [
            {"label": "35°C or below", "lower": None, "upper": 35.5},
            {"label": "36°C", "lower": 35.5, "upper": 36.5},
            {"label": "37°C or higher", "lower": 36.5, "upper": None},
        ],
    }


def test_score_uses_proper_bucket_metrics_and_one_event_row():
    score = score_forecast_source_event(
        market_event=_market(),
        bucket_probabilities={"35°C or below": 0.1, "36°C": 0.8, "37°C or higher": 0.1},
        source_id="gfs_model_peak",
        provider_family="nwp_gfs",
        cutoff_id="d0",
        cutoff_at="2026-07-09T16:00:00Z",
        forecast_peak=36.0,
        forecast_snapshot_ids=["snapshot-1"],
        lineage_quality="immutable_envelope",
    )

    assert score["state"] == "available"
    assert abs(score["nll"] + math.log(0.8)) < 1e-12
    assert abs(score["rps"] - 0.01) < 1e-12
    assert abs(score["brier_multiclass"] - 0.06) < 1e-12
    assert score["top1_hit"] is True
    assert score["top3_contains"] is True
    assert score["interval_mae"] == 0.0


def test_score_interval_error_handles_censored_winning_bucket():
    market = _market()
    market["winning_bucket_label"] = "37°C or higher"
    score = score_forecast_source_event(
        market_event=market,
        bucket_probabilities={"35°C or below": 0.1, "36°C": 0.2, "37°C or higher": 0.7},
        source_id="icon_model_peak",
        provider_family="nwp_icon",
        cutoff_id="d1",
        cutoff_at="2026-07-09T00:00:00Z",
        forecast_peak=36.0,
    )

    assert score["interval_mae"] == 0.5
    assert score["bias_interval"] == -0.5

