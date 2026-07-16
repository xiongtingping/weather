from __future__ import annotations

from forecast_source_bucket_distribution_runtime import (
    bucket_index_for_temperature,
    deterministic_peak_distribution,
    member_peak_distribution,
)


def _market() -> dict:
    return {
        "temperature_unit": "C",
        "bucket_schema": [
            {"label": "35°C or below", "lower": None, "upper": 35.5, "lower_inclusive": False, "upper_inclusive": False, "unit": "C"},
            {"label": "36°C", "lower": 35.5, "upper": 36.5, "lower_inclusive": True, "upper_inclusive": False, "unit": "C"},
            {"label": "37°C or higher", "lower": 36.5, "upper": None, "lower_inclusive": True, "upper_inclusive": False, "unit": "C"},
        ],
    }


def test_bucket_boundaries_follow_whole_degree_market_semantics():
    market = _market()
    assert bucket_index_for_temperature(market, 35.49) == 0
    assert bucket_index_for_temperature(market, 35.5) == 1
    assert bucket_index_for_temperature(market, 36.49) == 1
    assert bucket_index_for_temperature(market, 36.5) == 2


def test_member_and_deterministic_distributions_match_market_grid():
    member = member_peak_distribution(_market(), [35.0, 36.0, 36.0, 37.0], prior_strength=0.0)
    deterministic = deterministic_peak_distribution(_market(), 36.0, residual_sigma=0.8)

    assert member["state"] == "available"
    assert member["bucket_probabilities"] == {
        "35°C or below": 0.25,
        "36°C": 0.5,
        "37°C or higher": 0.25,
    }
    assert deterministic["state"] == "available"
    assert abs(sum(deterministic["bucket_probabilities"].values()) - 1.0) < 1e-12
    assert deterministic["bucket_probabilities"]["36°C"] > deterministic["bucket_probabilities"]["35°C or below"]

