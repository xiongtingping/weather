#!/usr/bin/env python3
"""Atomically activate a previously built identifiable-physical release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from identifiable_physical_state_bayes.core import (  # noqa: E402
    canonical_sha256,
    validate_observation_artifact,
    validate_state_model_artifact,
)
from identifiable_physical_state_bayes.reporting import (  # noqa: E402
    validate_reporting_operator_artifact,
)
from physical_state_bayes_v6.schema_contract import (  # noqa: E402
    build_forward_plan,
    verify_schema,
)
from production_probability_v6_activation_contract import (  # noqa: E402
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_live_route_contract import audit_v6_live_routes  # noqa: E402
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256  # noqa: E402
from scripts.build_identifiable_physical_state_release import (  # noqa: E402
    FILE_NAMES,
    REQUIRED_BLOCKING_GATES,
    SCHEMA_VERSION as RELEASE_SCHEMA,
)


SCHEMA_VERSION = "identifiable_physical_state_production_cutover_v1"
CONFIRMATION = "ACTIVATE_IDENTIFIABLE_PHYSICAL_STATE_BAYES_V1"
LIVE_PATHS = {
    "identifiable_physical_state_model_v1.json": ROOT
    / "config/artifacts/identifiable_physical_state_model_v1.json",
    "identifiable_physical_observation_v1.json": ROOT
    / "config/artifacts/identifiable_physical_observation_v1.json",
    "amsc_frozen_reporting_operator_v1.json": ROOT
    / "config/artifacts/amsc_frozen_reporting_operator_v1.json",
    "production_probability_runtime_activation.json": ROOT
    / "config/production_probability_runtime_activation.json",
}


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_replace(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.cutover.{os.getpid()}")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def verify_release(release_dir: Path) -> dict[str, Any]:
    manifest = json.loads(
        (release_dir / "release_manifest.json").read_text(encoding="utf-8")
    )
    if not isinstance(manifest, Mapping):
        raise ValueError("physical_cutover_manifest_not_mapping")
    value = dict(manifest)
    declared = str(value.pop("release_manifest_sha256", ""))
    if declared != canonical_sha256(value):
        raise ValueError("physical_cutover_manifest_sha_mismatch")
    if manifest.get("schema") != RELEASE_SCHEMA:
        raise ValueError("physical_cutover_manifest_schema_invalid")
    gates = dict(manifest.get("blocking_gates") or {})
    if set(gates) != REQUIRED_BLOCKING_GATES or not all(
        item is True for item in gates.values()
    ):
        raise ValueError("physical_cutover_release_gate_not_true")
    if any("nll" in key.lower() or "crps" in key.lower() for key in gates):
        raise ValueError("physical_cutover_nll_crps_blocker_forbidden")
    policy = dict(manifest.get("promotion_policy") or {})
    if policy.get("nll_crps_excluded_from_blocking_gate_set") is not True:
        raise ValueError("physical_cutover_nll_crps_policy_invalid")
    if policy.get("research_evidence_excluded_from_blocking_gate_set") is not True:
        raise ValueError("physical_cutover_research_evidence_policy_invalid")
    if any(
        key in gates
        for key in (
            "state_parameter_draws_bound_to_calibration",
            "strict_asof_parameter_calibration_ready",
            "canonical_replay_complete",
            "canonical_replay_blocking_gates_pass",
        )
    ):
        raise ValueError("physical_cutover_research_evidence_blocker_forbidden")
    file_contract = dict(manifest.get("files") or {})
    files = {}
    for name in FILE_NAMES.values():
        raw = (release_dir / name).read_bytes()
        expected = str((file_contract.get(name) or {}).get("sha256") or "")
        if _sha_bytes(raw) != expected:
            raise ValueError(f"physical_cutover_release_file_sha_mismatch:{name}")
        files[name] = raw
    activation = validate_v6_activation(
        json.loads(files[FILE_NAMES["activation"]].decode("utf-8"))
    )
    artifacts = {
        "physical_state_model_artifact_sha256": validate_state_model_artifact(
            json.loads(files[FILE_NAMES["state_model"]].decode("utf-8"))
        ),
        "physical_observation_artifact_sha256": validate_observation_artifact(
            json.loads(files[FILE_NAMES["observation"]].decode("utf-8"))
        ),
        "physical_reporting_operator_sha256": validate_reporting_operator_artifact(
            json.loads(files[FILE_NAMES["reporting_operator"]].decode("utf-8"))
        ),
    }
    if any(
        artifact.get("state") != "approved"
        or artifact.get("production_runtime_eligible") is not True
        or artifact.get("shadow_only") is not False
        or activation.get(field) != artifact.get("artifact_sha256")
        for field, artifact in artifacts.items()
    ):
        raise ValueError("physical_cutover_approved_artifact_binding_invalid")
    if activation.get("composer_schema") != IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        raise ValueError("physical_cutover_activation_composer_invalid")
    if (
        activation.get("release_readiness_sha256")
        != manifest.get("readiness_sha256")
    ):
        raise ValueError("physical_cutover_readiness_binding_mismatch")
    if activation.get("runtime_manifest_sha256") != v6_runtime_manifest_sha256():
        raise ValueError("physical_cutover_runtime_manifest_drift")
    if manifest.get("runtime_manifest_sha256") != activation.get(
        "runtime_manifest_sha256"
    ):
        raise ValueError("physical_cutover_release_runtime_binding_mismatch")
    if manifest.get("activation_contract_sha256") != activation.get(
        "activation_contract_sha256"
    ):
        raise ValueError("physical_cutover_release_activation_binding_mismatch")
    route = audit_v6_live_routes(ROOT)
    if route.get("ok") is not True:
        raise ValueError("physical_cutover_resident_route_not_ready")
    return {
        "manifest": dict(manifest),
        "activation": activation,
        "files": files,
        "route": route,
    }


def apply_cutover(*, db_path: Path, release: Mapping[str, Any]) -> dict[str, Any]:
    plan = build_forward_plan(db_path)
    if plan.get("ok") is not True:
        raise ValueError("physical_cutover_additive_schema_plan_blocked")
    files = dict(release["files"])
    activation_path = LIVE_PATHS[FILE_NAMES["activation"]]
    previous_activation = activation_path.read_bytes()
    activation_replaced = False
    with sqlite3.connect(db_path, timeout=1.0) as conn:
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("BEGIN EXCLUSIVE")
        try:
            for action in plan.get("actions") or []:
                conn.execute(str(action["sql"]))
            if verify_schema(conn).get("ok") is not True:
                raise RuntimeError("physical_cutover_schema_postcondition_failed")
            for name in (
                FILE_NAMES["state_model"],
                FILE_NAMES["observation"],
                FILE_NAMES["reporting_operator"],
            ):
                _atomic_replace(LIVE_PATHS[name], files[name])
            _atomic_replace(activation_path, files[FILE_NAMES["activation"]])
            activation_replaced = True
            conn.commit()
        except Exception:
            conn.rollback()
            if activation_replaced:
                _atomic_replace(activation_path, previous_activation)
            raise
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        schema = verify_schema(conn)
    active = validate_v6_activation(
        json.loads(activation_path.read_text(encoding="utf-8"))
    )
    postcommit_ok = bool(
        schema.get("ok") is True
        and active.get("activation_contract_sha256")
        == release["activation"].get("activation_contract_sha256")
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": postcommit_ok,
        "state": "activated_service_restart_required"
        if postcommit_ok
        else "postcommit_blocked_fail_closed",
        "activation_contract_sha256": active.get("activation_contract_sha256"),
        "readiness_sha256": release["manifest"].get("readiness_sha256"),
        "additive_schema": schema,
        "resident_service_restart_required": True,
        "automatic_old_composer_fallback_after_commit": False,
        "adds_fourth_service": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data/archive.db")
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writer-quiescent", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        release = verify_release(args.release_dir.expanduser().resolve())
        plan = build_forward_plan(args.db.expanduser().resolve())
        preview = {
            "schema": SCHEMA_VERSION,
            "ok": plan.get("ok") is True,
            "state": "cutover_preview_ready",
            "readiness_sha256": release["manifest"].get("readiness_sha256"),
            "activation_contract_sha256": release["activation"].get(
                "activation_contract_sha256"
            ),
            "additive_schema_action_count": len(plan.get("actions") or []),
            "requires_writer_quiescence": True,
            "requires_resident_service_restart": True,
            "adds_fourth_service": False,
            "places_orders": False,
        }
        if not args.apply:
            print(json.dumps(preview, ensure_ascii=False, sort_keys=True))
            return 0 if preview["ok"] else 2
        if not args.writer_quiescent or args.confirm != CONFIRMATION:
            raise ValueError("physical_cutover_confirmation_or_quiescence_missing")
        result = apply_cutover(
            db_path=args.db.expanduser().resolve(), release=release
        )
    except (OSError, ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as exc:
        result = {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_fail_closed",
            "reason": f"{type(exc).__name__}:{exc}",
            "adds_fourth_service": False,
            "places_orders": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
