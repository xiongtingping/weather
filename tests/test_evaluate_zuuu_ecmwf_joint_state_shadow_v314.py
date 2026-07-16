from __future__ import annotations

from scripts import evaluate_zuuu_ecmwf_joint_state_shadow_v314 as joint


def _row(**overrides):
    row = {
        "ecmwf__synoptic_850_wind_speed_mps": 8.0,
        "ecmwf__synoptic_700_wind_speed_mps": 4.0,
        "ecmwf__synoptic_850_700_shear_mps": 2.0,
        "relative_humidity_pct": 65.0,
        "low_cloud_cover_pct": 20.0,
        "temp_dewpoint_spread_c": 8.0,
        "current_convective_weather": False,
        "convective_cloud_reported": False,
        "current_precipitation_reported": False,
        "convective_seen_today": False,
        "precipitation_seen_today": False,
        "post_convective_cooling": False,
        "post_precipitation_cooling": False,
        "drop_from_observed_max_c": 0.0,
        "reports_below_observed_max": 0,
    }
    row.update(overrides)
    return row


def test_joint_state_is_mutually_exclusive_and_uses_suppressor_union() -> None:
    assert joint._state(_row()) == "850wind__unsuppressed"
    assert joint._state(_row(current_precipitation_reported=True)) == "850wind__suppressed"
    assert joint._state(_row(drop_from_observed_max_c=0.1)) == "850wind__suppressed"


def test_joint_state_fails_closed_when_cloud_channel_is_missing() -> None:
    assert joint._state(_row(low_cloud_cover_pct=None)) is None


def test_strict_selection_requires_binary_brier_improvement() -> None:
    gains = {"bucket_nll": 0.2, "bucket_rps": 0.1, "continue_brier": -0.001, "mae_c": 0.1}
    assert joint._strict_eligible(gains) is False
    assert joint._strict_eligible({**gains, "continue_brier": 0.001}) is True
