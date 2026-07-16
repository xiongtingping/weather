#!/usr/bin/env python3
"""Evaluate a fail-closed ECMWF joint-state shadow gate for ZUUU.

v3.14 is research only.  It repairs three deterministic defects in the older
high-ventilation shadow policy without changing the v3.1 production posterior:

* wind regimes are mutually exclusive instead of one broad OR state;
* humidity/cloud, convection/precipitation/cooling, and observed peak decline
  jointly determine whether a wind regime is eligible;
* 2024 selection requires a strictly positive continue-warming Brier gain.

The feature model remains the same v3.1 GFS(+JMA) baseline versus
GFS(+JMA)+ECMWF candidate.  This job only chooses a convex probability blend
on 2024 rolling-origin data, then evaluates that frozen policy on 2025.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import evaluate_ncei_zuuu_additional_models_shadow_v31 as shadow
from scripts import evaluate_ncei_zuuu_ecmwf_regime_shrinkage_v311 as regime
from scripts import evaluate_zuuu_ecmwf_high_ventilation_forward_v312 as v312

SCHEMA = "zuuu_ecmwf_joint_state_shadow_v314"
WEIGHT_GRID = tuple(round(value, 2) for value in (0.1, 0.2, 0.3, 0.4, 0.5))
SHRINKAGE_PRIOR_DAYS = 60.0
MIN_VALIDATION_DAYS = 15
WIND_TYPES = (
    "850wind",
    "700wind",
    "shear",
    "850wind+700wind",
    "850wind+shear",
    "700wind+shear",
    "850wind+700wind+shear",
)
SUPPRESSOR_REQUIRED_FIELDS = (
    "relative_humidity_pct",
    "low_cloud_cover_pct",
    "temp_dewpoint_spread_c",
    "current_convective_weather",
    "convective_cloud_reported",
    "current_precipitation_reported",
    "convective_seen_today",
    "precipitation_seen_today",
    "post_convective_cooling",
    "post_precipitation_cooling",
    "drop_from_observed_max_c",
    "reports_below_observed_max",
)
PRE_REGISTERED_STATES = tuple(
    f"{wind}__{context}" for wind in WIND_TYPES for context in ("suppressed", "unsuppressed")
)


def _number(value: Any) -> float | None:
    return v312._number(value)


def _peak_decline(row: Mapping[str, Any]) -> bool:
    drop = _number(row.get("drop_from_observed_max_c"))
    reports_below = _number(row.get("reports_below_observed_max"))
    return bool((drop is not None and drop > 0.0) or (reports_below is not None and reports_below > 0.0))


def _state(row: Mapping[str, Any]) -> str | None:
    """Return a mutually exclusive pre-registered joint gate state.

    The gate fails closed when a required physical channel is unavailable.  It
    does not infer a clear sky from a missing low-cloud field.
    """

    wind = v312._wind_trigger(row)
    if wind not in WIND_TYPES:
        return None
    if any(row.get(field) is None for field in SUPPRESSOR_REQUIRED_FIELDS):
        return None
    suppressor = bool(
        regime._high_humidity_low_cloud(row)
        or regime._antecedent_convection_or_precip(row)
        or _peak_decline(row)
    )
    return f"{wind}__{'suppressed' if suppressor else 'unsuppressed'}"


def _strict_eligible(gains: Mapping[str, float]) -> bool:
    """Require binary state calibration to improve, not merely not collapse."""

    return bool(
        float(gains["bucket_nll"]) > 0.0
        and float(gains["bucket_rps"]) > 0.0
        and float(gains["continue_brier"]) > 0.0
        and float(gains["mae_c"]) > -0.5
    )


def _score(gains: Mapping[str, float]) -> float:
    # Called only after _strict_eligible; the Brier term cannot be sacrificed.
    return float(sum(float(gains[key]) for key in ("bucket_nll", "bucket_rps", "continue_brier", "mae_c")))


def _select_state_rows(
    rows: Sequence[Mapping[str, Any]], state: str, *, state_fn: Callable[[Mapping[str, Any]], str | None] = _state
) -> list[Mapping[str, Any]]:
    return [row for row in rows if state_fn(row["source_weather_row"]) == state]


def _blend_rows(
    baseline_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    *,
    state: str,
    weight: float,
    state_fn: Callable[[Mapping[str, Any]], str | None] = _state,
) -> list[dict[str, Any]]:
    if len(baseline_rows) != len(candidate_rows):
        raise RuntimeError("baseline/candidate width mismatch")
    output = []
    for baseline, candidate in zip(baseline_rows, candidate_rows):
        if (
            baseline["event_date_local"] != candidate["event_date_local"]
            or float(baseline["local_hour"]) != float(candidate["local_hour"])
        ):
            raise RuntimeError("baseline/candidate ordering mismatch")
        effective_weight = float(weight if state_fn(baseline["source_weather_row"]) == state else 0.0)
        if effective_weight == 0.0:
            output.append(dict(baseline))
            continue
        b = np.asarray(baseline["bucket_probabilities"], dtype=float)
        c = np.asarray(candidate["bucket_probabilities"], dtype=float)
        probabilities = (1.0 - effective_weight) * b + effective_weight * c
        probabilities /= probabilities.sum()
        continue_probability = (1.0 - effective_weight) * float(
            baseline["predicted_continue_probability"]
        ) + effective_weight * float(candidate["predicted_continue_probability"])
        output.append(regime._prediction_from_probabilities(baseline, probabilities, continue_probability))
    return output


def _fit_pair(train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]]):
    return v312._fit_pair_fast(train_rows, test_rows)


def _rolling_predictions(rows: list[dict[str, Any]]):
    windows = (
        ("2024-02-03", "2024-07-01", "2024-07-01", "2024-09-01"),
        ("2024-02-03", "2024-09-01", "2024-09-01", "2024-11-01"),
        ("2024-02-03", "2024-11-01", "2024-11-01", "2025-01-01"),
    )
    baseline_all: list[dict[str, Any]] = []
    candidate_all: list[dict[str, Any]] = []
    trace = []
    for index, (train_start, train_end, valid_start, valid_end) in enumerate(windows, start=1):
        train = [row for row in rows if train_start <= str(row["event_date_local"]) < train_end]
        valid = [row for row in rows if valid_start <= str(row["event_date_local"]) < valid_end]
        baseline, candidate = _fit_pair(train, valid)
        baseline_all.extend(baseline)
        candidate_all.extend(candidate)
        trace.append(
            {
                "fold": index,
                "train_start": train_start,
                "train_end_exclusive": train_end,
                "validation_start": valid_start,
                "validation_end_exclusive": valid_end,
                "train_day_count": len({row["event_date_local"] for row in train}),
                "validation_day_count": len({row["event_date_local"] for row in valid}),
            }
        )
    return baseline_all, candidate_all, trace


def _validation_result(
    baseline_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    state: str,
    *,
    state_fn: Callable[[Mapping[str, Any]], str | None] = _state,
    min_validation_days: int = MIN_VALIDATION_DAYS,
) -> dict[str, Any]:
    baseline_state = _select_state_rows(baseline_rows, state, state_fn=state_fn)
    days = len({row["event_date_local"] for row in baseline_state})
    if days < min_validation_days:
        return {"state": state, "state_status": "insufficient_validation_days", "validation_day_count": days}
    summary = regime._summary(baseline_state)
    weights = []
    for raw_weight in WEIGHT_GRID:
        blended_state = _select_state_rows(
            _blend_rows(baseline_rows, candidate_rows, state=state, weight=raw_weight, state_fn=state_fn),
            state,
            state_fn=state_fn,
        )
        gains = regime._gains(summary, regime._summary(blended_state))
        weights.append(
            {
                "raw_weight": raw_weight,
                "gains_pct": gains,
                "strict_eligible": _strict_eligible(gains),
                "score": _score(gains) if _strict_eligible(gains) else None,
            }
        )
    eligible = [row for row in weights if row["strict_eligible"]]
    if not eligible:
        return {
            "state": state,
            "state_status": "no_strictly_eligible_weight",
            "validation_day_count": days,
            "validation_sample_count": len(baseline_state),
            "weight_grid": weights,
        }
    best = max(eligible, key=lambda row: float(row["score"]))
    effective_weight = float(best["raw_weight"]) * days / (days + SHRINKAGE_PRIOR_DAYS)
    shrunk = _select_state_rows(
        _blend_rows(baseline_rows, candidate_rows, state=state, weight=effective_weight, state_fn=state_fn),
        state,
        state_fn=state_fn,
    )
    shrunk_gains = regime._gains(summary, regime._summary(shrunk))
    return {
        "state": state,
        "state_status": "strictly_eligible" if _strict_eligible(shrunk_gains) else "shrinkage_lost_strict_eligibility",
        "validation_day_count": days,
        "validation_sample_count": len(baseline_state),
        "raw_selected_weight": best["raw_weight"],
        "shrinkage_prior_days": SHRINKAGE_PRIOR_DAYS,
        "effective_weight": effective_weight,
        "improvement_pct": shrunk_gains,
        "weight_grid": weights,
    }


def _render(report: Mapping[str, Any]) -> str:
    selection = report["selection"]
    held_out = report["held_out_2025"]
    return "\n".join(
        [
            "# ZUUU ECMWF 联合物理状态影子验证（v3.14）",
            "",
            f"- 2024 严格选择状态：`{selection['selected_state']}`；有效混合权重 `{selection['effective_weight']}`。",
            f"- 2025 留出政策收益：NLL `{held_out['policy_improvement_pct']['bucket_nll']:+.3f}%`、"
            f"RPS `{held_out['policy_improvement_pct']['bucket_rps']:+.3f}%`、"
            f"MAE `{held_out['policy_improvement_pct']['mae_c']:+.3f}%`、"
            f"continue Brier `{held_out['policy_improvement_pct']['continue_brier']:+.3f}%`。",
            f"- 发布门：`{report['release_gate']['decision']}`；生产接入固定为否。",
            "",
            "## 预注册联合状态",
            "",
            "- 风型为互斥的 850 风、700 风、风切变及其组合；不再把所有触发都当成一类高通风。",
            "- 每个风型再按‘抑制’与‘非抑制’分组；抑制来自高湿低云、当日对流/降水或其后冷却、以及实况已达峰回落的并集。",
            "- 任一必需抑制通道缺失即不混合 ECMWF（零权重）。",
            "- 选择要求 NLL、RPS、continue Brier 严格改善；MAE 不得劣化超过 0.5%。",
            "",
            "## 边界",
            "",
            "- 2025 未参与状态或权重选择；未通过则保留 ECMWF 非影响性影子状态。",
            "- 这是 NCEI 小时标签研究，仍需带不可变取得时间封套的 AMSC 前向样本验证。",
            "",
        ]
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    rows = [row for row in shadow._load(args.features) if row.get("ecmwf__join_available")]
    train_2024 = [row for row in rows if str(row["event_date_local"]).startswith("2024-")]
    test_2025 = [row for row in rows if str(row["event_date_local"]).startswith("2025-")]
    rolling_baseline, rolling_candidate, rolling_trace = _rolling_predictions(train_2024)
    candidates = [_validation_result(rolling_baseline, rolling_candidate, state) for state in PRE_REGISTERED_STATES]
    eligible = [row for row in candidates if row["state_status"] == "strictly_eligible"]
    chosen = max(eligible, key=lambda row: _score(row["improvement_pct"])) if eligible else None
    selected_state = str(chosen["state"]) if chosen else "none"
    effective_weight = float(chosen["effective_weight"]) if chosen else 0.0

    baseline_2025, candidate_2025 = _fit_pair(train_2024, test_2025)
    policy_2025 = (
        _blend_rows(baseline_2025, candidate_2025, state=selected_state, weight=effective_weight)
        if chosen
        else [dict(row) for row in baseline_2025]
    )
    baseline_selected = _select_state_rows(baseline_2025, selected_state) if chosen else []
    policy_selected = _select_state_rows(policy_2025, selected_state) if chosen else []
    selected_gains = (
        regime._gains(regime._summary(baseline_selected), regime._summary(policy_selected)) if chosen else None
    )
    policy_gains = regime._gains(regime._summary(baseline_2025), regime._summary(policy_2025))
    state_counts = Counter(_state(row) or "not_eligible_or_missing_channel" for row in rows)
    state_assignment_count = sum(state_counts.values())
    heldout_strict = bool(selected_gains and _strict_eligible(selected_gains))

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "question": "whether a joint physical ECMWF shadow state can pass strict 2024 selection and 2025 held-out validation",
        "method": {
            "timezone": "Asia/Shanghai",
            "baseline": "v3.1: predawn GFS+JMA, 06:00-23:59 GFS",
            "candidate": "same regime augmented by ECMWF inside the selected joint state only",
            "pre_registered_states": list(PRE_REGISTERED_STATES),
            "missing_physical_channel_policy": "fail_closed_zero_weight",
            "selection_year": 2024,
            "held_out_year": 2025,
            "selection_rule": "NLL>0, RPS>0, continue_Brier>0, MAE>-0.5% after shrinkage",
            "shrinkage": "raw_weight * n_days/(n_days+60)",
        },
        "rolling_2024": {"folds": rolling_trace, "state_results": candidates},
        "selection": {
            "selected_state": selected_state,
            "effective_weight": effective_weight,
            "selected_from_2024_only": True,
            "selected_result": chosen,
        },
        "held_out_2025": {
            "test_sample_count": len(test_2025),
            "test_day_count": len({row["event_date_local"] for row in test_2025}),
            "selected_state_sample_count": len(baseline_selected),
            "selected_state_day_count": len({row["event_date_local"] for row in baseline_selected}),
            "selected_state_improvement_pct": selected_gains,
            "policy_improvement_pct": policy_gains,
            "strict_heldout_pass": heldout_strict,
        },
        "data_quality": {
            "feature_row_count": len(rows),
            "state_assignment_count": state_assignment_count,
            "state_partition_reconciles": state_assignment_count == len(rows),
            "pre_registered_state_count": len(PRE_REGISTERED_STATES),
            "state_counts": dict(sorted(state_counts.items())),
            "test_duplicate_key_count": len(baseline_2025)
            - len({(row["event_date_local"], float(row["local_hour"])) for row in baseline_2025}),
        },
        "release_gate": {
            "strict_2024_state_found": bool(chosen),
            "strict_2025_heldout_pass": heldout_strict,
            "forward_amsc_asof_evidence_ready": False,
            "production_ready": False,
            "decision": "retain_ecmwf_as_non_influential_shadow",
            "blockers": [
                *([] if chosen else ["no_joint_state_passed_strict_2024_selection"]),
                *([] if heldout_strict else ["joint_policy_did_not_pass_strict_2025_heldout_gate"]),
                "no_30_day_amsc_complete_day_asof_forward_validation",
                "production_promotion_not_authorized_by_research_job",
            ],
        },
        "guards": {
            "downloads_data": False,
            "historical_backfill": False,
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
    (args.output_dir / "joint_state_shadow_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "joint_state_shadow_report.md").write_text(_render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "selected_state": selected_state,
                "effective_weight": effective_weight,
                "strict_heldout_pass": heldout_strict,
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
