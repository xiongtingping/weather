from __future__ import annotations

from production_probability_v6_canonical_event_drain_runtime import _initial_joint


def test_v6_initial_joint_is_prior_times_weak_times_latent_state() -> None:
    inputs = {
        "prior_context": {
            "continuous_distribution": {
                "grid": [{"temp_c": 34.0, "mass": 0.5}, {"temp_c": 38.0, "mass": 0.5}]
            },
            "latent_state_prior_by_temperature": {
                "34": {"warming": 0.5, "weather_suppressed": 0.5},
                "38": {"warming": 0.5, "weather_suppressed": 0.5},
            },
        },
        "weak_likelihood": {
            "joint_weak_likelihood": {
                "joint_forecast_weak_likelihood": [
                    {"temperature_c": 34.0, "likelihood": 0.2},
                    {"temperature_c": 38.0, "likelihood": 0.8},
                ]
            }
        },
    }
    rows = _initial_joint(inputs)
    marginal = {}
    for row in rows:
        marginal[row.y_c] = marginal.get(row.y_c, 0.0) + row.probability
    assert marginal == {34.0: 0.2, 38.0: 0.8}
    assert abs(sum(row.probability for row in rows) - 1.0) < 1e-12
