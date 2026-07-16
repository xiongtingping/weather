from __future__ import annotations

from metar_strong_likelihood_runtime import build_temperature_trajectory_summary


def _summary(features: dict[str, float | str]) -> dict:
    return build_temperature_trajectory_summary(
        features,
        local_hour=11.0,
        peak_window={"start_hour_local": 14.0, "end_hour_local": 17.0},
    )


def test_continuous_multihorizon_warming_overrides_legacy_platform_label() -> None:
    result = _summary(
        {
            "temperature_trajectory_realization_state": "platform",
            "temperature_trajectory_stable_rate_c_per_hour": 0.6,
            "temperature_momentum_30m_c_per_hour": 0.6,
            "temperature_momentum_1h_c_per_hour": 0.6,
            "temperature_momentum_2h_c_per_hour": 0.6,
            "temperature_momentum_3h_c_per_hour": 0.6,
        }
    )

    assert result["state"] == "sustained_warming"
    assert result["state_selection_reason"] == "continuous_positive_momentum_overrides_explicit_platform"
    assert result["gate_state"] == "confirmed"
    assert result["adjusted_rate_c_per_hour"] > 0.0


def test_single_window_positive_rate_does_not_override_legacy_platform_label() -> None:
    result = _summary(
        {
            "temperature_trajectory_realization_state": "platform",
            "temperature_trajectory_stable_rate_c_per_hour": 0.6,
            "temperature_momentum_1h_c_per_hour": 0.6,
        }
    )

    assert result["state"] == "platform"
    assert result["state_selection_reason"] == "explicit_state_preserved"
    assert result["gate_state"] == "not_warming"
    assert result["adjusted_rate_c_per_hour"] == 0.0
