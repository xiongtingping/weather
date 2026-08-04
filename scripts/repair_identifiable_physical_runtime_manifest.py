#!/usr/bin/env python3
"""Build a controlled runtime-manifest repair release.

This is a narrow recovery step for the case where the active identifiable
physical activation and the current local runtime closure differ, while the
approved physical artifacts and the prior release evidence are unchanged.
It does not promote artifacts, modify the database, or activate production;
the normal identifiable-physical cutover command remains the only installer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
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
from production_probability_v6_live_route_contract import (  # noqa: E402
    audit_v6_live_routes,
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


SCHEMA_VERSION = "identifiable_physical_runtime_manifest_repair_v1"
CONFIRMATION = "REPAIR_IDENTIFIABLE_PHYSICAL_RUNTIME_MANIFEST"
RELEASE_ROOT = ROOT / "output/releases"
DEFAULT_OUTPUT = ROOT / "output/releases/identifiable_physical_state_bayes_v1_runtime_manifest_repair"
LIVE_ACTIVATION = ROOT / "config/production_probability_runtime_activation.json"
LIVE_ARTIFACTS = {
    FILE_NAMES["state_model"]: ROOT / "config/artifacts/identifiable_physical_state_model_v1.json",
    FILE_NAMES["observation"]: ROOT / "config/artifacts/identifiable_physical_observation_v1.json",
    FILE_NAMES["reporting_operator"]: ROOT / "config/artifacts/amsc_frozen_reporting_operator_v1.json",
}


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        )
        + "\n"
    ).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_mapping(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"repair_json_not_mapping:{path}")
    return dict(value)


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.repair.{os.getpid()}")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def _basis_release_sort_key(path: Path) -> tuple[int, float, str]:
    """Prefer the newest numbered repair release, then the base release.

    The active physical-state activation can legitimately move through several
    controlled repair releases as the runtime closure changes.  A fixed v1
    default becomes an accidental semantic-drift blocker after that happens.
    Ordering is only a candidate preference; _verify_basis remains the hard
    compatibility gate.
    """

    match = re.search(r"_runtime_manifest_repair_v(\d+)$", path.name)
    repair_number = int(match.group(1)) if match else -1
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return (repair_number, modified, path.name)


def _select_basis_release(active: Mapping[str, Any]) -> Path:
    candidates = [
        path
        for path in RELEASE_ROOT.glob("identifiable_physical_state_bayes_v1*")
        if path.is_dir() and (path / "release_manifest.json").is_file()
    ]
    for candidate in sorted(candidates, key=_basis_release_sort_key, reverse=True):
        try:
            _verify_basis(candidate, active)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        return candidate
    raise ValueError("repair_compatible_basis_release_not_found")


def _verify_basis(release_dir: Path, active: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _read_mapping(release_dir / "release_manifest.json")
    declared = str(manifest.get("release_manifest_sha256") or "")
    manifest_core = dict(manifest)
    manifest_core.pop("release_manifest_sha256", None)
    if declared != canonical_sha256(manifest_core):
        raise ValueError("repair_basis_release_manifest_sha_mismatch")
    if manifest.get("schema") != RELEASE_SCHEMA:
        raise ValueError("repair_basis_release_schema_invalid")
    gates = dict(manifest.get("blocking_gates") or {})
    if set(gates) != REQUIRED_BLOCKING_GATES or not all(value is True for value in gates.values()):
        raise ValueError("repair_basis_release_blocking_gate_not_true")
    policy = dict(manifest.get("promotion_policy") or {})
    if policy.get("nll_crps_excluded_from_blocking_gate_set") is not True:
        raise ValueError("repair_basis_release_nll_crps_policy_invalid")
    if policy.get("research_evidence_excluded_from_blocking_gate_set") is not True:
        raise ValueError("repair_basis_release_research_evidence_policy_invalid")

    files: dict[str, bytes] = {}
    for name in FILE_NAMES.values():
        path = release_dir / name
        raw = path.read_bytes()
        expected = str((dict(manifest.get("files") or {}).get(name) or {}).get("sha256") or "")
        if _sha_bytes(raw) != expected:
            raise ValueError(f"repair_basis_release_file_sha_mismatch:{name}")
        files[name] = raw
    basis_activation = validate_v6_activation(
        json.loads(files[FILE_NAMES["activation"]].decode("utf-8"))
    )
    active_without_runtime = dict(active)
    basis_without_runtime = dict(basis_activation)
    for value in (active_without_runtime, basis_without_runtime):
        value.pop("runtime_manifest_sha256", None)
        value.pop("activation_contract_sha256", None)
    # This field was added to the already-installed activation as an explicit
    # reporting contract.  Preserve it during the repair, but do not permit
    # arbitrary semantic activation drift to hide behind a manifest repair.
    for value in (active_without_runtime, basis_without_runtime):
        value.pop("reporting_slot_semantics", None)
    if active_without_runtime != basis_without_runtime:
        raise ValueError("repair_active_activation_semantic_drift")
    if str(active.get("release_readiness_sha256") or "") != str(manifest.get("readiness_sha256") or ""):
        raise ValueError("repair_active_readiness_binding_drift")
    return {"manifest": manifest, "files": files}


def build_repair(*, release_dir: Path | None, output_dir: Path) -> dict[str, Any]:
    active = validate_v6_activation(_read_mapping(LIVE_ACTIVATION))
    previous_runtime = str(active.get("runtime_manifest_sha256") or "")
    current_runtime = v6_runtime_manifest_sha256()
    if previous_runtime == current_runtime:
        raise ValueError("repair_not_needed_runtime_manifest_current")

    artifacts = {}
    for name, path in LIVE_ARTIFACTS.items():
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if name == FILE_NAMES["state_model"]:
            validated = validate_state_model_artifact(value)
            field = "physical_state_model_artifact_sha256"
        elif name == FILE_NAMES["observation"]:
            validated = validate_observation_artifact(value)
            field = "physical_observation_artifact_sha256"
        else:
            validated = validate_reporting_operator_artifact(value)
            field = "physical_reporting_operator_sha256"
        if validated.get("state") != "approved" or validated.get("production_runtime_eligible") is not True or validated.get("shadow_only") is not False:
            raise ValueError(f"repair_live_artifact_not_approved:{name}")
        if str(active.get(field) or "") != str(validated.get("artifact_sha256") or ""):
            raise ValueError(f"repair_active_artifact_binding_drift:{name}")
        artifacts[name] = raw

    selected_release_dir = release_dir or _select_basis_release(active)
    basis = _verify_basis(selected_release_dir, active)
    closure = audit_v6_runtime_manifest_dependency_closure(ROOT)
    if closure.get("ok") is not True:
        raise ValueError("repair_runtime_dependency_closure_blocked")
    routes = audit_v6_live_routes(ROOT)
    if routes.get("ok") is not True:
        raise ValueError("repair_resident_route_not_ready")

    repaired_activation = dict(active)
    repaired_activation["runtime_manifest_sha256"] = current_runtime
    repaired_activation["activation_contract_sha256"] = activation_sha256(repaired_activation)
    repaired_activation = validate_v6_activation(repaired_activation)
    activation_bytes = _canonical_bytes(repaired_activation)
    file_bytes = {
        **artifacts,
        FILE_NAMES["activation"]: activation_bytes,
    }
    repair_metadata = {
        "schema": SCHEMA_VERSION,
        "reason": "active_activation_runtime_manifest_drift",
        "previous_runtime_manifest_sha256": previous_runtime,
        "current_runtime_manifest_sha256": current_runtime,
        "basis_release_manifest_sha256": basis["manifest"]["release_manifest_sha256"],
        "basis_readiness_sha256": basis["manifest"]["readiness_sha256"],
        "artifact_bindings_unchanged": True,
        "database_writes": False,
        "places_orders": False,
    }
    manifest_core = {
        "schema": RELEASE_SCHEMA,
        "state": "release_built_not_active",
        "promotion_ready": True,
        "readiness_sha256": basis["manifest"]["readiness_sha256"],
        "blocking_gates": dict(basis["manifest"]["blocking_gates"]),
        "non_blocking_diagnostics": dict(basis["manifest"].get("non_blocking_diagnostics") or {}),
        "promotion_policy": dict(basis["manifest"].get("promotion_policy") or {}),
        "runtime_manifest_sha256": current_runtime,
        "activation_contract_sha256": repaired_activation["activation_contract_sha256"],
        "files": {
            name: {"sha256": _sha_bytes(value), "size_bytes": len(value)}
            for name, value in sorted(file_bytes.items())
        },
        "resident_service": "com.weather.weather_basket_yes",
        "adds_fourth_service": False,
        "places_orders": False,
        "activates_production": False,
        "runtime_manifest_repair": repair_metadata,
    }
    manifest = {
        **manifest_core,
        "release_manifest_sha256": canonical_sha256(manifest_core),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "repair_release_ready",
        "output_dir": str(output_dir),
        "basis_release_dir": str(selected_release_dir),
        "previous_runtime_manifest_sha256": previous_runtime,
        "current_runtime_manifest_sha256": current_runtime,
        "activation_contract_sha256": repaired_activation["activation_contract_sha256"],
        "basis_release_manifest_sha256": basis["manifest"]["release_manifest_sha256"],
        "release_manifest": manifest,
        "file_bytes": file_bytes,
        "runtime_closure": closure,
        "live_routes": routes,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release-dir",
        type=Path,
        default=None,
        help="Optional basis release; omitted selects the newest compatible release.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    try:
        result = build_repair(
            release_dir=(args.release_dir.expanduser().resolve() if args.release_dir else None),
            output_dir=args.output_dir.expanduser().resolve(),
        )
        if args.write:
            if args.confirm != CONFIRMATION:
                raise ValueError("repair_write_confirmation_invalid")
            output = args.output_dir.expanduser().resolve()
            for name, value in result["file_bytes"].items():
                _atomic_write(output / name, value)
            _atomic_write(output / "release_manifest.json", _canonical_bytes(result["release_manifest"]))
            result.pop("file_bytes", None)
            result.pop("release_manifest", None)
            result["state"] = "repair_release_written_not_active"
            result["writes_release"] = True
        else:
            result.pop("file_bytes", None)
            result.pop("release_manifest", None)
            result["writes_release"] = False
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked_fail_closed",
            "reason": f"{type(exc).__name__}:{exc}",
            "places_orders": False,
        }, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
