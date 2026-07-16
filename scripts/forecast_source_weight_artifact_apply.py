#!/usr/bin/env python3
"""Explicitly approved atomic apply/rollback for source-weight artifact pointer."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from forecast_source_weight_artifact_registry import (
    ACTIVE_SCHEMA,
    DEFAULT_ACTIVE_POINTER,
    DEFAULT_REGISTRY_ROOT,
    validate_artifact,
    validate_runtime_approved_artifact,
)


APPLY_CONFIRMATION = "APPLY_APPROVED_FORECAST_SOURCE_WEIGHT_ARTIFACT"
ROLLBACK_CONFIRMATION = "ROLLBACK_APPROVED_FORECAST_SOURCE_WEIGHT_ARTIFACT"
APPROVAL_SCHEMA = "forecast_source_weight_approval_v1"


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"mapping_required:{path}")
    return dict(value)


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def build_apply_plan(
    *,
    packet: Mapping[str, Any],
    artifact: Mapping[str, Any],
    approval: Mapping[str, Any],
    active_pointer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    artifact_contract = validate_artifact(artifact)
    if not artifact_contract["ok"]:
        issues.extend(artifact_contract["issues"])
    runtime_contract = validate_runtime_approved_artifact(artifact)
    if not runtime_contract["ok"]:
        issues.extend(runtime_contract["issues"])
    if packet.get("schema") != "forecast_source_weight_promotion_packet_v1":
        issues.append("promotion_packet_schema_invalid")
    if packet.get("promotion_proposal_eligible") is not True or packet.get("production_apply_allowed") is not False:
        issues.append("promotion_packet_not_eligible_or_apply_boundary_invalid")
    if packet.get("blockers"):
        issues.append("promotion_packet_has_blockers")
    if approval.get("schema") != APPROVAL_SCHEMA:
        issues.append("approval_schema_invalid")
    if approval.get("state") != "approved" or approval.get("explicit_user_approval") is not True:
        issues.append("explicit_approval_missing")
    if str(approval.get("candidate_id") or "") != str(packet.get("candidate_id") or ""):
        issues.append("approval_candidate_id_mismatch")
    if str(approval.get("packet_sha256") or "") != str(packet.get("packet_sha256") or ""):
        issues.append("approval_packet_sha256_mismatch")
    bindings = packet.get("parameter_change_request", {}).get("artifact_bindings", {})
    artifact_sha = str(artifact.get("artifact_sha256") or "")
    parent_sha = str(bindings.get("parent_artifact_sha256") or "")
    parent_runtime_mode = str(bindings.get("parent_runtime_mode") or "approved_artifact")
    if str(bindings.get("artifact_sha256") or "") != artifact_sha:
        issues.append("packet_artifact_sha256_mismatch")
    active = active_pointer if isinstance(active_pointer, Mapping) else {}
    active_sha = str(active.get("artifact_sha256") or "")
    if active_sha and active_sha != parent_sha:
        issues.append("active_parent_artifact_changed_since_proposal")
    previous_sha = active_sha or ("" if parent_runtime_mode == "legacy_no_active_artifact" else parent_sha)
    pointer = {
        "schema": ACTIVE_SCHEMA,
        "artifact_sha256": artifact_sha,
        "previous_artifact_sha256": previous_sha,
        "previous_runtime_mode": "approved_artifact" if active_sha else parent_runtime_mode,
        "candidate_id": str(packet.get("candidate_id") or ""),
        "packet_sha256": str(packet.get("packet_sha256") or ""),
        "approval_id": str(approval.get("approval_id") or ""),
        "approval_state": "approved",
        "explicit_user_approval": True,
        "approved_by": str(approval.get("approved_by") or ""),
        "approved_at": str(approval.get("approved_at") or ""),
        "applied_at": str(approval.get("approved_at") or ""),
        "measurement_windows_days": [7, 30, 90],
        "rollback_requires_retraining": False,
    }
    return {
        "schema": "forecast_source_weight_artifact_apply_plan_v1",
        "ok": not issues,
        "dry_run": True,
        "issues": sorted(set(issues)),
        "artifact_sha256": artifact_sha,
        "active_pointer": pointer,
        "guards": {
            "writes_artifact_registry": False,
            "writes_active_pointer": False,
            "writes_archive_db": False,
            "changes_parameters": False,
            "places_orders": False,
            "restarts_launchd": False,
        },
        "next_boundary": "explicit_confirmation_required" if not issues else "fix_apply_plan_issues",
    }


def apply_approved_artifact(
    *,
    packet_path: Path,
    artifact_path: Path,
    approval_path: Path,
    confirmation: str,
    active_pointer_path: Path = DEFAULT_ACTIVE_POINTER,
    registry_root: Path = DEFAULT_REGISTRY_ROOT,
) -> dict[str, Any]:
    packet, artifact, approval = _read(packet_path), _read(artifact_path), _read(approval_path)
    active = _read(active_pointer_path) if Path(active_pointer_path).exists() else {}
    plan = build_apply_plan(packet=packet, artifact=artifact, approval=approval, active_pointer=active)
    if confirmation != APPLY_CONFIRMATION:
        return plan
    if not plan["ok"]:
        return plan
    lock_path = Path(active_pointer_path).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = _read(active_pointer_path) if Path(active_pointer_path).exists() else {}
        expected_parent = str(plan["active_pointer"].get("previous_artifact_sha256") or "")
        if str(current.get("artifact_sha256") or "") != expected_parent and current:
            plan["ok"] = False
            plan["issues"] = ["active_parent_changed_during_apply"]
            return plan
        target = Path(registry_root) / f"{plan['artifact_sha256']}.json"
        if target.exists() and _read(target) != artifact:
            plan["ok"] = False
            plan["issues"] = ["immutable_artifact_registry_collision"]
            return plan
        if not target.exists():
            _atomic_json_write(target, artifact)
        _atomic_json_write(Path(active_pointer_path), plan["active_pointer"])
    plan["dry_run"] = False
    plan["guards"]["writes_artifact_registry"] = True
    plan["guards"]["writes_active_pointer"] = True
    plan["guards"]["changes_parameters"] = True
    plan["next_boundary"] = "measure_7_30_90_days"
    return plan


def rollback_active_artifact(
    *,
    active_pointer_path: Path,
    registry_root: Path,
    confirmation: str,
) -> dict[str, Any]:
    active = _read(active_pointer_path)
    previous = str(active.get("previous_artifact_sha256") or "")
    previous_runtime_mode = str(active.get("previous_runtime_mode") or "approved_artifact")
    legacy_restore = previous_runtime_mode == "legacy_no_active_artifact" and not previous
    issues = []
    if not previous and not legacy_restore:
        issues.append("previous_artifact_sha256_missing")
    if previous and not (Path(registry_root) / f"{previous}.json").exists():
        issues.append("previous_artifact_file_missing")
    report = {
        "schema": "forecast_source_weight_artifact_rollback_plan_v1",
        "ok": not issues,
        "dry_run": True,
        "issues": issues,
        "from_artifact_sha256": str(active.get("artifact_sha256") or ""),
        "to_artifact_sha256": previous,
        "to_runtime_mode": previous_runtime_mode,
        "rollback_requires_retraining": False,
        "guards": {"changes_parameters": False, "restarts_launchd": False, "places_orders": False},
    }
    if confirmation != ROLLBACK_CONFIRMATION or issues:
        return report
    if legacy_restore:
        Path(active_pointer_path).unlink(missing_ok=True)
    else:
        rolled = dict(active)
        rolled["artifact_sha256"], rolled["previous_artifact_sha256"] = previous, str(active.get("artifact_sha256") or "")
        rolled["previous_runtime_mode"] = "approved_artifact"
        rolled["rollback_from_packet_sha256"] = str(active.get("packet_sha256") or "")
        rolled["candidate_id"] = ""
        rolled["packet_sha256"] = ""
        _atomic_json_write(Path(active_pointer_path), rolled)
    report["dry_run"] = False
    report["guards"]["changes_parameters"] = True
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--confirmation", default="")
    parser.add_argument("--active-pointer", type=Path, default=DEFAULT_ACTIVE_POINTER)
    parser.add_argument("--registry-root", type=Path, default=DEFAULT_REGISTRY_ROOT)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args(argv)
    if args.rollback:
        report = rollback_active_artifact(
            active_pointer_path=args.active_pointer,
            registry_root=args.registry_root,
            confirmation=args.confirmation,
        )
    else:
        if not args.packet or not args.artifact or not args.approval:
            parser.error("--packet, --artifact, and --approval are required for apply")
        report = apply_approved_artifact(
            packet_path=args.packet,
            artifact_path=args.artifact,
            approval_path=args.approval,
            confirmation=args.confirmation,
            active_pointer_path=args.active_pointer,
            registry_root=args.registry_root,
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
