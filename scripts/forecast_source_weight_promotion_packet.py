#!/usr/bin/env python3
"""Generate a governed forecast-source weight proposal; never apply it."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lineage_identity_runtime import lineage_id_from_parts
from parameter_change_governance import build_parameter_change_request
from scripts.forecast_source_weight_release_gate import build_forecast_source_weight_release_gate


SCHEMA_VERSION = "forecast_source_weight_promotion_packet_v1"
DEFAULT_RESEARCH_ROOT = ROOT / "data" / "research" / "forecast_source_skill"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _weight_diff(champion: Mapping[str, Any], challenger: Mapping[str, Any]) -> dict[str, Any]:
    old = champion.get("global_weights") if isinstance(champion.get("global_weights"), Mapping) else {}
    new = challenger.get("global_weights") if isinstance(challenger.get("global_weights"), Mapping) else {}
    sources = sorted(set(old) | set(new))
    rows = []
    maximum = 0.0
    for source in sources:
        before = float(old.get(source) or 0.0)
        after = float(new.get(source) or 0.0)
        delta = after - before
        maximum = max(maximum, abs(delta))
        rows.append({"source_id": source, "before": before, "after": after, "delta": delta, "delta_percentage_points": 100.0 * delta})
    return {"rows": rows, "maximum_absolute_delta": maximum, "maximum_absolute_delta_percentage_points": 100.0 * maximum}


def build_promotion_packet(
    *,
    challenger_artifact: Mapping[str, Any],
    shadow_evaluation: Mapping[str, Any],
    champion_artifact: Mapping[str, Any],
    ablation: Mapping[str, Any] | None = None,
    include_curve: bool = False,
    max_weight_change_percentage_points: float = 5.0,
) -> dict[str, Any]:
    release = build_forecast_source_weight_release_gate(
        artifact=challenger_artifact,
        shadow_evaluation=shadow_evaluation,
        ablation=ablation,
        require_candidate=True,
        require_curve=include_curve,
        require_ablation=True,
    )
    diff = _weight_diff(champion_artifact, challenger_artifact)
    blockers = [str(item.get("blocker") or "") for item in release.get("blockers") or []]
    if not str(champion_artifact.get("artifact_sha256") or ""):
        blockers.append("parent_artifact_sha256_missing")
    if diff["maximum_absolute_delta_percentage_points"] > float(max_weight_change_percentage_points) + 1e-9:
        blockers.append("single_promotion_weight_change_exceeds_governance_cap")
    challenger_sha = str(challenger_artifact.get("artifact_sha256") or "")
    champion_sha = str(champion_artifact.get("artifact_sha256") or "")
    parent_runtime_mode = str(champion_artifact.get("runtime_champion_mode") or "approved_artifact")
    evaluation_sha = str(shadow_evaluation.get("evaluation_manifest_sha256") or "")
    ablation_sha = _sha(ablation or {})
    candidate_id = lineage_id_from_parts("calibration_candidate_id", "forecast_source_weight", champion_sha, challenger_sha, evaluation_sha, ablation_sha, str(include_curve))
    review_id = lineage_id_from_parts("review_id", "forecast_source_weight", candidate_id)
    daily_record = {
        "daily_calibration_record_id": lineage_id_from_parts("daily_calibration_record_id", "forecast_source_weight", evaluation_sha),
        "review_id": review_id,
    }
    candidate = {
        "calibration_candidate_id": candidate_id,
        "review_id": review_id,
        "impact_layer": "forecast_source_weight_artifact",
        "candidate_type": "contextual_curve" if include_curve else "contextual",
        "expected_improvement_metric": "bucket_nll_and_bucket_rps_strict_oos",
        "requires_historical_recompute": False,
        "possible_side_effects": ["forecast_weak_distribution_shift", "source_share_shift"],
        "proposal_json": {
            "artifact_sha256": challenger_sha,
            "parent_artifact_sha256": champion_sha,
            "parent_runtime_mode": parent_runtime_mode,
            "evaluation_manifest_sha256": evaluation_sha,
            "ablation_sha256": ablation_sha,
            "artifact_diff": diff,
        },
    }
    request = build_parameter_change_request(candidate=candidate, daily_calibration_record=daily_record)
    request["artifact_bindings"] = {
        "artifact_sha256": challenger_sha,
        "parent_artifact_sha256": champion_sha,
        "parent_runtime_mode": parent_runtime_mode,
        "evaluation_manifest_sha256": evaluation_sha,
        "ablation_sha256": ablation_sha,
    }
    request["rollback_plan"].update(
        {
            "rollback_artifact_sha256": "" if parent_runtime_mode == "legacy_no_active_artifact" else champion_sha,
            "rollback_runtime_mode": parent_runtime_mode,
            "requires_retraining": False,
            "action": (
                "atomically_remove_active_pointer_restore_legacy_runtime"
                if parent_runtime_mode == "legacy_no_active_artifact"
                else "atomically_restore_previous_approved_artifact_reference"
            ),
        }
    )
    request["measurement_plan"].update(
        {
            "windows_days": [7, 30, 90],
            "primary_metrics": ["bucket_nll", "bucket_rps"],
            "paired_by": "event_day",
        }
    )
    packet = {
        "schema": SCHEMA_VERSION,
        "read_only": True,
        "state": "proposed" if not blockers else "blocked",
        "promotion_proposal_eligible": not blockers,
        "production_apply_allowed": False,
        "include_curve": bool(include_curve),
        "candidate_id": candidate_id,
        "review_id": review_id,
        "release_gate": release,
        "artifact_diff": diff,
        "max_weight_change_percentage_points": float(max_weight_change_percentage_points),
        "parameter_change_request": request,
        "blockers": sorted(set(value for value in blockers if value)),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "changes_parameters": False,
            "applies_artifact": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": "manual_review_then_explicit_approval" if not blockers else "fix_packet_blockers_before_review",
    }
    packet["packet_sha256"] = _sha({key: value for key, value in packet.items() if key != "packet_sha256"})
    return packet


def _safe_output(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = DEFAULT_RESEARCH_ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("promotion_packet_output_outside_research_root")
    return resolved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--challenger-artifact", required=True, type=Path)
    parser.add_argument("--champion-artifact", required=True, type=Path)
    parser.add_argument("--shadow-evaluation", required=True, type=Path)
    parser.add_argument("--ablation", required=True, type=Path)
    parser.add_argument("--include-curve", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-research-write", action="store_true")
    args = parser.parse_args(argv)
    packet = build_promotion_packet(
        challenger_artifact=json.loads(args.challenger_artifact.read_text(encoding="utf-8")),
        champion_artifact=json.loads(args.champion_artifact.read_text(encoding="utf-8")),
        shadow_evaluation=json.loads(args.shadow_evaluation.read_text(encoding="utf-8")),
        ablation=json.loads(args.ablation.read_text(encoding="utf-8")),
        include_curve=args.include_curve,
    )
    if args.output:
        if not args.allow_research_write:
            raise ValueError("research_write_requires_explicit_allow_flag")
        output = _safe_output(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_canonical(packet) + b"\n")
    print(json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if packet["promotion_proposal_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
