from __future__ import annotations

import math
from datetime import date, timedelta

from forecast_source_contextual_weight_model import (
    fit_contextual_stacking,
    mix_source_distributions,
    predict_contextual_weights,
)


def _samples(count: int = 50) -> list[dict]:
    start = date(2026, 1, 1)
    rows = []
    for index in range(count):
        winner = "hot" if index % 2 else "cool"
        wrong = "cool" if winner == "hot" else "hot"
        rows.append(
            {
                "event_date_local": (start + timedelta(days=index)).isoformat(),
                "lead_hours": 24.0,
                "winning_bucket": winner,
                "context": {"cloud_cover_pct": float(index % 10) * 10.0},
                "distributions": {
                    "source_a": {winner: 0.9, wrong: 0.1},
                    "source_b": {winner: 0.2, wrong: 0.8},
                },
            }
        )
    return rows


def test_contextual_stacking_learns_better_source_and_improves_log_loss():
    artifact = fit_contextual_stacking(
        _samples(),
        provider_families={"source_a": "family_a", "source_b": "family_b"},
        context_fields=["cloud_cover_pct"],
        max_iterations=500,
        source_cap=0.9,
        family_cap=0.9,
    )
    weights = predict_contextual_weights(
        artifact,
        {"event_date_local": "2026-02-20", "lead_hours": 24.0, "context": {"cloud_cover_pct": 20.0}},
    )
    mixed = mix_source_distributions(
        {"source_a": {"cool": 0.9, "hot": 0.1}, "source_b": {"cool": 0.2, "hot": 0.8}},
        weights,
    )

    assert weights["source_a"] > weights["source_b"]
    assert -math.log(mixed["cool"]) < -math.log(0.55)
    assert artifact["production_ready"] is False
    assert artifact["gates"]["strict_oos_evaluated"] is False


def test_seasonal_basis_is_continuous_across_year_boundary():
    artifact = fit_contextual_stacking(
        _samples(),
        provider_families={"source_a": "family_a", "source_b": "family_b"},
        max_iterations=200,
        source_cap=0.9,
        family_cap=0.9,
    )
    left = predict_contextual_weights(
        artifact,
        {"event_date_local": "2026-12-31", "lead_hours": 24.0, "context": {}},
    )
    right = predict_contextual_weights(
        artifact,
        {"event_date_local": "2027-01-01", "lead_hours": 24.0, "context": {}},
    )

    assert abs(left["source_a"] - right["source_a"]) < 0.02


def test_provider_family_cap_prevents_wu_twc_double_precision():
    samples = _samples()
    for row in samples:
        row["distributions"]["source_c"] = dict(row["distributions"]["source_a"])
    artifact = fit_contextual_stacking(
        samples,
        provider_families={
            "source_a": "twc_provider",
            "source_c": "twc_provider",
            "source_b": "nwp_gfs",
        },
        max_iterations=400,
        source_cap=0.9,
        family_cap=0.6,
    )
    weights = predict_contextual_weights(
        artifact,
        {"event_date_local": "2026-02-20", "lead_hours": 24.0, "context": {}},
    )

    assert abs(sum(weights.values()) - 1.0) < 1e-12
    assert weights["source_a"] + weights["source_c"] <= 0.6000001
    assert weights["source_b"] >= 0.3999999


def test_duplicate_interface_does_not_increase_provider_family_total_weight():
    base = fit_contextual_stacking(
        _samples(),
        provider_families={"source_a": "twc_provider", "source_b": "nwp_gfs"},
        max_iterations=200,
        source_cap=0.9,
        family_cap=0.9,
    )
    duplicated_samples = _samples()
    for row in duplicated_samples:
        row["distributions"]["source_c"] = dict(row["distributions"]["source_a"])
    duplicated = fit_contextual_stacking(
        duplicated_samples,
        provider_families={"source_a": "twc_provider", "source_c": "twc_provider", "source_b": "nwp_gfs"},
        max_iterations=200,
        source_cap=0.9,
        family_cap=0.9,
    )
    context = {"event_date_local": "2026-02-20", "lead_hours": 24.0, "context": {}}
    base_weights = predict_contextual_weights(base, context)
    duplicate_weights = predict_contextual_weights(duplicated, context)
    assert abs(base_weights["source_a"] - (duplicate_weights["source_a"] + duplicate_weights["source_c"])) < 1e-6


def test_missing_source_is_removed_and_remaining_weights_renormalize():
    artifact = fit_contextual_stacking(
        _samples(),
        provider_families={"source_a": "family_a", "source_b": "family_b"},
        max_iterations=100,
    )
    weights = predict_contextual_weights(
        artifact,
        {"event_date_local": "2026-03-01", "lead_hours": 24.0, "context": {}},
        available_entities=["source_b"],
    )

    assert weights == {"source_b": 1.0}


def test_sparse_weather_context_falls_back_to_parent_without_hard_boundary():
    samples = _samples(12)
    for index, sample in enumerate(samples):
        sample["context"] = {"rare_signal": 1.0 if index == 0 else None}
    artifact = fit_contextual_stacking(
        samples,
        context_fields=["rare_signal"],
        minimum_context_ess=20,
        max_iterations=100,
        source_cap=0.9,
        family_cap=0.9,
    )
    low = predict_contextual_weights(
        artifact,
        {"event_date_local": "2026-06-01", "lead_hours": 12, "context": {"rare_signal": -100.0}},
    )
    high = predict_contextual_weights(
        artifact,
        {"event_date_local": "2026-06-01", "lead_hours": 12, "context": {"rare_signal": 100.0}},
    )
    assert artifact["context_support"]["rare_signal"]["active"] is False
    assert low == high


def test_optimizer_stops_after_objective_stabilizes_without_data_thresholds():
    artifact = fit_contextual_stacking(
        _samples(),
        max_iterations=500,
        minimum_iterations=50,
        early_stopping_patience=1,
        early_stopping_tolerance=1.0,
    )

    assert artifact["training"]["converged"] is True
    assert artifact["training"]["iterations_completed"] < 500


def test_single_source_cap_is_enforced_across_unique_provider_families():
    artifact = fit_contextual_stacking(
        _samples(),
        provider_families={"source_a": "family_a", "source_b": "family_b"},
        max_iterations=100,
        source_cap=0.6,
        family_cap=0.9,
    )
    weights = predict_contextual_weights(
        artifact,
        {"event_date_local": "2026-02-20", "lead_hours": 24.0, "context": {}},
    )

    assert max(weights.values()) <= 0.6000001
