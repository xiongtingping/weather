#!/usr/bin/env python3
# %% [markdown]
# # ZUUU v2 research model validation notebook
#
# This percent-format notebook is executable as ordinary Python.  It validates
# source grain, timezone alignment, causal sequence features, forward folds,
# metric finiteness, and the research/production release boundary.

from __future__ import annotations

# %% Setup
import argparse
import gzip
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


def _parse(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _close(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
    return abs(float(left) - float(right)) <= tolerance


def _finite_tree(value: Any) -> bool:
    if isinstance(value, Mapping):
        return all(_finite_tree(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite_tree(item) for item in value)
    return not isinstance(value, float) or math.isfinite(value)


# %% Load data
def _load_features(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


# %% Validate causal sequence state
def _sequence_checks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["event_date_local"])].append(row)
    observed_max_mismatch = 0
    negative_peak_age = 0
    cumulative_flag_reversal = 0
    timezone_mismatch = 0
    peak_age_mismatch = 0
    checked_rows = 0
    for day_rows in grouped.values():
        day_rows.sort(key=lambda row: str(row["valid_time_utc"]))
        observed_max = None
        last_max_at = None
        seen_convective = False
        seen_precipitation = False
        for row in day_rows:
            checked_rows += 1
            utc = _parse(row["valid_time_utc"])
            local = _parse(row["valid_time_local"])
            local_offset = local.utcoffset()
            if local_offset is None or local_offset.total_seconds() != 8 * 3600 or local.timestamp() != utc.timestamp():
                timezone_mismatch += 1
            temp = row.get("temperature_c")
            if temp is not None and not row.get("core_qc_bad"):
                temp = float(temp)
                if observed_max is None or temp > observed_max:
                    observed_max = temp
                    last_max_at = utc
                elif temp == observed_max:
                    last_max_at = utc
            if observed_max is not None and not _close(row["observed_max_to_time_c"], observed_max):
                observed_max_mismatch += 1
            expected_age = (utc - last_max_at).total_seconds() / 3600.0 if last_max_at is not None else None
            actual_age = row.get("hours_since_observed_max")
            if expected_age is not None and actual_age is not None and not _close(expected_age, actual_age, 1e-4):
                peak_age_mismatch += 1
            if actual_age is not None and float(actual_age) < 0:
                negative_peak_age += 1
            current_convective = bool(row.get("current_convective_weather") or row.get("convective_cloud_reported"))
            current_precipitation = bool(row.get("current_precipitation_reported"))
            seen_convective = seen_convective or current_convective
            seen_precipitation = seen_precipitation or current_precipitation
            if bool(row.get("convective_seen_today")) != seen_convective:
                cumulative_flag_reversal += 1
            if bool(row.get("precipitation_seen_today")) != seen_precipitation:
                cumulative_flag_reversal += 1
    return {
        "checked_rows": checked_rows,
        "observed_max_mismatch_count": observed_max_mismatch,
        "peak_age_mismatch_count": peak_age_mismatch,
        "negative_peak_age_count": negative_peak_age,
        "cumulative_flag_mismatch_count": cumulative_flag_reversal,
        "timezone_offset_mismatch_count": timezone_mismatch,
    }


# %% Validate model artifact and claims
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--calibration-report", type=Path, required=True)
    parser.add_argument("--model-artifact", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    rows = _load_features(args.features)
    quality = json.loads(args.quality_report.read_text(encoding="utf-8"))
    calibration = json.loads(args.calibration_report.read_text(encoding="utf-8"))
    artifact = json.loads(args.model_artifact.read_text(encoding="utf-8"))
    sequence = _sequence_checks(rows)
    utc_times = [str(row["valid_time_utc"]) for row in rows]
    feature_names = list(artifact["feature_names"])
    forbidden_feature_names = [
        name for name in feature_names
        if "final_daily_max" in name or "remaining_warming" in name or "first_max_valid_time" in name
    ]
    folds = calibration["folds"]
    fold_order_ok = all(
        int(fold["test_year"]) > 2021 and int(fold["train_day_count"]) > 0
        for fold in folds
    ) and [int(fold["test_year"]) for fold in folds] == [2022, 2023, 2024, 2025]
    coefficients_aligned = (
        len(feature_names)
        == len(artifact["peak_already_coefficients"])
        == len(artifact["positive_remaining_coefficients"])
    )
    checks = {
        "raw_row_count_matches_quality_report": len(rows) == int(quality["row_count"]),
        "utc_timestamp_unique": len(utc_times) == len(set(utc_times)),
        "raw_metar_time_match_rate_100pct": float(quality["raw_metar"]["time_match_rate_pct"]) == 100.0,
        "sequence_observed_max_is_causal": sequence["observed_max_mismatch_count"] == 0,
        "sequence_peak_age_is_causal": sequence["peak_age_mismatch_count"] == 0 and sequence["negative_peak_age_count"] == 0,
        "cumulative_weather_flags_are_causal": sequence["cumulative_flag_mismatch_count"] == 0,
        "timezone_is_asia_shanghai_utc_plus_8": sequence["timezone_offset_mismatch_count"] == 0,
        "no_outcome_fields_in_feature_names": not forbidden_feature_names,
        "strict_forward_fold_order": fold_order_ok,
        "artifact_coefficients_aligned": coefficients_aligned,
        "all_report_and_artifact_numbers_finite": _finite_tree(calibration) and _finite_tree(artifact),
        "research_gate_passed": bool(calibration["release_gate"]["research_pass"]),
        "production_gate_closed": calibration["release_gate"]["production_ready"] is False,
        "no_production_writes_declared": all(value is False for value in calibration["guards"].values()),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    hour_19 = calibration["segments"]["v2"]["hour_19"]
    late_scenario = calibration["segments"]["v2"]["late_peak_age_ge_2h_drop_ge_2c"]
    hot_late_scenario = calibration["segments"]["v2"]["hot_season_late_peak_age_ge_2h_drop_ge_2c"]
    output = {
        "schema": "ncei_zuuu_v2_validation_notebook_result_v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "inputs": {
            "features": str(args.features),
            "quality_report": str(args.quality_report),
            "calibration_report": str(args.calibration_report),
            "model_artifact": str(args.model_artifact),
        },
        "checks": checks,
        "blockers": blockers,
        "sequence_check_details": sequence,
        "headline_recomputations": {
            "row_count": len(rows),
            "distinct_utc_timestamp_count": len(set(utc_times)),
            "complete_day_count": quality["daily_labels"]["complete_day_count"],
            "hour_19": hour_19,
            "late_peak_age_ge_2h_drop_ge_2c": late_scenario,
            "hot_season_late_peak_age_ge_2h_drop_ge_2c": hot_late_scenario,
            "improvement_pct": calibration["improvement_pct"],
            "year_wins_nll_and_rps": calibration["year_wins_nll_and_rps"],
        },
        "assessment": "ready_for_research_use" if not blockers else "needs_revision",
        "production_ready": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "validation_notebook_result.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# ZUUU v2 validation notebook result",
        "",
        f"- Assessment: `{output['assessment']}`",
        f"- Checks passed: `{sum(checks.values())}/{len(checks)}`",
        f"- Rows: `{len(rows)}`; complete days: `{quality['daily_labels']['complete_day_count']}`",
        f"- 19:00 actual continuation: `{100*hour_19['actual_continue_rate']:.4f}%`; predicted: `{100*hour_19['predicted_continue_rate']:.4f}%`.",
        f"- 19:00 zero-event 95% upper bound: `{100*(hour_19.get('zero_event_95pct_upper_rate') or 0):.3f}%`.",
        f"- Warm-season late/drop analogue: `{hot_late_scenario['day_count']}` days, `{hot_late_scenario['actual_continue_count']}` continuations, predicted `{100*hot_late_scenario['predicted_continue_rate']:.3f}%`.",
        f"- Production ready: `false`.",
        "",
    ]
    (args.output_dir / "validation_notebook_result.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"ok": not blockers, "assessment": output["assessment"], "checks": checks, "blockers": blockers}, ensure_ascii=False, sort_keys=True))
    return 0 if not blockers else 1


if __name__ == "__main__":
    raise SystemExit(main())
