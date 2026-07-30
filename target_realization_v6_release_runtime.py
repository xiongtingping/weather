"""Content-addressed release authority for Target Realization v6."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from dynamic_three_leg_v6_capital_allocator import CAPITAL_POLICY_ID
from target_realization_v6_runtime import (
    LEG_CAPS,
    RISK_BANDS,
    TRAJECTORY_NO_BANDS,
    TRAJECTORY_NO_DOWNSHIFT_HYSTERESIS,
)
from trade_exit_reason_contract import (
    V6_MARKET_ALPHA_TAKE_PROFIT_REASON_CODES,
    V6_TARGET_REALIZATION_EXIT_REASON_CODES,
)


ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "target_realization_v6_release_v1"
ARTIFACT_ROOT = Path("config/artifacts/target_realization_v6")
AUTHORITY_FILES = (
    "AGENTS.md",
    "docs/adr/ADR-0006-DYNAMIC-THREE-LEG-V6-SUCCESSOR.md",
    "docs/contracts/TARGET_REALIZATION_V6.md",
)
RUNTIME_FILES = (
    "target_realization_v6_release_runtime.py",
    "target_realization_v6_runtime.py",
    "dynamic_three_leg_v6_capital_allocator.py",
    "dynamic_three_leg_v6_position_cycle_runtime.py",
    "standing_exit_999_runtime.py",
    "dynamic_three_leg_v6_schema_contract.py",
    "dynamic_three_leg_v6_execution_materializer.py",
    "dynamic_three_leg_v6_position_lot_runtime.py",
    "production_dynamic_three_leg_v6_transaction_c_writer.py",
    "production_target_realization_v6_input_provider.py",
    "production_target_realization_v6_outbox_worker.py",
    "production_posterior_commit_outbox_worker.py",
    "production_v6_standing_exit_writer.py",
    "production_v6_standing_release_coordinator.py",
    "production_v6_target_sell_continuation.py",
    "resident_target_realization_v6_intent_submitter.py",
    "resident_dynamic_three_leg_execution_loop.py",
    "target_realization_v6_production_activation.py",
    "trade_exit_reason_contract.py",
    "dynamic_three_leg_clob_gateway_adapter.py",
    "scripts/clob_order_service.mjs",
    "scripts/clob_order_service.sh",
    "launchd/com.weather.dynamic_three_leg_execution.plist",
    "scripts/weatherctl.sh",
    "scripts/activate_target_realization_v6_schema.py",
    "scripts/production_target_realization_v6_readiness.py",
    "scripts/production_target_realization_v6_acceptance.py",
    "scripts/activate_target_realization_v6_release.py",
    "scripts/production_target_realization_v6_zero_order_drill.py",
    "scripts/prepare_target_realization_v6_release.py",
    "scripts/cutover_target_realization_v6_production.sh",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def materialize_exact_exit_reason_policy() -> dict[str, Any]:
    core = {
        "schema": "target_realization_v6_exact_exit_reason_policy_v1",
        "target_realization": sorted(V6_TARGET_REALIZATION_EXIT_REASON_CODES),
        "market_alpha_take_profit": sorted(V6_MARKET_ALPHA_TAKE_PROFIT_REASON_CODES),
        "prefix_authority": False,
        "legacy_v5_codes_authorized": False,
        "ev_edge_lcb_authorized": False,
    }
    return {**core, "exact_exit_reason_policy_sha256": _sha(core)}


def materialize_strategy_artifact(*, approved_at: str) -> dict[str, Any]:
    policy = materialize_exact_exit_reason_policy()
    authority_documents = {
        relative: (ROOT / relative).read_text(encoding="utf-8")
        for relative in AUTHORITY_FILES
    }
    core = {
        "schema": "target_realization_v6_strategy_artifact_v1",
        "state": "accepted_for_production_activation",
        "approved_at": str(approved_at),
        "authority_files": {
            relative: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for relative, content in authority_documents.items()
        },
        "authority_documents": authority_documents,
        "capital_policy_id": CAPITAL_POLICY_ID,
        "leg_caps_usdc": dict(LEG_CAPS),
        "risk_bands": [list(row) for row in RISK_BANDS],
        "trajectory_no_bands": [list(row) for row in TRAJECTORY_NO_BANDS],
        "trajectory_no_downshift_hysteresis": TRAJECTORY_NO_DOWNSHIFT_HYSTERESIS,
        "standing_exit_limit_price": 0.999,
        "exact_exit_reason_policy_sha256": policy["exact_exit_reason_policy_sha256"],
        "single_no_slot": True,
        "single_capital_scale_factor": True,
        "v5_fallback_forbidden": True,
        "external_disk_dependency": False,
    }
    return {**core, "strategy_artifact_sha256": _sha(core)}


def materialize_runtime_closure(*, root: Path = ROOT) -> dict[str, Any]:
    files = {relative: _file_sha(root / relative) for relative in RUNTIME_FILES}
    core = {
        "schema": "target_realization_v6_runtime_closure_v1",
        "files": files,
        "external_disk_dependency": False,
    }
    return {**core, "runtime_closure_sha256": _sha(core)}


def materialize_cutover_artifact(
    *, strategy: Mapping[str, Any], runtime: Mapping[str, Any], zero_order_receipt_sha256: str,
    test_receipt_sha256: str, approved_at: str,
) -> dict[str, Any]:
    core = {
        "schema": "target_realization_v6_cutover_artifact_v1",
        "state": "accepted_for_managed_atomic_cutover",
        "approved_at": str(approved_at),
        "strategy_artifact_sha256": str(strategy.get("strategy_artifact_sha256") or ""),
        "exact_exit_reason_policy_sha256": str(strategy.get("exact_exit_reason_policy_sha256") or ""),
        "runtime_closure_sha256": str(runtime.get("runtime_closure_sha256") or ""),
        "zero_order_receipt_sha256": str(zero_order_receipt_sha256),
        "test_receipt_sha256": str(test_receipt_sha256),
        "linearization_point": "activation_and_plist_installed_then_live_pause_removed",
        "v5_fallback_forbidden": True,
        "external_disk_dependency": False,
        "places_orders": False,
    }
    return {**core, "cutover_artifact_sha256": _sha(core)}


def validate_release_binding(
    *, strategy: Mapping[str, Any], runtime: Mapping[str, Any], cutover: Mapping[str, Any],
    root: Path = ROOT,
) -> None:
    strategy_body = {
        key: value for key, value in dict(strategy).items()
        if key != "strategy_artifact_sha256"
    }
    if strategy.get("strategy_artifact_sha256") != _sha(strategy_body):
        raise ValueError("target_realization_v6_strategy_artifact_drift")
    documents = strategy.get("authority_documents")
    authority_files = strategy.get("authority_files")
    if not isinstance(documents, Mapping) or set(documents) != set(AUTHORITY_FILES):
        raise ValueError("target_realization_v6_strategy_authority_snapshot_invalid")
    expected_authority_files = {
        relative: hashlib.sha256(str(documents[relative]).encode("utf-8")).hexdigest()
        for relative in AUTHORITY_FILES
    }
    if authority_files != expected_authority_files:
        raise ValueError("target_realization_v6_strategy_authority_hash_mismatch")
    expected_policy = materialize_exact_exit_reason_policy()
    semantic_checks = {
        "schema": "target_realization_v6_strategy_artifact_v1",
        "state": "accepted_for_production_activation",
        "capital_policy_id": CAPITAL_POLICY_ID,
        "leg_caps_usdc": dict(LEG_CAPS),
        "risk_bands": [list(row) for row in RISK_BANDS],
        "trajectory_no_bands": [list(row) for row in TRAJECTORY_NO_BANDS],
        "trajectory_no_downshift_hysteresis": TRAJECTORY_NO_DOWNSHIFT_HYSTERESIS,
        "standing_exit_limit_price": 0.999,
        "exact_exit_reason_policy_sha256": expected_policy["exact_exit_reason_policy_sha256"],
        "single_no_slot": True,
        "single_capital_scale_factor": True,
        "v5_fallback_forbidden": True,
        "external_disk_dependency": False,
    }
    for key, expected in semantic_checks.items():
        if strategy.get(key) != expected:
            raise ValueError(f"target_realization_v6_strategy_semantic_drift:{key}")
    expected_runtime = materialize_runtime_closure(root=root)
    if dict(runtime) != expected_runtime:
        raise ValueError("target_realization_v6_runtime_closure_drift")
    supplied = str(cutover.get("cutover_artifact_sha256") or "")
    body = {key: value for key, value in dict(cutover).items() if key != "cutover_artifact_sha256"}
    if supplied != _sha(body):
        raise ValueError("target_realization_v6_cutover_artifact_drift")
    if cutover.get("strategy_artifact_sha256") != strategy.get("strategy_artifact_sha256"):
        raise ValueError("target_realization_v6_cutover_strategy_mismatch")
    if cutover.get("runtime_closure_sha256") != runtime.get("runtime_closure_sha256"):
        raise ValueError("target_realization_v6_cutover_runtime_mismatch")


__all__ = [
    "ARTIFACT_ROOT", "AUTHORITY_FILES", "ROOT", "RUNTIME_FILES", "SCHEMA_VERSION",
    "materialize_cutover_artifact", "materialize_exact_exit_reason_policy",
    "materialize_runtime_closure", "materialize_strategy_artifact", "validate_release_binding",
]
