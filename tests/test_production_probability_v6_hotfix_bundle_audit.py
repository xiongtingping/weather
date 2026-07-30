from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.production_probability_v6_hotfix_bundle_audit import audit


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_prepared_bundle_verifies_without_mutating_active_runtime(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    bundle = tmp_path / "data/candidates/bundle"
    bundle.mkdir(parents=True)
    active_activation = tmp_path / "config/production_probability_runtime_activation.json"
    active_activation.write_text('{"active":true}\n')
    candidate_activation = bundle / "production_probability_runtime_activation.v6_candidate.json"
    candidate_activation.write_text('{"candidate":true}\n')
    active_file = tmp_path / "runtime.py"
    active_file.write_text("active\n")
    candidate_file = bundle / "runtime.py"
    candidate_file.write_text("candidate\n")
    contract = {
        "schema": "production_probability_v6_hotfix_bundle_v1",
        "bundle_dir": "data/candidates/bundle",
        "activation_contract_sha256": "a" * 64,
        "runtime_manifest_sha256": "b" * 64,
        "candidate_activation_sha256": _sha(candidate_activation),
        "candidate_file_sha256": {"runtime.py": _sha(candidate_file)},
        "baseline": {
            "active_activation_sha256": _sha(active_activation),
            "active_file_sha256": {"runtime.py": _sha(active_file)},
        },
        "receipts": {
            "host": {
                "ok": True,
                "activation_contract_sha256": "a" * 64,
                "runtime_manifest_sha256": "b" * 64,
            }
        },
    }
    contract["bundle_contract_sha256"] = hashlib.sha256(
        json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    contract_path = tmp_path / "config/bundle.json"
    contract_path.write_text(json.dumps(contract))

    result = audit(tmp_path, contract_path)

    assert result["ok"] is True
    assert result["state"] == "prepared_bundle_verified"
    assert result["external_disk_required"] is False


def test_prepared_bundle_rejects_active_baseline_drift(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    active_activation = tmp_path / "config/production_probability_runtime_activation.json"
    active_activation.write_text("active")
    candidate_activation = bundle / "production_probability_runtime_activation.v6_candidate.json"
    candidate_activation.write_text("candidate")
    active_file = tmp_path / "runtime.py"
    active_file.write_text("drifted")
    candidate_file = bundle / "runtime.py"
    candidate_file.write_text("candidate")
    contract = {
        "bundle_dir": "bundle",
        "candidate_activation_sha256": _sha(candidate_activation),
        "candidate_file_sha256": {"runtime.py": _sha(candidate_file)},
        "baseline": {
            "active_activation_sha256": _sha(active_activation),
            "active_file_sha256": {"runtime.py": "0" * 64},
        },
        "receipts": {},
    }
    contract["bundle_contract_sha256"] = hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    contract_path = tmp_path / "config/bundle.json"
    contract_path.write_text(json.dumps(contract))

    result = audit(tmp_path, contract_path)

    assert result["ok"] is False
    assert "active_baseline_drift:runtime.py" in result["blockers"]
