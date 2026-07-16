#!/usr/bin/env python3
"""Validate a causal pre-dawn thermal-carryover extension to ZUUU v3.1.

The candidate is deliberately research/shadow only.  It adds summaries that
are already observable at prediction time: the previous local day's final
observed temperature and maximum, plus the current local day's first report.
The current day's final maximum is used only as the label.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2
from scripts import train_ncei_zuuu_multimodel_allday_bayes_v31 as v31
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


SCHEMA = "ncei_zuuu_multimodel_predawn_carryover_validation_v311"
MODEL_SCHEMA = "ncei_zuuu_zero_inflated_multimodel_predawn_carryover_artifact_v311"
VERSION = "v3.1.1_predawn_carryover_shadow"

CARRYOVER_NUMERIC = [
    "carryover_previous_day_last_temperature_c",
    "carryover_previous_day_observed_max_c",
    "carryover_first_temperature_c",
    "carryover_previous_last_minus_current_c",
    "carryover_previous_max_minus_current_c",
    "carryover_current_minus_first_c",
    "carryover_first_minus_climo_c",
    "carryover_drop_from_current_day_max_c",
    "carryover_hours_since_current_day_max",
]
CARRYOVER_BOOLEAN = [
    "pre_dawn_carryover_phase",
    "previous_local_day_summary_available",
]
CANDIDATE_NUMERIC = [*v31.MULTIMODEL_NUMERIC, *CARRYOVER_NUMERIC]
CANDIDATE_BOOLEAN = [*v31.BOOLEAN, *CARRYOVER_BOOLEAN]


def _number(value: Any) -> float | None:
    return v1._number(value)


def _load_station_summary_rows(path: Path) -> list[dict[str, Any]]:
    """Load station facts independently of numerical-model join coverage."""
    rows: list[dict[str, Any]] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                continue
            year = int(str(raw.get("event_date_local") or "0000")[:4])
            hour = _number(raw.get("local_hour"))
            if (
                2021 <= year <= 2025
                and raw.get("daily_max_label_eligible")
                and not raw.get("core_qc_bad")
                and hour is not None
                and 0.0 <= hour < 24.0
                and _number(raw.get("temperature_c")) is not None
                and _number(raw.get("final_daily_max_c")) is not None
            ):
                rows.append(dict(raw))
    return rows


def _augment_carryover(
    rows: Sequence[Mapping[str, Any]],
    *,
    station_summary_rows: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Add causal local-day carryover summaries without using today's label."""
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        by_day[str(row["event_date_local"])].append(row)
    for values in by_day.values():
        values.sort(key=lambda row: (float(row["local_hour"]), str(row.get("valid_time_utc") or "")))

    summary_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in station_summary_rows or rows:
        summary_by_day[str(raw["event_date_local"])].append(dict(raw))
    for values in summary_by_day.values():
        values.sort(key=lambda row: (float(row["local_hour"]), str(row.get("valid_time_utc") or "")))

    daily_summary: dict[str, dict[str, Any]] = {}
    for day, values in summary_by_day.items():
        temperatures = [_number(row.get("temperature_c")) for row in values]
        valid_temperatures = [value for value in temperatures if value is not None]
        maxima = [_number(row.get("final_daily_max_c")) for row in values]
        final_max = next((value for value in maxima if value is not None), None)
        first = next((row for row in values if _number(row.get("temperature_c")) is not None), None)
        last = next((row for row in reversed(values) if _number(row.get("temperature_c")) is not None), None)
        daily_summary[day] = {
            "first_temperature_c": _number(first.get("temperature_c")) if first else None,
            "first_hour_local": _number(first.get("local_hour")) if first else None,
            "last_temperature_c": _number(last.get("temperature_c")) if last else None,
            "last_hour_local": _number(last.get("local_hour")) if last else None,
            "observed_max_c": final_max if final_max is not None else (max(valid_temperatures) if valid_temperatures else None),
        }

    augmented: list[dict[str, Any]] = []
    predawn_count = 0
    previous_day_available_count = 0
    first_report_after_01_count = 0
    for day in sorted(by_day):
        current_summary = daily_summary[day]
        previous_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        previous_summary = daily_summary.get(previous_day) or {}
        previous_available = bool(
            _number(previous_summary.get("last_temperature_c")) is not None
            and _number(previous_summary.get("observed_max_c")) is not None
            and _number(previous_summary.get("last_hour_local")) is not None
            and float(previous_summary["last_hour_local"]) >= 18.0
        )
        first_temperature = _number(current_summary.get("first_temperature_c"))
        first_hour = _number(current_summary.get("first_hour_local"))
        if first_hour is not None and first_hour > 1.0:
            first_report_after_01_count += 1
        for raw in by_day[day]:
            row = dict(raw)
            hour = float(row["local_hour"])
            predawn = 0.0 <= hour < 6.0
            current_temperature = _number(row.get("temperature_c"))
            current_observed_max = _number(row.get("observed_max_to_time_c"))
            previous_last = _number(previous_summary.get("last_temperature_c")) if previous_available else None
            previous_max = _number(previous_summary.get("observed_max_c")) if previous_available else None
            drop = _number(row.get("drop_from_observed_max_c"))
            peak_age = _number(row.get("hours_since_observed_max"))

            # The numeric values are zero outside pre-dawn so their slopes are
            # true interactions, not duplicated all-day main effects.
            row.update(
                {
                    "pre_dawn_carryover_phase": predawn,
                    "previous_local_day_summary_available": bool(predawn and previous_available),
                    "carryover_previous_day_last_temperature_c": previous_last if predawn else 0.0,
                    "carryover_previous_day_observed_max_c": previous_max if predawn else 0.0,
                    "carryover_first_temperature_c": first_temperature if predawn else 0.0,
                    "carryover_previous_last_minus_current_c": (
                        previous_last - current_temperature
                        if predawn and previous_last is not None and current_temperature is not None
                        else (None if predawn else 0.0)
                    ),
                    "carryover_previous_max_minus_current_c": (
                        previous_max - current_temperature
                        if predawn and previous_max is not None and current_temperature is not None
                        else (None if predawn else 0.0)
                    ),
                    "carryover_current_minus_first_c": (
                        current_temperature - first_temperature
                        if predawn and current_temperature is not None and first_temperature is not None
                        else (None if predawn else 0.0)
                    ),
                    "carryover_drop_from_current_day_max_c": drop if predawn else 0.0,
                    "carryover_hours_since_current_day_max": peak_age if predawn else 0.0,
                }
            )
            augmented.append(row)
            predawn_count += int(predawn)
            previous_day_available_count += int(predawn and previous_available)

    profile = {
        "row_count": len(augmented),
        "day_count": len(by_day),
        "pre_dawn_row_count": predawn_count,
        "pre_dawn_previous_day_summary_available_count": previous_day_available_count,
        "pre_dawn_previous_day_summary_coverage_pct": (
            100.0 * previous_day_available_count / predawn_count if predawn_count else 0.0
        ),
        "days_first_available_report_after_01_count": first_report_after_01_count,
        "current_day_label_used_as_feature": False,
        "previous_day_summary_rule": "exact_previous_Asia_Shanghai_local_day_only",
        "station_summary_day_count": len(summary_by_day),
        "station_summary_independent_of_model_join": True,
    }
    return augmented, profile


def _candidate_values(row: Mapping[str, Any], climo: float) -> dict[str, float | None]:
    values = v31._values(row, climo)
    predawn = bool(row.get("pre_dawn_carryover_phase"))
    first_temperature = _number(row.get("carryover_first_temperature_c"))
    for name in CARRYOVER_NUMERIC:
        values[name] = _number(row.get(name))
    values["carryover_first_minus_climo_c"] = (
        first_temperature - climo if predawn and first_temperature is not None else (0.0 if not predawn else None)
    )
    for name in CARRYOVER_BOOLEAN:
        values[name] = 1.0 if row.get(name) else 0.0
    return values


class CarryoverDesign(v1.Design):
    def _raw(self, rows: Sequence[Mapping[str, Any]], climo_values: Sequence[float]) -> np.ndarray:
        matrix = []
        for row, climo in zip(rows, climo_values):
            values = _candidate_values(row, climo)
            numeric = [float(values[name]) if values.get(name) is not None else float("nan") for name in self.numeric]
            missing = [1.0 if values.get(name) is None else 0.0 for name in self.numeric]
            boolean = [float(values.get(name) or 0.0) for name in self.boolean]
            matrix.append([*numeric, *missing, *boolean])
        return np.asarray(matrix, dtype=float)


def _improvement(old: float, new: float) -> float:
    return 100.0 * (old - new) / old if old else 0.0


def _gains(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, float]:
    return {
        key: _improvement(float(baseline[key]), float(candidate[key]))
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }


def _segments(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    definitions = {
        "all": lambda row: True,
        "pre_dawn_00_05": lambda row: 0.0 <= float(row["local_hour"]) < 6.0,
        "hot_season_pre_dawn": lambda row: 5 <= int(row["month"]) <= 9 and float(row["local_hour"]) < 6.0,
        "pre_dawn_drop_ge_1c": lambda row: float(row["local_hour"]) < 6.0 and float(row.get("drop_from_observed_max_c") or 0.0) >= 1.0,
        "daytime_06_23": lambda row: 6.0 <= float(row["local_hour"]) < 24.0,
    }
    return {name: v2._weighted_summary([row for row in rows if predicate(row)]) for name, predicate in definitions.items()}


def _prediction_loss(row: Mapping[str, Any], metric: str) -> float:
    if metric in {"bucket_nll", "bucket_rps"}:
        return float(row[metric])
    if metric == "mae_c":
        return abs(float(row["expected_final_max_c"]) - float(row["final_daily_max_c"]))
    if metric == "continue_brier":
        actual = 1.0 if row["actual_continue"] else 0.0
        return (float(row["predicted_continue_probability"]) - actual) ** 2
    raise KeyError(metric)


def _paired_day_validation(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    *,
    predawn_only: bool,
) -> dict[str, Any]:
    baseline_by_key = {
        (str(row["event_date_local"]), float(row["local_hour"])): row
        for row in baseline
        if not predawn_only or float(row["local_hour"]) < 6.0
    }
    candidate_by_key = {
        (str(row["event_date_local"]), float(row["local_hour"])): row
        for row in candidate
        if not predawn_only or float(row["local_hour"]) < 6.0
    }
    paired_keys = sorted(set(baseline_by_key) & set(candidate_by_key))
    day_keys: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for key in paired_keys:
        day_keys[key[0]].append(key)
    metrics: dict[str, Any] = {}
    rng = np.random.default_rng(311)
    for metric in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier"):
        deltas = np.asarray(
            [
                float(
                    np.mean(
                        [
                            _prediction_loss(candidate_by_key[key], metric)
                            - _prediction_loss(baseline_by_key[key], metric)
                            for key in keys
                        ]
                    )
                )
                for keys in day_keys.values()
            ],
            dtype=float,
        )
        if len(deltas):
            samples = rng.choice(deltas, size=(5000, len(deltas)), replace=True).mean(axis=1)
            ci_low, ci_high = np.quantile(samples, [0.025, 0.975])
        else:
            ci_low = ci_high = float("nan")
        metrics[metric] = {
            "candidate_minus_baseline_mean": float(np.mean(deltas)) if len(deltas) else None,
            "candidate_minus_baseline_median": float(np.median(deltas)) if len(deltas) else None,
            "bootstrap_95pct_ci": [float(ci_low), float(ci_high)] if len(deltas) else None,
            "candidate_better_day_count": int(np.sum(deltas < 0.0)),
            "candidate_worse_day_count": int(np.sum(deltas > 0.0)),
            "equal_day_count": int(np.sum(np.isclose(deltas, 0.0))),
        }
    return {
        "pairing_grain": "event_date_local_then_equal_weight_within_day",
        "paired_observation_count": len(paired_keys),
        "paired_day_count": len(day_keys),
        "metrics": metrics,
    }


def _feature_importance(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    names = list(artifact.get("feature_names") or [])
    zero = list(artifact.get("peak_already_coefficients") or [])
    positive = list(artifact.get("positive_remaining_coefficients") or [])
    output = []
    for name, zero_value, positive_value in zip(names, zero, positive):
        if name.startswith(("carryover_", "pre_dawn_carryover", "previous_local_day")):
            output.append(
                {
                    "feature": name,
                    "peak_already_coefficient": round(float(zero_value), 6),
                    "positive_remaining_coefficient": round(float(positive_value), 6),
                }
            )
    return output


def _render(report: Mapping[str, Any]) -> str:
    gains = report["improvement_candidate_vs_v31_pct"]
    predawn = report["pre_dawn_improvement_candidate_vs_v31_pct"]
    gate = report["release_gate"]
    return "\n".join(
        [
            "# ZUUU v3.1.1 跨午夜余热候选：严格样本外验证",
            "",
            f"- 总体结论：`{'可进入 AMSC shadow' if gate['ready_for_amsc_shadow'] else '暂不进入 AMSC shadow'}`；生产替换固定为 `否`。",
            f"- 全时段相对 v3.1：NLL `{gains['bucket_nll']:+.3f}%`、RPS `{gains['bucket_rps']:+.3f}%`、MAE `{gains['mae_c']:+.3f}%`、Brier `{gains['continue_brier']:+.3f}%`。",
            f"- 凌晨 00–05 时相对 v3.1：NLL `{predawn['bucket_nll']:+.3f}%`、RPS `{predawn['bucket_rps']:+.3f}%`、MAE `{predawn['mae_c']:+.3f}%`、Brier `{predawn['continue_brier']:+.3f}%`。",
            f"- 前一日摘要覆盖率：`{report['data_quality']['pre_dawn_previous_day_summary_coverage_pct']:.2f}%`。",
            "",
            "## 方法边界",
            "",
            "- 2022–2025 逐年扩展窗口；测试年从未参与当年拟合。",
            "- 前一日最高温只来自已完成的前一北京时间自然日；今天最终最高温只作标签，不作输入。",
            "- 候选只替换凌晨 GFS＋JMA 子模型，06–23 时与 v3.1 使用完全相同的 GFS 子模型。",
            "- NCEI 为小时标签，并非 AMSC 半小时市场结算标签，因此即使通过也只进入 shadow。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--join-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    rows, profile = _augment_carryover(
        v31._load(args.features),
        station_summary_rows=_load_station_summary_rows(args.features),
    )
    if not rows:
        raise SystemExit("no eligible v3.1 rows")
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    if years != [2021, 2022, 2023, 2024, 2025]:
        raise SystemExit(f"unexpected training years: {years}")
    join_report = json.loads(args.join_report.read_text(encoding="utf-8"))
    if int(join_report.get("future_join_violation_count") or 0) != 0:
        raise SystemExit("future multimodel join violation")

    predictions: dict[str, list[dict[str, Any]]] = {"v31": [], "v311": []}
    folds: list[dict[str, Any]] = []
    for test_year in years[1:]:
        train = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        fold_predictions: dict[str, list[dict[str, Any]]] = {}
        for name, design in (
            ("gfs", v31.AllDayDesign(v31.GFS_NUMERIC, v31.BOOLEAN)),
            ("multimodel", v31.AllDayDesign(v31.MULTIMODEL_NUMERIC, v31.BOOLEAN)),
            ("carryover", CarryoverDesign(CANDIDATE_NUMERIC, CANDIDATE_BOOLEAN)),
        ):
            _, _, values = v2._fit(train, test, version=name, design_override=design)
            fold_predictions[name] = values
        baseline = [
            dict(multimodel if float(gfs["local_hour"]) < 6.0 else gfs)
            for gfs, multimodel in zip(fold_predictions["gfs"], fold_predictions["multimodel"])
        ]
        candidate = [
            dict(carryover if float(gfs["local_hour"]) < 6.0 else gfs)
            for gfs, carryover in zip(fold_predictions["gfs"], fold_predictions["carryover"])
        ]
        predictions["v31"].extend(baseline)
        predictions["v311"].extend(candidate)
        folds.append(
            {
                "test_year": test_year,
                "train_day_count": len({row["event_date_local"] for row in train}),
                "test_day_count": len({row["event_date_local"] for row in test}),
                "v31": v2._weighted_summary(baseline),
                "v311": v2._weighted_summary(candidate),
            }
        )

    _, final_artifact, _ = v2._fit(
        rows,
        rows,
        version="v311_carryover",
        design_override=CarryoverDesign(CANDIDATE_NUMERIC, CANDIDATE_BOOLEAN),
    )
    aggregate = {name: v2._weighted_summary(values) for name, values in predictions.items()}
    segments = {name: _segments(values) for name, values in predictions.items()}
    gains = _gains(aggregate["v31"], aggregate["v311"])
    predawn_gains = _gains(segments["v31"]["pre_dawn_00_05"], segments["v311"]["pre_dawn_00_05"])
    hot_predawn_gains = _gains(segments["v31"]["hot_season_pre_dawn"], segments["v311"]["hot_season_pre_dawn"])
    drop_gains = _gains(segments["v31"]["pre_dawn_drop_ge_1c"], segments["v311"]["pre_dawn_drop_ge_1c"])
    year_wins = sum(
        float(fold["v311"]["bucket_nll"]) < float(fold["v31"]["bucket_nll"])
        and float(fold["v311"]["bucket_rps"]) < float(fold["v31"]["bucket_rps"])
        for fold in folds
    )
    paired_all = _paired_day_validation(predictions["v31"], predictions["v311"], predawn_only=False)
    paired_predawn = _paired_day_validation(predictions["v31"], predictions["v311"], predawn_only=True)
    predawn_nll_ci = paired_predawn["metrics"]["bucket_nll"]["bootstrap_95pct_ci"]
    predawn_rps_ci = paired_predawn["metrics"]["bucket_rps"]["bootstrap_95pct_ci"]
    coverage = float(profile["pre_dawn_previous_day_summary_coverage_pct"])
    ready_for_shadow = bool(
        all(gains[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c"))
        and gains["continue_brier"] >= 0.0
        and all(predawn_gains[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier"))
        and all(hot_predawn_gains[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c"))
        and predawn_nll_ci is not None
        and predawn_rps_ci is not None
        and float(predawn_nll_ci[1]) < 0.0
        and float(predawn_rps_ci[1]) < 0.0
        and year_wins >= 3
        and coverage >= 95.0
    )

    artifact = {
        **final_artifact,
        "schema": MODEL_SCHEMA,
        "version": VERSION,
        "trained_through_year": 2025,
        "training_sample_count": len(rows),
        "training_day_count": len({row["event_date_local"] for row in rows}),
        "carryover_feature_contract": {
            "timezone": "Asia/Shanghai",
            "phase": "00:00-05:59",
            "previous_day_rule": "exact_previous_local_day_only",
            "current_day_final_max_feature_allowed": False,
            "previous_day_completed_observation_allowed": True,
        },
        "ready_for_amsc_shadow": ready_for_shadow,
        "production_ready": False,
        "research_only": True,
        "activation_mode": "shadow_only" if ready_for_shadow else "blocked",
    }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "question": "does explicit causal pre-dawn carryover improve v3.1 daily-high probabilities",
        "method": {
            "split": "strict expanding-year walk-forward",
            "test_years": years[1:],
            "timezone": "Asia/Shanghai",
            "pairing_grain": "local event day",
            "baseline": "v3.1 pre-dawn GFS+JMA, hours 06-23 GFS",
            "candidate": "v3.1.1 carryover-augmented pre-dawn GFS+JMA, hours 06-23 unchanged GFS",
            "label": "NCEI local-day hourly maximum",
            "feature_availability": "current or completed previous local day only",
        },
        "folds": folds,
        "aggregate": aggregate,
        "segments": segments,
        "improvement_candidate_vs_v31_pct": gains,
        "pre_dawn_improvement_candidate_vs_v31_pct": predawn_gains,
        "hot_season_pre_dawn_improvement_candidate_vs_v31_pct": hot_predawn_gains,
        "pre_dawn_drop_ge_1c_improvement_candidate_vs_v31_pct": drop_gains,
        "year_wins_nll_and_rps": year_wins,
        "paired_day_validation": {"all": paired_all, "pre_dawn": paired_predawn},
        "carryover_feature_coefficients": _feature_importance(final_artifact),
        "data_quality": {
            **profile,
            "source": str(args.features),
            "source_years": years,
            "future_multimodel_join_violation_count": join_report.get("future_join_violation_count"),
            "multimodel_join_rate_pct": join_report.get("all_models_join_rate_pct"),
        },
        "release_gate": {
            "ready_for_amsc_shadow": ready_for_shadow,
            "production_ready": False,
            "production_blockers": [
                "amsc_half_hour_settlement_label_validation_required",
                "live_previous_day_summary_runtime_not_integrated",
                "shadow_stability_window_not_completed",
            ],
            "decision": "advance_to_amsc_shadow" if ready_for_shadow else "retain_v31_production",
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
    (args.output_dir / "model_artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "walk_forward_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "walk_forward_report.md").write_text(_render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "ready_for_amsc_shadow": ready_for_shadow,
                "production_ready": False,
                "improvement_candidate_vs_v31_pct": gains,
                "pre_dawn_improvement_candidate_vs_v31_pct": predawn_gains,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if ready_for_shadow else 2


if __name__ == "__main__":
    raise SystemExit(main())
