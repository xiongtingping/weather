from __future__ import annotations

import pytest

from physical_state_bayes_v6.forcing_adapter import build_candidate_scenario_controls


def test_forcing_adapter_preserves_responsibility_and_missing_path_semantics() -> None:
    weak = {
        "joint_forecast_weak_likelihood_sha256": "a" * 64,
        "forecast_scenario_paths": {
            "sha256": "b" * 64,
            "paths": [
                {
                    "scenario_id": "gfs-r1",
                    "source_id": "gfs",
                    "hourly_curve": [
                        {
                            "valid_time_utc": "2026-07-15T06:00:00Z",
                            "temperature_c": 28.0,
                            "cloud_cover_probability": 0.2,
                        },
                        {
                            "valid_time_utc": "2026-07-15T07:00:00Z",
                            "temperature_c": 30.0,
                            "cloud_cover_probability": 0.4,
                        },
                        {
                            "valid_time_utc": "2026-07-15T08:00:00Z",
                            "temperature_c": 31.0,
                            "cloud_cover_probability": 0.5,
                        },
                    ],
                },
                {
                    "scenario_id": "taf-r1",
                    "source_id": "taf",
                    "hourly_curve": [],
                },
            ],
        },
        "candidate_scenario_responsibilities": {
            "sha256": "c" * 64,
            "rows": [
                {
                    "continuous_y_c": 31.0,
                    "scenarios": [
                        {"scenario_id": "gfs-r1", "responsibility": 0.7},
                        {"scenario_id": "taf-r1", "responsibility": 0.3},
                    ],
                }
            ],
        },
    }

    bundle = build_candidate_scenario_controls(
        weak_likelihood=weak,
        event_valid_at="2026-07-15T06:30:00Z",
    )

    gfs, taf = bundle.controls_by_y[31.0]
    assert gfs.responsibility == 0.7
    assert gfs.forecast_rate_c_per_hour == 2.0
    assert gfs.future_max_increment_c == 2.0
    assert gfs.minutes_to_curve_peak == 90.0
    assert gfs.cloud_suppression_probability == pytest.approx(0.3)
    assert gfs.cloud_feature_available == 1.0
    assert taf.responsibility == 0.3
    assert taf.forcing_availability == 0.0
    assert taf.cloud_feature_available == 0.0
