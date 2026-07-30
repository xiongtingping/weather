#!/usr/bin/env python3
"""Rehearse the frozen bundle install in an isolated local source root."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prepare_production_probability_v6_hotfix_bundle import _copy_stage


def audit(root: Path, *, python: str) -> dict[str, object]:
    contract_path = root / "config/production_probability_runtime_bundle.v6_candidate.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    source_bundle = root / str(contract["bundle_dir"])
    with tempfile.TemporaryDirectory(prefix="v6-hotfix-install-rehearsal-", dir="/private/tmp") as raw:
        stage = Path(raw)
        _copy_stage(root, stage)
        stage_bundle = stage / str(contract["bundle_dir"])
        stage_bundle.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_bundle, stage_bundle)
        shutil.copy2(contract_path, stage / "config/production_probability_runtime_bundle.v6_candidate.json")
        for name in contract["candidate_file_sha256"]:
            shutil.copy2(stage_bundle / name, stage / name)
        shutil.copy2(
            stage_bundle / "production_probability_runtime_activation.v6_candidate.json",
            stage / "config/production_probability_runtime_activation.v6_candidate.json",
        )
        shutil.copy2(
            stage_bundle / "production_probability_runtime_activation.v6_candidate.json",
            stage / "config/production_probability_runtime_activation.json",
        )
        completed = subprocess.run(
            [python, "scripts/production_probability_v6_hotfix_bundle_audit.py", "--installed"],
            cwd=stage,
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            installed = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            installed = {
                "ok": False,
                "blockers": ["installed_bundle_audit_output_invalid"],
                "stderr": completed.stderr[-2000:],
            }
        blockers = list(installed.get("blockers") or [])
        if completed.returncode != 0:
            blockers.append(f"installed_bundle_audit_exit_{completed.returncode}")
        return {
            "schema": "production_probability_v6_hotfix_install_rehearsal_v1",
            "ok": not blockers and installed.get("ok") is True,
            "state": "isolated_install_rehearsal_accepted" if not blockers and installed.get("ok") is True else "blocked",
            "blockers": sorted(set(blockers)),
            "activation_contract_sha256": contract.get("activation_contract_sha256"),
            "runtime_manifest_sha256": contract.get("runtime_manifest_sha256"),
            "installed_bundle_audit": installed,
            "uses_private_tmp_source_root": True,
            "writes_active_runtime": False,
            "writes_archive_db": False,
            "places_orders": False,
            "external_disk_required": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    result = audit(args.root.resolve(), python=args.python)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
