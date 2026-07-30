#!/usr/bin/env python3
"""Read-only identity audit for the pre-rehearsed v6 hotfix bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root: Path, contract_path: Path, *, installed: bool = False) -> dict[str, Any]:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    blockers: list[str] = []
    core = dict(contract)
    claimed_contract_sha = str(core.pop("bundle_contract_sha256", ""))
    actual_contract_sha = hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if claimed_contract_sha != actual_contract_sha:
        blockers.append("bundle_contract_sha256_mismatch")
    bundle_dir = root / str(contract.get("bundle_dir") or "")
    activation_path = bundle_dir / "production_probability_runtime_activation.v6_candidate.json"
    if not activation_path.is_file() or _sha(activation_path) != contract.get("candidate_activation_sha256"):
        blockers.append("candidate_activation_file_mismatch")
    for name, expected in dict(contract.get("candidate_file_sha256") or {}).items():
        candidate = bundle_dir / name
        if not candidate.is_file() or _sha(candidate) != expected:
            blockers.append(f"candidate_runtime_file_mismatch:{name}")
        target = root / name
        if installed:
            if not target.is_file() or _sha(target) != expected:
                blockers.append(f"installed_runtime_file_mismatch:{name}")
        else:
            baseline = dict(contract.get("baseline") or {}).get("active_file_sha256") or {}
            if not target.is_file() or _sha(target) != baseline.get(name):
                blockers.append(f"active_baseline_drift:{name}")
    active_activation = root / "config/production_probability_runtime_activation.json"
    if installed:
        if not active_activation.is_file() or _sha(active_activation) != contract.get("candidate_activation_sha256"):
            blockers.append("installed_activation_mismatch")
        else:
            activation = json.loads(active_activation.read_text(encoding="utf-8"))
            if activation.get("activation_contract_sha256") != contract.get("activation_contract_sha256"):
                blockers.append("installed_activation_contract_mismatch")
            if activation.get("runtime_manifest_sha256") != contract.get("runtime_manifest_sha256"):
                blockers.append("installed_activation_manifest_mismatch")
            try:
                from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256

                installed_manifest = v6_runtime_manifest_sha256(root)
            except Exception as exc:
                blockers.append(f"installed_runtime_manifest_unavailable:{type(exc).__name__}")
            else:
                if installed_manifest != contract.get("runtime_manifest_sha256"):
                    blockers.append("installed_runtime_manifest_mismatch")
    elif not active_activation.is_file() or _sha(active_activation) != dict(contract.get("baseline") or {}).get("active_activation_sha256"):
        blockers.append("active_activation_baseline_drift")
    for name, receipt in dict(contract.get("receipts") or {}).items():
        if receipt.get("ok") is not True:
            blockers.append(f"candidate_receipt_not_accepted:{name}")
        receipt_manifest = str(receipt.get("runtime_manifest_sha256") or "")
        if receipt_manifest and receipt_manifest != contract.get("runtime_manifest_sha256"):
            blockers.append(f"candidate_receipt_manifest_mismatch:{name}")
        receipt_activation = str(receipt.get("activation_contract_sha256") or "")
        if receipt_activation and receipt_activation != contract.get("activation_contract_sha256"):
            blockers.append(f"candidate_receipt_activation_mismatch:{name}")
    return {
        "schema": "production_probability_v6_hotfix_bundle_audit_v1",
        "ok": not blockers,
        "state": "installed_bundle_verified" if installed and not blockers else "prepared_bundle_verified" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "activation_contract_sha256": contract.get("activation_contract_sha256"),
        "runtime_manifest_sha256": contract.get("runtime_manifest_sha256"),
        "bundle_contract_sha256": claimed_contract_sha,
        "installed": installed,
        "external_disk_probed": False,
        "external_disk_required": False,
        "read_only": True,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--contract",
        type=Path,
        default=ROOT / "config/production_probability_runtime_bundle.v6_candidate.json",
    )
    parser.add_argument("--installed", action="store_true")
    args = parser.parse_args()
    result = audit(args.root.resolve(), args.contract.resolve(), installed=args.installed)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
