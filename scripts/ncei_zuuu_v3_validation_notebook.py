#!/usr/bin/env python3
"""# %% Independent, top-to-bottom validation for the ZUUU synoptic v3 study."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "ncei_zuuu_synoptic_v3_validation_notebook_v1"


def _check(name: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_sequence(values: Any) -> bool:
    return isinstance(values, list) and all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in values)


def _render(result: dict[str, Any]) -> str:
    lines = [
        "# ZUUU 四川盆地大尺度 v3 独立质量校验",
        "",
        f"结论：`{'通过' if result['passed'] else '未通过'}`；`{result['passed_check_count']}/{result['check_count']}` 项检查通过。",
        "",
        "| 检查 | 结果 | 证据 |",
        "|---|---|---|",
    ]
    for row in result["checks"]:
        detail = json.dumps(row["detail"], ensure_ascii=False, sort_keys=True)
        lines.append(f"| {row['check']} | {'通过' if row['passed'] else '失败'} | `{detail}` |")
    lines.extend(
        [
            "",
            "## 已知限制",
            "",
            "- 暖季10–17时峰值窗的温度桶NLL回退0.24%，虽RPS、MAE和峰值状态Brier改善，仍须保留为研究警告。",
            "- NOAA再分析是六小时、2.5°网格的大尺度背景，不是ZUUU机场硬实况。",
            "- AMSC 2021–2025半小时历史在线不可得，生产校准、影子运行与接入均未完成。",
            "",
        ]
    )
    return "\n".join(lines)


# %% Load manifests and reports.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synoptic-dir", type=Path, required=True)
    parser.add_argument("--join-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()

    manifest = json.loads((args.synoptic_dir / "download_manifest.json").read_text(encoding="utf-8"))
    join_report = json.loads((args.join_dir / "join_quality_report.json").read_text(encoding="utf-8"))
    report = json.loads((args.model_dir / "walk_forward_report.json").read_text(encoding="utf-8"))
    artifact = json.loads((args.model_dir / "model_artifact.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    # %% Verify every downloaded raw file against the persisted byte count and hash.
    bad_downloads = []
    total_bytes = 0
    for item in manifest["downloads"]:
        path = args.synoptic_dir / item["path"]
        actual_bytes = path.stat().st_size if path.exists() else -1
        actual_hash = _sha256(path) if path.exists() else ""
        total_bytes += max(actual_bytes, 0)
        if actual_bytes != int(item["bytes"]) or actual_hash != item["sha256"]:
            bad_downloads.append(item["path"])
    checks.append(_check("raw_download_hash_and_size", not bad_downloads, {"file_count": len(manifest["downloads"]), "total_bytes": total_bytes, "bad_files": bad_downloads}))
    checks.append(_check("bounded_download", total_bytes <= int(manifest["download_cap_bytes"]), {"bytes": total_bytes, "cap": manifest["download_cap_bytes"]}))

    # %% Validate the normalized six-hour table: unique, ordered, complete, finite.
    synoptic_path = args.synoptic_dir / "synoptic_features_6hourly.csv.gz"
    timestamps = []
    nonfinite_cells = 0
    with gzip.open(synoptic_path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            timestamps.append(row["valid_time_utc"])
            for name, value in row.items():
                if name != "valid_time_utc" and not math.isfinite(float(value)):
                    nonfinite_cells += 1
    checks.append(_check("normalized_synoptic_table", len(timestamps) == 7304 and len(set(timestamps)) == 7304 and timestamps == sorted(timestamps) and nonfinite_cells == 0, {"rows": len(timestamps), "unique_timestamps": len(set(timestamps)), "nonfinite_cells": nonfinite_cells}))

    # %% Verify the causal station join did not use future information.
    join_ok = (
        int(join_report["input_feature_rows"]) == 43281
        and int(join_report["joined_rows"]) == 43281
        and float(join_report["join_rate_pct"]) == 100.0
        and int(join_report["future_join_violation_count"]) == 0
        and 0.0 <= float(join_report["lag_hours_min"]) <= float(join_report["lag_hours_max"]) <= 5.0
    )
    checks.append(_check("causal_join", join_ok, {key: join_report[key] for key in ("input_feature_rows", "joined_rows", "join_rate_pct", "future_join_violation_count", "lag_hours_min", "lag_hours_max")}))

    # %% Validate model dimensions, finite coefficients, walk-forward years, and metrics.
    feature_count = len(artifact["feature_names"])
    artifact_ok = (
        feature_count > 0
        and len(set(artifact["feature_names"])) == feature_count
        and len(artifact["means"]) == len(artifact["numeric_features"])
        and len(artifact["scales"]) == len(artifact["numeric_features"])
        and len(artifact["peak_already_coefficients"]) == feature_count
        and len(artifact["positive_remaining_coefficients"]) == feature_count
        and _finite_sequence(artifact["means"])
        and _finite_sequence(artifact["scales"])
        and _finite_sequence(artifact["peak_already_coefficients"])
        and _finite_sequence(artifact["positive_remaining_coefficients"])
    )
    checks.append(_check("model_artifact_dimensions_and_finiteness", artifact_ok, {"feature_count": feature_count, "training_sample_count": artifact["training_sample_count"], "trained_through_year": artifact["trained_through_year"]}))
    fold_years = [int(fold["test_year"]) for fold in report["folds"]]
    checks.append(_check("strict_expanding_year_folds", fold_years == [2022, 2023, 2024, 2025] and report["method"]["split"] == "strict expanding-year walk-forward", {"test_years": fold_years, "split": report["method"]["split"]}))
    aggregate_gain = report["improvement_pct"]
    hot_peak_gain = report["hot_season_peak_window_improvement_pct"]
    checks.append(_check("aggregate_research_gain", all(float(aggregate_gain[key]) > 0.0 for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")) and int(report["year_wins_nll_and_rps"]) >= 3, {"improvement_pct": aggregate_gain, "year_wins_nll_and_rps": report["year_wins_nll_and_rps"]}))
    caveat_ok = float(hot_peak_gain["bucket_nll"]) < 0.0 and "hot_season_peak_window_bucket_nll_regression" in report["release_gate"]["research_caveats"]
    checks.append(_check("warm_peak_nll_regression_disclosed", caveat_ok, hot_peak_gain))

    # %% Assert that the entire study stayed outside production mutation surfaces.
    guard_sets = [manifest["guards"], join_report["guards"], report["guards"]]
    guards_ok = all(value is False for guards in guard_sets for value in guards.values())
    checks.append(_check("production_mutation_guards", guards_ok and report["release_gate"]["production_ready"] is False, {"all_guard_values_false": guards_ok, "production_ready": report["release_gate"]["production_ready"]}))

    passed = all(row["passed"] for row in checks)
    result = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "passed": passed,
        "check_count": len(checks),
        "passed_check_count": sum(row["passed"] for row in checks),
        "checks": checks,
        "known_caveats": report["release_gate"]["research_caveats"],
        "production_ready": False,
    }
    (args.model_dir / "validation_notebook_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.model_dir / "validation_notebook_result.md").write_text(_render(result), encoding="utf-8")
    print(json.dumps({"ok": passed, "passed_check_count": result["passed_check_count"], "check_count": result["check_count"], "output_dir": str(args.model_dir)}, ensure_ascii=False, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
