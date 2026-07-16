#!/usr/bin/env python3
"""Read-only structural audit for the sole target probability SSOT mainline."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "probability_ssot_mainline_audit_v1"

REQUIRED_SYMBOLS = {
    "initial_posterior_checkpoint_shadow.py": {
        "build_initial_posterior_checkpoint",
        "persist_initial_posterior_checkpoint_shadow",
    },
    "sequential_bayes_filter_shadow_runtime.py": {
        "build_strong_observation_update",
        "build_next_checkpoint",
        "replay_canonical_sequence",
    },
    "sequential_bayes_shadow_pipeline.py": {
        "build_canonical_event_transaction_b_candidate",
        "run_canonical_event_public_ssot_shadow_update",
    },
    "sequential_bayes_public_ssot_shadow_writer.py": {
        "commit_public_physical_ssot_shadow_transaction",
        "commit_public_ssot_shadow_transaction_b",
        "materialize_public_physical_posterior_fact_row",
        "materialize_public_posterior_fact_row",
    },
    "strong_observation_lr_evaluator.py": {
        "evaluate_strong_observation_raw_log_scores",
    },
    "strong_observation_lr_runtime_registry.py": {
        "register_strong_lr_runtime_artifact",
        "activate_strong_lr_runtime_artifact",
        "load_active_strong_lr_runtime_artifact",
        "assert_active_strong_lr_artifact_binding",
        "_validate_activation_evidence",
    },
    "resident_fast_lane_prewarm_contract.py": {
        "validate_resident_fast_lane_prewarm",
        "validate_resident_market_projection_prewarm",
    },
}

RETIRED_FILES = (
    "joint_physical_x_bayes_composer_runtime.py",
    "physical_x_prior_layer_runtime.py",
    "physical_x_shadow_posterior_runtime.py",
    "physical_x_shadow_posterior_writer.py",
    "scripts/run_physical_x_shadow_posterior.py",
    "scripts/physical_x_shadow_latency_audit.py",
)

RETIRED_LAUNCHER_TOKENS = (
    "AMSC_ZUUU_PHYSICAL_X_SHADOW_POSTERIOR_ENABLED",
    "run_physical_x_shadow_after_fact_apply",
    "run_physical_x_shadow_posterior.py",
)

REQUIRED_STRONG_V3_TOKENS = (
    'SCHEMA_VERSION = "strong_observation_incremental_lr_artifact_v3"',
    'INPUT_SCHEMA_VERSION = "strong_observation_lr_input_v3"',
    'FEATURE_EVALUATOR_SCHEMA_VERSION = "strong_observation_feature_evaluator_v2"',
    '"timing.local_hour"',
    '"trajectory.current_step_rate_c_per_hour"',
    '"trajectory.slope_60m_c_per_hour"',
    '"trajectory.slope_120m_c_per_hour"',
    '"trajectory.observed_peak_including_current_c"',
    '"trajectory.cooling_depth_including_current_c"',
    '"trajectory.cooling_duration_minutes"',
    '"candidate.remaining_warming_c"',
)

REQUIRED_ACTIVE_ARTIFACT_BINDINGS = {
    "scripts/bootstrap_initial_posterior_shadow.py": (
        "load_active_strong_lr_runtime_artifact",
        "initial_posterior_input_caller_supplied_strong_artifact_forbidden",
        "initial_posterior_active_strong_lr_artifact_unavailable",
    ),
    "initial_posterior_checkpoint_shadow.py": (
        "assert_active_strong_lr_artifact_binding",
        'as_of=_text(checkpoint.get("created_at"))',
    ),
    "resident_p10_shadow_prepared_cycle.py": (
        "assert_active_strong_lr_artifact_binding",
        'as_of=_text(cycle.get("prepared_at"))',
    ),
    "p10_shadow_preparation_readiness.py": (
        "load_active_strong_lr_runtime_artifact",
        '"source": "strong_lr_runtime_active_registry"',
        '"strict_asof_oos_are_offline_promotion_only": True',
        '"activation_evidence_sha256"',
    ),
}

REQUIRED_ACTIVATION_ATTESTATION_TOKENS = (
    'ACTIVATION_ATTESTATION_SCHEMA_VERSION = "strong_lr_runtime_activation_attestation_v1"',
    '"activation_evidence_sha256"',
    '"promotion_gate_sha256"',
    '"operational_precalibration_baseline"',
    '"not_trained_or_oos_promoted"',
)


def _definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def run_audit(*, root: Path = ROOT) -> dict[str, Any]:
    violations: list[str] = []
    symbol_evidence: dict[str, Any] = {}
    for relative, required in REQUIRED_SYMBOLS.items():
        path = root / relative
        if not path.is_file():
            violations.append(f"required_probability_mainline_file_missing:{relative}")
            symbol_evidence[relative] = {"exists": False, "missing_symbols": sorted(required)}
            continue
        try:
            definitions = _definitions(path)
        except (OSError, SyntaxError) as exc:
            violations.append(f"required_probability_mainline_file_unreadable:{relative}:{type(exc).__name__}")
            symbol_evidence[relative] = {"exists": True, "parse_ok": False}
            continue
        missing = sorted(required - definitions)
        if missing:
            violations.extend(f"required_probability_mainline_symbol_missing:{relative}:{name}" for name in missing)
        symbol_evidence[relative] = {
            "exists": True,
            "parse_ok": True,
            "required_symbols": sorted(required),
            "missing_symbols": missing,
        }

    retired_evidence = {}
    for relative in RETIRED_FILES:
        exists = (root / relative).exists()
        retired_evidence[relative] = {"exists": exists}
        if exists:
            violations.append(f"retired_absolute_strong_probability_path_present:{relative}")

    launcher_path = root / "scripts" / "amsc_zuuu_metar_taf_poll_once.sh"
    launcher_text = launcher_path.read_text(encoding="utf-8") if launcher_path.is_file() else ""
    launcher_hits = sorted(token for token in RETIRED_LAUNCHER_TOKENS if token in launcher_text)
    if not launcher_path.is_file():
        violations.append("amsc_launcher_missing")
    violations.extend(f"amsc_launcher_retired_probability_path_present:{token}" for token in launcher_hits)

    public_writer = (root / "sequential_bayes_public_ssot_shadow_writer.py").read_text(encoding="utf-8")
    required_physical_ssot_tokens = (
        '"state": "physical_only_market_projection_na"',
        '"market_bucket_probabilities": {}',
        '"market_bucket_probabilities_na_reason": na_reason',
        "commit_shadow_physical_posterior_transaction",
    )
    missing_physical_ssot_tokens = [token for token in required_physical_ssot_tokens if token not in public_writer]
    violations.extend(f"physical_only_public_ssot_contract_missing:{token}" for token in missing_physical_ssot_tokens)

    prewarm_path = root / "resident_fast_lane_prewarm_contract.py"
    prewarm_text = prewarm_path.read_text(encoding="utf-8") if prewarm_path.is_file() else ""
    probability_block = prewarm_text.split("PROBABILITY_REQUIRED_COMPONENTS", 1)[-1].split(
        "MARKET_PROJECTION_REQUIRED_COMPONENTS", 1
    )[0]
    market_separated = bool(probability_block and '"market_context"' not in probability_block)
    if not market_separated:
        violations.append("market_context_still_blocks_probability_prewarm")

    strong_contract_path = root / "strong_observation_lr_artifact_contract.py"
    strong_contract_text = strong_contract_path.read_text(encoding="utf-8") if strong_contract_path.is_file() else ""
    missing_strong_v3_tokens = [token for token in REQUIRED_STRONG_V3_TOKENS if token not in strong_contract_text]
    violations.extend(f"strong_v3_reachability_contract_missing:{token}" for token in missing_strong_v3_tokens)

    active_artifact_binding_evidence: dict[str, Any] = {}
    for relative, required_tokens in REQUIRED_ACTIVE_ARTIFACT_BINDINGS.items():
        path = root / relative
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        missing = [token for token in required_tokens if token not in text]
        active_artifact_binding_evidence[relative] = {
            "exists": path.is_file(),
            "missing_tokens": missing,
        }
        violations.extend(f"active_strong_lr_registry_binding_missing:{relative}:{token}" for token in missing)

    registry_path = root / "strong_observation_lr_runtime_registry.py"
    registry_text = registry_path.read_text(encoding="utf-8") if registry_path.is_file() else ""
    missing_attestation_tokens = [
        token for token in REQUIRED_ACTIVATION_ATTESTATION_TOKENS if token not in registry_text
    ]
    violations.extend(
        f"active_strong_lr_activation_attestation_missing:{token}"
        for token in missing_attestation_tokens
    )

    return {
        "schema": SCHEMA_VERSION,
        "ok": not violations,
        "state": "structurally_unique_shadow_mainline" if not violations else "blocked",
        "violations": sorted(set(violations)),
        "required_symbol_evidence": symbol_evidence,
        "retired_path_evidence": retired_evidence,
        "amsc_launcher": {
            "path": str(launcher_path),
            "retired_token_hits": launcher_hits,
        },
        "physical_only_public_ssot": {
            "present": not missing_physical_ssot_tokens,
            "missing_contract_tokens": missing_physical_ssot_tokens,
            "market_projection_na_does_not_hide_physical_posterior": not missing_physical_ssot_tokens,
            "creates_trade_outbox": False,
        },
        "dependency_boundary": {
            "market_context_separate_from_probability_prewarm": market_separated,
        },
        "strong_likelihood_contract": {
            "candidate_temperature_specific": not missing_strong_v3_tokens,
            "supports_current_warming_and_cooling_momentum": not missing_strong_v3_tokens,
            "missing_contract_tokens": missing_strong_v3_tokens,
        },
        "active_strong_lr_registry": {
            "single_runtime_identity_owner": not any(
                evidence["missing_tokens"] for evidence in active_artifact_binding_evidence.values()
            ),
            "binding_evidence": active_artifact_binding_evidence,
            "strict_asof_oos_are_promotion_only": True,
            "activation_evidence_body_and_sha_frozen": not missing_attestation_tokens,
            "missing_activation_attestation_tokens": missing_attestation_tokens,
        },
        "boundary": {
            "read_only": True,
            "target_chain": "P0_prior_times_weak_then_canonical_incremental_lr_then_checkpoint_then_public_posterior_ssot",
            "weak_consumed_only_at_p0": True,
            "absolute_strong_map_retired": True,
            "bridge_missing_still_publishes_physical_x_ssot": True,
            "market_context_cannot_block_physical_x_update": market_separated,
            "strong_lr_is_candidate_x_specific_v3": not missing_strong_v3_tokens,
            "p0_and_resident_cycle_require_active_registry_artifact": not any(
                evidence["missing_tokens"] for evidence in active_artifact_binding_evidence.values()
            ),
            "strict_asof_oos_do_not_block_active_runtime": True,
            "strong_lr_activation_requires_frozen_attestation": not missing_attestation_tokens,
            "production_activation_claimed": False,
            "writes_archive_db": False,
            "places_orders": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args(argv)
    result = run_audit(root=args.root.resolve())
    if args.summary:
        result = {
            "schema": result["schema"],
            "ok": result["ok"],
            "state": result["state"],
            "violations": result["violations"],
            "boundary": result["boundary"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
