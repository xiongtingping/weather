#!/usr/bin/env python3
"""Train a research-only joint remaining-warming Bayesian model for ZUUU.

The model is zero-inflated: a regularized Bayesian/logistic component estimates
whether the daily peak is already realized, and a Bayesian ridge component
models positive remaining warming in log space.  Validation is strictly
forward by year; no random split or production write is permitted.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


SCHEMA = "ncei_zuuu_remaining_warming_bayes_research_v1"
MODEL_SCHEMA = "ncei_zuuu_zero_inflated_bayes_artifact_v1"
EPS = 1e-12
BUCKETS = np.arange(-10, 51, dtype=float)


BASE_NUMERIC = [
    "local_hour_sin", "local_hour_cos", "solar_heating_window",
    "day_of_year_sin", "day_of_year_cos", "climo_prior_mean_c",
    "temperature_c", "observed_max_to_time_c", "temperature_vs_observed_max_c",
    "temperature_delta_1h", "temperature_delta_3h",
]
FULL_NUMERIC = BASE_NUMERIC + [
    "dewpoint_c", "temp_dewpoint_spread_c", "relative_humidity_pct",
    "visibility_km", "pressure_hpa", "wind_speed_mps", "wind_gust_mps",
    "wind_dir_sin", "wind_dir_cos", "cloud_cover_pct", "low_cloud_cover_pct",
    "dewpoint_delta_1h", "dewpoint_delta_3h", "pressure_delta_1h", "pressure_delta_3h",
    "wind_speed_delta_1h", "wind_speed_delta_3h", "wind_direction_shift_1h_deg",
    "visibility_delta_1h_km", "cloud_cover_delta_1h",
    "interaction_dewpoint_cloud", "interaction_cloud_solar",
    "interaction_convective_outflow", "interaction_precip_moisture",
]
FULL_BOOLEAN = [
    "wind_variable", "current_precipitation_reported", "current_convective_weather",
    "convective_cloud_reported", "fog_or_mist_reported", "haze_or_dust_reported",
    "current_no_significant_weather", "trend_convective_or_precipitation", "metar_trend_nosig",
]


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _load(path: Path) -> list[dict[str, Any]]:
    rows = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if not isinstance(row, Mapping):
                continue
            hour = _number(row.get("local_hour"))
            if (
                row.get("daily_max_label_eligible")
                and not row.get("core_qc_bad")
                and hour is not None
                and 6.0 <= hour <= 19.0
                and _number(row.get("remaining_warming_c")) is not None
                and _number(row.get("observed_max_to_time_c")) is not None
            ):
                rows.append(dict(row))
    return rows


def _day_distance(left: int, right: int) -> int:
    raw = abs(left - right)
    return min(raw, 366 - raw)


def _climo_table(train_rows: Sequence[Mapping[str, Any]]) -> tuple[dict[int, tuple[float, int]], dict[str, float]]:
    per_day = {}
    for row in train_rows:
        day = str(row["event_date_local"])
        per_day.setdefault(day, float(row["final_daily_max_c"]))
    by_doy: dict[int, list[float]] = defaultdict(list)
    for day, value in per_day.items():
        by_doy[date.fromisoformat(day).timetuple().tm_yday].append(value)
    table = {}
    for doy in range(1, 367):
        values = [value for source_doy, vals in by_doy.items() if _day_distance(doy, source_doy) <= 15 for value in vals]
        table[doy] = (float(sum(values)), len(values))
    return table, per_day


def _climo_value(day: str, table: Mapping[int, tuple[float, int]], per_day: Mapping[str, float], *, exclude_same: bool) -> float:
    doy = date.fromisoformat(day).timetuple().tm_yday
    total, count = table[doy]
    if exclude_same and day in per_day:
        total -= per_day[day]
        count -= 1
    return total / count if count > 0 else float(np.mean(list(per_day.values())))


def _engineered(row: Mapping[str, Any], climo: float) -> dict[str, float | None]:
    hour = float(row["local_hour"])
    direction = _number(row.get("wind_dir_deg"))
    temp = _number(row.get("temperature_c"))
    observed = _number(row.get("observed_max_to_time_c"))
    return {
        "local_hour_sin": math.sin(2.0 * math.pi * hour / 24.0),
        "local_hour_cos": math.cos(2.0 * math.pi * hour / 24.0),
        "solar_heating_window": _number(row.get("solar_heating_window")),
        "day_of_year_sin": _number(row.get("day_of_year_sin")),
        "day_of_year_cos": _number(row.get("day_of_year_cos")),
        "climo_prior_mean_c": climo,
        "temperature_c": temp,
        "observed_max_to_time_c": observed,
        "temperature_vs_observed_max_c": temp - observed if temp is not None and observed is not None else None,
        "temperature_delta_1h": _number(row.get("temperature_delta_1h")),
        "temperature_delta_3h": _number(row.get("temperature_delta_3h")),
        "dewpoint_c": _number(row.get("dewpoint_c")),
        "temp_dewpoint_spread_c": _number(row.get("temp_dewpoint_spread_c")),
        "relative_humidity_pct": _number(row.get("relative_humidity_pct")),
        "visibility_km": (_number(row.get("visibility_m")) / 1000.0) if _number(row.get("visibility_m")) is not None else None,
        "pressure_hpa": _number(row.get("pressure_hpa")),
        "wind_speed_mps": _number(row.get("wind_speed_mps")),
        "wind_gust_mps": _number(row.get("wind_gust_mps")),
        "wind_dir_sin": math.sin(math.radians(direction)) if direction is not None else None,
        "wind_dir_cos": math.cos(math.radians(direction)) if direction is not None else None,
        "cloud_cover_pct": _number(row.get("cloud_cover_pct")),
        "low_cloud_cover_pct": _number(row.get("low_cloud_cover_pct")),
        "dewpoint_delta_1h": _number(row.get("dewpoint_delta_1h")),
        "dewpoint_delta_3h": _number(row.get("dewpoint_delta_3h")),
        "pressure_delta_1h": _number(row.get("pressure_delta_1h")),
        "pressure_delta_3h": _number(row.get("pressure_delta_3h")),
        "wind_speed_delta_1h": _number(row.get("wind_speed_delta_1h")),
        "wind_speed_delta_3h": _number(row.get("wind_speed_delta_3h")),
        "wind_direction_shift_1h_deg": _number(row.get("wind_direction_shift_1h_deg")),
        "visibility_delta_1h_km": (_number(row.get("visibility_delta_1h")) / 1000.0) if _number(row.get("visibility_delta_1h")) is not None else None,
        "cloud_cover_delta_1h": _number(row.get("cloud_cover_delta_1h")),
        "interaction_dewpoint_cloud": _number(row.get("interaction_dewpoint_cloud")),
        "interaction_cloud_solar": _number(row.get("interaction_cloud_solar")),
        "interaction_convective_outflow": _number(row.get("interaction_convective_outflow")),
        "interaction_precip_moisture": _number(row.get("interaction_precip_moisture")),
        **{name: 1.0 if row.get(name) else 0.0 for name in FULL_BOOLEAN},
    }


class Design:
    def __init__(self, numeric: list[str], boolean: list[str]):
        self.numeric = numeric
        self.boolean = boolean
        self.names = [*numeric, *[f"{name}__missing" for name in numeric], *boolean]
        self.means: np.ndarray | None = None
        self.scales: np.ndarray | None = None

    def fit_transform(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        raw = self._raw(rows, climo_values)
        numeric_width = len(self.numeric)
        values = raw[:, :numeric_width]
        means = np.nanmean(values, axis=0)
        means = np.where(np.isfinite(means), means, 0.0)
        filled = np.where(np.isnan(values), means, values)
        scales = np.nanstd(filled, axis=0)
        scales = np.where(scales > 1e-6, scales, 1.0)
        self.means, self.scales = means, scales
        raw[:, :numeric_width] = (filled - means) / scales
        return np.column_stack([np.ones(len(raw)), raw])

    def transform(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        assert self.means is not None and self.scales is not None
        raw = self._raw(rows, climo_values)
        width = len(self.numeric)
        values = np.where(np.isnan(raw[:, :width]), self.means, raw[:, :width])
        raw[:, :width] = (values - self.means) / self.scales
        return np.column_stack([np.ones(len(raw)), raw])

    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix = []
        for row, climo in zip(rows, climo_values):
            values = _engineered(row, climo)
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values[name] or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _day_weights(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    counts = Counter(str(row["event_date_local"]) for row in rows)
    values = np.asarray([1.0 / counts[str(row["event_date_local"])] for row in rows], dtype=float)
    return values * (len(values) / values.sum())


def _fit_logistic(x: np.ndarray, y: np.ndarray, weights: np.ndarray, alpha: float = 12.0) -> np.ndarray:
    beta = np.zeros(x.shape[1], dtype=float)
    penalty = np.eye(x.shape[1]) * alpha
    penalty[0, 0] = 0.01
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


def _fit_positive(x: np.ndarray, remaining: np.ndarray, weights: np.ndarray, alpha: float = 16.0) -> tuple[np.ndarray, float]:
    mask = remaining > 0.05
    xp = x[mask]
    yp = np.log1p(remaining[mask])
    wp = weights[mask]
    penalty = np.eye(x.shape[1]) * alpha
    penalty[0, 0] = 0.01
    beta = np.linalg.solve(xp.T @ (wp[:, None] * xp) + penalty, xp.T @ (wp * yp))
    residual = yp - xp @ beta
    variance = float(np.sum(wp * residual * residual) / max(float(np.sum(wp)), 1.0))
    return beta, max(math.sqrt(variance), 0.12)


def _predict_zero(x: np.ndarray, beta: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x @ beta, -30.0, 30.0)))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _positive_cdf(value: float, mu: float, sigma: float) -> float:
    if value <= 0.0:
        return 0.0
    base = _normal_cdf((0.0 - mu) / sigma)
    raw = _normal_cdf((math.log1p(value) - mu) / sigma)
    return min(1.0, max(0.0, (raw - base) / max(1.0 - base, EPS)))


def _expected_positive(mu: float, sigma: float) -> float:
    denominator = max(_normal_cdf(mu / sigma), EPS)
    numerator = math.exp(mu + 0.5 * sigma * sigma) * _normal_cdf((mu + sigma * sigma) / sigma)
    return max(0.0, numerator / denominator - 1.0)


def _bucket_probs(observed: float, p_zero: float, mu: float, sigma: float) -> np.ndarray:
    probs = np.zeros(len(BUCKETS), dtype=float)
    zero_index = int(np.argmin(np.abs(BUCKETS - round(observed))))
    probs[zero_index] += p_zero
    for index, bucket in enumerate(BUCKETS):
        low = float(bucket - 0.5 - observed)
        high = float(bucket + 0.5 - observed)
        mass = _positive_cdf(high, mu, sigma) - _positive_cdf(low, mu, sigma)
        probs[index] += (1.0 - p_zero) * max(0.0, mass)
    total = probs.sum()
    return probs / total if total > 0 else np.eye(1, len(BUCKETS), zero_index).ravel()


def _metrics(rows: Sequence[Mapping[str, Any]], x: np.ndarray, zero_beta: np.ndarray, positive_beta: np.ndarray, sigma: float) -> dict[str, float]:
    p_zero = _predict_zero(x, zero_beta)
    remaining = np.asarray([float(row["remaining_warming_c"]) for row in rows])
    observed = np.asarray([float(row["observed_max_to_time_c"]) for row in rows])
    actual = np.asarray([float(row["final_daily_max_c"]) for row in rows])
    weights = _day_weights(rows)
    expected_remaining = np.asarray([(1.0 - p) * min(15.0, _expected_positive(float(mu), sigma)) for p, mu in zip(p_zero, x @ positive_beta)])
    predicted = observed + expected_remaining
    nll = []
    rps = []
    hits = []
    for row, p, mu in zip(rows, p_zero, x @ positive_beta):
        probs = _bucket_probs(float(row["observed_max_to_time_c"]), float(p), float(mu), sigma)
        actual_bucket = round(float(row["final_daily_max_c"]))
        actual_index = int(np.argmin(np.abs(BUCKETS - actual_bucket)))
        nll.append(-math.log(max(float(probs[actual_index]), EPS)))
        empirical = np.zeros(len(BUCKETS)); empirical[actual_index] = 1.0
        rps.append(float(np.sum((np.cumsum(probs) - np.cumsum(empirical)) ** 2) / (len(BUCKETS) - 1)))
        hits.append(int(np.argmax(probs) == actual_index))
    y_zero = (remaining <= 0.05).astype(float)
    return {
        "sample_count": len(rows),
        "day_count": len({str(row["event_date_local"]) for row in rows}),
        "mae_c": float(np.average(np.abs(predicted - actual), weights=weights)),
        "rmse_c": float(math.sqrt(np.average((predicted - actual) ** 2, weights=weights))),
        "bucket_nll": float(np.average(np.asarray(nll), weights=weights)),
        "bucket_rps": float(np.average(np.asarray(rps), weights=weights)),
        "top_bucket_hit_rate": float(np.average(np.asarray(hits), weights=weights)),
        "peak_already_brier": float(np.average((p_zero - y_zero) ** 2, weights=weights)),
        "peak_already_predicted_rate": float(np.average(p_zero, weights=weights)),
        "peak_already_actual_rate": float(np.average(y_zero, weights=weights)),
    }


def _train(train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]], *, full: bool) -> tuple[dict[str, float], dict[str, Any]]:
    table, per_day = _climo_table(train_rows)
    train_climo = [_climo_value(str(row["event_date_local"]), table, per_day, exclude_same=True) for row in train_rows]
    test_climo = [_climo_value(str(row["event_date_local"]), table, per_day, exclude_same=False) for row in test_rows]
    design = Design(FULL_NUMERIC if full else BASE_NUMERIC, FULL_BOOLEAN if full else [])
    x_train = design.fit_transform(train_rows, train_climo)
    x_test = design.transform(test_rows, test_climo)
    remaining = np.asarray([float(row["remaining_warming_c"]) for row in train_rows])
    weights = _day_weights(train_rows)
    zero_beta = _fit_logistic(x_train, (remaining <= 0.05).astype(float), weights)
    positive_beta, sigma = _fit_positive(x_train, remaining, weights)
    metrics = _metrics(test_rows, x_test, zero_beta, positive_beta, sigma)
    artifact = {
        "feature_names": ["intercept", *design.names],
        "numeric_features": design.numeric,
        "boolean_features": design.boolean,
        "means": design.means.tolist() if design.means is not None else [],
        "scales": design.scales.tolist() if design.scales is not None else [],
        "peak_already_coefficients": zero_beta.tolist(),
        "positive_remaining_coefficients": positive_beta.tolist(),
        "positive_log1p_residual_sigma": sigma,
    }
    return metrics, artifact


def _importance(artifact: Mapping[str, Any], limit: int = 15) -> dict[str, list[dict[str, float | str]]]:
    names = list(artifact["feature_names"])
    result = {}
    for key in ("peak_already_coefficients", "positive_remaining_coefficients"):
        values = list(artifact[key])
        pairs = [
            {"feature": name, "coefficient": round(float(value), 6), "absolute": round(abs(float(value)), 6)}
            for name, value in zip(names, values)
            if name != "intercept"
        ]
        result[key] = sorted(pairs, key=lambda item: float(item["absolute"]), reverse=True)[:limit]
    return result


def _aggregate(folds: list[Mapping[str, Any]], model: str) -> dict[str, float]:
    metrics = [fold[model] for fold in folds]
    return {
        key: float(np.mean([float(row[key]) for row in metrics]))
        for key in ("mae_c", "rmse_c", "bucket_nll", "bucket_rps", "top_bucket_hit_rate", "peak_already_brier")
    }


def _improvement(baseline: float, full: float) -> float:
    return 100.0 * (baseline - full) / baseline if baseline else 0.0


def render_markdown(report: Mapping[str, Any]) -> str:
    baseline = report["aggregate"]["trajectory_climate_baseline"]
    full = report["aggregate"]["joint_weather_bayes"]
    gain = report["improvement_pct"]
    lines = [
        "# ZUUU 联合天气剩余升温 Bayes：逐年时间外验证",
        "",
        "## tl;dr",
        "",
        f"- 训练样本 `{report['sample_count']}` 条、完整日 `{report['day_count']}` 个；2022–2025逐年只用过去年份训练。",
        f"- 相对仅时刻/季节/温度轨迹基线：MAE改善 `{gain['mae_c']:.2f}%`，桶NLL改善 `{gain['bucket_nll']:.2f}%`，RPS改善 `{gain['bucket_rps']:.2f}%`。",
        f"- 研究发布门：`{'通过' if report['release_gate']['research_pass'] else '未通过'}`；生产接入始终阻断。",
        "",
        "## 聚合指标",
        "",
        "| 模型 | MAE °C | RMSE °C | 桶NLL | 桶RPS | 主桶命中率 | 峰值已出现Brier |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| 轨迹/气候基线 | {baseline['mae_c']:.3f} | {baseline['rmse_c']:.3f} | {baseline['bucket_nll']:.3f} | {baseline['bucket_rps']:.4f} | {baseline['top_bucket_hit_rate']:.3f} | {baseline['peak_already_brier']:.4f} |",
        f"| 联合天气 Bayes | {full['mae_c']:.3f} | {full['rmse_c']:.3f} | {full['bucket_nll']:.3f} | {full['bucket_rps']:.4f} | {full['top_bucket_hit_rate']:.3f} | {full['peak_already_brier']:.4f} |",
        "",
        "## 边界",
        "",
        "- 当前只证明历史表面天气组合是否增加预测信息；未加入850 hPa、副高、西南涡和辐射再分析场。",
        "- 小时最高温仍需用 AMSC 半小时峰值与市场唯一结算源校准。",
        "- 模型产物仅位于 research 目录，不写生产概率、参数、信号或数据库。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = _load(args.features)
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    folds = []
    for test_year in years[1:]:
        train_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        if not train_rows or not test_rows:
            continue
        baseline_metrics, _ = _train(train_rows, test_rows, full=False)
        full_metrics, _ = _train(train_rows, test_rows, full=True)
        folds.append({"test_year": test_year, "train_day_count": len({row['event_date_local'] for row in train_rows}), "trajectory_climate_baseline": baseline_metrics, "joint_weather_bayes": full_metrics})

    baseline = _aggregate(folds, "trajectory_climate_baseline")
    full = _aggregate(folds, "joint_weather_bayes")
    improvement = {key: _improvement(baseline[key], full[key]) for key in ("mae_c", "rmse_c", "bucket_nll", "bucket_rps", "peak_already_brier")}
    year_wins = sum(
        fold["joint_weather_bayes"]["bucket_nll"] < fold["trajectory_climate_baseline"]["bucket_nll"]
        and fold["joint_weather_bayes"]["bucket_rps"] < fold["trajectory_climate_baseline"]["bucket_rps"]
        for fold in folds
    )
    _, final_artifact = _train(rows, rows, full=True)
    final_artifact.update({"schema": MODEL_SCHEMA, "trained_through_year": max(years), "training_sample_count": len(rows), "research_only": True})
    research_pass = bool(
        improvement["bucket_nll"] >= 2.0
        and improvement["bucket_rps"] >= 2.0
        and improvement["mae_c"] >= 1.0
        and year_wins >= 3
    )
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "sample_count": len(rows),
        "day_count": len({str(row["event_date_local"]) for row in rows}),
        "local_hour_window": [6.0, 19.0],
        "folds": folds,
        "aggregate": {"trajectory_climate_baseline": baseline, "joint_weather_bayes": full},
        "improvement_pct": improvement,
        "joint_weather_feature_importance": _importance(final_artifact),
        "release_gate": {
            "research_pass": research_pass,
            "year_wins_nll_and_rps": year_wins,
            "required_year_wins": 3,
            "production_ready": False,
            "production_blockers": [
                "large_scale_reanalysis_context_missing",
                "amsc_half_hour_and_settlement_label_calibration_missing",
                "shadow_runtime_not_evaluated",
            ],
        },
        "method": {
            "target": "zero-inflated continuous remaining warming to Asia/Shanghai daily hourly maximum",
            "split": "strict expanding-year walk-forward",
            "day_weighting": "each station-local day equal total weight",
            "baseline": "time, seasonal climatology and temperature trajectory only",
            "full": "baseline plus dewpoint, humidity, visibility, cloud, weather, wind, pressure and interactions",
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
    (args.output_dir / "walk_forward_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "walk_forward_report.md").write_text(render_markdown(report), encoding="utf-8")
    (args.output_dir / "model_artifact.json").write_text(json.dumps(final_artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "research_pass": research_pass, "year_wins": year_wins, "improvement_pct": improvement, "output_dir": str(args.output_dir), "guards": report["guards"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
