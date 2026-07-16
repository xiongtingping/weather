#!/usr/bin/env python3
"""Train and compare the de-duplicated ZUUU hierarchical Bayes v4.1 model.

The climate/basin layer is fitted from context-only fields.  The station update
does not re-consume raw synoptic fields; it receives only a compact prior state
and causal station evidence.  Prior features used to train the update are
strictly out-of-fold by local date.  Evaluation uses the same expanding-year
outer folds as v3 and a paired, year-stratified bootstrap over local dates.

This is a bounded research writer.  It never writes production databases,
signals, strategy parameters, or launchd configuration.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2  # noqa: E402
from scripts import train_ncei_zuuu_hierarchical_joint_bayes_v4 as v4  # noqa: E402
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1  # noqa: E402
from scripts import train_ncei_zuuu_synoptic_bayes_v3 as v3  # noqa: E402


SCHEMA = "ncei_zuuu_hierarchical_bayes_v41_walk_forward_v1"
MODEL_SCHEMA = "ncei_zuuu_hierarchical_bayes_artifact_v41"
PAIR_SCHEMA = "ncei_zuuu_v41_vs_v3_paired_day_metrics_v1"

# Season and basin fields are represented by the context prior.  Local hour
# and solar position remain in the update because they describe the observation
# state, not the large-scale climate background.
CONTEXT_DUPLICATES_REMOVED = {
    "day_of_year_sin",
    "day_of_year_cos",
    "climo_prior_mean_c",
}
PHYSICAL_UPDATE_NUMERIC = [
    name for name in v2.V2_NUMERIC if name not in CONTEXT_DUPLICATES_REMOVED
]

# A Normal context prior relative to the observed maximum is fully represented
# by standardized distance and scale.  Mean, raw gap, CDF and raw synoptic
# fields are deliberately not repeated in the update.
PRIOR_STATE_NUMERIC = [
    "context_prior_z_gap_to_observed_max",
    "context_prior_log_sigma",
]
PRIOR_PHYSICAL_INTERACTIONS = [
    "prior_z_x_temperature_momentum",
    "prior_z_x_peak_age",
    "prior_z_x_peak_drop",
    "prior_z_x_cloud",
    "prior_z_x_precip_or_convection",
]
V41_NUMERIC = PHYSICAL_UPDATE_NUMERIC + PRIOR_STATE_NUMERIC + PRIOR_PHYSICAL_INTERACTIONS
V41_BOOLEAN = list(v2.V2_BOOLEAN)

GROUP_MULTIPLIERS = {
    "physical": 1.0,
    "prior_state": 1.25,
    "prior_physical_interaction": 4.0,
    "missing_indicator": 2.5,
    "weather_boolean": 1.5,
}
LOGISTIC_ALPHA = 12.0
POSITIVE_ALPHA = 16.0
DEFAULT_CROSSFIT_FOLDS = 5
DEFAULT_BOOTSTRAP_RESAMPLES = 20_000
DEFAULT_BOOTSTRAP_SEED = 4_102_026


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _augment_prior_state(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    mean_c = v1._number(row.get("context_prior_mean_final_c"))
    sigma_c = v1._number(row.get("context_prior_sigma_c"))
    observed_c = v1._number(row.get("observed_max_to_time_c"))
    if mean_c is None or sigma_c is None or observed_c is None or sigma_c <= 0.0:
        result["context_prior_z_gap_to_observed_max"] = None
        result["context_prior_log_sigma"] = None
    else:
        result["context_prior_z_gap_to_observed_max"] = (mean_c - observed_c) / sigma_c
        result["context_prior_log_sigma"] = math.log(max(sigma_c, 0.35))
    return result


def _v41_values(row: Mapping[str, Any], climo: float) -> dict[str, float | None]:
    values = dict(v2._values(row, climo))
    z_gap = v1._number(row.get("context_prior_z_gap_to_observed_max"))
    log_sigma = v1._number(row.get("context_prior_log_sigma"))
    momentum = v1._number(row.get("temperature_delta_1h"))
    peak_age = v1._number(row.get("hours_since_observed_max"))
    drop = v1._number(row.get("drop_from_observed_max_c"))
    cloud = v1._number(row.get("cloud_cover_pct"))
    active_weather = 1.0 if (
        row.get("current_precipitation_reported")
        or row.get("current_convective_weather")
        or row.get("convective_cloud_reported")
    ) else 0.0
    values.update(
        {
            "context_prior_z_gap_to_observed_max": z_gap,
            "context_prior_log_sigma": log_sigma,
            "prior_z_x_temperature_momentum": (
                z_gap * momentum if z_gap is not None and momentum is not None else None
            ),
            "prior_z_x_peak_age": z_gap * peak_age if z_gap is not None and peak_age is not None else None,
            "prior_z_x_peak_drop": z_gap * drop if z_gap is not None and drop is not None else None,
            "prior_z_x_cloud": z_gap * cloud / 100.0 if z_gap is not None and cloud is not None else None,
            "prior_z_x_precip_or_convection": z_gap * active_weather if z_gap is not None else None,
        }
    )
    return values


class V41UpdateDesign(v1.Design):
    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix: list[list[float]] = []
        for row, climo in zip(rows, climo_values):
            values = _v41_values(row, climo)
            numeric = [
                float(values[name]) if values.get(name) is not None else float("nan")
                for name in self.numeric
            ]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values.get(name) or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _feature_group(name: str) -> str:
    if name == "intercept":
        return "intercept"
    if name.endswith("__missing"):
        return "missing_indicator"
    if name in PRIOR_PHYSICAL_INTERACTIONS:
        return "prior_physical_interaction"
    if name in PRIOR_STATE_NUMERIC:
        return "prior_state"
    if name in V41_BOOLEAN:
        return "weather_boolean"
    return "physical"


def _penalty_vector(feature_names: Sequence[str], base_alpha: float) -> np.ndarray:
    penalty = np.asarray(
        [
            0.01 if name == "intercept" else base_alpha * GROUP_MULTIPLIERS[_feature_group(name)]
            for name in feature_names
        ],
        dtype=float,
    )
    return penalty


def _group_inventory(feature_names: Sequence[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for name in feature_names:
        groups[_feature_group(name)].append(name)
    return dict(groups)


def _fit_group_logistic(
    x: np.ndarray,
    y: np.ndarray,
    weights: np.ndarray,
    penalty_diagonal: np.ndarray,
) -> np.ndarray:
    beta = np.zeros(x.shape[1], dtype=float)
    penalty = np.diag(penalty_diagonal)
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


def _fit_group_positive(
    x: np.ndarray,
    remaining: np.ndarray,
    weights: np.ndarray,
    penalty_diagonal: np.ndarray,
) -> tuple[np.ndarray, float]:
    mask = remaining > 0.05
    xp = x[mask]
    yp = np.log1p(remaining[mask])
    wp = weights[mask]
    penalty = np.diag(penalty_diagonal)
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
    *,
    version: str,
) -> list[dict[str, Any]]:
    p_zero = v1._predict_zero(x, zero_beta)
    positive_mu = x @ positive_beta
    predictions: list[dict[str, Any]] = []
    for row, peak_probability, mu in zip(rows, p_zero, positive_mu):
        observed = float(row["observed_max_to_time_c"])
        final_max = float(row["final_daily_max_c"])
        bucket_probabilities = v1._bucket_probs(observed, float(peak_probability), float(mu), sigma)
        actual_index = int(np.argmin(np.abs(v1.BUCKETS - round(final_max))))
        empirical = np.zeros(len(v1.BUCKETS))
        empirical[actual_index] = 1.0
        expected_remaining = (1.0 - float(peak_probability)) * min(
            15.0, v1._expected_positive(float(mu), sigma)
        )
        actual_continue = float(row["remaining_warming_c"]) > 0.05
        predictions.append(
            {
                "version": version,
                "event_date_local": str(row["event_date_local"]),
                "test_year": int(str(row["event_date_local"])[:4]),
                "month": int(row["month"]),
                "local_hour": float(row["local_hour"]),
                "observed_max_c": observed,
                "final_daily_max_c": final_max,
                "remaining_warming_c": float(row["remaining_warming_c"]),
                "actual_continue": actual_continue,
                "predicted_continue_probability": 1.0 - float(peak_probability),
                "expected_final_max_c": observed + expected_remaining,
                "bucket_nll": -math.log(max(float(bucket_probabilities[actual_index]), v1.EPS)),
                "bucket_rps": float(
                    np.sum((np.cumsum(bucket_probabilities) - np.cumsum(empirical)) ** 2)
                    / (len(v1.BUCKETS) - 1)
                ),
            }
        )
    return predictions


def _fit_grouped_update(
    train_rows: list[dict[str, Any]],
    test_rows: list[dict[str, Any]],
    *,
    version: str,
) -> tuple[dict[str, float], dict[str, Any], list[dict[str, Any]]]:
    table, per_day = v1._climo_table(train_rows)
    train_climo = [
        v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=True)
        for row in train_rows
    ]
    test_climo = [
        v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=False)
        for row in test_rows
    ]
    design = V41UpdateDesign(V41_NUMERIC, V41_BOOLEAN)
    x_train = design.fit_transform(train_rows, train_climo)
    x_test = design.transform(test_rows, test_climo)
    feature_names = ["intercept", *design.names]
    remaining = np.asarray([float(row["remaining_warming_c"]) for row in train_rows])
    weights = v1._day_weights(train_rows)
    logistic_penalty = _penalty_vector(feature_names, LOGISTIC_ALPHA)
    positive_penalty = _penalty_vector(feature_names, POSITIVE_ALPHA)
    zero_beta = _fit_group_logistic(
        x_train, (remaining <= 0.05).astype(float), weights, logistic_penalty
    )
    positive_beta, sigma = _fit_group_positive(
        x_train, remaining, weights, positive_penalty
    )
    metrics = v1._metrics(test_rows, x_test, zero_beta, positive_beta, sigma)
    predictions = _prediction_rows(
        test_rows, x_test, zero_beta, positive_beta, sigma, version=version
    )
    artifact = {
        "schema": MODEL_SCHEMA,
        "version": version,
        "feature_names": feature_names,
        "numeric_features": list(design.numeric),
        "boolean_features": list(design.boolean),
        "means": design.means.tolist() if design.means is not None else [],
        "scales": design.scales.tolist() if design.scales is not None else [],
        "peak_already_coefficients": zero_beta.tolist(),
        "positive_remaining_coefficients": positive_beta.tolist(),
        "positive_log1p_residual_sigma": sigma,
        "group_penalty_multipliers": dict(GROUP_MULTIPLIERS),
        "group_base_alpha": {
            "peak_already_logistic": LOGISTIC_ALPHA,
            "positive_remaining_ridge": POSITIVE_ALPHA,
        },
        "coefficient_groups": _group_inventory(feature_names),
        "research_only": True,
    }
    return metrics, artifact, predictions


def _day_hash(day: str) -> bytes:
    return hashlib.sha256(day.encode("utf-8")).digest()


def _crossfit_context_prior(
    rows: list[dict[str, Any]], *, folds: int = DEFAULT_CROSSFIT_FOLDS
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    days = sorted({str(row["event_date_local"]) for row in rows})
    if folds < 2 or len(days) < folds * 2:
        raise ValueError(f"insufficient days for {folds}-fold context crossfit: {len(days)}")
    # Hash ordering prevents calendar/season blocks from lining up with fold
    # numbers; round-robin assignment guarantees every fold is populated.
    shuffled_days = sorted(days, key=lambda day: (_day_hash(day), day))
    assignments = {day: index % folds for index, day in enumerate(shuffled_days)}
    augmented_by_identity: dict[int, dict[str, Any]] = {}
    fold_audits: list[dict[str, Any]] = []
    for fold in range(folds):
        fit_rows = [row for row in rows if assignments[str(row["event_date_local"])] != fold]
        holdout_rows = [row for row in rows if assignments[str(row["event_date_local"])] == fold]
        if not fit_rows or not holdout_rows:
            raise RuntimeError(f"empty context crossfit partition: fold={fold}")
        artifact, _, holdout_augmented, _, _ = v4._fit_context_prior(fit_rows, holdout_rows)
        fit_days = {str(row["event_date_local"]) for row in fit_rows}
        holdout_days = {str(row["event_date_local"]) for row in holdout_rows}
        overlap = fit_days & holdout_days
        if overlap:
            raise RuntimeError(f"context prior day leakage: fold={fold} overlap={sorted(overlap)[:3]}")
        for raw, augmented in zip(holdout_rows, holdout_augmented):
            augmented_by_identity[id(raw)] = _augment_prior_state(augmented)
        fold_audits.append(
            {
                "fold": fold,
                "fit_day_count": len(fit_days),
                "holdout_day_count": len(holdout_days),
                "holdout_row_count": len(holdout_rows),
                "day_overlap_count": 0,
                "prior_sigma_c": float(artifact["residual_sigma_c"]),
            }
        )
    augmented = [augmented_by_identity[id(row)] for row in rows]
    return augmented, {
        "schema": "context_prior_grouped_date_crossfit_v1",
        "fold_count": folds,
        "row_count": len(rows),
        "day_count": len(days),
        "covered_row_count": len(augmented_by_identity),
        "same_day_leakage_count": 0,
        "all_rows_covered_once": len(augmented_by_identity) == len(rows),
        "folds": fold_audits,
    }


def _fit_context_for_test(
    train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    artifact, _, augmented, _, _ = v4._fit_context_prior(train_rows, test_rows)
    return artifact, [_augment_prior_state(row) for row in augmented]


def _paired_day_rows(
    baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if len(baseline) != len(candidate):
        raise ValueError("paired prediction length mismatch")
    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    for left, right in zip(baseline, candidate):
        identity_left = (str(left["event_date_local"]), float(left["local_hour"]))
        identity_right = (str(right["event_date_local"]), float(right["local_hour"]))
        if identity_left != identity_right:
            raise ValueError(f"paired identity mismatch: {identity_left} != {identity_right}")
        actual = float(left["final_daily_max_c"])
        if actual != float(right["final_daily_max_c"]):
            raise ValueError(f"paired target mismatch: {identity_left}")
        actual_continue = 1.0 if left["actual_continue"] else 0.0
        grouped[identity_left[0]].append(
            {
                "nll_v3": float(left["bucket_nll"]),
                "nll_v41": float(right["bucket_nll"]),
                "rps_v3": float(left["bucket_rps"]),
                "rps_v41": float(right["bucket_rps"]),
                "mae_v3": abs(float(left["expected_final_max_c"]) - actual),
                "mae_v41": abs(float(right["expected_final_max_c"]) - actual),
                "brier_v3": (float(left["predicted_continue_probability"]) - actual_continue) ** 2,
                "brier_v41": (float(right["predicted_continue_probability"]) - actual_continue) ** 2,
            }
        )
    result: list[dict[str, Any]] = []
    for day in sorted(grouped):
        records = grouped[day]
        row: dict[str, Any] = {
            "schema": PAIR_SCHEMA,
            "event_date_local": day,
            "test_year": int(day[:4]),
            "hour_state_count": len(records),
        }
        for metric in ("nll", "rps", "mae", "brier"):
            v3_value = float(np.mean([item[f"{metric}_v3"] for item in records]))
            v41_value = float(np.mean([item[f"{metric}_v41"] for item in records]))
            row[f"{metric}_v3"] = v3_value
            row[f"{metric}_v41"] = v41_value
            row[f"{metric}_delta_v41_minus_v3"] = v41_value - v3_value
        result.append(row)
    return result


def _stratified_bootstrap_metric(
    day_rows: Sequence[Mapping[str, Any]],
    metric: str,
    *,
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    field = f"{metric}_delta_v41_minus_v3"
    values_by_year: dict[int, np.ndarray] = {}
    for year in sorted({int(row["test_year"]) for row in day_rows}):
        values_by_year[year] = np.asarray(
            [float(row[field]) for row in day_rows if int(row["test_year"]) == year],
            dtype=float,
        )
    total = sum(len(values) for values in values_by_year.values())
    rng = np.random.default_rng(seed)
    bootstrap = np.zeros(resamples, dtype=float)
    for values in values_by_year.values():
        sampled = rng.choice(values, size=(resamples, len(values)), replace=True)
        bootstrap += (len(values) / total) * np.mean(sampled, axis=1)
    raw = np.asarray([float(row[field]) for row in day_rows], dtype=float)
    return {
        "metric": metric,
        "day_count": len(raw),
        "mean_delta_v41_minus_v3": float(np.mean(raw)),
        "median_day_delta": float(np.median(raw)),
        "candidate_day_win_rate": float(np.mean(raw < 0.0)),
        "ci95_lower": float(np.quantile(bootstrap, 0.025)),
        "ci95_upper": float(np.quantile(bootstrap, 0.975)),
        "bootstrap_resamples": resamples,
        "bootstrap_seed": seed,
        "stratified_by_test_year": True,
    }


def _paired_validation(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    *,
    resamples: int,
    seed: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    day_rows = _paired_day_rows(baseline, candidate)
    metrics = {
        metric: _stratified_bootstrap_metric(
            day_rows, metric, resamples=resamples, seed=seed + index
        )
        for index, metric in enumerate(("nll", "rps", "mae", "brier"))
    }
    primary_significant = all(
        metrics[name]["mean_delta_v41_minus_v3"] < 0.0
        and metrics[name]["ci95_upper"] < 0.0
        for name in ("nll", "rps")
    )
    all_point_metrics_better = all(
        metrics[name]["mean_delta_v41_minus_v3"] < 0.0 for name in metrics
    )
    replace_v3 = bool(primary_significant and all_point_metrics_better)
    return {
        "schema": "ncei_zuuu_v41_vs_v3_paired_validation_v1",
        "pairing_grain": "event_date_local",
        "test_years": sorted({int(row["test_year"]) for row in day_rows}),
        "day_count": len(day_rows),
        "metrics": metrics,
        "release_rule": {
            "primary_metrics": ["nll", "rps"],
            "primary_require_mean_below_zero": True,
            "primary_require_ci95_upper_below_zero": True,
            "secondary_metrics": ["mae", "brier"],
            "secondary_require_mean_below_zero": True,
        },
        "primary_metrics_significantly_better": primary_significant,
        "all_point_metrics_better": all_point_metrics_better,
        "replace_v3_on_accuracy": replace_v3,
        "selected_accuracy_version": "v4.1" if replace_v3 else "v3",
    }, day_rows


def _metric_gain(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, float]:
    keys = ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    return {
        key: 100.0 * (float(old[key]) - float(new[key])) / float(old[key])
        for key in keys
    }


def _render_report(report: Mapping[str, Any]) -> str:
    aggregate = report["aggregate"]
    gain = report["v41_vs_v3_improvement_pct"]
    paired = report["paired_validation"]
    lines = [
        "# ZUUU 去重复分层 Bayes v4.1：按日配对验证",
        "",
        "## 结论",
        "",
        f"- 精度选择：`{paired['selected_accuracy_version']}`；v4.1 是否替代v3：`{paired['replace_v3_on_accuracy']}`。",
        f"- v4.1相对v3：NLL `{gain['bucket_nll']:+.3f}%`、RPS `{gain['bucket_rps']:+.3f}%`、MAE `{gain['mae_c']:+.3f}%`、Brier `{gain['continue_brier']:+.3f}%`。",
        f"- 配对范围：`{paired['day_count']}`个独立日期，测试年 `{paired['test_years']}`，按测试年分层bootstrap。",
        "",
        "## 结构",
        "",
        "- 上下文先验只读季节与四川盆地形势；机场温度、露点、云雨、风和气压不进入先验。",
        "- 更新层不再读取任何原始850/700/500 hPa字段，也不重复读取季节气候均值。",
        "- 更新层只接收先验标准化距离、先验尺度、机场物理状态和5个重收缩的先验×物理交互。",
        "- 更新层训练先验按本地日期整组交叉拟合；同一天全部小时状态共同留出。",
        "",
        "## 总体指标",
        "",
        "| 模型 | NLL | RPS | MAE °C | Brier |",
        "|---|---:|---:|---:|---:|",
        f"| v3 | {aggregate['v3']['bucket_nll']:.6f} | {aggregate['v3']['bucket_rps']:.8f} | {aggregate['v3']['mae_c']:.6f} | {aggregate['v3']['continue_brier']:.6f} |",
        f"| v4.1 | {aggregate['v41']['bucket_nll']:.6f} | {aggregate['v41']['bucket_rps']:.8f} | {aggregate['v41']['mae_c']:.6f} | {aggregate['v41']['continue_brier']:.6f} |",
        "",
        "## 生产边界",
        "",
        "- 研究精度胜出不等于可上线；完整v3/v4.1仍需要同分布实时盆地形势源。",
        "- NOAA PSL R2当前2026压力层只到2月28日，不能作为7月实时输入。",
        "- 本脚本不写archive.db、事实层、策略参数、信号或订单。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--crossfit-folds", type=int, default=DEFAULT_CROSSFIT_FOLDS)
    parser.add_argument("--bootstrap-resamples", type=int, default=DEFAULT_BOOTSTRAP_RESAMPLES)
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    args = parser.parse_args()

    rows = v1._load(args.features)
    if not rows or not all(row.get("synoptic_join_available") for row in rows):
        raise SystemExit("synoptic coverage missing")
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    folds: list[dict[str, Any]] = []
    predictions: dict[str, list[dict[str, Any]]] = {"v3": [], "v41": []}
    for test_year in years[1:]:
        train_rows = [
            row for row in rows if int(str(row["event_date_local"])[:4]) < test_year
        ]
        test_rows = [
            row for row in rows if int(str(row["event_date_local"])[:4]) == test_year
        ]
        train_oof, crossfit_audit = _crossfit_context_prior(
            train_rows, folds=args.crossfit_folds
        )
        test_prior_artifact, test_augmented = _fit_context_for_test(train_rows, test_rows)
        v3_metrics, _, v3_predictions = v2._fit(
            train_rows,
            test_rows,
            version="v3",
            design_override=v3.V3Design(v3.V3_NUMERIC, v3.V3_BOOLEAN),
        )
        v41_metrics, _, v41_predictions = _fit_grouped_update(
            train_oof, test_augmented, version="v4.1"
        )
        predictions["v3"].extend(v3_predictions)
        predictions["v41"].extend(v41_predictions)
        folds.append(
            {
                "test_year": test_year,
                "train_day_count": len({row["event_date_local"] for row in train_rows}),
                "test_day_count": len({row["event_date_local"] for row in test_rows}),
                "v3": v3_metrics,
                "v41": v41_metrics,
                "v41_vs_v3_improvement_pct": _metric_gain(
                    {
                        **v3_metrics,
                        "continue_brier": v3_metrics["peak_already_brier"],
                    },
                    {
                        **v41_metrics,
                        "continue_brier": v41_metrics["peak_already_brier"],
                    },
                ),
                "context_prior_crossfit": crossfit_audit,
                "test_context_prior_sigma_c": float(
                    test_prior_artifact["residual_sigma_c"]
                ),
            }
        )

    aggregate = {
        "v3": v2._weighted_summary(predictions["v3"]),
        "v41": v2._weighted_summary(predictions["v41"]),
    }
    paired_validation, paired_day_rows = _paired_validation(
        predictions["v3"],
        predictions["v41"],
        resamples=args.bootstrap_resamples,
        seed=args.bootstrap_seed,
    )
    final_context_artifact, _, _, _, _ = v4._fit_context_prior(rows, rows)
    final_oof_rows, final_crossfit_audit = _crossfit_context_prior(
        rows, folds=args.crossfit_folds
    )
    _, final_update_artifact, _ = _fit_grouped_update(
        final_oof_rows, final_oof_rows, version="v4.1"
    )
    accuracy_selected = str(paired_validation["selected_accuracy_version"])
    model_artifact = {
        "schema": MODEL_SCHEMA,
        "version": "v4.1",
        "trained_through_year": max(years),
        "training_sample_count": len(rows),
        "context_prior": final_context_artifact,
        "physical_update": final_update_artifact,
        "training_prior_crossfit": final_crossfit_audit,
        "layer_contract": {
            "context_prior_excludes_station_observations": True,
            "update_excludes_raw_synoptic_features": True,
            "update_excludes_seasonal_context_duplicates": sorted(
                CONTEXT_DUPLICATES_REMOVED
            ),
            "update_prior_state_features": list(PRIOR_STATE_NUMERIC),
            "prior_physical_interactions": list(PRIOR_PHYSICAL_INTERACTIONS),
            "group_penalty_multipliers": dict(GROUP_MULTIPLIERS),
            "training_prior_is_strict_out_of_fold_by_local_date": True,
        },
        "paired_accuracy_selected_version": accuracy_selected,
        "research_only": True,
        "production_ready": False,
        "production_blockers": [
            "same_distribution_realtime_synoptic_source_unavailable",
            "no_live_v3_or_v41_feature_adapter",
            "amsc_half_hour_settlement_calibration_not_completed",
        ],
    }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "method": {
            "outer_split": "strict expanding-year walk-forward",
            "training_prior": "grouped-date strict out-of-fold context predictions",
            "pairing_grain": "event_date_local",
            "bootstrap": "paired year-stratified percentile bootstrap",
            "timezone": "Asia/Shanghai",
        },
        "feature_contract": model_artifact["layer_contract"],
        "folds": folds,
        "aggregate": aggregate,
        "v41_vs_v3_improvement_pct": _metric_gain(
            aggregate["v3"], aggregate["v41"]
        ),
        "paired_validation": paired_validation,
        "release_gate": {
            "accuracy_selected_version": accuracy_selected,
            "v41_replaces_v3_on_accuracy": paired_validation[
                "replace_v3_on_accuracy"
            ],
            "production_ready": False,
            "production_blockers": model_artifact["production_blockers"],
        },
        "guards": {
            "historical_backfill": False,
            "reads_archive_db": False,
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_strategy_signal": False,
            "places_orders": False,
            "changes_parameters": False,
            "restarts_launchd": False,
            "production_integration": False,
        },
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "walk_forward_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "walk_forward_report.md").write_text(
        _render_report(report), encoding="utf-8"
    )
    (args.output_dir / "model_artifact.json").write_text(
        json.dumps(model_artifact, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    with gzip.open(
        args.output_dir / "paired_day_metrics.jsonl.gz", "wt", encoding="utf-8"
    ) as handle:
        for row in paired_day_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "ok": True,
                "accuracy_selected_version": accuracy_selected,
                "v41_replaces_v3_on_accuracy": paired_validation[
                    "replace_v3_on_accuracy"
                ],
                "improvement_pct": report["v41_vs_v3_improvement_pct"],
                "paired_validation": paired_validation,
                "output_dir": str(args.output_dir),
                "guards": report["guards"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
