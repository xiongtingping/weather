#!/usr/bin/env python3
"""Strict state-grouped shrinkage validation for ECMWF in ZUUU v3.1.

The candidate never refits a tiny model inside a weather segment.  It first
fits the complete v3.1 and v3.1+ECMWF distributions, then blends them only in
one pre-registered state:

    P = (1 - w) * P_v31 + w * P_v31_plus_ecmwf

The segment and weight are selected on rolling 2024 validation windows.  The
entire 2025 sample is held out until that selection is fixed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2
from scripts import evaluate_ncei_zuuu_additional_models_shadow_v31 as shadow
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


SCHEMA = "ncei_zuuu_ecmwf_regime_shrinkage_validation_v311"
WEIGHT_GRID = tuple(round(value, 2) for value in np.linspace(0.0, 1.0, 11))
SHRINKAGE_PRIOR_DAYS = 60.0


def _number(value: Any) -> float | None:
    return v1._number(value)


def _season(row: Mapping[str, Any]) -> str:
    month = int(row.get("month") or str(row["event_date_local"])[5:7])
    if month in {11, 12, 1, 2}:
        return "cool_nov_feb"
    if month in {5, 6, 7, 8, 9}:
        return "warm_wet_may_sep"
    return "transition_mar_apr_oct"


def _active_convection_or_precip(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("current_convective_weather")
        or row.get("convective_cloud_reported")
        or row.get("current_precipitation_reported")
    )


def _antecedent_convection_or_precip(row: Mapping[str, Any]) -> bool:
    return bool(
        _active_convection_or_precip(row)
        or row.get("convective_seen_today")
        or row.get("precipitation_seen_today")
        or row.get("post_convective_cooling")
        or row.get("post_precipitation_cooling")
    )


def _high_humidity_low_cloud(row: Mapping[str, Any]) -> bool:
    rh = _number(row.get("relative_humidity_pct"))
    low_cloud = _number(row.get("low_cloud_cover_pct"))
    spread = _number(row.get("temp_dewpoint_spread_c"))
    return bool(
        low_cloud is not None
        and low_cloud >= 50.0
        and ((rh is not None and rh >= 85.0) or (spread is not None and spread <= 3.0))
    )


def _high_basin_ventilation(row: Mapping[str, Any]) -> bool:
    wind850 = _number(row.get("ecmwf__synoptic_850_wind_speed_mps"))
    wind700 = _number(row.get("ecmwf__synoptic_700_wind_speed_mps"))
    shear = _number(row.get("ecmwf__synoptic_850_700_shear_mps"))
    return bool(
        (wind850 is not None and wind850 >= 8.0)
        or (wind700 is not None and wind700 >= 10.0)
        or (shear is not None and shear >= 8.0)
    )


def _dry_westerly_foehn(row: Mapping[str, Any]) -> bool:
    proxy = _number(row.get("ecmwf__synoptic_westerly_dry_foehn_proxy"))
    rh850 = _number(row.get("ecmwf__basin_center_rhum_850_pct"))
    return bool(proxy is not None and proxy >= 1.5 and rh850 is not None and rh850 <= 65.0)


SEGMENTS: dict[str, tuple[str, Callable[[Mapping[str, Any]], bool]]] = {
    "season_cool": ("冷季（11—2月）", lambda row: _season(row) == "cool_nov_feb"),
    "season_transition": ("过渡季（3—4月、10月）", lambda row: _season(row) == "transition_mar_apr_oct"),
    "season_warm_wet": ("暖湿季（5—9月）", lambda row: _season(row) == "warm_wet_may_sep"),
    "active_convection_or_precip": ("当前对流或降水", _active_convection_or_precip),
    "antecedent_convection_or_precip": ("当日已出现对流或降水", _antecedent_convection_or_precip),
    "high_humidity_low_cloud": ("高湿低云", _high_humidity_low_cloud),
    "high_basin_ventilation": ("盆地高通风", _high_basin_ventilation),
    "dry_westerly_foehn": ("干西风焚风", _dry_westerly_foehn),
    "warm_active_convection_or_precip": (
        "暖湿季×当前对流或降水",
        lambda row: _season(row) == "warm_wet_may_sep" and _active_convection_or_precip(row),
    ),
    "warm_antecedent_convection_or_precip": (
        "暖湿季×当日已出现对流或降水",
        lambda row: _season(row) == "warm_wet_may_sep" and _antecedent_convection_or_precip(row),
    ),
    "warm_high_humidity_low_cloud": (
        "暖湿季×高湿低云",
        lambda row: _season(row) == "warm_wet_may_sep" and _high_humidity_low_cloud(row),
    ),
    "warm_high_basin_ventilation": (
        "暖湿季×盆地高通风",
        lambda row: _season(row) == "warm_wet_may_sep" and _high_basin_ventilation(row),
    ),
    "warm_dry_westerly_foehn": (
        "暖湿季×干西风焚风",
        lambda row: _season(row) == "warm_wet_may_sep" and _dry_westerly_foehn(row),
    ),
    "cool_high_humidity_low_cloud": (
        "冷季×高湿低云",
        lambda row: _season(row) == "cool_nov_feb" and _high_humidity_low_cloud(row),
    ),
}


def _fit_distribution(
    train_rows: list[dict[str, Any]],
    test_rows: list[dict[str, Any]],
    *,
    models: Sequence[str],
    version: str,
) -> list[dict[str, Any]]:
    design = shadow.ShadowDesign(models)
    _metrics, artifact, predictions = v2._fit(train_rows, test_rows, version=version, design_override=design)
    table, per_day = v1._climo_table(train_rows)
    test_climo = [
        v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=False)
        for row in test_rows
    ]
    x = design.transform(test_rows, test_climo)
    zero_beta = np.asarray(artifact["peak_already_coefficients"], dtype=float)
    positive_beta = np.asarray(artifact["positive_remaining_coefficients"], dtype=float)
    sigma = float(artifact["positive_log1p_residual_sigma"])
    p_zero = v1._predict_zero(x, zero_beta)
    positive_mu = x @ positive_beta
    output = []
    for source, prediction, peak_probability, mu in zip(test_rows, predictions, p_zero, positive_mu):
        observed = float(source["observed_max_to_time_c"])
        probabilities = v1._bucket_probs(observed, float(peak_probability), float(mu), sigma)
        row = dict(prediction)
        row.update(
            {
                "bucket_probabilities": probabilities.tolist(),
                "peak_already_probability": float(peak_probability),
                "predicted_continue_probability": 1.0 - float(peak_probability),
                # Use the same integer-bucket expectation on both sides of
                # every blend.  Otherwise weight=0 compares a discretized
                # candidate expectation with v2's continuous expectation and
                # creates a false MAE delta.
                "expected_final_max_c": float(np.sum(v1.BUCKETS * probabilities)),
                "source_weather_row": dict(source),
            }
        )
        output.append(row)
    return output


def _regime_distribution(
    day_rows: Sequence[Mapping[str, Any]],
    predawn_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(day_rows) != len(predawn_rows):
        raise RuntimeError("regime distribution widths differ")
    output = []
    for day, predawn in zip(day_rows, predawn_rows):
        if (
            day["event_date_local"] != predawn["event_date_local"]
            or float(day["local_hour"]) != float(predawn["local_hour"])
        ):
            raise RuntimeError("regime distribution ordering differs")
        output.append(dict(predawn if float(day["local_hour"]) < 6.0 else day))
    return output


def _fit_pair(train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]], *, tag: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    baseline_day = _fit_distribution(train_rows, test_rows, models=("gfs",), version=f"{tag}_baseline_day")
    baseline_predawn = _fit_distribution(train_rows, test_rows, models=("gfs", "jma"), version=f"{tag}_baseline_predawn")
    candidate_day = _fit_distribution(train_rows, test_rows, models=("gfs", "ecmwf"), version=f"{tag}_candidate_day")
    candidate_predawn = _fit_distribution(train_rows, test_rows, models=("gfs", "jma", "ecmwf"), version=f"{tag}_candidate_predawn")
    return (
        _regime_distribution(baseline_day, baseline_predawn),
        _regime_distribution(candidate_day, candidate_predawn),
    )


def _prediction_from_probabilities(template: Mapping[str, Any], probabilities: np.ndarray, continue_probability: float) -> dict[str, Any]:
    actual_bucket = round(float(template["final_daily_max_c"]))
    actual_index = int(np.argmin(np.abs(v1.BUCKETS - actual_bucket)))
    empirical = np.zeros(len(v1.BUCKETS), dtype=float)
    empirical[actual_index] = 1.0
    expected_final = float(np.sum(v1.BUCKETS * probabilities))
    output = dict(template)
    output.update(
        {
            "bucket_probabilities": probabilities.tolist(),
            "predicted_continue_probability": float(continue_probability),
            "expected_final_max_c": expected_final,
            "bucket_nll": -math.log(max(float(probabilities[actual_index]), v1.EPS)),
            "bucket_rps": float(
                np.sum((np.cumsum(probabilities) - np.cumsum(empirical)) ** 2) / (len(v1.BUCKETS) - 1)
            ),
        }
    )
    return output


def _blend_rows(
    baseline_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    segment: str,
    weight: float,
) -> list[dict[str, Any]]:
    predicate = SEGMENTS[segment][1]
    output = []
    for baseline, candidate in zip(baseline_rows, candidate_rows):
        if (
            baseline["event_date_local"] != candidate["event_date_local"]
            or float(baseline["local_hour"]) != float(candidate["local_hour"])
        ):
            raise RuntimeError("blend row ordering differs")
        weather = baseline["source_weather_row"]
        effective_weight = float(weight if predicate(weather) else 0.0)
        baseline_probs = np.asarray(baseline["bucket_probabilities"], dtype=float)
        candidate_probs = np.asarray(candidate["bucket_probabilities"], dtype=float)
        probabilities = (1.0 - effective_weight) * baseline_probs + effective_weight * candidate_probs
        probabilities /= probabilities.sum()
        continue_probability = (
            (1.0 - effective_weight) * float(baseline["predicted_continue_probability"])
            + effective_weight * float(candidate["predicted_continue_probability"])
        )
        blended = _prediction_from_probabilities(baseline, probabilities, continue_probability)
        blended["ecmwf_segment_match"] = bool(effective_weight > 0.0)
        blended["ecmwf_effective_weight"] = effective_weight
        output.append(blended)
    return output


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return dict(v2._weighted_summary(rows))


def _gains(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, float]:
    return {
        key: 100.0 * (float(baseline[key]) - float(candidate[key])) / float(baseline[key])
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }


def _selected(rows: Sequence[Mapping[str, Any]], segment: str) -> list[Mapping[str, Any]]:
    predicate = SEGMENTS[segment][1]
    return [row for row in rows if predicate(row["source_weather_row"])]


def _paired_day_delta(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    *,
    metric: str,
) -> np.ndarray:
    def loss(row: Mapping[str, Any]) -> float:
        if metric in {"bucket_nll", "bucket_rps"}:
            return float(row[metric])
        if metric == "mae_c":
            return abs(float(row["expected_final_max_c"]) - float(row["final_daily_max_c"]))
        if metric == "continue_brier":
            actual = 1.0 if row["actual_continue"] else 0.0
            return (float(row["predicted_continue_probability"]) - actual) ** 2
        raise KeyError(metric)

    baseline_map = {(str(row["event_date_local"]), float(row["local_hour"])): row for row in baseline}
    candidate_map = {(str(row["event_date_local"]), float(row["local_hour"])): row for row in candidate}
    keys = sorted(set(baseline_map) & set(candidate_map))
    by_day: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for key in keys:
        by_day[key[0]].append(key)
    return np.asarray(
        [
            np.mean([loss(candidate_map[key]) - loss(baseline_map[key]) for key in day_keys])
            for day_keys in by_day.values()
        ],
        dtype=float,
    )


def _paired_validation(baseline: Sequence[Mapping[str, Any]], candidate: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rng = np.random.default_rng(311_2025)
    output: dict[str, Any] = {}
    for metric in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier"):
        deltas = _paired_day_delta(baseline, candidate, metric=metric)
        if len(deltas):
            samples = rng.choice(deltas, size=(5000, len(deltas)), replace=True).mean(axis=1)
            interval = [float(value) for value in np.quantile(samples, [0.025, 0.975])]
            one_sided_p = float((1.0 + np.sum(samples >= 0.0)) / (len(samples) + 1.0))
        else:
            interval = None
            one_sided_p = None
        output[metric] = {
            "paired_day_count": len(deltas),
            "candidate_minus_baseline_mean": float(np.mean(deltas)) if len(deltas) else None,
            "candidate_minus_baseline_median": float(np.median(deltas)) if len(deltas) else None,
            "bootstrap_95pct_ci": interval,
            "one_sided_bootstrap_p_candidate_better": one_sided_p,
            "candidate_better_day_count": int(np.sum(deltas < 0.0)),
            "candidate_worse_day_count": int(np.sum(deltas > 0.0)),
            "equal_day_count": int(np.sum(np.isclose(deltas, 0.0))),
        }
    return output


def _holm_adjust(rows: Sequence[dict[str, Any]], *, metric: str) -> None:
    eligible = []
    for index, row in enumerate(rows):
        paired = row.get("paired_2025") if isinstance(row.get("paired_2025"), Mapping) else {}
        metric_row = paired.get(metric) if isinstance(paired.get(metric), Mapping) else {}
        p_value = _number(metric_row.get("one_sided_bootstrap_p_candidate_better"))
        if p_value is not None and float(row.get("effective_weight") or 0.0) > 0.0:
            eligible.append((index, p_value))
    eligible.sort(key=lambda item: item[1])
    running = 0.0
    adjusted_by_index: dict[int, float] = {}
    width = len(eligible)
    for rank, (index, p_value) in enumerate(eligible):
        adjusted = min(1.0, (width - rank) * p_value)
        running = max(running, adjusted)
        adjusted_by_index[index] = running
    for index, row in enumerate(rows):
        row[f"{metric}_holm_adjusted_p"] = adjusted_by_index.get(index)


def _candidate_score(gains: Mapping[str, float]) -> float:
    return float(sum(float(gains[key]) for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")))


def _validation_segment_result(
    baseline_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    *,
    segment: str,
) -> dict[str, Any]:
    baseline_segment = list(_selected(baseline_rows, segment))
    candidate_segment = list(_selected(candidate_rows, segment))
    day_count = len({row["event_date_local"] for row in baseline_segment})
    if not baseline_segment or day_count < 15:
        return {
            "segment": segment,
            "segment_label": SEGMENTS[segment][0],
            "state": "insufficient_validation_sample",
            "validation_day_count": day_count,
            "validation_sample_count": len(baseline_segment),
        }
    baseline_summary = _summary(baseline_segment)
    weights = []
    for raw_weight in WEIGHT_GRID:
        blended_all = _blend_rows(baseline_rows, candidate_rows, segment=segment, weight=raw_weight)
        blended_segment = list(_selected(blended_all, segment))
        summary = _summary(blended_segment)
        gains = _gains(baseline_summary, summary)
        eligible = bool(
            gains["bucket_nll"] > 0.0
            and gains["bucket_rps"] > 0.0
            and gains["mae_c"] > -0.5
            and gains["continue_brier"] > -0.5
        )
        weights.append(
            {
                "raw_weight": raw_weight,
                "summary": summary,
                "gains_pct": gains,
                "eligible": eligible,
                "score": _candidate_score(gains),
            }
        )
    eligible_weights = [row for row in weights if row["eligible"]]
    best = max(eligible_weights, key=lambda row: float(row["score"])) if eligible_weights else weights[0]
    shrinkage_factor = day_count / (day_count + SHRINKAGE_PRIOR_DAYS)
    effective_weight = float(best["raw_weight"]) * shrinkage_factor
    shrunk_all = _blend_rows(baseline_rows, candidate_rows, segment=segment, weight=effective_weight)
    shrunk_segment = list(_selected(shrunk_all, segment))
    shrunk_summary = _summary(shrunk_segment)
    shrunk_gains = _gains(baseline_summary, shrunk_summary)
    paired = _paired_validation(baseline_segment, shrunk_segment)
    return {
        "segment": segment,
        "segment_label": SEGMENTS[segment][0],
        "state": "evaluated",
        "validation_day_count": day_count,
        "validation_sample_count": len(baseline_segment),
        "raw_selected_weight": best["raw_weight"],
        "shrinkage_prior_days": SHRINKAGE_PRIOR_DAYS,
        "shrinkage_factor": shrinkage_factor,
        "effective_weight": effective_weight,
        "baseline_segment": baseline_summary,
        "shrunk_candidate_segment": shrunk_summary,
        "improvement_pct": shrunk_gains,
        "paired_validation": paired,
        "selection_score": _candidate_score(shrunk_gains),
        "weight_grid": weights,
    }


def _rolling_2024_validation(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    windows = [
        ("2024-02-03", "2024-07-01", "2024-07-01", "2024-09-01"),
        ("2024-02-03", "2024-09-01", "2024-09-01", "2024-11-01"),
        ("2024-02-03", "2024-11-01", "2024-11-01", "2025-01-01"),
    ]
    baseline_all: list[dict[str, Any]] = []
    candidate_all: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for index, (train_start, train_end, validation_start, validation_end) in enumerate(windows, start=1):
        train = [row for row in rows if train_start <= str(row["event_date_local"]) < train_end]
        validation = [row for row in rows if validation_start <= str(row["event_date_local"]) < validation_end]
        if not train or not validation:
            continue
        baseline, candidate = _fit_pair(train, validation, tag=f"rolling_2024_{index}")
        baseline_all.extend(baseline)
        candidate_all.extend(candidate)
        traces.append(
            {
                "fold": index,
                "train_start": train_start,
                "train_end_exclusive": train_end,
                "validation_start": validation_start,
                "validation_end_exclusive": validation_end,
                "train_day_count": len({row["event_date_local"] for row in train}),
                "validation_day_count": len({row["event_date_local"] for row in validation}),
                "train_sample_count": len(train),
                "validation_sample_count": len(validation),
            }
        )
    return baseline_all, candidate_all, traces


def _segment_profile(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    profile = {}
    for name, (label, predicate) in SEGMENTS.items():
        selected = [row for row in rows if predicate(row)]
        profile[name] = {
            "segment_label": label,
            "sample_count": len(selected),
            "day_count": len({row["event_date_local"] for row in selected}),
            "sample_rate_pct": 100.0 * len(selected) / len(rows) if rows else 0.0,
        }
    return profile


def _render(report: Mapping[str, Any]) -> str:
    selected = report["selection"]["selected_segment"]
    test = report["held_out_2025"]
    gains = test.get("improvement_selected_segment_pct") or {}
    return "\n".join(
        [
            "# ZUUU ECMWF 分天气状态收缩验证",
            "",
            f"- 2024滚动验证选择：`{selected}`（{report['selection']['selected_segment_label']}）。",
            f"- 2025独立检验：NLL `{float(gains.get('bucket_nll') or 0.0):+.3f}%`、RPS `{float(gains.get('bucket_rps') or 0.0):+.3f}%`、MAE `{float(gains.get('mae_c') or 0.0):+.3f}%`、Brier `{float(gains.get('continue_brier') or 0.0):+.3f}%`。",
            f"- 稳定状态结论：`{'通过 shadow 门' if report['release_gate']['stable_ecmwf_state_found'] else '未找到稳定增益状态'}`；生产接入固定为否。",
            "",
            "## 边界",
            "",
            "- 状态和阈值在运行前固定；2025结果不参与状态或权重选择。",
            "- 分组收缩是两个完整概率分布的凸组合，不在小组内重拟合高维系数。",
            "- ECMWF 历史从 2024-02-03 开始，无法证明跨多年气候稳定性。",
            "- 标签是 NCEI 小时最高温，不是 AMSC 半小时结算结果。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--join-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    all_rows = shadow._load(args.features)
    rows = [row for row in all_rows if row.get("ecmwf__join_available")]
    join_report = json.loads(args.join_report.read_text(encoding="utf-8"))
    if int(join_report.get("future_join_violation_count") or 0) != 0:
        raise SystemExit("future join violation")
    if not rows:
        raise SystemExit("no ECMWF joined rows")

    validation_source = [row for row in rows if str(row["event_date_local"]).startswith("2024-")]
    baseline_validation, candidate_validation, fold_trace = _rolling_2024_validation(validation_source)
    segment_results = [
        _validation_segment_result(baseline_validation, candidate_validation, segment=segment)
        for segment in SEGMENTS
    ]
    eligible_segments = [
        row
        for row in segment_results
        if row.get("state") == "evaluated"
        and float(row.get("effective_weight") or 0.0) > 0.0
        and all(float((row.get("improvement_pct") or {}).get(key) or 0.0) > 0.0 for key in ("bucket_nll", "bucket_rps"))
        and float((row.get("improvement_pct") or {}).get("mae_c") or 0.0) > -0.5
        and float((row.get("improvement_pct") or {}).get("continue_brier") or 0.0) > -0.5
    ]
    selected_result = max(eligible_segments, key=lambda row: float(row["selection_score"])) if eligible_segments else None
    selected_segment = str(selected_result["segment"]) if selected_result else "none"
    selected_weight = float(selected_result["effective_weight"]) if selected_result else 0.0

    train_2024 = [row for row in rows if int(str(row["event_date_local"])[:4]) == 2024]
    test_2025 = [row for row in rows if int(str(row["event_date_local"])[:4]) == 2025]
    baseline_test, candidate_test = _fit_pair(train_2024, test_2025, tag="held_out_2025")
    held_out_diagnostics: list[dict[str, Any]] = []
    for validation_result in segment_results:
        segment = str(validation_result.get("segment") or "")
        if not segment or validation_result.get("state") != "evaluated":
            held_out_diagnostics.append(
                {
                    "segment": segment,
                    "segment_label": validation_result.get("segment_label"),
                    "state": "not_tested_due_to_validation_sample",
                    "validation_state": validation_result.get("state"),
                    "validation_day_count": validation_result.get("validation_day_count"),
                }
            )
            continue
        effective_weight = float(validation_result.get("effective_weight") or 0.0)
        baseline_diag = list(_selected(baseline_test, segment))
        blended_diag_all = _blend_rows(
            baseline_test,
            candidate_test,
            segment=segment,
            weight=effective_weight,
        )
        blended_diag = list(_selected(blended_diag_all, segment))
        baseline_diag_summary = _summary(baseline_diag) if baseline_diag else {"sample_count": 0, "day_count": 0}
        blended_diag_summary = _summary(blended_diag) if blended_diag else {"sample_count": 0, "day_count": 0}
        gains_diag = _gains(baseline_diag_summary, blended_diag_summary) if baseline_diag else {}
        paired_diag = _paired_validation(baseline_diag, blended_diag) if baseline_diag else {}
        held_out_diagnostics.append(
            {
                "segment": segment,
                "segment_label": validation_result.get("segment_label"),
                "state": "diagnostic_only_2025_not_used_for_selection",
                "effective_weight": effective_weight,
                "validation_day_count": validation_result.get("validation_day_count"),
                "validation_improvement_pct": validation_result.get("improvement_pct"),
                "test_day_count": len({row["event_date_local"] for row in baseline_diag}),
                "test_sample_count": len(baseline_diag),
                "baseline_2025": baseline_diag_summary,
                "shrunk_candidate_2025": blended_diag_summary,
                "improvement_2025_pct": gains_diag,
                "paired_2025": paired_diag,
            }
        )
    _holm_adjust(held_out_diagnostics, metric="bucket_nll")
    _holm_adjust(held_out_diagnostics, metric="bucket_rps")
    for diagnostic in held_out_diagnostics:
        gains_diag = diagnostic.get("improvement_2025_pct") if isinstance(diagnostic.get("improvement_2025_pct"), Mapping) else {}
        paired_diag = diagnostic.get("paired_2025") if isinstance(diagnostic.get("paired_2025"), Mapping) else {}
        nll_diag_ci = (paired_diag.get("bucket_nll") or {}).get("bootstrap_95pct_ci") if paired_diag else None
        rps_diag_ci = (paired_diag.get("bucket_rps") or {}).get("bootstrap_95pct_ci") if paired_diag else None
        diagnostic["strict_held_out_pass"] = bool(
            float(diagnostic.get("effective_weight") or 0.0) > 0.0
            and int(diagnostic.get("test_day_count") or 0) >= 30
            and all(float(gains_diag.get(key) or 0.0) > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier"))
            and nll_diag_ci is not None
            and rps_diag_ci is not None
            and float(nll_diag_ci[1]) < 0.0
            and float(rps_diag_ci[1]) < 0.0
            and _number(diagnostic.get("bucket_nll_holm_adjusted_p")) is not None
            and _number(diagnostic.get("bucket_rps_holm_adjusted_p")) is not None
            and float(diagnostic["bucket_nll_holm_adjusted_p"]) < 0.05
            and float(diagnostic["bucket_rps_holm_adjusted_p"]) < 0.05
        )
    if selected_result:
        baseline_test_segment = list(_selected(baseline_test, selected_segment))
        shrunk_test_all = _blend_rows(
            baseline_test,
            candidate_test,
            segment=selected_segment,
            weight=selected_weight,
        )
        shrunk_test_segment = list(_selected(shrunk_test_all, selected_segment))
        baseline_segment_summary = _summary(baseline_test_segment) if baseline_test_segment else {"sample_count": 0, "day_count": 0}
        candidate_segment_summary = _summary(shrunk_test_segment) if shrunk_test_segment else {"sample_count": 0, "day_count": 0}
        test_gains = _gains(baseline_segment_summary, candidate_segment_summary) if baseline_test_segment else {}
        test_paired = _paired_validation(baseline_test_segment, shrunk_test_segment) if baseline_test_segment else {}
        policy_gains = _gains(_summary(baseline_test), _summary(shrunk_test_all))
    else:
        baseline_test_segment = []
        shrunk_test_segment = []
        baseline_segment_summary = {"sample_count": 0, "day_count": 0}
        candidate_segment_summary = {"sample_count": 0, "day_count": 0}
        test_gains = {}
        test_paired = {}
        policy_gains = {key: 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")}

    nll_ci = (test_paired.get("bucket_nll") or {}).get("bootstrap_95pct_ci")
    rps_ci = (test_paired.get("bucket_rps") or {}).get("bootstrap_95pct_ci")
    stable_found = bool(
        selected_result
        and len({row["event_date_local"] for row in baseline_test_segment}) >= 30
        and all(float(test_gains.get(key) or 0.0) > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier"))
        and nll_ci is not None
        and rps_ci is not None
        and float(nll_ci[1]) < 0.0
        and float(rps_ci[1]) < 0.0
        and next(
            (
                bool(row.get("strict_held_out_pass"))
                for row in held_out_diagnostics
                if row.get("segment") == selected_segment
            ),
            False,
        )
    )

    weather_rows = [row for row in rows if int(str(row["event_date_local"])[:4]) in {2024, 2025}]
    archive_window_rows = [
        row
        for row in all_rows
        if "2024-02-03" <= str(row["event_date_local"]) <= "2025-12-31"
    ]
    keys = [(str(row["event_date_local"]), str(row.get("valid_time_utc") or "")) for row in weather_rows]
    quality = {
        "eligible_row_count": len(weather_rows),
        "eligible_day_count": len({row["event_date_local"] for row in weather_rows}),
        "duplicate_event_time_count": len(keys) - len(set(keys)),
        "first_event_date": min(str(row["event_date_local"]) for row in weather_rows),
        "last_event_date": max(str(row["event_date_local"]) for row in weather_rows),
        "segment_profile": _segment_profile(weather_rows),
        "ecmwf_join_quality": (join_report.get("model_quality") or {}).get("ecmwf"),
        "ecmwf_archive_window_start": "2024-02-03",
        "ecmwf_archive_window_candidate_row_count": len(archive_window_rows),
        "ecmwf_archive_window_joined_row_count": len(weather_rows),
        "ecmwf_archive_window_join_rate_pct": (
            100.0 * len(weather_rows) / len(archive_window_rows) if archive_window_rows else 0.0
        ),
        "future_join_violation_count": join_report.get("future_join_violation_count"),
        "current_day_final_max_used_as_feature": False,
        "test_2025_used_for_selection": False,
    }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "question": "which pre-registered weather state, if any, receives stable held-out gain from ECMWF",
        "method": {
            "timezone": "Asia/Shanghai",
            "baseline": "v3.1: predawn GFS+JMA, hours 06-23 GFS",
            "candidate": "same regime augmented by ECMWF",
            "group_shrinkage": "convex probability blend with n_days/(n_days+60) shrinkage",
            "selection": "three rolling-origin 2024 validation windows",
            "final_test": "2025 held out from segment and weight selection",
            "pre_registered_segments": list(SEGMENTS),
            "weight_grid": list(WEIGHT_GRID),
        },
        "rolling_validation_folds": fold_trace,
        "validation_segment_results": segment_results,
        "selection": {
            "selected_segment": selected_segment,
            "selected_segment_label": selected_result.get("segment_label") if selected_result else "无",
            "selected_effective_weight": selected_weight,
            "selected_from_2024_only": True,
            "selected_validation_result": selected_result,
        },
        "held_out_2025": {
            "test_sample_count": len(test_2025),
            "test_day_count": len({row["event_date_local"] for row in test_2025}),
            "selected_segment_sample_count": len(baseline_test_segment),
            "selected_segment_day_count": len({row["event_date_local"] for row in baseline_test_segment}),
            "baseline_selected_segment": baseline_segment_summary,
            "shrunk_candidate_selected_segment": candidate_segment_summary,
            "improvement_selected_segment_pct": test_gains,
            "paired_selected_segment": test_paired,
            "improvement_full_gated_policy_pct": policy_gains,
            "all_pre_registered_segment_diagnostics": held_out_diagnostics,
            "diagnostic_multiple_testing": "Holm adjustment across nonzero-weight segments, separately for NLL and RPS",
            "diagnostics_cannot_change_2024_selection": True,
        },
        "data_quality": quality,
        "release_gate": {
            "stable_ecmwf_state_found": stable_found,
            "shadow_ready": stable_found,
            "production_ready": False,
            "decision": "advance_selected_state_to_forward_shadow" if stable_found else "retain_ecmwf_as_non_influential_shadow",
            "production_blockers": [
                "ecmwf_history_begins_2024_02_03",
                "only_one_independent_held_out_year",
                "ncei_hourly_label_not_amsc_half_hour_settlement_label",
                "production_promotion_not_authorized_by_this_research_audit",
                *([] if stable_found else ["no_state_passed_held_out_distribution_and_uncertainty_gate"]),
            ],
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
    (args.output_dir / "ecmwf_regime_shrinkage_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "ecmwf_regime_shrinkage_report.md").write_text(_render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "selected_segment": selected_segment,
                "selected_weight": selected_weight,
                "stable_ecmwf_state_found": stable_found,
                "held_out_2025_improvement": test_gains,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if stable_found else 2


if __name__ == "__main__":
    raise SystemExit(main())
