#!/usr/bin/env python3
"""Read-only release contract for forecast source weight artifacts and shadow OOS."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SCHEMA_VERSION = "forecast_source_weight_release_gate_v1"


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "changes_parameters": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
    }


def build_forecast_source_weight_release_gate(
    *,
    artifact: Mapping[str, Any] | None = None,
    shadow_evaluation: Mapping[str, Any] | None = None,
    ablation: Mapping[str, Any] | None = None,
    require_candidate: bool = False,
    require_curve: bool = False,
    require_ablation: bool = False,
) -> dict[str, Any]:
    artifact_map = artifact if isinstance(artifact, Mapping) else {}
    shadow = shadow_evaluation if isinstance(shadow_evaluation, Mapping) else {}
    ablation_map = ablation if isinstance(ablation, Mapping) else {}
    if not artifact_map and not shadow and not require_candidate:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "read_only": True,
            "state": "not_configured",
            "coverage": {"checked_fact_count": 1},
            "checks": {"candidate_optional": True},
            "blockers": [],
            "warnings": ["forecast_source_weight_candidate_not_configured"],
            "guards": _guards(),
            "next_boundary": "retain_static_forecast_weak_weights",
        }
    blockers: list[dict[str, Any]] = []
    checks = {
        "artifact_schema": artifact_map.get("schema") == "forecast_source_contextual_weight_artifact_v1",
        "artifact_external_only": artifact_map.get("source_data_policy") == "external_only",
        "artifact_hash_present": bool(str(artifact_map.get("artifact_sha256") or "")),
        "artifact_runtime_not_self_applying": artifact_map.get("gates", {}).get("promotion_eligible") is not True
        or artifact_map.get("approval_state") == "approved",
        "shadow_schema": shadow.get("schema") == "forecast_source_weight_shadow_evaluation_v1",
        "strict_oos_evaluated": shadow.get("release_gate", {}).get("strict_oos_evaluated") is True,
        "contextual_nll_improved": shadow.get("release_gate", {}).get("nll_improved") is True,
        "contextual_rps_improved": shadow.get("release_gate", {}).get("rps_improved") is True,
        "contextual_statistical_gate": shadow.get("release_gate", {}).get("statistical_gate_passed") is True,
        "market_sources_excluded": shadow.get("guards", {}).get("market_sources_excluded") is True,
        "weak_sources_never_hard_lock": shadow.get("guards", {}).get("weak_sources_never_hard_lock") is True,
        "provider_family_dedup_enabled": shadow.get("guards", {}).get("provider_family_dedup_enabled") is True,
        "champion_matches_production_legacy_runtime": shadow.get("guards", {}).get("champion_matches_production_legacy_runtime") is True,
        "runtime_requests_never_change_parameters": shadow.get("guards", {}).get("runtime_requests_never_change_parameters") is True,
        "production_apply_closed": shadow.get("release_gate", {}).get("production_apply_allowed") is False,
    }
    if require_ablation:
        artifact_specification = str(artifact_map.get("selected_specification") or "") or (
            "weather_context"
            if artifact_map.get("context_fields")
            else "seasonal_lead"
            if int(artifact_map.get("seasonal_order") or 0) > 0 or artifact_map.get("include_lead_time") is True
            else "global"
        )
        checks.update(
            {
                "ablation_schema": ablation_map.get("schema") == "forecast_source_contextual_ablation_v1",
                "ablation_selected_artifact_specification": ablation_map.get("selected_specification") == artifact_specification,
            }
        )
    if require_curve:
        checks.update(
            {
                "curve_incremental_statistical_gate": shadow.get("release_gate", {}).get("curve_incremental_statistical_gate_passed") is True,
                "curve_consumed_once": shadow.get("guards", {}).get("amsc_curve_reliability_consumed_once") is True,
                "curve_production_proposal_eligible": shadow.get("release_gate", {}).get("curve_production_promotion_proposal_eligible") is True,
            }
        )
    for check, ok in checks.items():
        if not ok:
            blockers.append({"blocker": f"forecast_source_weight_{check}_failed", "check": check})
    for name, guard in (("artifact", artifact_map.get("guards")), ("shadow", shadow.get("guards"))):
        if isinstance(guard, Mapping):
            unsafe = {
                key: value
                for key, value in guard.items()
                if key in {"writes_archive_db", "writes_facts_sqlite", "changes_parameters", "places_orders", "restarts_launchd"}
                and bool(value)
            }
            if unsafe:
                blockers.append({"blocker": f"forecast_source_weight_{name}_unsafe_guard", "unsafe": unsafe})
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "state": "ready_for_governed_proposal" if not blockers else "blocked",
        "coverage": {"checked_fact_count": 1},
        "artifact_sha256": str(artifact_map.get("artifact_sha256") or ""),
        "evaluation_manifest_sha256": str(shadow.get("evaluation_manifest_sha256") or ""),
        "checks": checks,
        "blockers": blockers,
        "warnings": [],
        "guards": _guards(),
        "next_boundary": "generate_proposal_without_apply" if not blockers else "fix_forecast_source_weight_gate_before_proposal",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--shadow-evaluation", type=Path)
    parser.add_argument("--ablation", type=Path)
    parser.add_argument("--require-candidate", action="store_true")
    parser.add_argument("--require-curve", action="store_true")
    parser.add_argument("--require-ablation", action="store_true")
    args = parser.parse_args(argv)
    artifact = json.loads(args.artifact.read_text(encoding="utf-8")) if args.artifact else None
    shadow = json.loads(args.shadow_evaluation.read_text(encoding="utf-8")) if args.shadow_evaluation else None
    ablation = json.loads(args.ablation.read_text(encoding="utf-8")) if args.ablation else None
    report = build_forecast_source_weight_release_gate(
        artifact=artifact,
        shadow_evaluation=shadow,
        ablation=ablation,
        require_candidate=args.require_candidate,
        require_curve=args.require_curve,
        require_ablation=args.require_ablation,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
