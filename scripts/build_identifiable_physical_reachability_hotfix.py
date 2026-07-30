#!/usr/bin/env python3
"""Build a controlled release for the source-backed physical reachability fix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
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
from production_probability_v6_activation_contract import (  # noqa: E402
    activation_sha256,
    validate_v6_activation,
)
from production_probability_v6_runtime_manifest import (  # noqa: E402
    audit_v6_runtime_manifest_dependency_closure,
    v6_runtime_manifest_sha256,
)
from scripts.build_identifiable_physical_state_release import (  # noqa: E402
    FILE_NAMES,
    REQUIRED_BLOCKING_GATES,
    SCHEMA_VERSION as RELEASE_SCHEMA,
)


SCHEMA_VERSION = "identifiable_physical_reachability_hotfix_release_v1"
CONFIRMATION = "BUILD_IDENTIFIABLE_PHYSICAL_REACHABILITY_HOTFIX"
BASIS_RELEASE = ROOT / "output/releases/identifiable_physical_state_bayes_v1"
DEFAULT_OUTPUT = ROOT / "output/releases/identifiable_physical_state_bayes_v1_physical_reachability_hotfix"
ACTIVE_ACTIVATION = ROOT / "config/production_probability_runtime_activation.json"
ACTIVE_STATE = ROOT / "config/artifacts/identifiable_physical_state_model_v1.json"
ACTIVE_OBSERVATION = ROOT / "config/artifacts/identifiable_physical_observation_v1.json"
ACTIVE_REPORTING = ROOT / "config/artifacts/amsc_frozen_reporting_operator_v1.json"


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n").encode()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"hotfix_json_not_mapping:{path}")
    return dict(value)


def build(*, output_dir: Path) -> dict[str, Any]:
    basis = _read(BASIS_RELEASE / "release_manifest.json")
    basis_core = dict(basis)
    if basis_core.pop("release_manifest_sha256", "") != canonical_sha256(basis_core):
        raise ValueError("hotfix_basis_release_manifest_sha_mismatch")
    if basis.get("schema") != RELEASE_SCHEMA or basis.get("promotion_ready") is not True:
        raise ValueError("hotfix_basis_release_not_promotion_ready")
    gates = dict(basis.get("blocking_gates") or {})
    if set(gates) != REQUIRED_BLOCKING_GATES or not all(gates.values()):
        raise ValueError("hotfix_basis_blocking_gate_not_true")
    readiness_sha = str(basis.get("readiness_sha256") or "")
    if len(readiness_sha) != 64:
        raise ValueError("hotfix_basis_readiness_sha_invalid")

    state = validate_state_model_artifact(_read(ACTIVE_STATE))
    observation = validate_observation_artifact(_read(ACTIVE_OBSERVATION))
    reporting = validate_reporting_operator_artifact(_read(ACTIVE_REPORTING))
    for name, artifact in (("state", state), ("observation", observation), ("reporting", reporting)):
        if artifact.get("state") != "approved" or artifact.get("production_runtime_eligible") is not True or artifact.get("shadow_only") is not False:
            raise ValueError(f"hotfix_artifact_not_approved:{name}")
    if str(state.get("promotion_evidence_sha256") or "") != readiness_sha:
        raise ValueError("hotfix_state_readiness_binding_invalid")

    activation = validate_v6_activation(_read(ACTIVE_ACTIVATION))
    if activation.get("release_readiness_sha256") != readiness_sha:
        raise ValueError("hotfix_active_readiness_binding_invalid")
    if activation.get("physical_observation_artifact_sha256") != observation.get("artifact_sha256"):
        raise ValueError("hotfix_observation_binding_invalid")
    if activation.get("physical_reporting_operator_sha256") != reporting.get("artifact_sha256"):
        raise ValueError("hotfix_reporting_binding_invalid")
    old_state_hash = str(activation.get("physical_state_model_artifact_sha256") or "")
    if old_state_hash == state.get("artifact_sha256"):
        raise ValueError("hotfix_state_artifact_already_active")

    runtime = v6_runtime_manifest_sha256(ROOT)
    closure = audit_v6_runtime_manifest_dependency_closure(ROOT)
    if closure.get("ok") is not True:
        raise ValueError("hotfix_runtime_dependency_closure_blocked")
    next_activation = dict(activation)
    next_activation["physical_state_model_artifact_sha256"] = state["artifact_sha256"]
    next_activation["runtime_manifest_sha256"] = runtime
    next_activation["activation_contract_sha256"] = activation_sha256(next_activation)
    next_activation = validate_v6_activation(next_activation)
    files = {
        FILE_NAMES["state_model"]: _canonical_bytes(state),
        FILE_NAMES["observation"]: _canonical_bytes(observation),
        FILE_NAMES["reporting_operator"]: _canonical_bytes(reporting),
        FILE_NAMES["activation"]: _canonical_bytes(next_activation),
    }
    manifest_core = {
        "schema": RELEASE_SCHEMA,
        "state": "release_built_not_active",
        "promotion_ready": True,
        "readiness_sha256": readiness_sha,
        "blocking_gates": gates,
        "non_blocking_diagnostics": dict(basis.get("non_blocking_diagnostics") or {}),
        "promotion_policy": dict(basis.get("promotion_policy") or {}),
        "runtime_manifest_sha256": runtime,
        "activation_contract_sha256": next_activation["activation_contract_sha256"],
        "files": {name: {"sha256": _sha_bytes(raw), "size_bytes": len(raw)} for name, raw in sorted(files.items())},
        "resident_service": "com.weather.reported_trajectory_no",
        "adds_fourth_service": False,
        "places_orders": False,
        "activates_production": False,
        "physical_reachability_hotfix": {
            "schema": SCHEMA_VERSION,
            "previous_state_model_artifact_sha256": old_state_hash,
            "state_model_artifact_sha256": state["artifact_sha256"],
            "pure_state_space_transition": True,
            "historical_database_mutation": False,
        },
    }
    manifest = {**manifest_core, "release_manifest_sha256": canonical_sha256(manifest_core)}
    return {"manifest": manifest, "files": files, "output_dir": str(output_dir), "closure": closure}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        result = build(output_dir=args.output_dir.expanduser().resolve())
        if args.write:
            if args.confirm != CONFIRMATION:
                raise ValueError("hotfix_write_confirmation_invalid")
            output = args.output_dir.expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
            for name, raw in result["files"].items():
                temporary = output / f".{name}.hotfix.{os.getpid()}"
                temporary.write_bytes(raw)
                os.replace(temporary, output / name)
            manifest_raw = _canonical_bytes(result["manifest"])
            temporary = output / f".release_manifest.json.hotfix.{os.getpid()}"
            temporary.write_bytes(manifest_raw)
            os.replace(temporary, output / "release_manifest.json")
        result.pop("files", None)
        result["ok"] = True
        result["state"] = "release_written_not_active" if args.write else "release_preview_ready"
        result["writes_database"] = False
        result["places_orders"] = False
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "state": "blocked_fail_closed", "reason": f"{type(exc).__name__}:{exc}"}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
