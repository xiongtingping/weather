#!/usr/bin/env python3
"""Research-only OOS evaluation of a deduplicated smooth peak-origin model.

The candidate first removes two exact aliases from the v3.1 physical state
basis.  It then represents the inferred local time of the running maximum
with a cyclic Fourier basis and combines it with peak-vs-climatology excess,
astronomical daylight progress, and remaining solar-heating potential.

No temperature or clock-hour threshold gates any candidate feature.  The
existing v3.1 GFS/multimodel regime selection is held fixed solely so the
comparison isolates the feature-basis change.  Every candidate coefficient
uses the existing standardized ridge shrinkage in the v3.1 trainer.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2
from scripts import train_ncei_zuuu_multimodel_allday_bayes_v31 as v31
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1
from scripts.evaluate_ncei_zuuu_morning_peak_origin_v312 import (
    _baseline_v31,
    _improvement,
    _paired_day_validation,
)


SCHEMA = "ncei_zuuu_smooth_peak_origin_validation_v313"
MODEL_SCHEMA = "ncei_zuuu_deduplicated_smooth_peak_origin_artifact_v313"
DEDUP_MODEL_SCHEMA = "ncei_zuuu_deduplicated_peak_state_artifact_v313"
VERSION = "v3.1.3_deduplicated_smooth_peak_origin_research"

# These pairs were proven to be exact aliases at the hourly training grain.
# Keep the duration and positive cooling-depth representations.
DEDUP_REMOVED_NUMERIC = (
    "temperature_vs_observed_max_c",
    "reports_below_observed_max",
)

SMOOTH_NUMERIC = (
    "peak_boundary_closeness",
    "peak_boundary_direction",
    "solar_daylight_progress",
    "solar_remaining_heating_potential",
    "peak_boundary_closeness_x_climo_excess",
    "peak_boundary_direction_x_climo_excess",
    "climo_excess_x_daylight_progress",
    "climo_excess_x_remaining_heating",
    "peak_boundary_closeness_x_climo_excess_x_progress_x_remaining",
    "peak_boundary_direction_x_climo_excess_x_progress_x_remaining",
)

DEDUP_GFS_NUMERIC = [name for name in v31.GFS_NUMERIC if name not in DEDUP_REMOVED_NUMERIC]
DEDUP_MULTIMODEL_NUMERIC = [
    name for name in v31.MULTIMODEL_NUMERIC if name not in DEDUP_REMOVED_NUMERIC
]
SMOOTH_GFS_NUMERIC = [*DEDUP_GFS_NUMERIC, *SMOOTH_NUMERIC]
SMOOTH_MULTIMODEL_NUMERIC = [*DEDUP_MULTIMODEL_NUMERIC, *SMOOTH_NUMERIC]
SMOOTH_GROUP_PENALTY_MULTIPLIER = float(len(SMOOTH_NUMERIC))

# Fixed station geometry, not fitted cutoffs.
ZUUU_LATITUDE_DEG = 30.5785
ZUUU_LONGITUDE_DEG = 103.9471
LOCAL_UTC_OFFSET_HOURS = 8.0
SOLAR_GRID_STEP_MINUTES = 5


def _number(value: Any) -> float | None:
    return v1._number(value)


def _solar_elevation_sine(day_of_year: int, local_hour: float) -> float:
    """Approximate positive solar elevation using continuous NOAA geometry."""

    gamma = 2.0 * math.pi / 365.0 * (float(day_of_year) - 1.0 + (local_hour - 12.0) / 24.0)
    equation_of_time_minutes = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.00148 * math.sin(3.0 * gamma)
    )
    time_offset_minutes = (
        equation_of_time_minutes
        + 4.0 * ZUUU_LONGITUDE_DEG
        - 60.0 * LOCAL_UTC_OFFSET_HOURS
    )
    true_solar_minutes = local_hour * 60.0 + time_offset_minutes
    hour_angle = math.radians(true_solar_minutes / 4.0 - 180.0)
    latitude = math.radians(ZUUU_LATITUDE_DEG)
    sine_elevation = (
        math.sin(latitude) * math.sin(declination)
        + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)
    )
    return max(0.0, float(sine_elevation))


@lru_cache(maxsize=367)
def _daily_solar_curve(day_of_year: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float, float]:
    step_hours = SOLAR_GRID_STEP_MINUTES / 60.0
    hours = np.arange(0.0, 24.0 + step_hours / 2.0, step_hours, dtype=float)
    elevation = np.asarray([_solar_elevation_sine(day_of_year, float(hour)) for hour in hours])
    trapezoids = 0.5 * (elevation[:-1] + elevation[1:]) * step_hours
    cumulative = np.concatenate([np.zeros(1, dtype=float), np.cumsum(trapezoids)])
    total = float(cumulative[-1])
    positive = np.flatnonzero(elevation > 0.0)
    if not len(positive) or total <= 0.0:
        return hours, elevation, cumulative, 0.0, 24.0, 1.0
    sunrise = float(hours[int(positive[0])])
    sunset = float(hours[int(positive[-1])])
    return hours, elevation, cumulative, sunrise, sunset, total


def solar_state(day_of_year: int, local_hour: float) -> tuple[float, float]:
    """Return daylight-time progress and normalized remaining solar energy."""

    hours, _, cumulative, sunrise, sunset, total = _daily_solar_curve(int(day_of_year))
    hour = min(24.0, max(0.0, float(local_hour)))
    daylight_span = max(sunset - sunrise, 1e-6)
    progress = min(1.0, max(0.0, (hour - sunrise) / daylight_span))
    accumulated = float(np.interp(hour, hours, cumulative))
    remaining = min(1.0, max(0.0, (total - accumulated) / max(total, 1e-9)))
    return progress, remaining


def smooth_peak_origin_values(row: Mapping[str, Any], climo: float) -> dict[str, float | None]:
    hour = _number(row.get("local_hour"))
    peak_age = _number(row.get("hours_since_observed_max"))
    observed_max = _number(row.get("observed_max_to_time_c"))
    event_date = str(row.get("event_date_local") or "")
    if hour is None or not event_date:
        return {name: None for name in SMOOTH_NUMERIC}

    day_of_year = date.fromisoformat(event_date).timetuple().tm_yday
    progress, remaining = solar_state(day_of_year, hour)
    if peak_age is None or observed_max is None:
        values = {name: None for name in SMOOTH_NUMERIC}
        values["solar_daylight_progress"] = progress
        values["solar_remaining_heating_potential"] = remaining
        return values

    inferred_peak_hour = (hour - peak_age) % 24.0
    peak_angle = 2.0 * math.pi * inferred_peak_hour / 24.0
    closeness = 0.5 * (1.0 + math.cos(peak_angle))
    direction = math.sin(peak_angle)
    climo_excess = observed_max - float(climo)
    four_way_scale = climo_excess * progress * remaining
    return {
        "peak_boundary_closeness": closeness,
        "peak_boundary_direction": direction,
        "solar_daylight_progress": progress,
        "solar_remaining_heating_potential": remaining,
        "peak_boundary_closeness_x_climo_excess": closeness * climo_excess,
        "peak_boundary_direction_x_climo_excess": direction * climo_excess,
        "climo_excess_x_daylight_progress": climo_excess * progress,
        "climo_excess_x_remaining_heating": climo_excess * remaining,
        "peak_boundary_closeness_x_climo_excess_x_progress_x_remaining": closeness * four_way_scale,
        "peak_boundary_direction_x_climo_excess_x_progress_x_remaining": direction * four_way_scale,
    }


class SmoothPeakOriginDesign(v31.AllDayDesign):
    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix: list[list[float]] = []
        for row, climo in zip(rows, climo_values):
            values = v31._values(row, climo)
            values.update(smooth_peak_origin_values(row, climo))
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values.get(name) or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _penalty_diagonal(design: v1.Design, *, base_alpha: float) -> np.ndarray:
    """Multiplicity-aware ridge: shrink the added smooth group as a group."""

    penalties = np.full(1 + len(design.names), float(base_alpha), dtype=float)
    penalties[0] = 0.01
    smooth_names = set(SMOOTH_NUMERIC) | {f"{name}__missing" for name in SMOOTH_NUMERIC}
    for index, name in enumerate(design.names, start=1):
        if name in smooth_names:
            penalties[index] *= SMOOTH_GROUP_PENALTY_MULTIPLIER
    return penalties


def _fit_logistic_group_shrinkage(
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    *,
    design: v1.Design,
) -> np.ndarray:
    beta = np.zeros(x.shape[1], dtype=float)
    penalty = np.diag(_penalty_diagonal(design, base_alpha=12.0))
    for _ in range(60):
        linear = np.clip(x @ beta, -30.0, 30.0)
        probability = 1.0 / (1.0 + np.exp(-linear))
        variance = np.maximum(probability * (1.0 - probability), 1e-6)
        gradient = x.T @ (weights * (probability - y)) + penalty @ beta
        hessian = x.T @ ((weights * variance)[:, None] * x) + penalty
        step = np.linalg.solve(hessian, gradient)
        beta -= step
        if float(np.max(np.abs(step))) < 1e-7:
            break
    return beta


def _fit_positive_group_shrinkage(
    x: np.ndarray,
    remaining: np.ndarray,
    weights: np.ndarray,
    *,
    design: v1.Design,
) -> tuple[np.ndarray, float]:
    mask = remaining > 0.05
    xp = x[mask]
    yp = np.log1p(remaining[mask])
    wp = weights[mask]
    penalty = np.diag(_penalty_diagonal(design, base_alpha=16.0))
    beta = np.linalg.solve(xp.T @ (wp[:, None] * xp) + penalty, xp.T @ (wp * yp))
    residual = yp - xp @ beta
    variance = float(np.sum(wp * residual * residual) / max(float(np.sum(wp)), 1.0))
    return beta, max(math.sqrt(variance), 0.12)


def _prediction_rows(
    rows: Sequence[Mapping[str, Any]],
    x: np.ndarray,
    zero_beta: np.ndarray,
    positive_beta: np.ndarray,
    sigma: float,
) -> list[dict[str, Any]]:
    p_zero = v1._predict_zero(x, zero_beta)
    positive_mu = x @ positive_beta
    predictions: list[dict[str, Any]] = []
    for row, peak_probability, mu in zip(rows, p_zero, positive_mu):
        observed = float(row["observed_max_to_time_c"])
        bucket_probabilities = v1._bucket_probs(observed, float(peak_probability), float(mu), sigma)
        actual_bucket = round(float(row["final_daily_max_c"]))
        actual_index = int(np.argmin(np.abs(v1.BUCKETS - actual_bucket)))
        empirical = np.zeros(len(v1.BUCKETS))
        empirical[actual_index] = 1.0
        expected_remaining = (1.0 - float(peak_probability)) * min(
            15.0, v1._expected_positive(float(mu), sigma)
        )
        predictions.append(
            {
                "event_date_local": row["event_date_local"],
                "test_year": int(str(row["event_date_local"])[:4]),
                "month": int(row["month"]),
                "local_hour": float(row["local_hour"]),
                "observed_max_c": observed,
                "final_daily_max_c": float(row["final_daily_max_c"]),
                "remaining_warming_c": float(row["remaining_warming_c"]),
                "actual_continue": float(row["remaining_warming_c"]) > 0.05,
                "predicted_continue_probability": 1.0 - float(peak_probability),
                "expected_final_max_c": observed + expected_remaining,
                "bucket_nll": -math.log(max(float(bucket_probabilities[actual_index]), v1.EPS)),
                "bucket_rps": float(
                    np.sum((np.cumsum(bucket_probabilities) - np.cumsum(empirical)) ** 2)
                    / (len(v1.BUCKETS) - 1)
                ),
                "drop_from_observed_max_c": float(row.get("drop_from_observed_max_c") or 0.0),
                "hours_since_observed_max": float(row.get("hours_since_observed_max") or 0.0),
                "reports_below_observed_max": int(row.get("reports_below_observed_max") or 0),
                "convective_seen_today": bool(row.get("convective_seen_today")),
                "precipitation_seen_today": bool(row.get("precipitation_seen_today")),
                "post_convective_cooling": bool(row.get("post_convective_cooling")),
                "post_precipitation_cooling": bool(row.get("post_precipitation_cooling")),
                "current_precipitation_reported": bool(row.get("current_precipitation_reported")),
                "current_convective_weather": bool(
                    row.get("current_convective_weather") or row.get("convective_cloud_reported")
                ),
                "cloud_cover_pct": row.get("cloud_cover_pct"),
                "temp_dewpoint_spread_c": row.get("temp_dewpoint_spread_c"),
            }
        )
    return predictions


def _fit_group_shrinkage(
    train: list[dict[str, Any]],
    test: list[dict[str, Any]],
    *,
    version: str,
    design: v1.Design,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    table, per_day = v1._climo_table(train)
    train_climo = [
        v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=True) for row in train
    ]
    test_climo = [
        v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=False) for row in test
    ]
    x_train = design.fit_transform(train, train_climo)
    x_test = design.transform(test, test_climo)
    remaining = np.asarray([float(row["remaining_warming_c"]) for row in train])
    weights = v1._day_weights(train)
    zero_beta = _fit_logistic_group_shrinkage(
        x_train, (remaining <= 0.05).astype(float), weights, design=design
    )
    positive_beta, sigma = _fit_positive_group_shrinkage(
        x_train, remaining, weights, design=design
    )
    artifact = {
        "schema": MODEL_SCHEMA,
        "version": version,
        "feature_names": ["intercept", *design.names],
        "numeric_features": design.numeric,
        "boolean_features": design.boolean,
        "means": design.means.tolist() if design.means is not None else [],
        "scales": design.scales.tolist() if design.scales is not None else [],
        "peak_already_coefficients": zero_beta.tolist(),
        "positive_remaining_coefficients": positive_beta.tolist(),
        "positive_log1p_residual_sigma": sigma,
        "smooth_group_penalty_multiplier": SMOOTH_GROUP_PENALTY_MULTIPLIER,
        "research_only": True,
    }
    return artifact, _prediction_rows(test, x_test, zero_beta, positive_beta, sigma)


def _fit_pair(
    train: list[dict[str, Any]],
    test: list[dict[str, Any]],
    *,
    version: str,
    gfs_design: v1.Design,
    multimodel_design: v1.Design,
    group_shrinkage: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if group_shrinkage:
        gfs_artifact, gfs_predictions = _fit_group_shrinkage(
            train, test, version=f"{version}_gfs", design=gfs_design
        )
        multimodel_artifact, multimodel_predictions = _fit_group_shrinkage(
            train, test, version=f"{version}_multimodel", design=multimodel_design
        )
    else:
        _, gfs_artifact, gfs_predictions = v2._fit(
            train,
            test,
            version=f"{version}_gfs",
            design_override=gfs_design,
        )
        _, multimodel_artifact, multimodel_predictions = v2._fit(
            train,
            test,
            version=f"{version}_multimodel",
            design_override=multimodel_design,
        )
    return _baseline_v31(gfs_predictions, multimodel_predictions), gfs_artifact, multimodel_artifact


def _fold_improvement(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, float]:
    return {
        metric: 100.0 * (float(baseline[metric]) - float(candidate[metric])) / float(baseline[metric])
        for metric in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }


def _pair_gate(paired: Mapping[str, Any]) -> bool:
    for metric in ("bucket_nll", "bucket_rps"):
        interval = paired["metrics"][metric]["bootstrap_95pct_ci"]
        if interval is None or float(interval[1]) > 0.0:
            return False
    return True


def _render(report: Mapping[str, Any]) -> str:
    dedup_gain = report["all_rows"]["deduplicated_improvement_vs_original_pct"]
    original_gain = report["all_rows"]["smooth_improvement_vs_original_pct"]
    smooth_vs_dedup_gain = report["all_rows"]["smooth_improvement_vs_deduplicated_pct"]
    gate = report["release_gate"]
    return "\n".join(
        [
            "# ZUUU 去重物理基底 + 平滑峰值来源：严格 OOS",
            "",
            f"- 结论：`{gate['decision']}`；去重版 shadow：`{gate['deduplicated_ready_for_shadow']}`；平滑版 shadow：`{gate['smooth_ready_for_shadow']}`。",
            "- 新特征全时段连续计算，不以温度或本地小时阈值启停；v3.1 原有模型来源选择保持不变以隔离特征改动。",
            f"- 去重版相对原 v3.1：NLL `{dedup_gain['bucket_nll']:+.4f}%`，RPS `{dedup_gain['bucket_rps']:+.4f}%`。",
            f"- 平滑候选相对原 v3.1：NLL `{original_gain['bucket_nll']:+.4f}%`，RPS `{original_gain['bucket_rps']:+.4f}%`。",
            f"- 平滑候选相对去重基线：NLL `{smooth_vs_dedup_gain['bucket_nll']:+.4f}%`，RPS `{smooth_vs_dedup_gain['bucket_rps']:+.4f}%`。",
            f"- 双指标逐年同时获胜：相对原版 `{report['year_wins']['smooth_vs_original_nll_and_rps']}/4`，相对去重版 `{report['year_wins']['smooth_vs_deduplicated_nll_and_rps']}/4`。",
            "",
            "## 研究边界",
            "",
            "- 训练标签仍是 NCEI 小时最高温，不是 AMSC 半小时或市场最终结算标签。",
            "- 没有写入事实库、参数、信号或订单；没有 runtime 集成或进程重启。",
            "",
        ]
    )


def build_deduplicated_model_artifact(rows: list[dict[str, Any]]) -> dict[str, Any]:
    _, final_deduplicated_gfs, final_deduplicated_multimodel = _fit_pair(
        rows,
        rows,
        version="v3.1.3_deduplicated_peak_state_research",
        gfs_design=v31.AllDayDesign(DEDUP_GFS_NUMERIC, v31.BOOLEAN),
        multimodel_design=v31.AllDayDesign(DEDUP_MULTIMODEL_NUMERIC, v31.BOOLEAN),
    )
    return {
        "schema": DEDUP_MODEL_SCHEMA,
        "version": "v3.1.3_deduplicated_peak_state_research",
        "selected_candidate": "deduplicated_only",
        "rejected_candidate": VERSION,
        "research_only": True,
        "production_ready": False,
        "runtime_integrated": False,
        "trained_through_year": 2025,
        "training_sample_count": len(rows),
        "training_day_count": len({str(row["event_date_local"]) for row in rows}),
        "trained_local_hour_min": min(float(row["local_hour"]) for row in rows),
        "trained_local_hour_max": max(float(row["local_hour"]) for row in rows),
        "climo_by_day_of_year_c": v31._climo_by_doy(rows),
        "source_contract": {
            "core_models": {"gfs": "gfs_seamless", "jma": "jma_gsm"},
            "same_as_v31_except_confirmed_alias_removal": True,
        },
        "regime_policy": {
            "pre_dawn_00_05": "gfs_jma_multimodel",
            "hours_06_23": "gfs_only",
            "inherited_from_v31": True,
        },
        "deduplicated_removed_numeric": list(DEDUP_REMOVED_NUMERIC),
        "shrinkage": {
            "standardized_features": True,
            "peak_already_ridge_alpha": 12.0,
            "positive_remaining_ridge_alpha": 16.0,
            "hyperparameter_search": False,
        },
        "candidate_feature_gating": "none",
        "inherited_v31_source_regime_selection_unchanged": True,
        "submodels": {
            "gfs_only": final_deduplicated_gfs,
            "gfs_jma_multimodel": final_deduplicated_multimodel,
        },
    }


def evaluate(features_path: Path, *, bootstrap_replicates: int) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = v31._load(features_path)
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    if years != [2021, 2022, 2023, 2024, 2025]:
        raise ValueError(f"unexpected_training_years:{years}")

    predictions: dict[str, list[dict[str, Any]]] = {"original_v31": [], "deduplicated": [], "smooth": []}
    folds: list[dict[str, Any]] = []
    for test_year in years[1:]:
        train = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]

        original, _, _ = _fit_pair(
            train,
            test,
            version="v31_original",
            gfs_design=v31.AllDayDesign(v31.GFS_NUMERIC, v31.BOOLEAN),
            multimodel_design=v31.AllDayDesign(v31.MULTIMODEL_NUMERIC, v31.BOOLEAN),
        )
        deduplicated, _, _ = _fit_pair(
            train,
            test,
            version="v313_deduplicated",
            gfs_design=v31.AllDayDesign(DEDUP_GFS_NUMERIC, v31.BOOLEAN),
            multimodel_design=v31.AllDayDesign(DEDUP_MULTIMODEL_NUMERIC, v31.BOOLEAN),
        )
        smooth, _, _ = _fit_pair(
            train,
            test,
            version=VERSION,
            gfs_design=SmoothPeakOriginDesign(SMOOTH_GFS_NUMERIC, v31.BOOLEAN),
            multimodel_design=SmoothPeakOriginDesign(SMOOTH_MULTIMODEL_NUMERIC, v31.BOOLEAN),
            group_shrinkage=True,
        )
        for name, values in (("original_v31", original), ("deduplicated", deduplicated), ("smooth", smooth)):
            predictions[name].extend(values)

        summaries = {name: v2._weighted_summary(values) for name, values in (
            ("original_v31", original),
            ("deduplicated", deduplicated),
            ("smooth", smooth),
        )}
        folds.append(
            {
                "test_year": test_year,
                "train_day_count": len({str(row["event_date_local"]) for row in train}),
                "test_day_count": len({str(row["event_date_local"]) for row in test}),
                "models": summaries,
                "smooth_improvement_vs_original_pct": _fold_improvement(summaries["original_v31"], summaries["smooth"]),
                "smooth_improvement_vs_deduplicated_pct": _fold_improvement(summaries["deduplicated"], summaries["smooth"]),
            }
        )

    summaries = {name: v2._weighted_summary(values) for name, values in predictions.items()}
    paired_original = _paired_day_validation(
        predictions["original_v31"], predictions["smooth"], bootstrap_replicates=bootstrap_replicates
    )
    paired_deduplicated_vs_original = _paired_day_validation(
        predictions["original_v31"], predictions["deduplicated"], bootstrap_replicates=bootstrap_replicates
    )
    paired_deduplicated = _paired_day_validation(
        predictions["deduplicated"], predictions["smooth"], bootstrap_replicates=bootstrap_replicates
    )
    gains_deduplicated_vs_original = _improvement(
        summaries["original_v31"], summaries["deduplicated"]
    )
    gains_original = _improvement(summaries["original_v31"], summaries["smooth"])
    gains_deduplicated = _improvement(summaries["deduplicated"], summaries["smooth"])
    wins_original = sum(
        fold["models"]["smooth"]["bucket_nll"] < fold["models"]["original_v31"]["bucket_nll"]
        and fold["models"]["smooth"]["bucket_rps"] < fold["models"]["original_v31"]["bucket_rps"]
        for fold in folds
    )
    wins_deduplicated = sum(
        fold["models"]["smooth"]["bucket_nll"] < fold["models"]["deduplicated"]["bucket_nll"]
        and fold["models"]["smooth"]["bucket_rps"] < fold["models"]["deduplicated"]["bucket_rps"]
        for fold in folds
    )
    wins_deduplicated_vs_original = sum(
        fold["models"]["deduplicated"]["bucket_nll"] < fold["models"]["original_v31"]["bucket_nll"]
        and fold["models"]["deduplicated"]["bucket_rps"] < fold["models"]["original_v31"]["bucket_rps"]
        for fold in folds
    )
    deduplicated_ready_for_shadow = bool(
        gains_deduplicated_vs_original["bucket_nll"] > 0.0
        and gains_deduplicated_vs_original["bucket_rps"] > 0.0
        and wins_deduplicated_vs_original >= 3
        and _pair_gate(paired_deduplicated_vs_original)
    )
    smooth_ready_for_shadow = bool(
        gains_original["bucket_nll"] > 0.0
        and gains_original["bucket_rps"] > 0.0
        and gains_deduplicated["bucket_nll"] > 0.0
        and gains_deduplicated["bucket_rps"] > 0.0
        and wins_original >= 3
        and wins_deduplicated >= 3
        and _pair_gate(paired_original)
        and _pair_gate(paired_deduplicated)
    )
    ready_for_shadow = bool(deduplicated_ready_for_shadow or smooth_ready_for_shadow)
    if smooth_ready_for_shadow:
        decision = "advance_smooth_candidate_to_shadow_only"
    elif deduplicated_ready_for_shadow:
        decision = "advance_deduplicated_only_to_shadow"
    else:
        decision = "retain_v31_no_runtime_change"

    model_artifact = build_deduplicated_model_artifact(rows)
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "split": "strict_expanding_year_walk_forward_2022_2025",
            "pairing": "event_date_local_equal_weight_within_day",
            "timezone": "Asia/Shanghai",
            "label": "remaining warming to complete local-day NCEI hourly maximum",
            "models": ["original_v31", "deduplicated", "deduplicated_plus_smooth_peak_origin"],
            "candidate_feature_gating": "none",
            "temperature_thresholds": [],
            "clock_hour_thresholds": [],
            "solar_basis": "continuous NOAA-style station solar geometry",
            "inherited_v31_source_regime_selection_unchanged": True,
            "hyperparameter_search": False,
            "smooth_group_penalty_multiplier": SMOOTH_GROUP_PENALTY_MULTIPLIER,
            "smooth_group_multiplier_rule": "number_of_added_smooth_features",
        },
        "data_quality": {
            "feature_path": str(features_path),
            "row_count": len(rows),
            "day_count": len({str(row["event_date_local"]) for row in rows}),
            "years": years,
        },
        "basis": {
            "deduplicated_removed_numeric": list(DEDUP_REMOVED_NUMERIC),
            "smooth_numeric": list(SMOOTH_NUMERIC),
            "deduplicated_gfs_numeric_count": len(DEDUP_GFS_NUMERIC),
            "smooth_gfs_numeric_count": len(SMOOTH_GFS_NUMERIC),
            "deduplicated_multimodel_numeric_count": len(DEDUP_MULTIMODEL_NUMERIC),
            "smooth_multimodel_numeric_count": len(SMOOTH_MULTIMODEL_NUMERIC),
        },
        "folds": folds,
        "all_rows": {
            "models": summaries,
            "deduplicated_improvement_vs_original_pct": gains_deduplicated_vs_original,
            "smooth_improvement_vs_original_pct": gains_original,
            "smooth_improvement_vs_deduplicated_pct": gains_deduplicated,
        },
        "paired_day_validation": {
            "deduplicated_vs_original": paired_deduplicated_vs_original,
            "smooth_vs_original": paired_original,
            "smooth_vs_deduplicated": paired_deduplicated,
        },
        "year_wins": {
            "deduplicated_vs_original_nll_and_rps": wins_deduplicated_vs_original,
            "smooth_vs_original_nll_and_rps": wins_original,
            "smooth_vs_deduplicated_nll_and_rps": wins_deduplicated,
            "required": 3,
        },
        "release_gate": {
            "ready_for_shadow": ready_for_shadow,
            "deduplicated_ready_for_shadow": deduplicated_ready_for_shadow,
            "smooth_ready_for_shadow": smooth_ready_for_shadow,
            "production_ready": False,
            "decision": decision,
            "required": [
                "deduplicated-only candidate: NLL/RPS improve, win at least 3 of 4 years, paired-day 95% upper bounds <= 0",
                "all-row NLL and RPS improve versus original v3.1",
                "all-row NLL and RPS improve versus deduplicated baseline",
                "both NLL and RPS win in at least 3 of 4 OOS years for both comparisons",
                "paired-day bootstrap 95% upper bound is <= 0 for NLL and RPS in both comparisons",
            ],
            "production_blockers": [
                "amsc_half_hour_and_settlement_label_validation_required",
                "live_shadow_stability_not_observed",
                "production_replacement_not_authorized_by_this_research_audit",
            ],
        },
        "guards": {
            "historical_backfill": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_integration": False,
        },
    }
    return report, model_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, help="Optional research-only output directory.")
    parser.add_argument("--bootstrap-replicates", type=int, default=2000)
    parser.add_argument("--artifact-only", action="store_true", help="Fit only the selected deduplicated artifact.")
    args = parser.parse_args()
    if args.artifact_only:
        if not args.output_dir:
            parser.error("--artifact-only requires --output-dir")
        rows = v31._load(args.features)
        artifact = build_deduplicated_model_artifact(rows)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "model_artifact.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "artifact_only": True,
                    "selected_candidate": artifact["selected_candidate"],
                    "output": str(args.output_dir / "model_artifact.json"),
                    "production_ready": artifact["production_ready"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    report, artifact = evaluate(args.features, bootstrap_replicates=max(100, int(args.bootstrap_replicates)))
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "smooth_peak_origin_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (args.output_dir / "smooth_peak_origin_report.md").write_text(_render(report), encoding="utf-8")
        (args.output_dir / "model_artifact.json").write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "ok": True,
                "decision": report["release_gate"]["decision"],
                "ready_for_shadow": report["release_gate"]["ready_for_shadow"],
                "deduplicated_ready_for_shadow": report["release_gate"]["deduplicated_ready_for_shadow"],
                "smooth_ready_for_shadow": report["release_gate"]["smooth_ready_for_shadow"],
                "deduplicated_improvement_vs_original_pct": report["all_rows"]["deduplicated_improvement_vs_original_pct"],
                "smooth_improvement_vs_original_pct": report["all_rows"]["smooth_improvement_vs_original_pct"],
                "smooth_improvement_vs_deduplicated_pct": report["all_rows"]["smooth_improvement_vs_deduplicated_pct"],
                "year_wins": report["year_wins"],
                "output_dir": str(args.output_dir) if args.output_dir else None,
                "guards": report["guards"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
