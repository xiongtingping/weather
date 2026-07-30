"""Content-bound production activation contract for Target Realization v6."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "target_realization_v6_production_activation_v1"
DEFAULT_PATH = Path(__file__).resolve().parent / "config/trading/target_realization_v6_production_activation.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def activation_contract_sha256(value: Mapping[str, Any]) -> str:
    body = {key: item for key, item in dict(value).items() if key != "activation_contract_sha256"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def validate_target_realization_v6_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    blockers: list[str] = []
    if result.get("schema") != SCHEMA_VERSION:
        blockers.append("target_realization_v6_activation_schema_invalid")
    if result.get("state") not in {"disabled", "enabled"}:
        blockers.append("target_realization_v6_activation_state_invalid")
    for field in (
        "strategy_artifact_sha256", "cutover_artifact_sha256",
        "exact_exit_reason_policy_sha256", "runtime_closure_sha256",
    ):
        if len(_text(result.get(field))) != 64:
            blockers.append(f"target_realization_v6_activation_{field}_invalid")
    expected = activation_contract_sha256(result)
    if _text(result.get("activation_contract_sha256")) != expected:
        blockers.append("target_realization_v6_activation_contract_sha256_mismatch")
    enabled = result.get("state") == "enabled"
    if enabled and (
        result.get("live_decision_write_authorized") is not True
        or result.get("live_submit_authorized") is not True
        or result.get("v5_fallback_forbidden") is not True
    ):
        blockers.append("target_realization_v6_activation_authority_incomplete")
    if blockers:
        raise ValueError(";".join(sorted(set(blockers))))
    return result


def load_target_realization_v6_activation(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(parsed, Mapping):
        raise ValueError("target_realization_v6_activation_not_object")
    activation = validate_target_realization_v6_activation(parsed)
    if activation.get("state") == "enabled":
        from target_realization_v6_release_runtime import (
            ARTIFACT_ROOT, ROOT, materialize_exact_exit_reason_policy, validate_release_binding,
        )

        base = ROOT / ARTIFACT_ROOT
        strategy = json.loads((base / "strategy" / f"{activation['strategy_artifact_sha256']}.json").read_text(encoding="utf-8"))
        runtime = json.loads((base / "runtime" / f"{activation['runtime_closure_sha256']}.json").read_text(encoding="utf-8"))
        cutover = json.loads((base / "cutover" / f"{activation['cutover_artifact_sha256']}.json").read_text(encoding="utf-8"))
        policy = json.loads((base / "exit_policy" / f"{activation['exact_exit_reason_policy_sha256']}.json").read_text(encoding="utf-8"))
        validate_release_binding(strategy=strategy, runtime=runtime, cutover=cutover, root=ROOT)
        if policy != materialize_exact_exit_reason_policy() or policy.get("exact_exit_reason_policy_sha256") != activation.get("exact_exit_reason_policy_sha256"):
            raise ValueError("target_realization_v6_activation_exit_policy_binding_mismatch")
    return activation


__all__ = [
    "DEFAULT_PATH", "SCHEMA_VERSION", "activation_contract_sha256",
    "load_target_realization_v6_activation", "validate_target_realization_v6_activation",
]
