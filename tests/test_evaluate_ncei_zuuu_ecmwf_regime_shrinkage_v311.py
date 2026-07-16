from __future__ import annotations

import numpy as np

from scripts import evaluate_ncei_zuuu_ecmwf_regime_shrinkage_v311 as regime
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


def _prediction(*, probabilities: np.ndarray, weather: dict) -> dict:
    return {
        "event_date_local": "2025-07-01",
        "local_hour": 9.0,
        "final_daily_max_c": 30.0,
        "remaining_warming_c": 2.0,
        "actual_continue": True,
        "predicted_continue_probability": 0.7,
        "expected_final_max_c": float(np.sum(v1.BUCKETS * probabilities)),
        "bucket_probabilities": probabilities.tolist(),
        "bucket_nll": 1.0,
        "bucket_rps": 0.01,
        "source_weather_row": weather,
    }


def test_zero_weight_is_exact_metric_identity():
    probabilities = np.zeros(len(v1.BUCKETS), dtype=float)
    probabilities[int(np.argmin(np.abs(v1.BUCKETS - 30.0)))] = 1.0
    weather = {"relative_humidity_pct": 90.0, "low_cloud_cover_pct": 80.0}
    baseline = _prediction(probabilities=probabilities, weather=weather)
    candidate_probabilities = np.zeros(len(v1.BUCKETS), dtype=float)
    candidate_probabilities[int(np.argmin(np.abs(v1.BUCKETS - 32.0)))] = 1.0
    candidate = _prediction(probabilities=candidate_probabilities, weather=weather)

    blended = regime._blend_rows(
        [baseline],
        [candidate],
        segment="high_humidity_low_cloud",
        weight=0.0,
    )[0]

    assert blended["bucket_probabilities"] == baseline["bucket_probabilities"]
    assert blended["expected_final_max_c"] == baseline["expected_final_max_c"]
    assert blended["predicted_continue_probability"] == baseline["predicted_continue_probability"]
    assert blended["ecmwf_effective_weight"] == 0.0


def test_weather_states_use_only_pre_registered_current_inputs():
    row = {
        "event_date_local": "2025-07-01",
        "month": 7,
        "relative_humidity_pct": 91.0,
        "low_cloud_cover_pct": 70.0,
        "temp_dewpoint_spread_c": 2.0,
        "current_precipitation_reported": True,
        "ecmwf__synoptic_850_wind_speed_mps": 9.0,
        "ecmwf__synoptic_700_wind_speed_mps": 5.0,
        "ecmwf__synoptic_850_700_shear_mps": 3.0,
        "ecmwf__synoptic_westerly_dry_foehn_proxy": 2.0,
        "ecmwf__basin_center_rhum_850_pct": 60.0,
        # This outcome label must not be consulted by any state predicate.
        "final_daily_max_c": 99.0,
    }
    assert regime._season(row) == "warm_wet_may_sep"
    assert regime._active_convection_or_precip(row) is True
    assert regime._high_humidity_low_cloud(row) is True
    assert regime._high_basin_ventilation(row) is True
    assert regime._dry_westerly_foehn(row) is True

    changed = dict(row, final_daily_max_c=-99.0)
    assert {
        name: predicate(row)
        for name, (_label, predicate) in regime.SEGMENTS.items()
    } == {
        name: predicate(changed)
        for name, (_label, predicate) in regime.SEGMENTS.items()
    }


def test_holm_adjustment_ignores_zero_weight_and_is_monotone():
    rows = [
        {"effective_weight": 0.2, "paired_2025": {"bucket_nll": {"one_sided_bootstrap_p_candidate_better": 0.01}}},
        {"effective_weight": 0.2, "paired_2025": {"bucket_nll": {"one_sided_bootstrap_p_candidate_better": 0.03}}},
        {"effective_weight": 0.0, "paired_2025": {"bucket_nll": {"one_sided_bootstrap_p_candidate_better": 0.001}}},
    ]
    regime._holm_adjust(rows, metric="bucket_nll")
    assert rows[0]["bucket_nll_holm_adjusted_p"] == 0.02
    assert rows[1]["bucket_nll_holm_adjusted_p"] == 0.03
    assert rows[2]["bucket_nll_holm_adjusted_p"] is None
