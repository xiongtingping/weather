#!/usr/bin/env python3
"""Run v4.1 grouped joint-state ECMWF shadow validation for ZUUU.

This is the successor to the deliberately fine-grained v3.14 diagnostic.  It
keeps its physical suppressor union and strict binary-Brier requirement, while
grouping mutually exclusive wind types before shrinkage so a 15--19 day exact
subgroup cannot decide a model weight.  The job is research-only and cannot
change the v3.1 production posterior.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import evaluate_ncei_zuuu_additional_models_shadow_v31 as shadow
from scripts import evaluate_zuuu_ecmwf_high_ventilation_forward_v312 as v312
from scripts import evaluate_zuuu_ecmwf_joint_state_shadow_v314 as joint
from scripts import evaluate_ncei_zuuu_ecmwf_regime_shrinkage_v311 as regime


SCHEMA = "zuuu_ecmwf_grouped_joint_shadow_v41"
MIN_VALIDATION_DAYS = 30
WIND_FAMILIES = ("850_involved", "shear_without_850", "700_only")
PRE_REGISTERED_STATES = tuple(
    f"{family}__{context}" for family in WIND_FAMILIES for context in ("suppressed", "unsuppressed")
)


def _wind_family(wind_type: str) -> str | None:
    if wind_type == "none":
        return None
    if "850wind" in wind_type:
        return "850_involved"
    if "shear" in wind_type:
        return "shear_without_850"
    if wind_type == "700wind":
        return "700_only"
    return None


def _state(row: Mapping[str, Any]) -> str | None:
    exact = joint._state(row)
    if exact is None:
        return None
    wind_type, context = exact.split("__", maxsplit=1)
    family = _wind_family(wind_type)
    return f"{family}__{context}" if family else None


def _render(report: Mapping[str, Any]) -> str:
    selection = report["selection"]
    heldout = report["held_out_2025"]
    return "\n".join(
        [
            "# ZUUU ECMWF 分组联合状态影子验证（v4.1）",
            "",
            f"- 2024 严格选择状态：`{selection['selected_state']}`；有效权重 `{selection['effective_weight']}`。",
            f"- 2025 留出政策：NLL `{heldout['policy_improvement_pct']['bucket_nll']:+.3f}%`、"
            f"RPS `{heldout['policy_improvement_pct']['bucket_rps']:+.3f}%`、"
            f"MAE `{heldout['policy_improvement_pct']['mae_c']:+.3f}%`、"
            f"continue Brier `{heldout['policy_improvement_pct']['continue_brier']:+.3f}%`。",
            f"- 结论：`{report['release_gate']['decision']}`；生产接入固定为否。",
            "",
            "## v4.1 约束",
            "",
            "- 风型先合并为 850 hPa 风参与、无850的风切变、仅700 hPa 风三类，再与抑制/非抑制物理状态组合。",
            "- 每个状态至少 30 个 2024 滚动验证日；有效权重按 n/(n+60) 收缩。",
            "- 必须同时改善 NLL、RPS 和 continue Brier；任一物理通道缺失则零权重。",
            "- 2025 不参与状态、阈值或权重选择；未通过留出门即不进入生产。",
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
    rolling_baseline, rolling_candidate, rolling_trace = joint._rolling_predictions(train_2024)
    candidates = [
        joint._validation_result(
            rolling_baseline,
            rolling_candidate,
            state,
            state_fn=_state,
            min_validation_days=MIN_VALIDATION_DAYS,
        )
        for state in PRE_REGISTERED_STATES
    ]
    eligible = [row for row in candidates if row["state_status"] == "strictly_eligible"]
    chosen = max(eligible, key=lambda row: joint._score(row["improvement_pct"])) if eligible else None
    selected_state = str(chosen["state"]) if chosen else "none"
    effective_weight = float(chosen["effective_weight"]) if chosen else 0.0

    baseline_2025, candidate_2025 = joint._fit_pair(train_2024, test_2025)
    policy_2025 = (
        joint._blend_rows(
            baseline_2025, candidate_2025, state=selected_state, weight=effective_weight, state_fn=_state
        )
        if chosen
        else [dict(row) for row in baseline_2025]
    )
    baseline_selected = (
        joint._select_state_rows(baseline_2025, selected_state, state_fn=_state) if chosen else []
    )
    policy_selected = joint._select_state_rows(policy_2025, selected_state, state_fn=_state) if chosen else []
    selected_gains = (
        regime._gains(regime._summary(baseline_selected), regime._summary(policy_selected)) if chosen else None
    )
    policy_gains = regime._gains(regime._summary(baseline_2025), regime._summary(policy_2025))
    heldout_pass = bool(selected_gains and joint._strict_eligible(selected_gains))
    state_counts = Counter(_state(row) or "not_eligible_or_missing_channel" for row in rows)

    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "question": "whether grouped, shrunk physical ECMWF states can pass strict held-out validation",
        "method": {
            "timezone": "Asia/Shanghai",
            "baseline": "v3.1: predawn GFS+JMA, 06:00-23:59 GFS",
            "candidate": "same regime with ECMWF only inside the selected grouped state",
            "pre_registered_states": list(PRE_REGISTERED_STATES),
            "minimum_validation_days": MIN_VALIDATION_DAYS,
            "selection_year": 2024,
            "held_out_year": 2025,
            "strict_gate": "NLL>0, RPS>0, continue_Brier>0, MAE>-0.5% after shrinkage",
            "missing_physical_channel": "fail_closed_zero_weight",
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
            "strict_heldout_pass": heldout_pass,
        },
        "data_quality": {
            "feature_row_count": len(rows),
            "state_partition_reconciles": sum(state_counts.values()) == len(rows),
            "state_counts": dict(sorted(state_counts.items())),
            "test_duplicate_key_count": len(baseline_2025)
            - len({(row["event_date_local"], float(row["local_hour"])) for row in baseline_2025}),
        },
        "release_gate": {
            "strict_2024_state_found": bool(chosen),
            "strict_2025_heldout_pass": heldout_pass,
            "forward_amsc_asof_evidence_ready": False,
            "production_ready": False,
            "decision": "retain_ecmwf_as_non_influential_shadow",
            "blockers": [
                *([] if chosen else ["no_grouped_state_passed_strict_2024_selection"]),
                *([] if heldout_pass else ["grouped_policy_did_not_pass_strict_2025_heldout_gate"]),
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
    (args.output_dir / "grouped_joint_state_shadow_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "grouped_joint_state_shadow_report.md").write_text(_render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "selected_state": selected_state,
                "effective_weight": effective_weight,
                "strict_heldout_pass": heldout_pass,
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
