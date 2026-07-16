from __future__ import annotations

from datetime import date, timedelta

from joint_weak_bias_calibration_runtime import fit_joint_weak_bias_calibration, select_joint_weak_bias_state


def _sample(index: int, *, peak: float, target: float = 30.0, as_of: str | None = None) -> dict:
    day = date(2025, 6, 1) + timedelta(days=index)
    cutoff = as_of or f"{day.isoformat()}T06:30:00Z"  # 14:30 Asia/Shanghai -> D0 12-15
    return {
        "event_date": day.isoformat(),
        "decision_as_of": cutoff,
        "physical_x_final_c": target,
        "forecast_context": {"season": "summer", "weather_regime": "clear", "lead_bucket": "0_6h"},
        "source_revisions": [{"source_id": "gfs", "source_revision_id": f"gfs-{index}", "forecast_peak_c": peak}],
    }


def test_bias_state_uses_contract_sign_d0_deduplication_and_context_selection() -> None:
    # The later same-bin revision is deliberately too hot; first revision must
    # be ignored so snapshots never masquerade as independent event days.
    samples = [_sample(index, peak=30.4) for index in range(30)]
    duplicate = _sample(0, peak=20.0, as_of="2025-06-01T05:00:00Z")
    samples.append(duplicate)

    state = fit_joint_weak_bias_calibration(samples=samples)
    selected = select_joint_weak_bias_state(
        calibration=state, source_id="gfs", season="summer", weather_regime="clear",
        lead_bucket="0_6h", decision_as_of="2025-08-01T06:30:00Z",
    )

    assert state["residual_definition"] == "forecast_peak_c_minus_physical_x_final_c"
    assert state["training_event_day_count"] == 30
    assert state["training_event_date_min"] == "2025-06-01"
    assert state["training_event_date_max"] == "2025-06-30"
    assert state["state_asof_policy"] == "training_event_date_max_strictly_before_scored_event_date"
    assert state["robust_clip_method"] == "winsorize_around_frozen_residual_median_one_robust_scale"
    assert abs(state["context_states"][selected["bias_state_id"]]["residual_median_c"] - 0.4) < 1e-9
    assert selected["bias_level"] == "source_season_weather_horizon_bin"
    assert 0.0 < selected["bias_c"] <= 1.5
    assert selected["bias_sample_count"] == 30


def test_small_context_falls_back_to_zero_not_an_unpooled_mean() -> None:
    state = fit_joint_weak_bias_calibration(samples=[_sample(index, peak=31.4) for index in range(4)])
    selected = select_joint_weak_bias_state(
        calibration=state, source_id="gfs", season="summer", weather_regime="clear",
        lead_bucket="0_6h", decision_as_of="2025-08-01T06:30:00Z",
    )

    assert selected["bias_c"] == 0.0
    assert selected["bias_level"] == "zero"
    assert selected["bias_fallback"] == "source_global_insufficient"


def test_sparse_d0_context_falls_back_to_eligible_d1_season_state() -> None:
    d1_samples = []
    for index in range(30):
        sample = _sample(index, peak=31.0)
        sample["forecast_context"]["lead_bucket"] = "d1_frozen"
        d1_samples.append(sample)
    d0_samples = [_sample(index + 40, peak=34.0) for index in range(4)]

    state = fit_joint_weak_bias_calibration(samples=d1_samples + d0_samples)
    selected = select_joint_weak_bias_state(
        calibration=state,
        source_id="gfs",
        season="summer",
        weather_regime="clear",
        lead_bucket="0_6h",
        decision_as_of="2025-08-01T06:30:00Z",
    )

    assert selected["bias_state_id"] == "season:gfs|summer|d1|d1"
    assert selected["bias_fallback"] == "d0_context_insufficient_used_d1_season"
    assert selected["bias_c"] > 0.0
