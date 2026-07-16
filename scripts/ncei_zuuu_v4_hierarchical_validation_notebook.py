#!/usr/bin/env python3
"""# %% Reproducible validation notebook for hierarchical ZUUU Bayes v4."""

from __future__ import annotations

import argparse
import gzip
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "ncei_zuuu_v4_hierarchical_validation_notebook_v1"


def _check(name: str, passed: bool, detail: Any, severity: str = "blocker") -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "severity": severity, "detail": detail}


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    return False


def _gain(old: float, new: float) -> float:
    return 100.0 * (old - new) / old


def _render(result: dict[str, Any]) -> str:
    lines = [
        "# ZUUU 分层联合 Bayes v4 验证报告",
        "",
        f"## Overall Assessment: {result['overall_assessment']}",
        "",
        f"阻断检查 `{result['passed_blocker_checks']}/{result['blocker_check_count']}` 通过；已知警告 `{len(result['caveats'])}` 项。",
        "",
        "## Methodology Review",
        "",
        "- 外层验证严格按年份前推：2022、2023、2024、2025只使用更早年份训练。",
        "- 先验层只读取季节、时刻和四川盆地大尺度场；机场温度、露点、云雨、地面风、能见度和气压仅进入第二层。",
        "- 第二层同时读取动态先验摘要、完整实况物理变量以及盆地背景×实况交互项。",
        "",
        "## Calculation Spot-Checks",
        "",
        "| 检查 | 结果 | 证据 |",
        "|---|---|---|",
    ]
    for row in result["checks"]:
        detail = json.dumps(row["detail"], ensure_ascii=False, sort_keys=True)
        lines.append(f"| {row['check']} | {'通过' if row['passed'] else '未通过'} | `{detail}` |")
    lines.extend(["", "## Required Caveats for Stakeholders", ""])
    lines.extend(f"- {item}" for item in result["caveats"])
    lines.extend(["", "## Takeaways", "", result["takeaway"], ""])
    return "\n".join(lines)


# %% Load persisted evidence.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--join-report", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads((args.model_dir / "walk_forward_report.json").read_text(encoding="utf-8"))
    artifact = json.loads((args.model_dir / "model_artifact.json").read_text(encoding="utf-8"))
    join = json.loads(args.join_report.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    # %% Profile the model feature table at its intended observation-time grain.
    total_rows = 0
    valid_times: set[str] = set()
    model_rows = 0
    model_dates: set[str] = set()
    label_contradictions = 0
    missing_required = {name: 0 for name in ("temperature_c", "dewpoint_c", "pressure_hpa", "wind_speed_mps", "cloud_cover_pct")}
    min_time = ""
    max_time = ""
    with gzip.open(args.features, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            total_rows += 1
            valid_time = str(row.get("valid_time_utc") or "")
            valid_times.add(valid_time)
            min_time = valid_time if not min_time or valid_time < min_time else min_time
            max_time = valid_time if not max_time or valid_time > max_time else max_time
            hour = row.get("local_hour")
            eligible = bool(row.get("daily_max_label_eligible")) and not bool(row.get("core_qc_bad")) and hour is not None and 6.0 <= float(hour) <= 19.0 and row.get("remaining_warming_c") is not None and row.get("observed_max_to_time_c") is not None
            if not eligible:
                continue
            model_rows += 1
            model_dates.add(str(row["event_date_local"]))
            final_max = float(row["final_daily_max_c"])
            observed_max = float(row["observed_max_to_time_c"])
            remaining = float(row["remaining_warming_c"])
            if final_max + 1e-9 < observed_max or abs((final_max - observed_max) - remaining) > 1e-9:
                label_contradictions += 1
            for name in missing_required:
                if row.get(name) in (None, ""):
                    missing_required[name] += 1
    feature_profile = {
        "total_rows": total_rows,
        "unique_valid_times": len(valid_times),
        "model_rows": model_rows,
        "model_day_count": len(model_dates),
        "valid_time_min": min_time,
        "valid_time_max": max_time,
        "label_contradictions": label_contradictions,
        "missing_required_rate_pct": {name: 100.0 * count / model_rows for name, count in missing_required.items()},
    }
    checks.append(_check("feature_grain_uniqueness_and_label_consistency", total_rows == 43281 and len(valid_times) == total_rows and model_rows == 22260 and len(model_dates) == 1590 and label_contradictions == 0, feature_profile))

    # %% Validate source grain, causal join, and time split.
    checks.append(
        _check(
            "source_join_complete_and_causal",
            int(join["joined_rows"]) == 43281
            and float(join["join_rate_pct"]) == 100.0
            and int(join["future_join_violation_count"]) == 0
            and float(join["lag_hours_max"]) <= 5.0,
            {key: join[key] for key in ("joined_rows", "join_rate_pct", "future_join_violation_count", "lag_hours_min", "lag_hours_max")},
        )
    )
    fold_years = [int(fold["test_year"]) for fold in report["folds"]]
    checks.append(_check("strict_expanding_year_split", fold_years == [2022, 2023, 2024, 2025] and report["method"]["split"] == "strict expanding-year walk-forward", {"test_years": fold_years, "split": report["method"]["split"]}))

    # %% Assert layer separation rather than trusting narrative labels.
    prior_features = set(artifact["context_prior"]["numeric_features"])
    forbidden_station_features = {
        "temperature_c", "observed_max_to_time_c", "temperature_delta_1h", "temperature_delta_3h",
        "dewpoint_c", "temp_dewpoint_spread_c", "relative_humidity_pct", "visibility_km",
        "pressure_hpa", "wind_speed_mps", "wind_gust_mps", "cloud_cover_pct",
        "low_cloud_cover_pct", "current_precipitation_reported", "current_convective_weather",
    }
    leaked = sorted(prior_features & forbidden_station_features)
    checks.append(_check("prior_excludes_station_observations", not leaked and artifact["layer_contract"]["context_prior_excludes_station_observations"] is True, {"prior_numeric_feature_count": len(prior_features), "leaked_features": leaked}))
    update_numeric = set(artifact["physical_update"]["numeric_features"])
    update_boolean = set(artifact["physical_update"]["boolean_features"])
    required_physical = {"temperature_c", "dewpoint_c", "temp_dewpoint_spread_c", "pressure_hpa", "wind_speed_mps", "cloud_cover_pct"}
    required_boolean = {"current_precipitation_reported", "current_convective_weather", "convective_cloud_reported"}
    missing_physical = sorted(required_physical - update_numeric)
    missing_boolean = sorted(required_boolean - update_boolean)
    checks.append(_check("physical_update_coverage", not missing_physical and not missing_boolean, {"missing_numeric": missing_physical, "missing_boolean": missing_boolean}))
    interactions = set(artifact["layer_contract"]["explicit_context_physical_interactions"])
    checks.append(_check("context_physical_interactions_present", len(interactions) == 9 and interactions <= update_numeric, {"interaction_count": len(interactions), "interactions": sorted(interactions)}))

    # %% Independently recompute headline gains from persisted aggregate metrics.
    aggregate = report["aggregate"]
    recomputed_prior = {key: _gain(float(aggregate["seasonal_climatology_prior"][key]), float(aggregate["basin_context_prior"][key])) for key in ("bucket_nll", "bucket_rps", "mae_c")}
    recomputed_update = {key: _gain(float(aggregate["physical_v2"][key]), float(aggregate["hierarchical_v4"][key])) for key in ("bucket_nll", "bucket_rps", "mae_c")}
    recomputed_flat = {key: _gain(float(aggregate["flat_v3"][key]), float(aggregate["hierarchical_v4"][key])) for key in ("bucket_nll", "bucket_rps", "mae_c")}
    checks.append(_check("headline_gains_recomputed", all(abs(recomputed_prior[key] - float(report["prior_improvement_pct"][key])) < 1e-10 for key in recomputed_prior) and all(abs(recomputed_update[key] - float(report["hierarchical_vs_physical_v2_improvement_pct"][key])) < 1e-10 for key in recomputed_update), {"prior_improvement_pct": recomputed_prior, "hierarchical_vs_physical_v2_improvement_pct": recomputed_update}))
    checks.append(_check("prior_improves_all_metrics", all(value > 0.0 for value in recomputed_prior.values()) and int(report["prior_year_wins_nll_and_rps"]) == 4, {"improvement_pct": recomputed_prior, "year_wins": report["prior_year_wins_nll_and_rps"]}))
    checks.append(_check("joint_update_improves_over_physical_only", all(value > 0.0 for value in recomputed_update.values()) and int(report["update_year_wins_nll_and_rps"]) >= 3, {"improvement_pct": recomputed_update, "year_wins": report["update_year_wins_nll_and_rps"]}))

    # %% Preserve adverse evidence: v4 is hierarchical but does not yet dominate v3.
    flat_regression_disclosed = all(value < 0.0 for value in recomputed_flat.values()) and "hierarchical_v4_does_not_dominate_flat_v3_on_every_metric" in report["release_gate"]["research_caveats"]
    checks.append(_check("flat_v3_regression_disclosed", flat_regression_disclosed, {"hierarchical_vs_flat_v3_improvement_pct": recomputed_flat}, severity="caveat"))
    checks.append(_check("finite_artifacts", _finite(report) and _finite(artifact), {"report_finite": _finite(report), "artifact_finite": _finite(artifact)}))
    all_guards_false = all(value is False for value in report["guards"].values())
    checks.append(_check("production_guards_closed", all_guards_false and report["release_gate"]["production_ready"] is False, {"all_guard_values_false": all_guards_false, "production_ready": report["release_gate"]["production_ready"]}))

    blocker_checks = [row for row in checks if row["severity"] == "blocker"]
    passed = all(row["passed"] for row in blocker_checks)
    caveats = [
        f"分层v4相对单层v3仍回退：NLL {recomputed_flat['bucket_nll']:+.2f}%、RPS {recomputed_flat['bucket_rps']:+.2f}%、MAE {recomputed_flat['mae_c']:+.2f}%；因此不能替换v3。",
        "标签是NCEI小时最高温，不是AMSC半小时峰值或市场官方结算；多年AMSC校准仍缺失。",
        "环流特征是统计关联与物理背景，不能把单个系数表述为确定因果效应。",
        "当前结果只支持研究和影子比较，不支持生产参数写入。",
    ]
    result = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "overall_assessment": "Share with caveats" if passed else "Needs revision",
        "passed": passed,
        "blocker_check_count": len(blocker_checks),
        "passed_blocker_checks": sum(row["passed"] for row in blocker_checks),
        "checks": checks,
        "data_profile": feature_profile,
        "caveats": caveats,
        "takeaway": "盆地动态先验和分层实况更新均通过各自消融验证；但现阶段最稳妥的研究结论是保留v3作为精度基线、将v4作为可解释分层影子候选。",
        "production_ready": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "validation_notebook_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "validation_notebook_result.md").write_text(_render(result), encoding="utf-8")
    print(json.dumps({"ok": passed, "assessment": result["overall_assessment"], "blocker_checks": len(blocker_checks), "passed_blocker_checks": result["passed_blocker_checks"], "output_dir": str(args.output_dir)}, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
