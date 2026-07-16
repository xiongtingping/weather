#!/usr/bin/env python3
"""Calibrate the ZUUU research remaining-warming model with causal path memory.

V2 adds only information available at or before each observation: elapsed time
since the observed maximum, depth and duration of cooling, and whether
convection/precipitation has already occurred.  Evaluation is expanding-year
walk-forward and the output is research-only.
"""

from __future__ import annotations

import argparse
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

from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


SCHEMA = "ncei_zuuu_remaining_warming_calibration_v2"
MODEL_SCHEMA = "ncei_zuuu_zero_inflated_bayes_artifact_v2"

V2_NUMERIC = v1.FULL_NUMERIC + [
    "local_hour_linear",
    "hours_after_17",
    "drop_from_observed_max_c",
    "hours_since_observed_max",
    "reports_below_observed_max",
    "hours_since_convective",
    "hours_since_precipitation",
    "max_cloud_cover_to_time_pct",
    "interaction_peak_age_drop",
    "interaction_late_drop",
    "interaction_late_peak_age",
]

V2_BOOLEAN = v1.FULL_BOOLEAN + [
    "convective_seen_today",
    "precipitation_seen_today",
    "post_convective_cooling",
    "post_precipitation_cooling",
]


def _values(row: Mapping[str, Any], climo: float) -> dict[str, float | None]:
    values = dict(v1._engineered(row, climo))
    hour = float(row["local_hour"])
    after_17 = max(0.0, hour - 17.0)
    drop = v1._number(row.get("drop_from_observed_max_c"))
    peak_age = v1._number(row.get("hours_since_observed_max"))
    values.update(
        {
            "local_hour_linear": hour,
            "hours_after_17": after_17,
            "drop_from_observed_max_c": drop,
            "hours_since_observed_max": peak_age,
            "reports_below_observed_max": v1._number(row.get("reports_below_observed_max")),
            "hours_since_convective": v1._number(row.get("hours_since_convective")),
            "hours_since_precipitation": v1._number(row.get("hours_since_precipitation")),
            "max_cloud_cover_to_time_pct": v1._number(row.get("max_cloud_cover_to_time_pct")),
            "interaction_peak_age_drop": v1._number(row.get("interaction_peak_age_drop")),
            "interaction_late_drop": after_17 * drop if drop is not None else None,
            "interaction_late_peak_age": after_17 * peak_age if peak_age is not None else None,
            **{name: 1.0 if row.get(name) else 0.0 for name in V2_BOOLEAN},
        }
    )
    return values


class V2Design(v1.Design):
    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix = []
        for row, climo in zip(rows, climo_values):
            values = _values(row, climo)
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values.get(name) or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _fit(
    train_rows: list[dict[str, Any]],
    test_rows: list[dict[str, Any]],
    *,
    version: str,
    design_override: v1.Design | None = None,
) -> tuple[dict[str, float], dict[str, Any], list[dict[str, Any]]]:
    table, per_day = v1._climo_table(train_rows)
    train_climo = [v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=True) for row in train_rows]
    test_climo = [v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=False) for row in test_rows]
    if design_override is not None:
        design = design_override
    elif version == "v1":
        design: v1.Design = v1.Design(v1.FULL_NUMERIC, v1.FULL_BOOLEAN)
    elif version == "v2":
        design = V2Design(V2_NUMERIC, V2_BOOLEAN)
    else:
        raise ValueError(version)
    x_train = design.fit_transform(train_rows, train_climo)
    x_test = design.transform(test_rows, test_climo)
    remaining_train = np.asarray([float(row["remaining_warming_c"]) for row in train_rows])
    train_weights = v1._day_weights(train_rows)
    zero_beta = v1._fit_logistic(x_train, (remaining_train <= 0.05).astype(float), train_weights)
    positive_beta, sigma = v1._fit_positive(x_train, remaining_train, train_weights)
    metrics = v1._metrics(test_rows, x_test, zero_beta, positive_beta, sigma)
    p_zero = v1._predict_zero(x_test, zero_beta)
    positive_mu = x_test @ positive_beta
    predictions: list[dict[str, Any]] = []
    for row, peak_probability, mu in zip(test_rows, p_zero, positive_mu):
        observed = float(row["observed_max_to_time_c"])
        bucket_probabilities = v1._bucket_probs(observed, float(peak_probability), float(mu), sigma)
        actual_bucket = round(float(row["final_daily_max_c"]))
        actual_index = int(np.argmin(np.abs(v1.BUCKETS - actual_bucket)))
        empirical = np.zeros(len(v1.BUCKETS)); empirical[actual_index] = 1.0
        expected_remaining = (1.0 - float(peak_probability)) * min(15.0, v1._expected_positive(float(mu), sigma))
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
                "bucket_rps": float(np.sum((np.cumsum(bucket_probabilities) - np.cumsum(empirical)) ** 2) / (len(v1.BUCKETS) - 1)),
                "drop_from_observed_max_c": float(row.get("drop_from_observed_max_c") or 0.0),
                "hours_since_observed_max": float(row.get("hours_since_observed_max") or 0.0),
                "reports_below_observed_max": int(row.get("reports_below_observed_max") or 0),
                "convective_seen_today": bool(row.get("convective_seen_today")),
                "precipitation_seen_today": bool(row.get("precipitation_seen_today")),
                "post_convective_cooling": bool(row.get("post_convective_cooling")),
                "post_precipitation_cooling": bool(row.get("post_precipitation_cooling")),
                "current_precipitation_reported": bool(row.get("current_precipitation_reported")),
                "current_convective_weather": bool(row.get("current_convective_weather") or row.get("convective_cloud_reported")),
                "cloud_cover_pct": row.get("cloud_cover_pct"),
                "temp_dewpoint_spread_c": row.get("temp_dewpoint_spread_c"),
            }
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
        "research_only": True,
    }
    return metrics, artifact, predictions


def _weighted_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, float | int]:
    if not rows:
        return {"sample_count": 0, "day_count": 0}
    counts = Counter(str(row["event_date_local"]) for row in rows)
    weights = np.asarray([1.0 / counts[str(row["event_date_local"])] for row in rows], dtype=float)
    weights /= weights.sum()
    actual_continue = np.asarray([1.0 if row["actual_continue"] else 0.0 for row in rows])
    predicted_continue = np.asarray([float(row["predicted_continue_probability"]) for row in rows])
    actual_max = np.asarray([float(row["final_daily_max_c"]) for row in rows])
    predicted_max = np.asarray([float(row["expected_final_max_c"]) for row in rows])
    actual_continue_count = int(np.sum(actual_continue))
    day_count = len(counts)
    return {
        "sample_count": len(rows),
        "day_count": day_count,
        "actual_continue_count": actual_continue_count,
        "zero_event_95pct_upper_rate": (
            float(1.0 - math.pow(0.05, 1.0 / day_count))
            if actual_continue_count == 0 and day_count > 0
            else None
        ),
        "actual_continue_rate": float(np.sum(weights * actual_continue)),
        "predicted_continue_rate": float(np.sum(weights * predicted_continue)),
        "continue_calibration_gap_pct": float(100.0 * np.sum(weights * (predicted_continue - actual_continue))),
        "continue_brier": float(np.sum(weights * (predicted_continue - actual_continue) ** 2)),
        "mae_c": float(np.sum(weights * np.abs(predicted_max - actual_max))),
        "bucket_nll": float(np.sum(weights * np.asarray([float(row["bucket_nll"]) for row in rows]))),
        "bucket_rps": float(np.sum(weights * np.asarray([float(row["bucket_rps"]) for row in rows]))),
    }


def _segments(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float | int]]:
    definitions = {
        "all": lambda row: True,
        "hot_season_all": lambda row: 5 <= row["month"] <= 9,
        "hot_season_peak_window_10_17": lambda row: 5 <= row["month"] <= 9 and 10.0 <= row["local_hour"] <= 17.0,
        "hot_season_current_convective": lambda row: 5 <= row["month"] <= 9 and bool(row["current_convective_weather"]),
        "hot_season_current_precipitation": lambda row: 5 <= row["month"] <= 9 and bool(row["current_precipitation_reported"]),
        "hour_17": lambda row: row["local_hour"] == 17.0,
        "hour_18": lambda row: row["local_hour"] == 18.0,
        "hour_19": lambda row: row["local_hour"] == 19.0,
        "late_18_19": lambda row: row["local_hour"] >= 18.0,
        "hot_season_hour_19": lambda row: 5 <= row["month"] <= 9 and row["local_hour"] == 19.0,
        "hot_season_high_max_late": lambda row: 5 <= row["month"] <= 9 and row["local_hour"] >= 18.0 and row["observed_max_c"] >= 35.0,
        "peak_age_ge_2h_drop_ge_2c": lambda row: row["hours_since_observed_max"] >= 2.0 and row["drop_from_observed_max_c"] >= 2.0,
        "late_peak_age_ge_2h_drop_ge_2c": lambda row: row["local_hour"] >= 18.0 and row["hours_since_observed_max"] >= 2.0 and row["drop_from_observed_max_c"] >= 2.0,
        "hot_season_late_peak_age_ge_2h_drop_ge_2c": lambda row: 5 <= row["month"] <= 9 and row["local_hour"] >= 18.0 and row["hours_since_observed_max"] >= 2.0 and row["drop_from_observed_max_c"] >= 2.0,
        "post_convective_cooling": lambda row: bool(row["post_convective_cooling"]),
        "post_precipitation_cooling": lambda row: bool(row["post_precipitation_cooling"]),
        "current_convective": lambda row: bool(row["current_convective_weather"]),
        "current_precipitation": lambda row: bool(row["current_precipitation_reported"]),
    }
    return {name: _weighted_summary([row for row in rows if predicate(row)]) for name, predicate in definitions.items()}


def _calibration_bins(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, float | int]]:
    edges = [0.0, 0.02, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 1.000001]
    result = []
    for low, high in zip(edges, edges[1:]):
        selected = [row for row in rows if low <= float(row["predicted_continue_probability"]) < high]
        if not selected:
            continue
        summary = _weighted_summary(selected)
        result.append({"lower": low, "upper": min(high, 1.0), **summary})
    return result


def _importance(artifact: Mapping[str, Any], limit: int = 20) -> list[dict[str, float | str]]:
    names = list(artifact["feature_names"])
    values = list(artifact["peak_already_coefficients"])
    pairs = [
        {"feature": name, "coefficient": round(float(value), 6), "absolute": round(abs(float(value)), 6)}
        for name, value in zip(names, values)
        if name != "intercept"
    ]
    return sorted(pairs, key=lambda item: float(item["absolute"]), reverse=True)[:limit]


def _render(report: Mapping[str, Any]) -> str:
    v1_all = report["segments"]["v1"]["all"]
    v2_all = report["segments"]["v2"]["all"]
    late1 = report["segments"]["v1"]["late_18_19"]
    late2 = report["segments"]["v2"]["late_18_19"]
    scenario1 = report["segments"]["v1"]["late_peak_age_ge_2h_drop_ge_2c"]
    scenario2 = report["segments"]["v2"]["late_peak_age_ge_2h_drop_ge_2c"]
    hot_scenario = report["segments"]["v2"]["hot_season_late_peak_age_ge_2h_drop_ge_2c"]
    lines = [
        "# ZUUU 剩余升温模型 v2：时间外校准审计",
        "",
        "## tl;dr",
        "",
        f"- 2022–2025严格逐年向前验证；样本 `{v2_all['sample_count']}` 条、完整日 `{v2_all['day_count']}` 个。",
        f"- 全时段桶NLL：v1 `{v1_all['bucket_nll']:.4f}` → v2 `{v2_all['bucket_nll']:.4f}`；MAE：`{v1_all['mae_c']:.3f}` → `{v2_all['mae_c']:.3f}` °C。",
        f"- 18–19时继续升温预测率：v1 `{100*late1['predicted_continue_rate']:.2f}%` → v2 `{100*late2['predicted_continue_rate']:.2f}%`，实际 `{100*late2['actual_continue_rate']:.2f}%`。",
        f"- 晚间且峰值已过去≥2小时、已回落≥2°C：v1 `{100*scenario1.get('predicted_continue_rate',0):.2f}%` → v2 `{100*scenario2.get('predicted_continue_rate',0):.2f}%`，实际 `{100*scenario2.get('actual_continue_rate',0):.2f}%`。",
        f"- 暖季同类场景共 `{hot_scenario.get('day_count',0)}` 天，实际续创新高 `{hot_scenario.get('actual_continue_count',0)}` 次；v2预测 `{100*hot_scenario.get('predicted_continue_rate',0):.3f}%`，零事件95%上限 `{100*(hot_scenario.get('zero_event_95pct_upper_rate') or 0):.2f}%`。",
        f"- 研究门：`{'通过' if report['release_gate']['research_pass'] else '未通过'}`；生产门固定关闭。",
        "",
        "## 分时段继续升温校准",
        "",
        "| 时段 | v1预测 | v2预测 | 实际 | v2校准差(pct) |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, label in (("hour_17", "17时"), ("hour_18", "18时"), ("hour_19", "19时")):
        left = report["segments"]["v1"][name]
        right = report["segments"]["v2"][name]
        lines.append(
            f"| {label} | {100*left.get('predicted_continue_rate',0):.2f}% | {100*right.get('predicted_continue_rate',0):.2f}% | {100*right.get('actual_continue_rate',0):.2f}% | {right.get('continue_calibration_gap_pct',0):+.2f} |"
        )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            "- 标签仍是NCEI小时最高温，不是AMSC半小时峰值或市场官方结算值。",
            "- 新增顺序特征只读取当前及过去报文；未使用最终峰值时刻作为输入。",
            "- 大尺度再分析场和数值降水量尚未接入。",
            "- 所有产物仅写research目录，不写事实库、策略、信号或订单。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = v1._load(args.features)
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    folds = []
    all_predictions: dict[str, list[dict[str, Any]]] = {"v1": [], "v2": []}
    for test_year in years[1:]:
        train_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        if not train_rows or not test_rows:
            continue
        fold = {"test_year": test_year, "train_day_count": len({row["event_date_local"] for row in train_rows})}
        for version in ("v1", "v2"):
            metrics, _, predictions = _fit(train_rows, test_rows, version=version)
            fold[version] = metrics
            all_predictions[version].extend(predictions)
        folds.append(fold)

    _, final_artifact, _ = _fit(rows, rows, version="v2")
    final_artifact.update({"trained_through_year": max(years), "training_sample_count": len(rows)})
    segments = {version: _segments(predictions) for version, predictions in all_predictions.items()}
    bins = {version: _calibration_bins(predictions) for version, predictions in all_predictions.items()}
    v1_all, v2_all = segments["v1"]["all"], segments["v2"]["all"]
    late1, late2 = segments["v1"]["late_18_19"], segments["v2"]["late_18_19"]
    scenario1 = segments["v1"]["late_peak_age_ge_2h_drop_ge_2c"]
    scenario2 = segments["v2"]["late_peak_age_ge_2h_drop_ge_2c"]
    nll_improvement = 100.0 * (float(v1_all["bucket_nll"]) - float(v2_all["bucket_nll"])) / float(v1_all["bucket_nll"])
    rps_improvement = 100.0 * (float(v1_all["bucket_rps"]) - float(v2_all["bucket_rps"])) / float(v1_all["bucket_rps"])
    mae_improvement = 100.0 * (float(v1_all["mae_c"]) - float(v2_all["mae_c"])) / float(v1_all["mae_c"])
    late_gap_reduction = abs(float(late1["continue_calibration_gap_pct"])) - abs(float(late2["continue_calibration_gap_pct"]))
    scenario_gap_reduction = abs(float(scenario1.get("continue_calibration_gap_pct", 0.0))) - abs(float(scenario2.get("continue_calibration_gap_pct", 0.0)))
    year_wins = sum(
        float(fold["v2"]["bucket_nll"]) < float(fold["v1"]["bucket_nll"])
        and float(fold["v2"]["bucket_rps"]) < float(fold["v1"]["bucket_rps"])
        for fold in folds
    )
    research_pass = bool(
        nll_improvement > 0.0
        and rps_improvement > 0.0
        and mae_improvement > 0.0
        and late_gap_reduction > 0.0
        and scenario_gap_reduction > 0.0
        and year_wins >= 3
    )
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "split": "strict expanding-year walk-forward",
            "test_years": years[1:],
            "timezone": "Asia/Shanghai",
            "v1": "instantaneous joint weather and temperature trajectory",
            "v2": "v1 plus causal peak-age, cooling-duration and cumulative convection/precipitation memory",
            "label": "remaining warming to complete local-day NCEI hourly maximum",
        },
        "folds": folds,
        "segments": segments,
        "calibration_bins": bins,
        "improvement_pct": {"bucket_nll": nll_improvement, "bucket_rps": rps_improvement, "mae_c": mae_improvement},
        "calibration_gap_reduction_pct_points": {"late_18_19": late_gap_reduction, "late_peak_age_ge_2h_drop_ge_2c": scenario_gap_reduction},
        "year_wins_nll_and_rps": year_wins,
        "v2_peak_already_feature_importance": _importance(final_artifact),
        "release_gate": {
            "research_pass": research_pass,
            "production_ready": False,
            "production_blockers": [
                "amsc_half_hour_and_market_settlement_calibration_missing",
                "large_scale_reanalysis_context_missing",
                "shadow_runtime_not_evaluated",
                "fact_summary_merge_fill_mismatch_unresolved",
            ],
        },
        "guards": {
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
    (args.output_dir / "calibration_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "calibration_report.md").write_text(_render(report), encoding="utf-8")
    (args.output_dir / "model_artifact.json").write_text(json.dumps(final_artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "research_pass": research_pass,
                "year_wins": year_wins,
                "improvement_pct": report["improvement_pct"],
                "calibration_gap_reduction_pct_points": report["calibration_gap_reduction_pct_points"],
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
