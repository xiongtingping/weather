#!/usr/bin/env python3
"""Build immutable Target Realization v6 release artifacts; never activate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from target_realization_v6_production_activation import activation_contract_sha256  # noqa: E402
from target_realization_v6_release_runtime import (  # noqa: E402
    ARTIFACT_ROOT,
    materialize_cutover_artifact,
    materialize_exact_exit_reason_policy,
    materialize_runtime_closure,
    materialize_strategy_artifact,
    validate_release_binding,
)


TESTS = (
    "tests/test_target_realization_v6_runtime.py",
    "tests/test_trade_exit_reason_contract_v6.py",
    "tests/test_production_dynamic_three_leg_v6_transaction_c_writer.py",
    "tests/test_production_v6_standing_exit_writer.py",
    "tests/test_dynamic_three_leg_v6_position_lot_runtime.py",
    "tests/test_dynamic_three_leg_v6_execution_materializer.py",
    "tests/test_production_target_realization_v6_input_provider.py",
    "tests/test_production_v6_standing_release_coordinator.py",
    "tests/test_production_v6_target_sell_continuation.py",
    "tests/test_resident_target_realization_v6_activation_boundary.py",
    "tests/test_target_realization_v6_release_runtime.py",
    "tests/test_target_realization_v6_unique_execution_owner.py",
    "tests/test_target_realization_v6_acceptance_static.py",
    "tests/test_clob_gateway_live_pause_boundary.py",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run(command: list[str]) -> dict[str, Any]:
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": command,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def prepare(*, approved_at: str, apply: bool) -> dict[str, Any]:
    test = _run([sys.executable, "-m", "pytest", "-q", *TESTS])
    document = _run([sys.executable, "scripts/target_realization_v6_document_contract_audit.py"])
    trade_gate = _run([sys.executable, "scripts/production_trade_gate_zero_audit.py"])
    zero_order = _run([sys.executable, "scripts/production_target_realization_v6_zero_order_drill.py"])
    receipts = {"tests": test, "document": document, "trade_gate": trade_gate, "zero_order": zero_order}
    if not all(row["ok"] for row in receipts.values()):
        return {"schema": "target_realization_v6_release_preparation_v1", "ok": False, "state": "blocked", "receipts": receipts, "places_orders": False}
    policy = materialize_exact_exit_reason_policy()
    strategy = materialize_strategy_artifact(approved_at=approved_at)
    runtime = materialize_runtime_closure(root=ROOT)
    zero_receipt = json.loads(zero_order["stdout_tail"])
    if zero_receipt.get("ok") is not True or zero_receipt.get("places_orders") is not False:
        raise ValueError("target_realization_v6_zero_order_receipt_invalid")
    test_receipt = {"schema": "target_realization_v6_test_receipt_v1", "tests": list(TESTS), "result": test}
    cutover = materialize_cutover_artifact(
        strategy=strategy, runtime=runtime,
        zero_order_receipt_sha256=_sha(zero_receipt),
        test_receipt_sha256=_sha(test_receipt), approved_at=approved_at,
    )
    active_path = ROOT / "config/trading/target_realization_v6_production_activation.json"
    try:
        active_value = json.loads(active_path.read_text(encoding="utf-8"))
        active_generation = int(active_value.get("activation_generation") or 0) if active_value.get("state") == "enabled" else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        active_generation = 0
    activation = {
        "schema": "target_realization_v6_production_activation_v1",
        "state": "enabled",
        "strategy_artifact_sha256": strategy["strategy_artifact_sha256"],
        "cutover_artifact_sha256": cutover["cutover_artifact_sha256"],
        "exact_exit_reason_policy_sha256": policy["exact_exit_reason_policy_sha256"],
        "runtime_closure_sha256": runtime["runtime_closure_sha256"],
        "live_decision_write_authorized": True,
        "live_submit_authorized": True,
        "v5_fallback_forbidden": True,
        "activated_at": approved_at,
        "activation_generation": active_generation + 1,
    }
    activation["activation_contract_sha256"] = activation_contract_sha256(activation)
    validate_release_binding(strategy=strategy, runtime=runtime, cutover=cutover, root=ROOT)
    base = ROOT / ARTIFACT_ROOT
    paths = {
        "policy": base / "exit_policy" / f"{policy['exact_exit_reason_policy_sha256']}.json",
        "strategy": base / "strategy" / f"{strategy['strategy_artifact_sha256']}.json",
        "runtime": base / "runtime" / f"{runtime['runtime_closure_sha256']}.json",
        "cutover": base / "cutover" / f"{cutover['cutover_artifact_sha256']}.json",
        "test_receipt": ROOT / "data/logs/target_realization_v6_test_receipt.json",
        "zero_order_receipt": ROOT / "data/logs/target_realization_v6_zero_order_receipt.json",
        "activation_candidate": ROOT / "data/candidates/target_realization_v6_production_activation.json",
    }
    values = {
        "policy": policy, "strategy": strategy, "runtime": runtime, "cutover": cutover,
        "test_receipt": test_receipt, "zero_order_receipt": zero_receipt,
        "activation_candidate": activation,
    }
    if apply:
        for key, path in paths.items():
            _atomic_json(path, values[key])
    return {
        "schema": "target_realization_v6_release_preparation_v1",
        "ok": True,
        "state": "candidate_files_materialized" if apply else "ready_to_materialize",
        "strategy_artifact_sha256": strategy["strategy_artifact_sha256"],
        "cutover_artifact_sha256": cutover["cutover_artifact_sha256"],
        "runtime_closure_sha256": runtime["runtime_closure_sha256"],
        "exact_exit_reason_policy_sha256": policy["exact_exit_reason_policy_sha256"],
        "activation_contract_sha256": activation["activation_contract_sha256"],
        "paths": {key: str(path) for key, path in paths.items()},
        "receipts": receipts,
        "writes_archive_db": False,
        "changes_activation": False,
        "external_disk_dependency": False,
        "places_orders": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-at", default=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = prepare(approved_at=args.approved_at, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
