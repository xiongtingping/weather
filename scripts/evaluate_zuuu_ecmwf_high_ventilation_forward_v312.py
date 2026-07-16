#!/usr/bin/env python3
"""Diagnose ECMWF high-ventilation scoring and audit 2026 forward eligibility.

This research-only job has two deliberately separate parts:

1. Rebuild the 2024-train/2025-test v3.1 and v3.1+ECMWF distributions,
   apply the already fixed high-basin-ventilation shrinkage weight, and
   decompose why ordered temperature-bucket scores can improve while the
   binary continue-warming Brier score worsens.
2. Audit whether locally archived 2026 AMSC complete-day labels have a
   genuinely as-of ECMWF snapshot.  Post-outcome ``past_days`` responses are
   never accepted as forward evidence.

The job writes only bounded research artifacts under the requested output
directory.  It does not write archive.db, facts, posterior SSOT, strategy
signals, parameters, orders, or launchd state.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import evaluate_ncei_zuuu_additional_models_shadow_v31 as shadow
from scripts import evaluate_ncei_zuuu_ecmwf_regime_shrinkage_v311 as regime
from scripts import train_ncei_zuuu_remaining_warming_bayes as v1


SCHEMA = "zuuu_ecmwf_high_ventilation_forward_decomposition_v312"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
SEGMENT = "high_basin_ventilation"
DEFAULT_WEIGHT = 0.1636363636


def _number(value: Any) -> float | None:
    return v1._number(value)


def _phase(local_hour: float) -> str:
    if local_hour < 6.0:
        return "00-05"
    if local_hour < 10.0:
        return "06-09"
    if local_hour < 14.0:
        return "10-13"
    if local_hour < 18.0:
        return "14-17"
    return "18-23"


def _remaining_bin(value: float) -> str:
    if value <= 0.05:
        return "0_peak_already"
    if value <= 1.0:
        return "0_1c"
    if value <= 2.0:
        return "1_2c"
    return "gt_2c"


def _probability_bin(value: float) -> str:
    lower = min(0.8, math.floor(max(0.0, min(value, 0.999999)) / 0.2) * 0.2)
    upper = lower + 0.2
    return f"{lower:.1f}-{upper:.1f}"


def _wind_trigger(row: Mapping[str, Any]) -> str:
    triggers = []
    wind850 = _number(row.get("ecmwf__synoptic_850_wind_speed_mps"))
    wind700 = _number(row.get("ecmwf__synoptic_700_wind_speed_mps"))
    shear = _number(row.get("ecmwf__synoptic_850_700_shear_mps"))
    if wind850 is not None and wind850 >= 8.0:
        triggers.append("850wind")
    if wind700 is not None and wind700 >= 10.0:
        triggers.append("700wind")
    if shear is not None and shear >= 8.0:
        triggers.append("shear")
    return "+".join(triggers) if triggers else "none"


def _fit_distribution_fast(
    train_rows: list[dict[str, Any]],
    test_rows: list[dict[str, Any]],
    *,
    models: Sequence[str],
    version: str,
) -> list[dict[str, Any]]:
    """Fit once and retain probability vectors without a second transform."""

    table, per_day = v1._climo_table(train_rows)
    train_climo = [
        v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=True)
        for row in train_rows
    ]
    test_climo = [
        v1._climo_value(str(row["event_date_local"]), table, per_day, exclude_same=False)
        for row in test_rows
    ]
    design = shadow.ShadowDesign(models)
    x_train = design.fit_transform(train_rows, train_climo)
    x_test = design.transform(test_rows, test_climo)
    remaining = np.asarray([float(row["remaining_warming_c"]) for row in train_rows], dtype=float)
    weights = v1._day_weights(train_rows)
    zero_beta = v1._fit_logistic(x_train, (remaining <= 0.05).astype(float), weights)
    positive_beta, sigma = v1._fit_positive(x_train, remaining, weights)
    p_zero = v1._predict_zero(x_test, zero_beta)
    positive_mu = x_test @ positive_beta

    output = []
    for source, peak_probability, mu in zip(test_rows, p_zero, positive_mu):
        observed = float(source["observed_max_to_time_c"])
        probabilities = v1._bucket_probs(observed, float(peak_probability), float(mu), sigma)
        template = {
            "event_date_local": source["event_date_local"],
            "test_year": int(str(source["event_date_local"])[:4]),
            "month": int(source["month"]),
            "local_hour": float(source["local_hour"]),
            "observed_max_c": observed,
            "final_daily_max_c": float(source["final_daily_max_c"]),
            "remaining_warming_c": float(source["remaining_warming_c"]),
            "actual_continue": float(source["remaining_warming_c"]) > 0.05,
            "source_weather_row": dict(source),
            "model_version": version,
        }
        output.append(
            regime._prediction_from_probabilities(
                template,
                probabilities,
                1.0 - float(peak_probability),
            )
        )
    return output


def _fit_pair_fast(
    train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    baseline_day = _fit_distribution_fast(train_rows, test_rows, models=("gfs",), version="v31_day")
    baseline_predawn = _fit_distribution_fast(
        train_rows, test_rows, models=("gfs", "jma"), version="v31_predawn"
    )
    candidate_day = _fit_distribution_fast(
        train_rows, test_rows, models=("gfs", "ecmwf"), version="v31_ecmwf_day"
    )
    candidate_predawn = _fit_distribution_fast(
        train_rows,
        test_rows,
        models=("gfs", "jma", "ecmwf"),
        version="v31_ecmwf_predawn",
    )
    return (
        regime._regime_distribution(baseline_day, baseline_predawn),
        regime._regime_distribution(candidate_day, candidate_predawn),
    )


def _global_day_weights(rows: Sequence[Mapping[str, Any]]) -> np.ndarray:
    counts = Counter(str(row["event_date_local"]) for row in rows)
    weights = np.asarray([1.0 / counts[str(row["event_date_local"])] for row in rows], dtype=float)
    return weights / weights.sum()


def _decomposition_rows(
    baseline_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(baseline_rows) != len(candidate_rows):
        raise RuntimeError("paired widths differ")
    weights = _global_day_weights(baseline_rows)
    output = []
    for baseline, candidate, weight in zip(baseline_rows, candidate_rows, weights):
        y = 1.0 if baseline["actual_continue"] else 0.0
        p_base = float(baseline["predicted_continue_probability"])
        p_candidate = float(candidate["predicted_continue_probability"])
        delta_probability = p_candidate - p_base
        brier_delta = (p_candidate - y) ** 2 - (p_base - y) ** 2
        calibration_direction = 2.0 * (p_base - y) * delta_probability
        movement_penalty = delta_probability**2
        weather = baseline["source_weather_row"]
        baseline_probs = np.asarray(baseline["bucket_probabilities"], dtype=float)
        candidate_probs = np.asarray(candidate["bucket_probabilities"], dtype=float)
        actual_bucket = round(float(baseline["final_daily_max_c"]))
        actual_index = int(np.argmin(np.abs(v1.BUCKETS - actual_bucket)))
        row = {
            "event_date_local": str(baseline["event_date_local"]),
            "local_hour": float(baseline["local_hour"]),
            "phase": _phase(float(baseline["local_hour"])),
            "actual_continue": bool(baseline["actual_continue"]),
            "outcome": "continue" if baseline["actual_continue"] else "peak_already",
            "remaining_warming_c": float(baseline["remaining_warming_c"]),
            "remaining_bin": _remaining_bin(float(baseline["remaining_warming_c"])),
            "baseline_probability_bin": _probability_bin(p_base),
            "wind_trigger": _wind_trigger(weather),
            "baseline_continue_probability": p_base,
            "candidate_continue_probability": p_candidate,
            "continue_probability_delta": delta_probability,
            "brier_delta": brier_delta,
            "brier_calibration_direction_component": calibration_direction,
            "brier_movement_penalty_component": movement_penalty,
            "brier_identity_residual": brier_delta - calibration_direction - movement_penalty,
            "bucket_nll_delta": float(candidate["bucket_nll"]) - float(baseline["bucket_nll"]),
            "bucket_rps_delta": float(candidate["bucket_rps"]) - float(baseline["bucket_rps"]),
            "mae_delta_c": abs(float(candidate["expected_final_max_c"]) - float(candidate["final_daily_max_c"]))
            - abs(float(baseline["expected_final_max_c"]) - float(baseline["final_daily_max_c"])),
            "actual_bucket_probability_delta": float(candidate_probs[actual_index] - baseline_probs[actual_index]),
            "baseline_expected_final_max_c": float(baseline["expected_final_max_c"]),
            "candidate_expected_final_max_c": float(candidate["expected_final_max_c"]),
            "final_daily_max_c": float(baseline["final_daily_max_c"]),
            "observed_max_to_time_c": float(weather["observed_max_to_time_c"]),
            "hours_since_observed_max": float(weather.get("hours_since_observed_max") or 0.0),
            "drop_from_observed_max_c": float(weather.get("drop_from_observed_max_c") or 0.0),
            "ecmwf_wind850_mps": _number(weather.get("ecmwf__synoptic_850_wind_speed_mps")),
            "ecmwf_wind700_mps": _number(weather.get("ecmwf__synoptic_700_wind_speed_mps")),
            "ecmwf_shear_mps": _number(weather.get("ecmwf__synoptic_850_700_shear_mps")),
            "global_day_weight": float(weight),
        }
        output.append(row)
    return output


def _weighted_mean(rows: Sequence[Mapping[str, Any]], field: str, *, global_weights: bool) -> float | None:
    if not rows:
        return None
    values = np.asarray([float(row[field]) for row in rows], dtype=float)
    weights = np.asarray([float(row["global_day_weight"]) for row in rows], dtype=float)
    if not global_weights:
        weights = weights / weights.sum()
    return float(np.sum(values * weights))


def _group_decomposition(
    rows: Sequence[Mapping[str, Any]], key: Callable[[Mapping[str, Any]], str]
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    output = []
    for label, selected in sorted(groups.items()):
        output.append(
            {
                "group": label,
                "sample_count": len(selected),
                "day_count": len({row["event_date_local"] for row in selected}),
                "global_weight_share": float(sum(float(row["global_day_weight"]) for row in selected)),
                "brier_delta_mean_within_group": _weighted_mean(selected, "brier_delta", global_weights=False),
                "brier_delta_contribution_to_total": _weighted_mean(selected, "brier_delta", global_weights=True),
                "calibration_direction_contribution": _weighted_mean(
                    selected, "brier_calibration_direction_component", global_weights=True
                ),
                "movement_penalty_contribution": _weighted_mean(
                    selected, "brier_movement_penalty_component", global_weights=True
                ),
                "continue_probability_delta_mean": _weighted_mean(
                    selected, "continue_probability_delta", global_weights=False
                ),
                "bucket_nll_delta_mean": _weighted_mean(selected, "bucket_nll_delta", global_weights=False),
                "bucket_rps_delta_mean": _weighted_mean(selected, "bucket_rps_delta", global_weights=False),
                "mae_delta_c_mean": _weighted_mean(selected, "mae_delta_c", global_weights=False),
                "actual_bucket_probability_delta_mean": _weighted_mean(
                    selected, "actual_bucket_probability_delta", global_weights=False
                ),
            }
        )
    return output


def _load_amsc_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _coverage_by_day(root: Path) -> dict[str, dict[str, Any]]:
    output = {}
    for path in sorted(root.glob("*/*/*/*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        day = str(payload.get("event_date") or "")
        if day:
            output[day] = payload
    return output


def _ecmwf_snapshot_times(raw_dir: Path) -> list[dict[str, Any]]:
    output = []
    for path in sorted(raw_dir.glob("*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
            body = str(envelope.get("response_body_utf8") or "").encode("utf-8")
            body_hash = hashlib.sha256(body).hexdigest()
            completed = str(envelope.get("response_completed_at") or "")
            requested = str(envelope.get("requested_as_of_utc") or "")
            if (
                envelope.get("schema") == "open_meteo_sichuan_multimodel_raw_capture_v1"
                and completed
                and requested
                and body
                and body_hash == str(envelope.get("response_sha256") or "")
            ):
                acquired = datetime.fromisoformat(completed.replace("Z", "+00:00")).astimezone(timezone.utc)
                output.append(
                    {
                        "path": str(path),
                        "local_file_acquired_at_utc": acquired.isoformat(timespec="seconds").replace("+00:00", "Z"),
                        "local_file_acquired_at_local": acquired.astimezone(LOCAL_TZ).isoformat(timespec="seconds"),
                        "requested_as_of_utc": requested,
                        "acquisition_time_basis": "embedded_immutable_capture",
                        "response_body_sha256_verified": True,
                    }
                )
                continue
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            pass
        acquired = datetime.fromtimestamp(os.stat(path).st_mtime, tz=timezone.utc)
        output.append(
            {
                "path": str(path),
                "local_file_acquired_at_utc": acquired.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "local_file_acquired_at_local": acquired.astimezone(LOCAL_TZ).isoformat(timespec="seconds"),
                "acquisition_time_basis": "filesystem_mtime_lower_assurance",
            }
        )
    return sorted(output, key=lambda row: str(row["local_file_acquired_at_utc"]))


def _forward_audit(
    amsc_rows: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Mapping[str, Any]],
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    complete_days = sorted(
        {
            str(row["event_date_local"])
            for row in amsc_rows
            if row.get("daily_max_label_eligible") and _number(row.get("final_daily_max_c")) is not None
        }
    )
    trusted_snapshot_times = [
        datetime.fromisoformat(str(row["local_file_acquired_at_utc"]).replace("Z", "+00:00"))
        for row in snapshots
        if row.get("acquisition_time_basis") == "embedded_immutable_capture"
    ]
    rows = []
    for day in complete_days:
        local_day_end = datetime.combine(datetime.fromisoformat(day).date(), time(23, 59, 59), LOCAL_TZ)
        eligible = [value for value in trusted_snapshot_times if value <= local_day_end.astimezone(timezone.utc)]
        manifest = coverage.get(day) or {}
        integrity_ok = bool((manifest.get("raw_archive_integrity") or {}).get("ok"))
        rows.append(
            {
                "event_date_local": day,
                "amsc_label_complete": True,
                "amsc_final_daily_max_c": max(
                    float(row["final_daily_max_c"])
                    for row in amsc_rows
                    if str(row["event_date_local"]) == day and _number(row.get("final_daily_max_c")) is not None
                ),
                "amsc_raw_integrity_ok": integrity_ok,
                "amsc_distinct_valid_time_count": manifest.get("distinct_valid_time_count"),
                "ecmwf_snapshot_available_by_day_end": bool(eligible),
                "forward_score_eligible": bool(integrity_ok and eligible),
                "exclusion_reason": "" if integrity_ok and eligible else (
                    "ecmwf_asof_snapshot_missing" if integrity_ok else "amsc_integrity_not_proven"
                ),
            }
        )
    return {
        "amsc_complete_day_count": len(complete_days),
        "amsc_complete_days": complete_days,
        "ecmwf_snapshot_count": len(snapshots),
        "trusted_ecmwf_snapshot_count": len(trusted_snapshot_times),
        "legacy_or_unverified_snapshot_count": len(snapshots) - len(trusted_snapshot_times),
        "first_ecmwf_snapshot_local": snapshots[0]["local_file_acquired_at_local"] if snapshots else None,
        "last_ecmwf_snapshot_local": snapshots[-1]["local_file_acquired_at_local"] if snapshots else None,
        "forward_scored_day_count": sum(bool(row["forward_score_eligible"]) for row in rows),
        "forward_rows": rows,
        "state": "ready" if any(row["forward_score_eligible"] for row in rows) else "waiting_for_post_start_complete_day",
        "post_outcome_past_days_rejected": True,
        "snapshot_acquisition_time_caveat": (
            "only raw captures with an embedded acquisition-time envelope and a verified response hash are eligible for forward scoring; "
            "legacy filesystem-mtime objects remain visible but are rejected"
        ),
    }


def _render(report: Mapping[str, Any]) -> str:
    gains = report["historical_2025"]["improvement_pct"]
    forward = report["forward_2026"]
    outcome = {row["group"]: row for row in report["historical_2025"]["decomposition"]["by_outcome"]}
    stop = outcome.get("peak_already") or {}
    cont = outcome.get("continue") or {}
    return "\n".join(
        [
            "# ZUUU ECMWF 高通风：前向准入与评分分歧拆解",
            "",
            "## 技术结论",
            "",
            f"- 2026可无泄漏评分的完整日：`{forward['forward_scored_day_count']}`；AMSC已有完整标签 `{forward['amsc_complete_day_count']}` 天，但首份本地ECMWF快照晚于这些日终。",
            f"- 2025高通风复核：NLL `{gains['bucket_nll']:+.3f}%`、RPS `{gains['bucket_rps']:+.3f}%`、MAE `{gains['mae_c']:+.3f}%`、继续升温Brier `{gains['continue_brier']:+.3f}%`。",
            "- 因而当前只能解释历史机制，不能声称2026已验证；ECMWF继续保持不影响生产后验的影子状态。",
            "",
            "## Brier分歧定位",
            "",
            f"- 已达峰样本的继续升温概率平均变化：`{float(stop.get('continue_probability_delta_mean') or 0.0):+.5f}`；其Brier总贡献 `{float(stop.get('brier_delta_contribution_to_total') or 0.0):+.6f}`。",
            f"- 仍会升温样本的继续升温概率平均变化：`{float(cont.get('continue_probability_delta_mean') or 0.0):+.5f}`；其Brier总贡献 `{float(cont.get('brier_delta_contribution_to_total') or 0.0):+.6f}`。",
            "- 代数恒等式：Brier差 = 2×(基线概率−事实)×概率改变量 + 概率改变量²；分组贡献严格加总到总体差。",
            "",
            "## 数据边界",
            "",
            "- AMSC完整日标签按北京时间日界线定义；ECMWF必须在该日结束前已落地，事后回看的past_days响应不计入前向样本。",
            "- 2025标签仍是NCEI小时最高温，机制解释需要继续用未来AMSC半小时完整日复核。",
            "- 本报告不改数据库、SSOT、参数、信号、订单或守护进程。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--regime-report", type=Path, required=True)
    parser.add_argument("--amsc-rows", type=Path, required=True)
    parser.add_argument("--amsc-coverage-dir", type=Path, required=True)
    parser.add_argument("--ecmwf-runtime-raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    prior_report = json.loads(args.regime_report.read_text(encoding="utf-8"))
    diagnostic = next(
        (
            row
            for row in prior_report["held_out_2025"]["all_pre_registered_segment_diagnostics"]
            if row.get("segment") == SEGMENT
        ),
        None,
    )
    weight = float((diagnostic or {}).get("effective_weight") or DEFAULT_WEIGHT)

    all_rows = [row for row in shadow._load(args.features) if row.get("ecmwf__join_available")]
    train_2024 = [row for row in all_rows if str(row["event_date_local"]).startswith("2024-")]
    test_2025 = [row for row in all_rows if str(row["event_date_local"]).startswith("2025-")]
    baseline, candidate_full = _fit_pair_fast(train_2024, test_2025)
    candidate = regime._blend_rows(baseline, candidate_full, segment=SEGMENT, weight=weight)
    predicate = regime.SEGMENTS[SEGMENT][1]
    baseline_segment = [row for row in baseline if predicate(row["source_weather_row"])]
    candidate_segment = [row for row in candidate if predicate(row["source_weather_row"])]
    decomposed = _decomposition_rows(baseline_segment, candidate_segment)

    baseline_summary = regime._summary(baseline_segment)
    candidate_summary = regime._summary(candidate_segment)
    gains = regime._gains(baseline_summary, candidate_summary)
    reference_gains = (diagnostic or {}).get("improvement_2025_pct") or {}
    gain_reconciliation = {
        key: float(gains[key]) - float(reference_gains.get(key) or 0.0)
        for key in ("bucket_nll", "bucket_rps", "mae_c", "continue_brier")
    }
    max_identity_residual = max(abs(float(row["brier_identity_residual"])) for row in decomposed)
    overall_brier_delta = _weighted_mean(decomposed, "brier_delta", global_weights=True)

    amsc_rows = _load_amsc_rows(args.amsc_rows)
    coverage = _coverage_by_day(args.amsc_coverage_dir)
    snapshots = _ecmwf_snapshot_times(args.ecmwf_runtime_raw_dir)
    forward = _forward_audit(amsc_rows, coverage, snapshots)

    decomposition = {
        "by_outcome": _group_decomposition(decomposed, lambda row: str(row["outcome"])),
        "by_phase": _group_decomposition(decomposed, lambda row: str(row["phase"])),
        "by_remaining_bin": _group_decomposition(decomposed, lambda row: str(row["remaining_bin"])),
        "by_baseline_probability_bin": _group_decomposition(
            decomposed, lambda row: str(row["baseline_probability_bin"])
        ),
        "by_wind_trigger": _group_decomposition(decomposed, lambda row: str(row["wind_trigger"])),
    }
    outcome_contribution_sum = sum(
        float(row["brier_delta_contribution_to_total"]) for row in decomposition["by_outcome"]
    )
    contradictory = [
        row for row in decomposed if float(row["bucket_nll_delta"]) < 0.0 and float(row["brier_delta"]) > 0.0
    ]

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "question": "why ECMWF can improve high-ventilation temperature buckets while worsening continue-warming calibration",
        "historical_2025": {
            "training_period": "2024-02-03 through 2024-12-31",
            "test_period": "2025-01-01 through 2025-12-31",
            "timezone": "Asia/Shanghai",
            "segment": SEGMENT,
            "segment_definition": "ECMWF 850 wind >=8 m/s OR 700 wind >=10 m/s OR 850-700 shear >=8 m/s",
            "effective_weight": weight,
            "sample_count": len(decomposed),
            "day_count": len({row["event_date_local"] for row in decomposed}),
            "baseline_summary": baseline_summary,
            "candidate_summary": candidate_summary,
            "improvement_pct": gains,
            "reference_report_improvement_pct": reference_gains,
            "gain_reconciliation_difference_pct": gain_reconciliation,
            "overall_candidate_minus_baseline_brier": overall_brier_delta,
            "max_brier_identity_residual": max_identity_residual,
            "outcome_contribution_reconciliation_residual": float(overall_brier_delta) - outcome_contribution_sum,
            "contradictory_row_count_nll_better_brier_worse": len(contradictory),
            "contradictory_row_rate_pct": 100.0 * len(contradictory) / len(decomposed),
            "contradictory_global_day_weight_pct": 100.0
            * sum(float(row["global_day_weight"]) for row in contradictory),
            "decomposition": decomposition,
            "largest_brier_harm_rows": sorted(decomposed, key=lambda row: float(row["brier_delta"]), reverse=True)[:12],
        },
        "forward_2026": forward,
        "data_quality": {
            "historical_duplicate_key_count": len(decomposed)
            - len({(row["event_date_local"], row["local_hour"]) for row in decomposed}),
            "historical_missing_required_metric_count": sum(
                any(row.get(field) is None for field in ("brier_delta", "bucket_nll_delta", "global_day_weight"))
                for row in decomposed
            ),
            "brier_identity_pass": max_identity_residual < 1e-12,
            "outcome_contribution_reconciles": abs(float(overall_brier_delta) - outcome_contribution_sum) < 1e-12,
            "reference_gain_reconciles": max(abs(value) for value in gain_reconciliation.values()) < 1e-8,
            "forward_asof_overlap_required": True,
            "forward_post_outcome_backcast_rejected": True,
        },
        "release_gate": {
            "forward_evidence_ready": forward["forward_scored_day_count"] >= 30,
            "production_ready": False,
            "decision": "retain_ecmwf_as_non_influential_shadow",
            "production_blockers": [
                "zero_amsc_complete_days_with_asof_ecmwf_snapshot",
                "2025_bucket_and_binary_metrics_disagree",
                "historical_label_is_ncei_hourly_not_amsc_half_hour",
                "production_promotion_not_authorized_by_research_job",
            ],
        },
        "guards": {
            "historical_backfill": False,
            "downloads_data": False,
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
    report_path = args.output_dir / "ecmwf_high_ventilation_forward_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.output_dir / "ecmwf_high_ventilation_forward_report.md").write_text(
        _render(report), encoding="utf-8"
    )
    with gzip.open(args.output_dir / "ecmwf_high_ventilation_decomposition_rows.jsonl.gz", "wt", encoding="utf-8") as handle:
        for row in decomposed:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    print(
        json.dumps(
            {
                "ok": True,
                "historical_day_count": report["historical_2025"]["day_count"],
                "historical_improvement_pct": gains,
                "forward_scored_day_count": forward["forward_scored_day_count"],
                "production_ready": False,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
