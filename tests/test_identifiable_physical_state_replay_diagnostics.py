from __future__ import annotations

from scripts.replay_identifiable_physical_state_bayes_candidate import (
    _posterior_state_summary,
    build_state_space_root_cause_diagnostics,
)


def test_posterior_state_summary_exposes_internal_tendency_without_recomputing_probability() -> None:
    extension = {
        "physical_state_posterior_json": {
            "components": [
                {
                    "normalized_weight": 0.25,
                    "continuous_state_mean": {
                        "latent_temperature": 26.0,
                        "net_temperature_tendency": 1.0,
                    },
                },
                {
                    "normalized_weight": 0.75,
                    "continuous_state_mean": {
                        "latent_temperature": 28.0,
                        "net_temperature_tendency": 2.0,
                    },
                },
            ]
        }
    }
    result = _posterior_state_summary(extension)
    assert result == {
        "available": True,
        "expected_latent_temperature_c": 27.5,
        "expected_net_temperature_tendency_c_per_hour": 1.75,
    }


def test_forecast_peak_diagnostics_are_excluded_from_replay() -> None:
    # The replay module intentionally has no peak reader.  This is a stronger
    # boundary than keeping a diagnostic reader that could later be wired in.
    import scripts.replay_identifiable_physical_state_bayes_candidate as replay_module

    assert not hasattr(replay_module, "_forecast_peak_diagnostics")


def test_root_cause_diagnostics_mark_scaffold_and_do_not_turn_source_peaks_into_inputs() -> None:
    result = build_state_space_root_cause_diagnostics(
        state_artifact={
            "calibration_state": "untrained_scaffold",
            "parameter_calibration": {"state": "untrained"},
        },
        weak_likelihood={
            "forecast_scenario_paths": {
                "paths": [
                    {
                        "scenario_id": "nwp#r1",
                        "hourly_curve": [
                            {"valid_time_utc": "2026-07-21T00:00:00Z", "temperature_c": 40.0}
                        ],
                    }
                ]
            }
        },
        event_date="2026-07-21",
        events=(),
        final_extension={
            "physical_state_posterior_json": {
                "components": [
                    {
                        "normalized_weight": 1.0,
                        "continuous_state_mean": {
                            "latent_temperature": 27.0,
                            "net_temperature_tendency": 1.5,
                        },
                    }
                ]
            }
        },
        candidate_final={"top_bucket": 32},
    )
    assert result["transition_parameters_are_scaffold"] is True
    assert result["temperature_forecast_not_consumed_by_contract"] is True
    assert result["temperature_peak_trajectory_and_slope_external_fields_consumed"] is False
    assert result["source_forecast_peaks_read_for_diagnostic_only"] is False
    assert result["source_forecast_peak_diagnostics"] == "excluded_from_replay"
    assert result["latest_internal_state"]["expected_net_temperature_tendency_c_per_hour"] == 1.5
