from __future__ import annotations

import pytest

from bayes_basket_target import ContractError
from scripts.build_bayes_basket_target_45station_18z_no_live_packets import (
    _station_quality,
)


def test_insufficient_oos_station_fails_closed_without_point_crash() -> None:
    point, probability_passed, point_passed, state = _station_quality(
        {
            "truth_label_authority": "FORMAL_STATION_LANE_TRUTH",
            "oos": {
                "state": "BLOCKED_INSUFFICIENT_EVIDENCE",
                "research_winner": None,
            },
        },
        truth_by_date={},
    )

    assert state == "BLOCKED_INSUFFICIENT_EVIDENCE"
    assert probability_passed is False
    assert point_passed is False
    assert point["point_oos_materialized"] is False
    assert point["holdout_n"] == 0


def test_probability_oos_activation_is_not_vetoed_by_point_diagnostic() -> None:
    payload = {
        "truth_label_authority": "FORMAL_STATION_LANE_TRUTH",
        "exact_join_rows": [{
            "event_date": "2026-07-28",
            "models": {
                "GFS": {"forecast_tmax_c": 11.0},
                "ICON": {"forecast_tmax_c": 9.0},
            },
        }],
        "oos": {
            "holdout_dates": ["2026-07-28"],
            "calibration_artifacts": [
                {"model_id": "GFS", "bias_c": 0.0},
                {"model_id": "ICON", "bias_c": 0.0},
            ],
            "research_winner": {
                "selected_models": ["GFS", "ICON"],
                "selected_weights": {"GFS": 0.7, "ICON": 0.3},
                "probability_non_degradation_vs_uniform": True,
            },
        },
    }

    point, probability_passed, point_passed, state = _station_quality(
        payload,
        truth_by_date={"2026-07-28": 10.0},
    )

    assert probability_passed is True
    assert point_passed is False
    assert point["point_non_degradation_vs_uniform"] is False
    assert state == "ACTIVATION_PACKET_READY"


def test_temporary_awc_truth_is_retired_even_for_no_live() -> None:
    payload = {
        "truth_label_authority": "TEMPORARY_AWC_CALIBRATION_LABEL",
        "exact_join_rows": [{
            "event_date": "2026-07-28",
            "models": {
                "GFS": {"forecast_tmax_c": 10.0},
                "ICON": {"forecast_tmax_c": 10.0},
            },
        }],
        "oos": {
            "holdout_dates": ["2026-07-28"],
            "calibration_artifacts": [
                {"model_id": "GFS", "bias_c": 0.0},
                {"model_id": "ICON", "bias_c": 0.0},
            ],
            "research_winner": {
                "selected_models": ["GFS", "ICON"],
                "selected_weights": {"GFS": 0.5, "ICON": 0.5},
                "probability_non_degradation_vs_uniform": True,
            },
        },
    }

    with pytest.raises(
        ContractError, match="explicit_18z_temporary_awc_truth_retired"
    ):
        _station_quality(
            payload,
            truth_by_date={"2026-07-28": 10.0},
        )
