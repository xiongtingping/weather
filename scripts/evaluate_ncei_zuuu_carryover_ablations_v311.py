#!/usr/bin/env python3
"""Nested, year-forward ablation audit for ZUUU pre-dawn carryover features."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import calibrate_ncei_zuuu_remaining_warming_v2 as v2
from scripts import train_ncei_zuuu_multimodel_allday_bayes_v31 as v31
from scripts import train_ncei_zuuu_multimodel_carryover_bayes_v311 as v311


SCHEMA = "ncei_zuuu_predawn_carryover_nested_ablation_audit_v311"
FEATURE_GROUPS = {
    "first_temperature_path": {
        "numeric": [
            "carryover_first_minus_climo_c",
            "carryover_current_minus_first_c",
        ],
        "boolean": ["pre_dawn_carryover_phase"],
        "physical_role": "retained midnight heat anomaly and subsequent pre-dawn change",
    },
    "previous_evening_retention": {
        "numeric": [
            "carryover_previous_day_last_temperature_c",
            "carryover_previous_last_minus_current_c",
        ],
        "boolean": ["pre_dawn_carryover_phase", "previous_local_day_summary_available"],
        "physical_role": "temperature continuity from previous local evening",
    },
    "previous_day_max_gap": {
        "numeric": [
            "carryover_previous_day_observed_max_c",
            "carryover_previous_max_minus_current_c",
        ],
        "boolean": ["pre_dawn_carryover_phase", "previous_local_day_summary_available"],
        "physical_role": "cooling distance from completed previous-day maximum",
    },
    "compressed_carryover": {
        "numeric": [
            "carryover_first_minus_climo_c",
            "carryover_current_minus_first_c",
            "carryover_previous_last_minus_current_c",
        ],
        "boolean": ["pre_dawn_carryover_phase", "previous_local_day_summary_available"],
        "physical_role": "three non-duplicated anomalies spanning previous evening to current report",
    },
    "predawn_peak_path_interaction": {
        "numeric": [
            "carryover_drop_from_current_day_max_c",
            "carryover_hours_since_current_day_max",
        ],
        "boolean": ["pre_dawn_carryover_phase"],
        "physical_role": "pre-dawn interaction of drop and peak age already present as all-day main effects",
    },
}


def _gated(gfs: Sequence[Mapping[str, Any]], predawn: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(candidate if float(base["local_hour"]) < 6.0 else base)
        for base, candidate in zip(gfs, predawn)
    ]


def _gain(baseline: Mapping[str, Any], candidate: Mapping[str, Any], key: str) -> float:
    old = float(baseline[key])
    new = float(candidate[key])
    return 100.0 * (old - new) / old if old else 0.0


def _gain_map(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, float]:
    return {key: _gain(baseline, candidate, key) for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")}


def _select_from_prior_years(
    fold_predictions: Mapping[int, Mapping[str, Sequence[Mapping[str, Any]]]],
    test_year: int,
) -> tuple[str, dict[str, Any]]:
    prior_years = sorted(year for year in fold_predictions if year < test_year)
    if not prior_years:
        return "baseline_v31", {"reason": "no_prior_out_of_sample_year_available", "prior_years": []}
    baseline = [row for year in prior_years for row in fold_predictions[year]["baseline_v31"]]
    baseline_summary = v2._weighted_summary(baseline)
    candidates: dict[str, Any] = {}
    for group in FEATURE_GROUPS:
        values = [row for year in prior_years for row in fold_predictions[year][group]]
        summary = v2._weighted_summary(values)
        gains = _gain_map(baseline_summary, summary)
        eligible = bool(
            all(gains[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c"))
            and gains["continue_brier"] >= 0.0
        )
        score = sum(gains[key] for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier"))
        candidates[group] = {"summary": summary, "gains_pct": gains, "eligible": eligible, "score": score}
    eligible_groups = [name for name, result in candidates.items() if result["eligible"]]
    selected = max(eligible_groups, key=lambda name: float(candidates[name]["score"])) if eligible_groups else "baseline_v31"
    return selected, {
        "reason": "best_all_core_metrics_positive_on_prior_oos_years" if eligible_groups else "no_feature_group_passed_prior_oos_gate",
        "prior_years": prior_years,
        "candidate_results": candidates,
    }


def _render(report: Mapping[str, Any]) -> str:
    nested = report["nested_policy"]
    gains = nested["improvement_vs_v31_pct"]
    next_group = report["selection_for_next_shadow_period"]["selected_group"]
    return "\n".join(
        [
            "# ZUUU v3.1.1 跨午夜余热：嵌套消融审计",
            "",
            f"- 嵌套策略相对 v3.1：NLL `{gains['bucket_nll']:+.3f}%`、RPS `{gains['bucket_rps']:+.3f}%`、MAE `{gains['mae_c']:+.3f}%`、Brier `{gains['continue_brier']:+.3f}%`。",
            f"- 下一 shadow 周期的时间外选择：`{next_group}`。",
            f"- 结论：`{'仅允许进入 shadow' if report['release_gate']['ready_for_amsc_shadow'] else '保留 v3.1，不进入 shadow'}`；生产替换固定为否。",
            "",
            "每个测试年只能使用更早测试年的样本外结果选择特征组；没有更早样本外年份时回退 v3.1。",
            "当前日最终最高温从未作为输入；前一日摘要按 Asia/Shanghai 精确前一自然日连接。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--join-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    rows, profile = v311._augment_carryover(
        v31._load(args.features),
        station_summary_rows=v311._load_station_summary_rows(args.features),
    )
    join_report = json.loads(args.join_report.read_text(encoding="utf-8"))
    if int(join_report.get("future_join_violation_count") or 0) != 0:
        raise SystemExit("future multimodel join violation")
    years = sorted({int(str(row["event_date_local"])[:4]) for row in rows})
    if years != [2021, 2022, 2023, 2024, 2025]:
        raise SystemExit(f"unexpected years: {years}")

    fold_predictions: dict[int, dict[str, list[dict[str, Any]]]] = {}
    folds: list[dict[str, Any]] = []
    for test_year in years[1:]:
        train = [row for row in rows if int(str(row["event_date_local"])[:4]) < test_year]
        test = [row for row in rows if int(str(row["event_date_local"])[:4]) == test_year]
        _, _, gfs = v2._fit(
            train,
            test,
            version="gfs",
            design_override=v31.AllDayDesign(v31.GFS_NUMERIC, v31.BOOLEAN),
        )
        _, _, multimodel = v2._fit(
            train,
            test,
            version="multimodel",
            design_override=v31.AllDayDesign(v31.MULTIMODEL_NUMERIC, v31.BOOLEAN),
        )
        outputs: dict[str, list[dict[str, Any]]] = {"baseline_v31": _gated(gfs, multimodel)}
        for name, group in FEATURE_GROUPS.items():
            numeric = [*v31.MULTIMODEL_NUMERIC, *group["numeric"]]
            boolean = [*v31.BOOLEAN, *group["boolean"]]
            _, _, candidate = v2._fit(
                train,
                test,
                version=name,
                design_override=v311.CarryoverDesign(numeric, boolean),
            )
            outputs[name] = _gated(gfs, candidate)
        fold_predictions[test_year] = outputs
        fold_summary = {
            "test_year": test_year,
            "train_day_count": len({row["event_date_local"] for row in train}),
            "test_day_count": len({row["event_date_local"] for row in test}),
            "versions": {name: v2._weighted_summary(values) for name, values in outputs.items()},
        }
        folds.append(fold_summary)

    nested_predictions: list[dict[str, Any]] = []
    nested_choices: list[dict[str, Any]] = []
    baseline_predictions: list[dict[str, Any]] = []
    for test_year in years[1:]:
        selected, trace = _select_from_prior_years(fold_predictions, test_year)
        selected_values = fold_predictions[test_year][selected]
        nested_predictions.extend(selected_values)
        baseline_predictions.extend(fold_predictions[test_year]["baseline_v31"])
        nested_choices.append({"test_year": test_year, "selected_group": selected, "selection_trace": trace})

    baseline_summary = v2._weighted_summary(baseline_predictions)
    nested_summary = v2._weighted_summary(nested_predictions)
    nested_gains = _gain_map(baseline_summary, nested_summary)
    nested_paired = v311._paired_day_validation(baseline_predictions, nested_predictions, predawn_only=True)

    next_group, next_trace = _select_from_prior_years(fold_predictions, 2026)
    next_ready = next_group != "baseline_v31"
    nll_ci = nested_paired["metrics"]["bucket_nll"]["bootstrap_95pct_ci"]
    rps_ci = nested_paired["metrics"]["bucket_rps"]["bootstrap_95pct_ci"]
    ready_for_shadow = bool(
        next_ready
        and all(nested_gains[key] > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c"))
        and nested_gains["continue_brier"] >= 0.0
        and nll_ci is not None
        and rps_ci is not None
        and float(nll_ci[1]) <= 0.0
        and float(rps_ci[1]) <= 0.0
        and float(profile["pre_dawn_previous_day_summary_coverage_pct"]) >= 85.0
    )
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "method": {
            "outer_split": "strict expanding-year walk-forward 2022-2025",
            "selection_rule": "each outer year may use only earlier outer-year results",
            "timezone": "Asia/Shanghai",
            "feature_groups_fixed_before_ablation_run": list(FEATURE_GROUPS),
            "current_day_final_max_used_as_feature": False,
        },
        "feature_groups": FEATURE_GROUPS,
        "folds": folds,
        "nested_policy": {
            "choices": nested_choices,
            "baseline_v31": baseline_summary,
            "nested_candidate": nested_summary,
            "improvement_vs_v31_pct": nested_gains,
            "paired_predawn_day_validation": nested_paired,
        },
        "selection_for_next_shadow_period": {
            "selected_group": next_group,
            "selection_trace": next_trace,
            "selection_uses_2026_outcomes": False,
        },
        "data_quality": {
            **profile,
            "future_multimodel_join_violation_count": join_report.get("future_join_violation_count"),
            "multimodel_join_rate_pct": join_report.get("all_models_join_rate_pct"),
        },
        "release_gate": {
            "ready_for_amsc_shadow": ready_for_shadow,
            "production_ready": False,
            "decision": "advance_selected_group_to_amsc_shadow" if ready_for_shadow else "retain_v31_without_carryover_probability_feature",
            "production_blockers": [
                "ncei_hourly_label_not_amsc_half_hour_settlement_label",
                "live_shadow_stability_not_observed",
                "production_replacement_not_authorized_by_this_audit",
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
    (args.output_dir / "nested_ablation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "nested_ablation_report.md").write_text(_render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "ready_for_amsc_shadow": ready_for_shadow,
                "production_ready": False,
                "next_selected_group": next_group,
                "nested_improvement_vs_v31_pct": nested_gains,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if ready_for_shadow else 2


if __name__ == "__main__":
    raise SystemExit(main())
