#!/usr/bin/env python3
"""Prepare immutable production dynamic-three-leg artifacts without activating them."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dynamic_three_leg_production_activation import materialize_dynamic_three_leg_activation  # noqa: E402
from dynamic_three_leg_release_runtime import (  # noqa: E402
    CUTOVER_ARTIFACT_DIR,
    CUTOVER_SCHEMA,
    STRATEGY_ARTIFACT_DIR,
    compile_dynamic_three_leg_strategy_artifact,
    materialize_dynamic_three_leg_cutover_artifact,
    validate_dynamic_three_leg_cutover_artifact,
    validate_dynamic_three_leg_strategy_artifact,
)
from market_alpha_take_profit_activation import materialize_market_alpha_take_profit_activation  # noqa: E402
from scripts.production_dynamic_three_leg_strategy_contract_audit import run_audit as strategy_audit  # noqa: E402
from scripts.production_trade_gate_zero_audit import run_audit as trade_gate_audit  # noqa: E402
from scripts.signal_to_order_safety_contract_audit import build_audit as safety_audit  # noqa: E402


TESTS = (
    "tests/test_dynamic_three_leg_transition_runtime.py",
    "tests/test_strong_posterior_three_leg_strategy.py",
    "tests/test_production_dynamic_three_leg_decision_writer.py",
    "tests/test_production_market_alpha_take_profit_coordinator.py",
    "tests/test_production_market_alpha_take_profit_writer.py",
    "tests/test_production_dynamic_three_leg_strategy_contract_audit.py",
    "tests/test_signal_to_order_safety_contract_audit.py",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"release_input_not_mapping:{path}")
    return dict(value)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(*, approved_at: str, apply: bool) -> dict[str, Any]:
    strategy_receipt = strategy_audit()
    trade_receipt = trade_gate_audit()
    safety_receipt = safety_audit(root=ROOT)
    test = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TESTS],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    test_receipt = {
        "ok": test.returncode == 0,
        "returncode": test.returncode,
        "test_files": list(TESTS),
        "stdout_tail": test.stdout[-1000:],
        "stderr_tail": test.stderr[-1000:],
        "places_orders": False,
    }
    if not all(receipt.get("ok") is True for receipt in (strategy_receipt, trade_receipt, safety_receipt, test_receipt)):
        return {
            "schema": "dynamic_three_leg_production_release_preparation_v1",
            "ok": False,
            "state": "blocked",
            "receipts": {
                "strategy_contract_audit": strategy_receipt,
                "trade_gate_zero_audit": trade_receipt,
                "signal_to_order_safety_audit": safety_receipt,
                "test_receipt": test_receipt,
            },
            "places_orders": False,
        }
    probability = _load(ROOT / "config/production_probability_runtime_activation.v6_candidate.json")
    bridge = _load(ROOT / "data/candidates/resolution_bridge_production_activation.json")
    context = _load(ROOT / "data/candidates/production_probability_v6_market_context_activation.json")
    strategy = compile_dynamic_three_leg_strategy_artifact(root=ROOT, approved_at=approved_at)
    validate_dynamic_three_leg_strategy_artifact(strategy, root=ROOT)
    cutover = materialize_dynamic_three_leg_cutover_artifact({
        "schema": CUTOVER_SCHEMA,
        "state": "accepted_for_production_activation",
        "approved_at": approved_at,
        "strategy_artifact_sha256": strategy["artifact_sha256"],
        "required_probability_activation_contract_sha256": _text(probability.get("activation_contract_sha256")),
        "required_bridge_activation_sha256": _text(bridge.get("activation_sha256")),
        "required_market_context_activation_sha256": _text(context.get("activation_sha256")),
        "strategy_contract_audit": strategy_receipt,
        "trade_gate_zero_audit": trade_receipt,
        "signal_to_order_safety_audit": safety_receipt,
        "test_receipt": test_receipt,
        "cold_start_t1_to_t5_policy": {
            "historical_samples_block_initial_live": False,
            "hard_deadline_ms": 3000,
            "late_post_forbidden": True,
        },
        "external_disk_required": False,
        "places_orders": False,
    })
    validate_dynamic_three_leg_cutover_artifact(
        cutover,
        strategy_artifact_sha256=strategy["artifact_sha256"],
        root=ROOT,
        require_active_bindings=False,
    ) if (ROOT / STRATEGY_ARTIFACT_DIR / f"{strategy['artifact_sha256']}.json").exists() else None
    dynamic_activation = materialize_dynamic_three_leg_activation({
        "schema": "dynamic_three_leg_production_activation_v1",
        "state": "enabled",
        "strategy_artifact_sha256": strategy["artifact_sha256"],
        "cutover_artifact_sha256": cutover["cutover_artifact_sha256"],
        "risk_reservation_write_authorized": True,
        "live_decision_write_authorized": True,
        "live_submit_authorized": True,
    })
    market_alpha_activation = materialize_market_alpha_take_profit_activation({
        "schema": "market_alpha_take_profit_production_activation_v1",
        "state": "enabled",
        "strategy_artifact_sha256": strategy["artifact_sha256"],
        "cutover_artifact_sha256": cutover["cutover_artifact_sha256"],
        "live_decision_write_authorized": True,
        "live_submit_authorized": True,
        "reason_family": "market_alpha_take_profit",
        "reason_code": "market_alpha_take_profit_top_yes_wave_completed",
        "frozen_take_profit_price": 0.90,
    })
    paths = {
        "strategy_artifact": ROOT / STRATEGY_ARTIFACT_DIR / f"{strategy['artifact_sha256']}.json",
        "cutover_artifact": ROOT / CUTOVER_ARTIFACT_DIR / f"{cutover['cutover_artifact_sha256']}.json",
        "dynamic_activation_candidate": ROOT / "data/candidates/dynamic_three_leg_production_activation.json",
        "market_alpha_activation_candidate": ROOT / "data/candidates/market_alpha_take_profit_production_activation.json",
    }
    if apply:
        _atomic_json(paths["strategy_artifact"], strategy)
        _atomic_json(paths["cutover_artifact"], cutover)
        _atomic_json(paths["dynamic_activation_candidate"], dynamic_activation)
        _atomic_json(paths["market_alpha_activation_candidate"], market_alpha_activation)
        validate_dynamic_three_leg_cutover_artifact(
            cutover,
            strategy_artifact_sha256=strategy["artifact_sha256"],
            root=ROOT,
            require_active_bindings=False,
        )
    return {
        "schema": "dynamic_three_leg_production_release_preparation_v1",
        "ok": True,
        "state": "candidate_files_materialized" if apply else "ready_to_materialize",
        "strategy_artifact_sha256": strategy["artifact_sha256"],
        "cutover_artifact_sha256": cutover["cutover_artifact_sha256"],
        "dynamic_activation_contract_sha256": dynamic_activation["activation_contract_sha256"],
        "market_alpha_activation_contract_sha256": market_alpha_activation["activation_contract_sha256"],
        "required_probability_activation_contract_sha256": cutover["required_probability_activation_contract_sha256"],
        "required_bridge_activation_sha256": cutover["required_bridge_activation_sha256"],
        "required_market_context_activation_sha256": cutover["required_market_context_activation_sha256"],
        "paths": {key: str(value) for key, value in paths.items()},
        "t1_to_t5_historical_sample_live_gate": False,
        "external_disk_required": False,
        "writes_archive_db": False,
        "places_orders": False,
        "test_receipt": test_receipt,
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
