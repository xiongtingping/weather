import pytest

from temperature_posterior_runtime import (
    VERSION,
    bucket_probabilities_by_label,
    build_temperature_posterior_distribution,
)


def test_ctp_outputs_required_fact_contract() -> None:
    posterior = build_temperature_posterior_distribution(
        ["30°C", "31°C", "32°C"],
        rounding_rule="whole_degrees",
        source_components=[
            {
                "source_id": "model_ensemble",
                "group": "soft_forecast",
                "mean_c": 31.2,
                "sigma_c": 0.8,
            },
            {
                "source_id": "climate_prior",
                "group": "climate_prior",
                "bucket_probs": [0.2, 0.5, 0.3],
                "precision": 0.5,
            },
        ],
    )

    assert posterior["version"] == VERSION
    assert posterior["status"] == "ok"
    assert posterior["target_variable"] == "settlement_daily_max_c"
    assert posterior["grid"]
    assert posterior["cdf_values"]
    assert set(posterior["quantiles_c"]) == {"p05", "p10", "p50", "p90", "p95"}
    assert [row["q"] for row in posterior["quantile_grid"]] == [0.05, 0.10, 0.50, 0.90, 0.95]
    probs = bucket_probabilities_by_label(posterior)
    assert set(probs) == {"30°C", "31°C", "32°C"}
    assert sum(probs.values()) == pytest.approx(1.0)


def test_ctp_d0_constraint_removes_probability_below_observed_max() -> None:
    posterior = build_temperature_posterior_distribution(
        ["30°C", "31°C", "32°C"],
        rounding_rule="whole_degrees",
        observed_today_max_c=31.0,
        source_components=[
            {
                "source_id": "soft_forecast",
                "group": "soft_forecast",
                "mean_c": 30.2,
                "sigma_c": 0.7,
            }
        ],
    )

    probs = bucket_probabilities_by_label(posterior)
    assert probs["30°C"] == 0.0
    assert probs["31°C"] > 0.0
    assert probs["32°C"] > 0.0
    assert sum(probs.values()) == pytest.approx(1.0)
    constraints = posterior["observation_constraints"]
    assert constraints["d0_lower_bound_applied"] is True
    assert constraints["d0_violation_count"] == 0
    assert posterior["quantiles_c"]["p05"] >= 31.0


def test_ctp_hard_source_lock_is_one_hot() -> None:
    posterior = build_temperature_posterior_distribution(
        ["30°C", "31°C", "32°C"],
        rounding_rule="whole_degrees",
        observed_today_max_c=31.0,
        hard_lock_bucket="31°C",
        hard_lock_temp_c=31.0,
        source_components=[
            {
                "source_id": "model_ensemble",
                "group": "soft_forecast",
                "mean_c": 32.0,
                "sigma_c": 0.8,
            }
        ],
    )

    probs = bucket_probabilities_by_label(posterior)
    assert posterior["status"] == "hard_source_locked"
    assert probs == {"30°C": 0.0, "31°C": 1.0, "32°C": 0.0}
    assert posterior["mean_c"] == 31.0
    assert posterior["sigma_c"] == 0.0
    assert posterior["observation_constraints"]["hard_source_locked"] is True


def test_ctp_source_group_shrinkage_caps_correlated_sources() -> None:
    posterior = build_temperature_posterior_distribution(
        ["30°C", "31°C", "32°C", "33°C"],
        rounding_rule="whole_degrees",
        group_rho=0.25,
        precision_cap=4.0,
        source_components=[
            {
                "source_id": "wu",
                "group": "soft_forecast",
                "mean_c": 32.0,
                "sigma_c": 0.5,
            },
            {
                "source_id": "twc",
                "group": "soft_forecast",
                "mean_c": 32.2,
                "sigma_c": 0.5,
            },
            {
                "source_id": "climate_prior",
                "group": "climate_prior",
                "mean_c": 31.0,
                "sigma_c": 1.2,
            },
        ],
    )

    groups = {row["group"]: row for row in posterior["source_components"]["groups"]}
    soft = groups["soft_forecast"]
    assert soft["member_count"] == 2
    assert soft["raw_precision"] > soft["effective_precision"]
    assert soft["effective_precision"] <= 4.0
    assert posterior["mean_c"] > 31.0


def test_ctp_trajectory_reachability_updates_grid_not_source_components() -> None:
    baseline = build_temperature_posterior_distribution(
        ["31°C", "32°C", "33°C", "34°C"],
        rounding_rule="rounded_degrees",
        source_components=[
            {
                "source_id": "model_ensemble",
                "group": "soft_forecast",
                "bucket_probs": [0.35, 0.40, 0.20, 0.05],
                "precision": 1.0,
            }
        ],
    )
    updated = build_temperature_posterior_distribution(
        ["31°C", "32°C", "33°C", "34°C"],
        rounding_rule="rounded_degrees",
        source_components=[
            {
                "source_id": "model_ensemble",
                "group": "soft_forecast",
                "bucket_probs": [0.35, 0.40, 0.20, 0.05],
                "precision": 1.0,
            }
        ],
        likelihood_updates=[
            {
                "source_id": "temperature_trajectory",
                "group": "intraday_path",
                "kind": "trajectory_reachability",
                "observed_temp_c": 31.0,
                "observed_today_max_c": 31.0,
                "rate_c_per_hour": 0.20,
                "hours_to_peak_window": 1.0,
                "strength": 1.0,
                "precision": 0.8,
            }
        ],
    )

    base_probs = bucket_probabilities_by_label(baseline)
    updated_probs = bucket_probabilities_by_label(updated)
    assert updated["likelihood_updates"]["applied"] is True
    assert updated["likelihood_updates"]["updates"][0]["kind"] == "trajectory_reachability"
    assert [row["group"] for row in updated["source_components"]["groups"]] == ["soft_forecast"]
    assert updated_probs["34°C"] < base_probs["34°C"]
    assert updated_probs["31°C"] > base_probs["31°C"]


def test_ctp_bucket_likelihood_update_multiplies_density_before_bucket_integration() -> None:
    baseline = build_temperature_posterior_distribution(
        ["30°C", "31°C", "32°C"],
        rounding_rule="rounded_degrees",
        source_components=[
            {
                "source_id": "soft_forecast",
                "group": "soft_forecast",
                "mean_c": 31.0,
                "sigma_c": 0.8,
            }
        ],
    )
    updated = build_temperature_posterior_distribution(
        ["30°C", "31°C", "32°C"],
        rounding_rule="rounded_degrees",
        source_components=[
            {
                "source_id": "soft_forecast",
                "group": "soft_forecast",
                "mean_c": 31.0,
                "sigma_c": 0.8,
            }
        ],
        likelihood_updates=[
            {
                "source_id": "independent_physics_residual",
                "group": "physical_residual",
                "kind": "bucket_likelihood",
                "labels": ["30°C", "31°C", "32°C"],
                "probabilities": [0.65, 0.25, 0.10],
                "strength": 1.0,
                "precision": 0.75,
            }
        ],
    )

    base_probs = bucket_probabilities_by_label(baseline)
    updated_probs = bucket_probabilities_by_label(updated)
    assert updated["likelihood_updates"]["applied"] is True
    assert updated["likelihood_updates"]["updates"][0]["kind"] == "bucket_likelihood"
    assert updated_probs["30°C"] > base_probs["30°C"]
    assert updated_probs["32°C"] < base_probs["32°C"]
