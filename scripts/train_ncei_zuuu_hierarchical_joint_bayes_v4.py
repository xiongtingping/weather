#!/usr/bin/env python3
"""Train a research-only two-layer ZUUU daily-high model.

Layer 1 estimates a dynamic climate/Sichuan-basin prior without station
observations.  Layer 2 updates that prior with causal METAR physical evidence
and explicit background-by-observation interactions.  Validation is strictly
expanding-year; no production state is read or written.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2  # noqa: E402
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1  # noqa: E402
from scripts import train_ncei_zuuu_synoptic_bayes_v3 as v3  # noqa: E402


SCHEMA = "ncei_zuuu_hierarchical_climate_physical_bayes_research_v4"
MODEL_SCHEMA = "ncei_zuuu_hierarchical_climate_physical_bayes_artifact_v4"

CONTEXT_NUMERIC = [
    "local_hour_sin",
    "local_hour_cos",
    "solar_heating_window",
    "day_of_year_sin",
    "day_of_year_cos",
    "climo_prior_mean_c",
    "synoptic_lag_hours",
    "basin_center_air_850_c",
    "basin_center_rhum_850_pct",
    "basin_center_rhum_700_pct",
    "basin_center_hgt_500_m",
    "basin_center_hgt_850_m",
    "basin_center_uwnd_850_mps",
    "basin_center_vwnd_850_mps",
    "basin_center_uwnd_700_mps",
    "basin_center_vwnd_700_mps",
    "basin_center_omega_700_pa_per_s",
    "synoptic_850_wind_speed_mps",
    "synoptic_700_wind_speed_mps",
    "synoptic_850_700_shear_mps",
    "synoptic_air850_west_minus_east_c",
    "synoptic_air850_north_minus_south_c",
    "synoptic_hgt500_west_minus_east_m",
    "synoptic_hgt500_north_minus_south_m",
    "synoptic_warm_advection_850_c_per_h",
    "synoptic_westerly_dry_foehn_proxy",
    "synoptic_southerly_moisture_proxy",
    "synoptic_hgt500_minus_hgt850_m",
    "interaction_air850_solar",
]

PHYSICAL_NUMERIC = [
    name
    for name in v2.V2_NUMERIC
    if name
    not in {
        "local_hour_sin",
        "local_hour_cos",
        "solar_heating_window",
        "day_of_year_sin",
        "day_of_year_cos",
        "climo_prior_mean_c",
    }
]

PRIOR_UPDATE_NUMERIC = [
    "context_prior_mean_final_c",
    "context_prior_sigma_c",
    "context_prior_gap_to_observed_max_c",
    "context_prior_peak_already_probability",
]

JOINT_INTERACTIONS = [
    "joint_prior_gap_peak_age",
    "joint_prior_gap_drop",
    "joint_ascent_cloud",
    "joint_rh700_cloud",
    "joint_foehn_surface_dryness",
    "joint_shear_convective_cloud",
    "joint_warm_advection_temperature_momentum",
    "joint_basin_ventilation_surface_wind",
    "joint_prior_gap_precipitation",
]

V4_NUMERIC = v3.V3_NUMERIC + PRIOR_UPDATE_NUMERIC + JOINT_INTERACTIONS
V4_BOOLEAN = v2.V2_BOOLEAN


class ContextDesign(v1.Design):
    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix = []
        for row, climo in zip(rows, climo_values):
            values = v3._values(row, climo)
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            matrix.append([*numeric, *missing])
        return np.asarray(matrix, dtype=float)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _normal_bucket_probs(mean_c: float, sigma_c: float) -> np.ndarray:
    sigma = max(float(sigma_c), 0.35)
    probs = np.zeros(len(v1.BUCKETS), dtype=float)
    for index, bucket in enumerate(v1.BUCKETS):
        low = float(bucket - 0.5)
        high = float(bucket + 0.5)
        if index == 0:
            probs[index] = _normal_cdf((high - mean_c) / sigma)
        elif index == len(v1.BUCKETS) - 1:
            probs[index] = 1.0 - _normal_cdf((low - mean_c) / sigma)
        else:
            probs[index] = _normal_cdf((high - mean_c) / sigma) - _normal_cdf((low - mean_c) / sigma)
    probs = np.maximum(probs, 0.0)
    return probs / max(float(probs.sum()), v1.EPS)


def _fit_ridge(x: np.ndarray, y: np.ndarray, weights: np.ndarray, alpha: float = 28.0) -> tuple[np.ndarray, float]:
    penalty = np.eye(x.shape[1]) * alpha
    penalty[0, 0] = 0.01
    beta = np.linalg.solve(x.T @ (weights[:, None] * x) + penalty, x.T @ (weights * y))
    residual = y - x @ beta
    variance = float(np.sum(weights * residual * residual) / max(float(np.sum(weights)), 1.0))
    return beta, max(math.sqrt(variance), 0.5)


def _fit_context_prior(
    train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    table, per_day = v1._climo_table(train_rows)
    train_climo = [v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=True) for row in train_rows]
    test_climo = [v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=False) for row in test_rows]
    design = ContextDesign(CONTEXT_NUMERIC, [])
    x_train = design.fit_transform(train_rows, train_climo)
    x_test = design.transform(test_rows, test_climo)
    y_train = np.asarray([float(row["final_daily_max_c"]) for row in train_rows])
    weights = v1._day_weights(train_rows)
    beta, sigma = _fit_ridge(x_train, y_train, weights)
    train_mean = x_train @ beta
    test_mean = x_test @ beta
    climo_residual = y_train - np.asarray(train_climo)
    climo_sigma = max(math.sqrt(float(np.sum(weights * climo_residual * climo_residual) / np.sum(weights))), 0.5)

    def augment(rows: list[dict[str, Any]], means: np.ndarray) -> list[dict[str, Any]]:
        result = []
        for row, mean_c in zip(rows, means):
            observed = float(row["observed_max_to_time_c"])
            peak_probability = _normal_cdf((observed + 0.5 - float(mean_c)) / sigma)
            copy = dict(row)
            copy.update(
                {
                    "context_prior_mean_final_c": float(mean_c),
                    "context_prior_sigma_c": sigma,
                    "context_prior_gap_to_observed_max_c": float(mean_c) - observed,
                    "context_prior_peak_already_probability": peak_probability,
                }
            )
            result.append(copy)
        return result

    prior_artifact = {
        "feature_names": ["intercept", *design.names],
        "numeric_features": design.numeric,
        "means": design.means.tolist() if design.means is not None else [],
        "scales": design.scales.tolist() if design.scales is not None else [],
        "coefficients": beta.tolist(),
        "residual_sigma_c": sigma,
        "climatology_residual_sigma_c": climo_sigma,
        "station_observation_features_excluded": True,
    }
    train_augmented = augment(train_rows, train_mean)
    test_augmented = augment(test_rows, test_mean)
    context_predictions = _prior_predictions(test_rows, test_mean, sigma, "basin_context_prior")
    climo_predictions = _prior_predictions(test_rows, np.asarray(test_climo), climo_sigma, "seasonal_climatology_prior")
    return prior_artifact, train_augmented, test_augmented, context_predictions, climo_predictions


def _prior_predictions(
    rows: Sequence[Mapping[str, Any]], means: np.ndarray, sigma: float, model: str
) -> list[dict[str, Any]]:
    predictions = []
    for row, mean_c in zip(rows, means):
        probabilities = _normal_bucket_probs(float(mean_c), sigma)
        actual = float(row["final_daily_max_c"])
        actual_index = int(np.argmin(np.abs(v1.BUCKETS - round(actual))))
        empirical = np.zeros(len(v1.BUCKETS)); empirical[actual_index] = 1.0
        predictions.append(
            {
                "model": model,
                "event_date_local": row["event_date_local"],
                "local_hour": float(row["local_hour"]),
                "month": int(row["month"]),
                "expected_final_max_c": float(mean_c),
                "final_daily_max_c": actual,
                "bucket_nll": -math.log(max(float(probabilities[actual_index]), v1.EPS)),
                "bucket_rps": float(np.sum((np.cumsum(probabilities) - np.cumsum(empirical)) ** 2) / (len(v1.BUCKETS) - 1)),
            }
        )
    return predictions


def _value(row: Mapping[str, Any], name: str, climo: float) -> float | None:
    if name in v2.V2_NUMERIC or name in v2.V2_BOOLEAN:
        return v2._values(row, climo).get(name)
    return v1._number(row.get(name))


class V4UpdateDesign(v1.Design):
    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix = []
        for row, climo in zip(rows, climo_values):
            # The update is P(physical evidence | target, basin context): raw
            # context remains available to condition the physical likelihood,
            # while the separately trained prior enters through explicit
            # distribution summaries below.
            base = v3._values(row, climo)
            prior_gap = v1._number(row.get("context_prior_gap_to_observed_max_c"))
            peak_age = v1._number(row.get("hours_since_observed_max"))
            drop = v1._number(row.get("drop_from_observed_max_c"))
            ascent = v1._number(row.get("synoptic_ascent_moisture_proxy"))
            rh700 = v1._number(row.get("basin_center_rhum_700_pct"))
            cloud = v1._number(row.get("cloud_cover_pct"))
            foehn = v1._number(row.get("synoptic_westerly_dry_foehn_proxy"))
            spread = v1._number(row.get("temp_dewpoint_spread_c"))
            shear = v1._number(row.get("synoptic_850_700_shear_mps"))
            warm_advection = v1._number(row.get("synoptic_warm_advection_850_c_per_h"))
            momentum = v1._number(row.get("temperature_delta_1h"))
            ventilation = v1._number(row.get("synoptic_850_wind_speed_mps"))
            surface_wind = v1._number(row.get("wind_speed_mps"))
            convective = 1.0 if row.get("current_convective_weather") or row.get("convective_cloud_reported") else 0.0
            precip = 1.0 if row.get("current_precipitation_reported") else 0.0
            values = dict(base)
            values.update({name: v1._number(row.get(name)) for name in PRIOR_UPDATE_NUMERIC})
            values.update(
                {
                    "joint_prior_gap_peak_age": prior_gap * peak_age if prior_gap is not None and peak_age is not None else None,
                    "joint_prior_gap_drop": prior_gap * drop if prior_gap is not None and drop is not None else None,
                    "joint_ascent_cloud": ascent * cloud if ascent is not None and cloud is not None else None,
                    "joint_rh700_cloud": rh700 * cloud / 100.0 if rh700 is not None and cloud is not None else None,
                    "joint_foehn_surface_dryness": foehn * spread if foehn is not None and spread is not None else None,
                    "joint_shear_convective_cloud": shear * convective if shear is not None else None,
                    "joint_warm_advection_temperature_momentum": warm_advection * momentum if warm_advection is not None and momentum is not None else None,
                    "joint_basin_ventilation_surface_wind": ventilation * surface_wind if ventilation is not None and surface_wind is not None else None,
                    "joint_prior_gap_precipitation": prior_gap * precip if prior_gap is not None else None,
                }
            )
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values.get(name) or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _prior_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, float | int]:
    counts = Counter(str(row["event_date_local"]) for row in rows)
    weights = np.asarray([1.0 / counts[str(row["event_date_local"])] for row in rows], dtype=float)
    weights /= weights.sum()
    actual = np.asarray([float(row["final_daily_max_c"]) for row in rows])
    predicted = np.asarray([float(row["expected_final_max_c"]) for row in rows])
    return {
        "sample_count": len(rows),
        "day_count": len(counts),
        "mae_c": float(np.sum(weights * np.abs(predicted - actual))),
        "bucket_nll": float(np.sum(weights * np.asarray([float(row["bucket_nll"]) for row in rows]))),
        "bucket_rps": float(np.sum(weights * np.asarray([float(row["bucket_rps"]) for row in rows]))),
    }


def _improvement(old: float, new: float) -> float:
    return 100.0 * (old - new) / old if old else 0.0


def _metric_gain(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, float]:
    return {key: _improvement(float(old[key]), float(new[key])) for key in ("bucket_nll", "bucket_rps", "mae_c")}


def _importance(artifact: Mapping[str, Any], coefficient_key: str, allowed: set[str], limit: int = 15) -> list[dict[str, Any]]:
    rows = []
    for name, coefficient in zip(artifact["feature_names"], artifact[coefficient_key]):
        if name in allowed:
            rows.append({"feature": name, "coefficient": round(float(coefficient), 6), "absolute": round(abs(float(coefficient)), 6)})
    return sorted(rows, key=lambda row: float(row["absolute"]), reverse=True)[:limit]


def _render(report: Mapping[str, Any]) -> str:
    prior = report["prior_improvement_pct"]
    update = report["hierarchical_vs_physical_v2_improvement_pct"]
    flat = report["hierarchical_vs_flat_v3_improvement_pct"]
    lines = [
        "# ZUUU 气候/盆地先验＋实况物理证据联合 Bayes v4",
        "",
        "## tl;dr",
        "",
        f"- 盆地动态先验相对季节气候先验：NLL `{prior['bucket_nll']:+.2f}%`、RPS `{prior['bucket_rps']:+.2f}%`、MAE `{prior['mae_c']:+.2f}%`。",
        f"- 加入实况物理更新后，相对v2实况顺序模型：NLL `{update['bucket_nll']:+.2f}%`、RPS `{update['bucket_rps']:+.2f}%`、MAE `{update['mae_c']:+.2f}%`。",
        f"- 相对v3单层混合模型：NLL `{flat['bucket_nll']:+.2f}%`、RPS `{flat['bucket_rps']:+.2f}%`、MAE `{flat['mae_c']:+.2f}%`。",
        f"- 双门研究结论：`{'通过' if report['release_gate']['research_pass'] else '未通过'}`；生产门固定关闭。",
        "",
        "## 模型顺序",
        "",
        "1. 季节日期、850/700 hPa温湿风、500 hPa高度场及盆地梯度生成每日最高温动态先验；该层不读取机场温度、露点、云雨或地面风。",
        "2. 每次METAR到达后，用温度轨迹、露点/温露差、云雨、风、能见度、气压和峰值年龄更新先验。",
        "3. 上升运动×云、700 hPa湿度×云、焚风背景×地面干燥度等交互项表达“同一天气在不同盆地背景下影响不同”。",
        "",
        "## 边界",
        "",
        "- 标签仍是NCEI小时日最高温；AMSC半小时与市场结算校准尚未达到样本门槛。",
        "- NOAA再分析是背景先验，不升级为机场硬实况；TAF不参与硬标签。",
        "- 所有结果只写research目录，不写事实库、策略参数、信号或订单。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = v1._load(args.features)
    if not rows or not all(row.get("synoptic_join_available") for row in rows):
        raise SystemExit("synoptic coverage missing")
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    folds = []
    predictions: dict[str, list[dict[str, Any]]] = {
        "seasonal_climatology_prior": [],
        "basin_context_prior": [],
        "physical_v2": [],
        "flat_v3": [],
        "hierarchical_v4": [],
    }
    for test_year in years[1:]:
        train_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        prior_artifact, train_augmented, test_augmented, context_pred, climo_pred = _fit_context_prior(train_rows, test_rows)
        physical_metrics, _, physical_pred = v2._fit(
            train_rows, test_rows, version="v2", design_override=v2.V2Design(v2.V2_NUMERIC, v2.V2_BOOLEAN)
        )
        flat_metrics, _, flat_pred = v2._fit(
            train_rows, test_rows, version="v3", design_override=v3.V3Design(v3.V3_NUMERIC, v3.V3_BOOLEAN)
        )
        hierarchical_metrics, _, hierarchical_pred = v2._fit(
            train_augmented,
            test_augmented,
            version="v4",
            design_override=V4UpdateDesign(V4_NUMERIC, V4_BOOLEAN),
        )
        predictions["seasonal_climatology_prior"].extend(climo_pred)
        predictions["basin_context_prior"].extend(context_pred)
        predictions["physical_v2"].extend(physical_pred)
        predictions["flat_v3"].extend(flat_pred)
        predictions["hierarchical_v4"].extend(hierarchical_pred)
        folds.append(
            {
                "test_year": test_year,
                "train_day_count": len({row["event_date_local"] for row in train_rows}),
                "seasonal_climatology_prior": _prior_summary(climo_pred),
                "basin_context_prior": _prior_summary(context_pred),
                "physical_v2": physical_metrics,
                "flat_v3": flat_metrics,
                "hierarchical_v4": hierarchical_metrics,
                "prior_sigma_c": prior_artifact["residual_sigma_c"],
            }
        )

    final_prior, train_augmented, _, _, _ = _fit_context_prior(rows, rows)
    _, final_update, _ = v2._fit(
        train_augmented,
        train_augmented,
        version="v4",
        design_override=V4UpdateDesign(V4_NUMERIC, V4_BOOLEAN),
    )
    aggregate = {
        "seasonal_climatology_prior": _prior_summary(predictions["seasonal_climatology_prior"]),
        "basin_context_prior": _prior_summary(predictions["basin_context_prior"]),
        "physical_v2": v2._weighted_summary(predictions["physical_v2"]),
        "flat_v3": v2._weighted_summary(predictions["flat_v3"]),
        "hierarchical_v4": v2._weighted_summary(predictions["hierarchical_v4"]),
    }
    prior_gain = _metric_gain(aggregate["seasonal_climatology_prior"], aggregate["basin_context_prior"])
    versus_physical = _metric_gain(aggregate["physical_v2"], aggregate["hierarchical_v4"])
    versus_flat = _metric_gain(aggregate["flat_v3"], aggregate["hierarchical_v4"])
    prior_year_wins = sum(
        fold["basin_context_prior"]["bucket_nll"] < fold["seasonal_climatology_prior"]["bucket_nll"]
        and fold["basin_context_prior"]["bucket_rps"] < fold["seasonal_climatology_prior"]["bucket_rps"]
        for fold in folds
    )
    update_year_wins = sum(
        fold["hierarchical_v4"]["bucket_nll"] < fold["physical_v2"]["bucket_nll"]
        and fold["hierarchical_v4"]["bucket_rps"] < fold["physical_v2"]["bucket_rps"]
        for fold in folds
    )
    research_pass = bool(
        all(prior_gain[key] > 0.0 for key in prior_gain)
        and all(versus_physical[key] > 0.0 for key in versus_physical)
        and prior_year_wins >= 3
        and update_year_wins >= 3
    )
    final_artifact = {
        "schema": MODEL_SCHEMA,
        "trained_through_year": max(years),
        "training_sample_count": len(rows),
        "context_prior": final_prior,
        "physical_update": final_update,
        "layer_contract": {
            "context_prior_excludes_station_observations": True,
            "physical_update_consumes_context_prior_summary": True,
            "explicit_context_physical_interactions": JOINT_INTERACTIONS,
            "hard_observation_floor_applied_after_probabilistic_update": True,
        },
        "research_only": True,
    }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "split": "strict expanding-year walk-forward",
            "timezone": "Asia/Shanghai",
            "prior_layer": "seasonal climate plus causally joined Sichuan-basin synoptic background; no station observation inputs",
            "update_layer": "context-prior summaries plus causal METAR physical evidence and context-by-physical interactions",
            "label": "complete local-day NCEI hourly maximum / remaining warming",
        },
        "folds": folds,
        "aggregate": aggregate,
        "prior_improvement_pct": prior_gain,
        "hierarchical_vs_physical_v2_improvement_pct": versus_physical,
        "hierarchical_vs_flat_v3_improvement_pct": versus_flat,
        "prior_year_wins_nll_and_rps": prior_year_wins,
        "update_year_wins_nll_and_rps": update_year_wins,
        "context_prior_feature_importance": _importance(final_prior, "coefficients", set(CONTEXT_NUMERIC)),
        "joint_update_feature_importance": _importance(final_update, "peak_already_coefficients", set(PRIOR_UPDATE_NUMERIC + JOINT_INTERACTIONS)),
        "release_gate": {
            "research_pass": research_pass,
            "production_ready": False,
            "production_blockers": [
                "amsc_multi_year_half_hour_history_unavailable",
                "market_settlement_calibration_missing",
                "shadow_runtime_not_evaluated",
                "fact_summary_merge_fill_mismatch_unresolved",
            ],
            "research_caveats": [] if all(versus_flat[key] >= 0.0 for key in versus_flat) else ["hierarchical_v4_does_not_dominate_flat_v3_on_every_metric"],
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
    (args.output_dir / "walk_forward_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "walk_forward_report.md").write_text(_render(report), encoding="utf-8")
    (args.output_dir / "model_artifact.json").write_text(json.dumps(final_artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "research_pass": research_pass, "prior_gain": prior_gain, "versus_physical": versus_physical, "versus_flat": versus_flat, "prior_year_wins": prior_year_wins, "update_year_wins": update_year_wins, "output_dir": str(args.output_dir), "guards": report["guards"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
