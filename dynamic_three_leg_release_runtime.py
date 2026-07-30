"""Content-addressed production release authority for the dynamic three-leg lane."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
STRATEGY_SCHEMA = "dynamic_three_leg_strategy_artifact_v1"
CUTOVER_SCHEMA = "dynamic_three_leg_cutover_artifact_v1"
STRATEGY_ARTIFACT_DIR = Path("config/artifacts/dynamic_three_leg_strategy")
CUTOVER_ARTIFACT_DIR = Path("config/artifacts/dynamic_three_leg_cutover")
RUNTIME_ROOTS = (
    "strong_posterior_three_leg_strategy.py",
    "production_dynamic_three_leg_input_provider.py",
    "production_posterior_commit_outbox_worker.py",
    "resident_dynamic_three_leg_execution_loop.py",
    "resident_dynamic_three_leg_intent_submitter.py",
    "production_market_alpha_take_profit_coordinator.py",
    "production_market_alpha_take_profit_writer.py",
    "trade_exit_reason_contract.py",
    "scripts/resident_dynamic_three_leg_execution.sh",
    "launchd/com.weather.dynamic_three_leg_execution.plist",
    "scripts/clob_order_service.mjs",
    "scripts/clob_order_service.sh",
    "launchd/com.weather.clob_order_service.plist",
)
EVIDENCE_RUNTIME_FILES = (
    "scripts/market_ws_listener.py",
    "scripts/market_ws_listener.sh",
    "launchd/com.weather.market_ws.plist",
    "scan_weather_market.py",
    "package-lock.json",
)
POLICY_AUTHORITY_FILES = (
    "AGENTS.md",
    "docs/adr/ADR-0005-PRODUCTION-DYNAMIC-THREE-LEG-V5.md",
    "docs/contracts/TARGET_REALIZATION_V5.md",
    "docs/constitution/SYSTEM_CHARTER.md",
    "docs/WEATHER_DECISION_CORE.md",
    "docs/architecture/CURRENT_ARCHITECTURE.md",
)


def _local_module_index(root: Path) -> dict[str, Path]:
    index: dict[str, Path] = {}
    excluded = {".git", ".venv", "data", "output", "tests", "__pycache__"}
    for directory, directories, filenames in os.walk(root, topdown=True):
        directories[:] = [name for name in directories if name not in excluded]
        base = Path(directory)
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = base / filename
            relative = path.relative_to(root)
            module = (
                ".".join(relative.parent.parts)
                if path.name == "__init__.py"
                else ".".join(relative.with_suffix("").parts)
            )
            if module:
                index[module] = relative
    return index


def _module_name(relative: Path) -> str:
    return (
        ".".join(relative.parent.parts)
        if relative.name == "__init__.py"
        else ".".join(relative.with_suffix("").parts)
    )


def _reachable_runtime_files(root: Path) -> tuple[str, ...]:
    index = _local_module_index(root)
    seen: set[Path] = set()
    queue = [Path(value) for value in RUNTIME_ROOTS]
    while queue:
        relative = queue.pop()
        if relative in seen:
            continue
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(f"dynamic_three_leg_runtime_file_missing:{relative}")
        seen.add(relative)
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise ValueError(
                f"dynamic_three_leg_runtime_parse_failed:{relative}:{type(exc).__name__}"
            ) from exc
        current_module = _module_name(relative)
        current_package = current_module if relative.name == "__init__.py" else current_module.rpartition(".")[0]
        for node in ast.walk(tree):
            candidates: list[str] = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = current_package.split(".") if current_package else []
                    prefix = ".".join(parts[: max(0, len(parts) - node.level + 1)])
                    base = ".".join(value for value in (prefix, base) if value)
                if base:
                    candidates.append(base)
                candidates.extend(
                    ".".join(value for value in (base, alias.name) if value)
                    for alias in node.names
                    if alias.name != "*"
                )
            for name in candidates:
                candidate = index.get(name)
                if candidate is not None and candidate not in seen:
                    queue.append(candidate)
                parts = name.split(".")
                for end in range(1, len(parts)):
                    package = index.get(".".join(parts[:end]))
                    if package is not None and package not in seen:
                        queue.append(package)
    return tuple(sorted(str(path) for path in seen))


def _runtime_files(root: Path) -> tuple[str, ...]:
    files = set(_reachable_runtime_files(root))
    for value in EVIDENCE_RUNTIME_FILES:
        if not (root / value).is_file():
            raise FileNotFoundError(f"dynamic_three_leg_evidence_runtime_file_missing:{value}")
        files.add(value)
    return tuple(sorted(files))


RUNTIME_FILES = _runtime_files(ROOT)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha_value(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _materialize(core: Mapping[str, Any], hash_field: str) -> dict[str, Any]:
    value = dict(core)
    value.pop(hash_field, None)
    return {**value, hash_field: _sha_value(value)}


def compile_dynamic_three_leg_strategy_artifact(
    *, root: Path = ROOT, approved_at: str,
) -> dict[str, Any]:
    # The production resident validates activation on every fast loop.  The
    # local dependency graph is immutable for that process and was resolved at
    # module import, so never rescan the whole repository in the hot path.
    # Alternate roots used by isolated tests/releases still resolve themselves.
    runtime_files = (
        RUNTIME_FILES
        if root.resolve() == ROOT.resolve()
        else _runtime_files(root)
    )
    runtime_sha = {name: _sha_file(root / name) for name in runtime_files}
    policy_authority_sha = {}
    for name in POLICY_AUTHORITY_FILES:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"dynamic_three_leg_policy_authority_file_missing:{name}")
        policy_authority_sha[name] = _sha_file(path)
    core = {
        "schema": STRATEGY_SCHEMA,
        "state": "accepted_for_production",
        "approved_at": _text(approved_at),
        "posterior_authority": "unique_v6_market_bucket_posterior_ssot",
        "role_selection": "dynamic_on_every_committed_posterior",
        "legs": {
            "top_yes_wave": {
                "rank": 1,
                "side": "YES",
                "max_notional_usdc": 20.0,
                "settlement_behavior": "wave_only_not_held_to_settlement",
                "take_profit_price": 0.90,
            },
            "second_yes": {
                "rank": 2,
                "side": "YES",
                "max_notional_usdc": 4.99,
                "entry_window": "strictly_within_final_30_minutes_before_predicted_peak",
                "settlement_behavior": "small_lottery_held_to_settlement",
            },
            "strongly_excluded_no": {
                "side": "NO",
                "max_notional_usdc": 100.0,
                "selection": "joint_physical_hard_exclusion_certificate_only",
            },
        },
        "forbidden_runtime_gates": ["EV", "Edge", "LCB", "fair_value", "buy_price_cap"],
        "forbidden_legacy_no": ["remote_no", "trajectory_no_current_report", "current_report_temperature_no"],
        "t1_to_t5": {
            "hard_deadline_ms": 3000,
            "historical_samples_block_initial_live": False,
            "late_post_forbidden": True,
        },
        "runtime_sha256": runtime_sha,
        "runtime_roots": list(RUNTIME_ROOTS),
        "evidence_runtime_files": list(EVIDENCE_RUNTIME_FILES),
        "reachable_local_runtime_file_count": len(runtime_files),
        "dependency_closure_complete": True,
        "policy_authority": "adr_0005_target_realization_v5",
        "policy_authority_sha256": policy_authority_sha,
    }
    return _materialize(core, "artifact_sha256")


def validate_dynamic_three_leg_strategy_artifact(
    artifact: Mapping[str, Any], *, root: Path = ROOT,
) -> dict[str, Any]:
    row = dict(artifact)
    supplied = _text(row.pop("artifact_sha256", ""))
    issues: list[str] = []
    if row.get("schema") != STRATEGY_SCHEMA or row.get("state") != "accepted_for_production":
        issues.append("dynamic_three_leg_strategy_artifact_state_invalid")
    try:
        approved_at = datetime.fromisoformat(_text(row.get("approved_at")).replace("Z", "+00:00"))
    except ValueError:
        approved_at = None
    if approved_at is None or approved_at.tzinfo is None:
        issues.append("dynamic_three_leg_strategy_artifact_approved_at_invalid")
    if not supplied or supplied != _sha_value(row):
        issues.append("dynamic_three_leg_strategy_artifact_sha256_invalid")
    expected = compile_dynamic_three_leg_strategy_artifact(
        root=root, approved_at=_text(row.get("approved_at")),
    )
    for key in (
        "posterior_authority", "role_selection", "legs", "forbidden_runtime_gates",
        "forbidden_legacy_no", "t1_to_t5", "runtime_sha256", "runtime_roots",
        "evidence_runtime_files",
        "reachable_local_runtime_file_count", "dependency_closure_complete",
        "policy_authority", "policy_authority_sha256",
    ):
        if row.get(key) != expected.get(key):
            issues.append(f"dynamic_three_leg_strategy_artifact_{key}_invalid")
    if issues:
        raise ValueError(",".join(sorted(set(issues))))
    return {**row, "artifact_sha256": supplied}


def load_dynamic_three_leg_strategy_artifact(
    artifact_sha256: str, *, root: Path = ROOT,
) -> dict[str, Any]:
    sha = _text(artifact_sha256)
    if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ValueError("dynamic_three_leg_strategy_artifact_identity_invalid")
    path = root / STRATEGY_ARTIFACT_DIR / f"{sha}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = validate_dynamic_three_leg_strategy_artifact(raw, root=root)
    if artifact.get("artifact_sha256") != sha:
        raise ValueError("dynamic_three_leg_strategy_artifact_path_mismatch")
    return artifact


def materialize_dynamic_three_leg_cutover_artifact(core: Mapping[str, Any]) -> dict[str, Any]:
    return _materialize(core, "cutover_artifact_sha256")


def validate_dynamic_three_leg_cutover_artifact(
    artifact: Mapping[str, Any], *, strategy_artifact_sha256: str, root: Path = ROOT,
    require_active_bindings: bool = True,
) -> dict[str, Any]:
    row = dict(artifact)
    supplied = _text(row.pop("cutover_artifact_sha256", ""))
    issues: list[str] = []
    if row.get("schema") != CUTOVER_SCHEMA or row.get("state") != "accepted_for_production_activation":
        issues.append("dynamic_three_leg_cutover_artifact_state_invalid")
    try:
        approved_at = datetime.fromisoformat(_text(row.get("approved_at")).replace("Z", "+00:00"))
    except ValueError:
        approved_at = None
    if approved_at is None or approved_at.tzinfo is None:
        issues.append("dynamic_three_leg_cutover_artifact_approved_at_invalid")
    if not supplied or supplied != _sha_value(row):
        issues.append("dynamic_three_leg_cutover_artifact_sha256_invalid")
    if _text(row.get("strategy_artifact_sha256")) != _text(strategy_artifact_sha256):
        issues.append("dynamic_three_leg_cutover_strategy_artifact_mismatch")
    for key in ("strategy_contract_audit", "trade_gate_zero_audit", "signal_to_order_safety_audit", "test_receipt"):
        value = row.get(key)
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            issues.append(f"dynamic_three_leg_cutover_{key}_invalid")
    load_dynamic_three_leg_strategy_artifact(strategy_artifact_sha256, root=root)
    if require_active_bindings:
        bindings = (
            (root / "config/production_probability_runtime_activation.json", "activation_contract_sha256", "required_probability_activation_contract_sha256"),
            (root / "config/resolution_bridge_production_activation.json", "activation_sha256", "required_bridge_activation_sha256"),
            (root / "config/trading/production_probability_v6_market_context_activation.json", "activation_sha256", "required_market_context_activation_sha256"),
        )
        for path, active_key, required_key in bindings:
            try:
                active = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                active = {}
            if not _text(row.get(required_key)) or _text(active.get(active_key)) != _text(row.get(required_key)):
                issues.append(f"dynamic_three_leg_cutover_{required_key}_not_active")
    if issues:
        raise ValueError(",".join(sorted(set(issues))))
    return {**row, "cutover_artifact_sha256": supplied}


def load_dynamic_three_leg_cutover_artifact(
    cutover_artifact_sha256: str, *, strategy_artifact_sha256: str,
    root: Path = ROOT, require_active_bindings: bool = True,
) -> dict[str, Any]:
    sha = _text(cutover_artifact_sha256)
    if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ValueError("dynamic_three_leg_cutover_artifact_identity_invalid")
    path = root / CUTOVER_ARTIFACT_DIR / f"{sha}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    artifact = validate_dynamic_three_leg_cutover_artifact(
        raw,
        strategy_artifact_sha256=strategy_artifact_sha256,
        root=root,
        require_active_bindings=require_active_bindings,
    )
    if artifact.get("cutover_artifact_sha256") != sha:
        raise ValueError("dynamic_three_leg_cutover_artifact_path_mismatch")
    return artifact


def validate_dynamic_three_leg_release_binding(
    *, strategy_artifact_sha256: str, cutover_artifact_sha256: str,
    root: Path = ROOT, require_active_bindings: bool = True,
) -> dict[str, Any]:
    strategy = load_dynamic_three_leg_strategy_artifact(strategy_artifact_sha256, root=root)
    cutover = load_dynamic_three_leg_cutover_artifact(
        cutover_artifact_sha256,
        strategy_artifact_sha256=strategy_artifact_sha256,
        root=root,
        require_active_bindings=require_active_bindings,
    )
    return {"strategy_artifact": strategy, "cutover_artifact": cutover}


__all__ = [
    "CUTOVER_ARTIFACT_DIR", "CUTOVER_SCHEMA", "STRATEGY_ARTIFACT_DIR", "STRATEGY_SCHEMA",
    "EVIDENCE_RUNTIME_FILES", "RUNTIME_FILES", "RUNTIME_ROOTS",
    "compile_dynamic_three_leg_strategy_artifact", "load_dynamic_three_leg_cutover_artifact",
    "load_dynamic_three_leg_strategy_artifact", "materialize_dynamic_three_leg_cutover_artifact",
    "validate_dynamic_three_leg_cutover_artifact", "validate_dynamic_three_leg_release_binding",
    "validate_dynamic_three_leg_strategy_artifact",
]
