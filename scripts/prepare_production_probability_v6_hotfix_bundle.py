#!/usr/bin/env python3
"""Build and rehearse the v6 successor bundle before the maintenance window.

The builder never mutates active runtime files, the active activation, or the
archive database.  It creates a local isolated source tree, installs the
candidate transform there, runs tests/replay/drills, and publishes a
content-addressed bundle for the short controlled cutover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BUNDLE_POINTER = ROOT / "config/production_probability_runtime_bundle.v6_candidate.json"
ACTIVE_ACTIVATION = ROOT / "config/production_probability_runtime_activation.json"
ACTIVE_FILES = (
    "production_probability_v6_canonical_event_drain_runtime.py",
    "production_probability_v6_amsc_archive_ingress.py",
)
COPY_DIRS = (
    "bot",
    "config",
    "configs",
    "dist",
    "launchd",
    "identifiable_physical_state_bayes",
    "physical_state_bayes_v6",
    "scripts",
    "tests",
    "ui",
)
TEST_FILES = (
    "tests/test_install_v6_market_transaction_branch_runtime.py",
    "tests/test_production_probability_v6_market_context_runtime.py",
    "tests/test_production_probability_v6_market_inputs_runtime.py",
    "tests/test_production_probability_v6_realization_policy_runtime.py",
    "tests/test_production_probability_v6_strong_physical_projection_runtime.py",
    "tests/test_production_probability_v6_market_bundle_runtime.py",
    "tests/test_production_probability_v6_market_transaction_writer.py",
    "tests/test_production_probability_v6_transaction_branch_runtime.py",
    "tests/test_production_probability_v6_transaction_commit_runtime.py",
    "tests/test_production_probability_v6_path_auxiliary_runtime.py",
    "tests/test_physical_state_bayes_v6.py",
    "tests/test_physical_state_bayes_v6_scenario_joint.py",
    "tests/test_physical_state_bayes_v6_models.py",
    "tests/test_physical_state_bayes_v6_forcing_adapter.py",
    "tests/test_physical_state_bayes_v6_observation_adapter.py",
    "tests/test_production_probability_v6_activation_contract.py",
    "tests/test_production_probability_v6_p0_runtime.py",
    "tests/test_production_probability_v6_candidate_compiler.py",
    "tests/test_production_probability_v6_event_runtime.py",
    "tests/test_production_probability_v6_weak_revision_runtime.py",
    "tests/test_production_probability_v6_runtime_manifest.py",
    "tests/test_production_probability_v6_live_route_contract.py",
    "tests/test_forecast_background_context_runtime.py",
    "tests/test_joint_weak_live_evidence_runtime.py",
    "tests/test_weak_forecast_evidence_snapshot_runtime.py",
    "tests/test_amsc_half_hour_trigger_runtime.py",
    "tests/test_production_weak_forecast_revision_event_runtime.py",
    "tests/test_physical_state_bayes_v6_cutover_static.py",
    "tests/test_production_probability_v6_canonical_event_drain_runtime.py",
    "tests/test_physical_state_bayes_v6_schema_contract.py",
    "tests/test_apply_amsc_probability_fast_lane_v6_from_plan.py",
    "tests/test_check_ui_page_probability_consistency.py",
)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _write_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_bytes(value)
    temporary.replace(path)


def _run(command: list[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "candidate_command_failed:"
            + json.dumps(
                {
                    "command": command,
                    "returncode": completed.returncode,
                    "stdout_tail": completed.stdout[-4000:],
                    "stderr_tail": completed.stderr[-4000:],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def _copy_stage(root: Path, stage: Path) -> None:
    for path in root.glob("*.py"):
        shutil.copy2(path, stage / path.name)
    for name in COPY_DIRS:
        source = root / name
        if source.is_dir():
            shutil.copytree(
                source,
                stage / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
    data = stage / "data"
    data.mkdir()
    for name in ("archive.db", "archive.db-wal", "archive.db-shm"):
        source = root / "data" / name
        if source.exists():
            (data / name).symlink_to(source)
    for name in ("research", "runtime"):
        source = root / "data" / name
        if source.exists():
            (data / name).symlink_to(source, target_is_directory=True)


def _json_receipt(path: Path, expected_schema: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("ok") is not True or value.get("schema") != expected_schema:
        raise ValueError(f"candidate_receipt_rejected:{path.name}")
    return value


def prepare(root: Path, *, event_date: str, python: str) -> dict[str, Any]:
    source_db = root / "data/archive.db"
    if not source_db.is_file():
        raise FileNotFoundError("production_archive_db_missing")
    baseline = {
        "active_activation_sha256": _sha_file(
            root / ACTIVE_ACTIVATION.relative_to(ROOT)
        ),
        "active_file_sha256": {name: _sha_file(root / name) for name in ACTIVE_FILES},
    }

    def assert_active_unchanged(stage_label: str) -> None:
        activation = root / ACTIVE_ACTIVATION.relative_to(ROOT)
        if _sha_file(activation) != baseline["active_activation_sha256"]:
            raise RuntimeError(f"candidate_preparation_mutated_active_activation:{stage_label}")
        for name, expected in baseline["active_file_sha256"].items():
            if _sha_file(root / name) != expected:
                raise RuntimeError(f"candidate_preparation_mutated_active_runtime:{stage_label}:{name}")

    with tempfile.TemporaryDirectory(prefix="v6-hotfix-candidate-", dir="/private/tmp") as raw_stage:
        stage = Path(raw_stage)
        _copy_stage(root, stage)
        assert_active_unchanged("copy_stage")
        _run(
            [
                python,
                "scripts/install_v6_market_transaction_branch_runtime.py",
                "--root",
                str(stage),
                "--apply",
                "--confirm",
                "INSTALL_V6_MARKET_TRANSACTION_BRANCH_RUNTIME",
            ],
            cwd=stage,
        )
        assert_active_unchanged("install_candidate_in_stage")
        activation_path = stage / "config/production_probability_runtime_activation.v6_candidate.json"
        _run(
            [
                python,
                "scripts/build_production_probability_v6_candidate_activation.py",
                "--root",
                str(stage),
                "--write",
            ],
            cwd=stage,
        )
        assert_active_unchanged("freeze_candidate_activation_in_stage")
        activation = json.loads(activation_path.read_text(encoding="utf-8"))
        # The isolated rehearsal must execute the candidate artifact and
        # candidate activation together.  Leaving the predecessor activation
        # in the stage while loading the candidate observation artifact is an
        # intentional fail-closed mismatch, not a meaningful candidate test.
        shutil.copy2(activation_path, stage / ACTIVE_ACTIVATION.relative_to(ROOT))
        test_receipt = _run([python, "-m", "pytest", "-q", *TEST_FILES], cwd=stage)
        assert_active_unchanged("candidate_tests")

        receipts_dir = stage / "output/audits"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        replay_path = receipts_dir / "canonical_replay.json"
        zero_path = receipts_dir / "zero_order.json"
        host_path = receipts_dir / "host_rehearsal.json"
        physical_counterfactual_path = receipts_dir / "physical_counterfactual.json"
        walk_forward_path = receipts_dir / "walk_forward_calibration.json"
        _run(
            [python, "scripts/replay_physical_state_bayes_v6_canonical.py", "--db", str(source_db), "--output", str(replay_path)],
            cwd=stage,
        )
        assert_active_unchanged("canonical_replay")
        _run(
            [python, "scripts/production_probability_v6_zero_order_drill.py", "--db", str(source_db), "--output", str(zero_path)],
            cwd=stage,
        )
        assert_active_unchanged("zero_order_drill")
        _run(
            [
                python,
                "scripts/production_probability_v6_host_rehearsal.py",
                "--db",
                str(source_db),
                "--event-date",
                event_date,
                "--output",
                str(host_path),
            ],
            cwd=stage,
        )
        assert_active_unchanged("host_rehearsal")
        _run(
            [
                python,
                "scripts/production_probability_v6_physical_counterfactual_acceptance.py",
                "--db", str(source_db),
                "--event-date", event_date,
                "--output", str(physical_counterfactual_path),
            ],
            cwd=stage,
        )
        assert_active_unchanged("physical_counterfactual")
        _run(
            [
                python,
                "scripts/production_probability_v6_walk_forward_calibration.py",
                "--db", str(source_db),
                "--activation", str(activation_path),
                "--output", str(walk_forward_path),
            ],
            cwd=stage,
        )
        assert_active_unchanged("walk_forward_calibration")
        receipts = {
            "canonical_replay": _json_receipt(replay_path, "physical_state_bayes_v6_canonical_production_replay_v1"),
            "zero_order": _json_receipt(zero_path, "production_probability_v6_zero_order_drill_v1"),
            "host_rehearsal": _json_receipt(host_path, "production_probability_v6_host_rehearsal_v1"),
            "physical_counterfactual": _json_receipt(
                physical_counterfactual_path,
                "production_probability_v6_physical_counterfactual_acceptance_v1",
            ),
            "walk_forward_calibration": _json_receipt(
                walk_forward_path,
                "production_probability_v6_strict_day_walk_forward_calibration_v1",
            ),
        }
        activation_sha = str(activation["activation_contract_sha256"])
        bundle_dir = root / "data/candidates/physical_state_bayes_v6_runtime_hotfix" / activation_sha
        temporary_bundle = bundle_dir.with_name(bundle_dir.name + f".tmp-{os.getpid()}")
        if temporary_bundle.exists():
            shutil.rmtree(temporary_bundle)
        temporary_bundle.mkdir(parents=True)
        candidate_files = {}
        for name in ACTIVE_FILES:
            source = stage / name
            destination = temporary_bundle / name
            shutil.copy2(source, destination)
            candidate_files[name] = _sha_file(destination)
        shutil.copy2(activation_path, temporary_bundle / activation_path.name)
        for name, source in (
            ("canonical_replay.json", replay_path),
            ("zero_order.json", zero_path),
            ("host_rehearsal.json", host_path),
            ("physical_counterfactual.json", physical_counterfactual_path),
            ("walk_forward_calibration.json", walk_forward_path),
        ):
            shutil.copy2(source, temporary_bundle / name)
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir)
        temporary_bundle.replace(bundle_dir)
        assert_active_unchanged("publish_content_addressed_bundle")

    candidate_activation_sha256 = _sha_file(
        bundle_dir / "production_probability_runtime_activation.v6_candidate.json"
    )
    already_active = (
        baseline["active_activation_sha256"] == candidate_activation_sha256
        and all(
            baseline["active_file_sha256"].get(name) == sha
            for name, sha in candidate_files.items()
        )
    )
    contract = {
        "schema": "production_probability_v6_hotfix_bundle_v1",
        "state": "already_active_verified_bundle" if already_active else "prepared_not_active",
        "event_date": event_date,
        "bundle_dir": str(bundle_dir.relative_to(root)),
        "activation_contract_sha256": activation_sha,
        "runtime_manifest_sha256": activation["runtime_manifest_sha256"],
        "candidate_activation_sha256": candidate_activation_sha256,
        "candidate_file_sha256": candidate_files,
        "baseline": baseline,
        "receipts": {
            name: {
                "ok": receipt.get("ok") is True,
                "schema": receipt.get("schema"),
                "runtime_manifest_sha256": receipt.get("runtime_manifest_sha256"),
                "activation_contract_sha256": receipt.get("activation_contract_sha256"),
            }
            for name, receipt in receipts.items()
        },
        "tests": {
            "ok": True,
            "file_count": len(TEST_FILES),
            "stdout_tail": test_receipt["stdout_tail"],
        },
        "writes_active_runtime": False,
        "writes_archive_db": False,
        "places_orders": False,
        "external_disk_required": False,
        "active_runtime_already_matches_candidate": already_active,
    }
    contract_core = dict(contract)
    contract["bundle_contract_sha256"] = _sha_bytes(
        json.dumps(contract_core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    _write_atomic(
        root / BUNDLE_POINTER.relative_to(ROOT),
        (json.dumps(contract, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
    )
    _write_atomic(
        root / "config/production_probability_runtime_activation.v6_candidate.json",
        (bundle_dir / "production_probability_runtime_activation.v6_candidate.json").read_bytes(),
    )
    assert_active_unchanged("publish_candidate_contracts")
    return contract


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    result = prepare(args.root.expanduser().resolve(), event_date=args.event_date[:10], python=args.python)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
