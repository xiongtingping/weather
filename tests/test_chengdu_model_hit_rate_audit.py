from datetime import datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chengdu_model_hit_rate_audit as audit
import climo_regime_runtime as climo


def test_dte_bucket_uses_chengdu_local_day():
    assert audit.dte_bucket("2026-06-30", "2026-06-29T16:00:00+00:00") == "d0"
    assert audit.dte_bucket("2026-06-30", "2026-06-29T15:59:59+00:00") == "d_minus_1"


def test_d0_pre_peak_filter_uses_local_noon_boundary():
    assert audit.before_peak_window_start("2026-06-30", "2026-06-30T03:59:59+00:00") is True
    assert audit.before_peak_window_start("2026-06-30", "2026-06-30T04:00:00+00:00") is False


def test_bucket_label_round_and_floor_temperature_market_edges():
    assert audit.bucket_label(29.6, rule="round") == "30°C"
    assert audit.bucket_label(29.6, rule="floor") == "29°C"
    assert audit.bucket_label(24.8, rule="round") == "25°C or below"
    assert audit.bucket_label(35.2, rule="round") == "35°C or higher"
    assert audit.bucket_index(35.2, rule="round") == 35


def test_wilson_interval_is_bounded_and_contains_observed_rate():
    low, high = audit._wilson_interval(5, 20)

    assert 0.0 <= low < 0.25 < high <= 1.0


def test_trade_skill_summary_separates_exact_bucket_from_threshold_skill():
    rows = [
        {
            "model": "gfs_seamless",
            "event_date": "2026-06-30",
            "top_round_index": 30,
            "top3_round_indices": [30, 31, 32],
            "mean_round_index": 31,
            "actual_round_index": 32,
            "mean_c": 31.4,
            "actual_c": 32.0,
        }
    ]

    summary = audit._summarize_trade_metrics(rows, models=["gfs_seamless"])["gfs_seamless"]

    assert summary["exact_top1_hit_rate"] == 0.0
    assert summary["top3_contains_actual_rate"] == 1.0
    assert summary["mean_within_1c_rate"] == 1.0
    assert summary["threshold_direction"]["mean_ge_30c_direction_hit_rate"] == 1.0


def test_distribution_metrics_reward_probability_on_actual_bucket():
    good = [{"model": "gfs_seamless", "event_date": "2026-06-30", "actual_round_index": 32, "bucket_probabilities_round": {"32": 0.8, "31": 0.2}}]
    bad = [{"model": "gfs_seamless", "event_date": "2026-06-30", "actual_round_index": 32, "bucket_probabilities_round": {"30": 0.8, "31": 0.2}}]

    good_metrics = audit._summarize_distribution_metrics(good, models=["gfs_seamless"])["gfs_seamless"]
    bad_metrics = audit._summarize_distribution_metrics(bad, models=["gfs_seamless"])["gfs_seamless"]

    assert good_metrics["brier"] < bad_metrics["brier"]
    assert good_metrics["logloss"] < bad_metrics["logloss"]
    assert good_metrics["rps"] < bad_metrics["rps"]
    assert good_metrics["crps_c"] < bad_metrics["crps_c"]


def _shadow_row(
    model: str,
    event_date: str,
    probs: dict,
    actual: int = 32,
    basin_regime: str = "unknown",
) -> dict:
    top_idx = max(probs.items(), key=lambda item: item[1])[0]
    return {
        "model": model,
        "event_date": event_date,
        "season": "summer",
        "basin_regime": basin_regime,
        "actual_round_index": actual,
        "actual_c": float(actual),
        "top_round_index": int(top_idx),
        "top3_round_indices": [int(k) for k, _v in sorted(probs.items(), key=lambda item: item[1], reverse=True)[:3]],
        "mean_round_index": int(top_idx),
        "mean_c": float(top_idx),
        "bucket_probabilities_round": {str(k): float(probs.get(str(k), 0.0)) for k in audit.BUCKET_INDICES},
    }


def test_shadow_weights_use_distribution_metrics_not_exact_top1():
    rows = []
    for day in range(1, 41):
        event_date = f"2026-06-{day:02d}" if day <= 30 else f"2026-07-{day - 30:02d}"
        rows.extend(
            [
                _shadow_row("gfs_seamless", event_date, {"32": 0.75, "31": 0.25}),
                _shadow_row("icon_seamless", event_date, {"32": 0.45, "31": 0.35, "30": 0.20}),
                _shadow_row("ecmwf_ifs025", event_date, {"30": 0.70, "31": 0.20, "32": 0.10}),
            ]
        )

    segment = audit._shadow_weight_segment(
        rows,
        models=("gfs_seamless", "icon_seamless", "ecmwf_ifs025"),
        segment_key="d0_pre_peak|season=summer|basin_regime=unknown",
        dte="d0_pre_peak",
        season="summer",
        basin_regime="unknown",
        segment_level="dte+season+basin_regime_fallback",
    )

    weights = {row["model"]: row["shadow_weight"] for row in segment["model_weights"]}
    assert segment["weighting_basis"] == list(audit.CONTINUOUS_WEIGHT_METRICS)
    assert segment["exact_top1_role"] == "diagnostics_only_not_weight_basis"
    assert segment["shadow_display_allowed"] is True
    assert segment["runtime_promotion_allowed"] is False
    assert "strict_walk_forward_non_degradation_not_yet_verified" in segment["not_online_reasons"]
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert max(weights.values()) <= audit.SHADOW_SINGLE_MODEL_WEIGHT_CAP + 1e-9
    assert weights["gfs_seamless"] > weights["ecmwf_ifs025"]
    for row in segment["model_weights"]:
        assert "exact_top1_hit_rate" in row["diagnostics_only"]
        assert "exact_top1_hit_rate" not in row["metrics"]


def test_basin_regime_feature_set_declares_fallback_and_blockers():
    rows = [_shadow_row("gfs_seamless", "2026-06-30", {"32": 0.6, "31": 0.4})]

    feature_set = audit._build_basin_regime_feature_set(
        live_d1=rows,
        live_d0_pre=rows,
        live_d0_all=rows,
    )

    assert feature_set["applied_to_runtime"] is False
    assert "source+similar_city_climate_zone" in feature_set["fallback_order"]
    assert "basin_regime_feature_not_materialized_in_model_member_cache" in feature_set["promotion_blockers"]
    assert any(feature["name"] == "basin_regime" for feature in feature_set["features"])


def test_basin_regime_feature_set_uses_materialized_model_cache_regime():
    rows = [
        _shadow_row(
            "gfs_seamless",
            "2026-06-30",
            {"32": 0.6, "31": 0.4},
            basin_regime="moderate_ventilation_heat_buildup",
        )
    ]

    feature_set = audit._build_basin_regime_feature_set(
        live_d1=rows,
        live_d0_pre=[],
        live_d0_all=[],
    )

    basin_feature = next(feature for feature in feature_set["features"] if feature["name"] == "basin_regime")
    assert feature_set["status"] == "materialized_sample_coverage"
    assert basin_feature["status"] == "active"
    assert "basin_regime_feature_not_materialized_in_model_member_cache" not in feature_set["promotion_blockers"]
    assert any(
        row["basin_regime"] == "moderate_ventilation_heat_buildup"
        and row["fallback_used"] == "source+station+dte+season+basin_regime"
        for row in feature_set["sample_strata"]
    )


def test_basin_regime_feature_set_counts_unsettled_materialized_cache_rows():
    cache_rows = [
        {
            "id": 101,
            "model": "gfs_seamless",
            "event_date": "2026-07-02",
            "created_at": "2026-07-01T00:00:00+00:00",
            "season": "summer",
            "dte_bucket": "d_minus_1",
            "before_peak_window_start": True,
            "basin_regime": "cloud_limited_or_rain_cooled",
            "has_settlement_label": False,
        }
    ]

    feature_set = audit._build_basin_regime_feature_set(
        live_d1=[],
        live_d0_pre=[],
        live_d0_all=[],
        materialized_cache_rows=cache_rows,
    )

    assert feature_set["status"] == "materialized_sample_coverage"
    assert feature_set["materialized_rows"] == 1
    assert any(
        row["basin_regime"] == "cloud_limited_or_rain_cooled"
        and row["promotion_status"] == "materialized_waiting_for_settlement_labels"
        and row["labeled_rows"] == 0
        for row in feature_set["sample_strata"]
    )


def test_basin_regime_feature_set_prefers_labeled_rows_over_cache_only_duplicates():
    live_row = _shadow_row(
        "gfs_seamless",
        "2026-06-30",
        {"32": 0.6, "31": 0.4},
        basin_regime="well_ventilated",
    )
    live_row["id"] = 202
    cache_row = {
        "id": 202,
        "model": "gfs_seamless",
        "event_date": "2026-06-30",
        "created_at": "2026-06-29T00:00:00+00:00",
        "season": "summer",
        "dte_bucket": "d_minus_1",
        "before_peak_window_start": True,
        "basin_regime": "well_ventilated",
        "has_settlement_label": False,
    }

    feature_set = audit._build_basin_regime_feature_set(
        live_d1=[live_row],
        live_d0_pre=[],
        live_d0_all=[],
        materialized_cache_rows=[cache_row],
    )

    assert any(
        row["basin_regime"] == "well_ventilated"
        and row["labeled_rows"] == 1
        and row["promotion_status"] == "sample_accumulating_not_runtime_promoted"
        for row in feature_set["sample_strata"]
    )


def test_model_weight_shadow_emits_real_basin_regime_segment():
    rows = [
        _shadow_row("gfs_seamless", "2026-06-30", {"32": 0.7, "31": 0.3}, basin_regime="moderate_ventilation"),
        _shadow_row("icon_seamless", "2026-06-30", {"31": 0.6, "32": 0.4}, basin_regime="moderate_ventilation"),
        _shadow_row("ecmwf_ifs025", "2026-06-30", {"30": 0.6, "31": 0.4}, basin_regime="moderate_ventilation"),
    ]

    shadow = audit._build_model_weight_shadow(
        live_d1=rows,
        live_d0_pre=[],
        live_d0_all=[],
        models=("gfs_seamless", "icon_seamless", "ecmwf_ifs025"),
    )

    segment_keys = {segment["segment_key"] for segment in shadow["segments"]}
    assert "d_minus_1|season=summer|basin_regime=moderate_ventilation" in segment_keys


def test_derive_basin_regime_label_prefers_heat_buildup_over_background():
    label = climo.derive_basin_regime_label(
        {
            "basin_ventilation_regime": "moderate_ventilation",
            "basin_heat_mechanism_state": "active",
            "monsoon_humidity_regime": "humid_monsoon",
        }
    )

    assert label == "moderate_ventilation_heat_buildup"
