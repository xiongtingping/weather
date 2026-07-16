from __future__ import annotations

from scripts import evaluate_ncei_zuuu_carryover_ablations_v311 as ablations
from scripts import train_ncei_zuuu_multimodel_carryover_bayes_v311 as v311


def _station_row(day: str, hour: float, temperature: float, final_max: float) -> dict:
    return {
        "event_date_local": day,
        "local_hour": hour,
        "valid_time_utc": f"{day}T00:00:00Z",
        "temperature_c": temperature,
        "final_daily_max_c": final_max,
        "observed_max_to_time_c": temperature,
        "drop_from_observed_max_c": 0.0,
        "hours_since_observed_max": 0.0,
    }


def test_carryover_uses_exact_previous_local_day_and_never_current_label():
    previous = [
        _station_row("2025-07-09", 20.0, 30.0, 35.0),
        _station_row("2025-07-09", 23.0, 29.0, 35.0),
    ]
    current_predawn = _station_row("2025-07-10", 0.0, 28.0, 99.0)
    current_daytime = _station_row("2025-07-10", 8.0, 27.0, 99.0)
    rows, profile = v311._augment_carryover(
        [current_predawn, current_daytime],
        station_summary_rows=[*previous, current_predawn, current_daytime],
    )

    predawn, daytime = rows
    assert predawn["carryover_previous_day_last_temperature_c"] == 29.0
    assert predawn["carryover_previous_day_observed_max_c"] == 35.0
    assert predawn["carryover_previous_max_minus_current_c"] == 7.0
    assert predawn["carryover_first_temperature_c"] == 28.0
    assert 99.0 not in {
        predawn["carryover_previous_day_last_temperature_c"],
        predawn["carryover_previous_day_observed_max_c"],
        predawn["carryover_first_temperature_c"],
    }
    assert daytime["carryover_previous_day_last_temperature_c"] == 0.0
    assert daytime["carryover_previous_day_observed_max_c"] == 0.0
    assert daytime["pre_dawn_carryover_phase"] is False
    assert profile["current_day_label_used_as_feature"] is False


def _prediction(day: str, loss: float) -> dict:
    return {
        "event_date_local": day,
        "actual_continue": True,
        "predicted_continue_probability": 0.8,
        "final_daily_max_c": 30.0,
        "expected_final_max_c": 30.0 + loss,
        "bucket_nll": loss,
        "bucket_rps": loss,
    }


def test_nested_selection_cannot_use_current_test_year_results():
    baseline_2022 = [_prediction("2022-07-01", 1.0)]
    baseline_2023 = [_prediction("2023-07-01", 1.0)]
    fold_predictions = {
        2022: {"baseline_v31": baseline_2022},
        2023: {"baseline_v31": baseline_2023},
    }
    for group in ablations.FEATURE_GROUPS:
        fold_predictions[2022][group] = [_prediction("2022-07-01", 0.9 if group == "first_temperature_path" else 1.1)]
        # The current test year strongly favors a different group; selection
        # must still be determined exclusively by 2022.
        fold_predictions[2023][group] = [_prediction("2023-07-01", 0.1 if group == "previous_day_max_gap" else 2.0)]

    selected, trace = ablations._select_from_prior_years(fold_predictions, 2023)

    assert selected == "first_temperature_path"
    assert trace["prior_years"] == [2022]
    assert 2023 not in trace["prior_years"]
