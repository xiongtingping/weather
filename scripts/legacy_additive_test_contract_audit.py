#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "legacy_additive_test_contract_audit_v1"
DEFAULT_TEST_FILE = ROOT / "tests" / "test_bayesian_probability_runtime.py"


CONTRACT_PATTERNS = [
    {
        "name": "legacy_runtime_env_gate",
        "tokens": [
            'os.environ["PM_BAYES_LEGACY_ADDITIVE_COMPAT_ALLOW_UNSAFE_RUNTIME"] = "1"',
            'os.environ["PM_BAYES_HIERARCHICAL_FAIL_OPEN_TO_LEGACY"] = "1"',
        ],
        "target_contract": "hierarchical_runtime_always_on_env_ignored",
        "cleanup_action": "remove_unsafe_legacy_env_expectations",
    },
    {
        "name": "legacy_runtime_source",
        "tokens": ["continuous_temperature_posterior_v1", "legacy_additive_bayesian_probability_runtime.py"],
        "target_contract": "hierarchical_posterior_pipeline_runtime_v1",
        "cleanup_action": "assert_hierarchical_source_and_inactive_legacy_bridge",
    },
    {
        "name": "old_additive_bridge_active",
        "tokens": [
            'assertTrue(meta["old_additive_evidence_pool_used"])',
            'assertTrue(meta["production_replacement_required"])',
            "replacement_posterior_runtime",
        ],
        "target_contract": "legacy_bridge_inactive_production_boundary",
        "cleanup_action": "assert_false_or_absent_for_default_runtime",
    },
    {
        "name": "forecast_weak_old_channels",
        "tokens": [
            "wu_forecast_enabled",
            "weather_com_forecast_enabled",
            "taf_peak_enabled",
            "nowcast" + "_evidence",
            "model" + "_evidence",
        ],
        "target_contract": "forecast_weak_likelihood.source_distributions",
        "cleanup_action": "assert_layer_contributions_forecast_weak_likelihood",
    },
    {
        "name": "metar_strong_old_channels",
        "tokens": [
            "physical_likelihood_enabled",
            "physical_likelihood" + "_evidence",
            "independent_physics_residual" + "_evidence",
            "temperature_trajectory" + "_evidence",
            "temperature_trajectory_posterior_enabled",
        ],
        "target_contract": "metar_strong_likelihood.state_probs_or_layer_contributions",
        "cleanup_action": "assert_metar_strong_likelihood_and_temperature_realization",
    },
    {
        "name": "hard_source_old_override_meta",
        "tokens": [
            "hard_source_posterior_override_applied",
            "hard_source_posterior_override_final_bucket_lock",
            "hard_source_posterior_override_constraint_mode",
        ],
        "target_contract": "hard_mask.accepted_and_masked_zero_buckets",
        "cleanup_action": "assert_hard_mask_contract",
    },
    {
        "name": "post_cool_rebound_old_channels",
        "tokens": [
            "post_cool_rebound_risk" + "_evidence",
            "post_cool_rebound_regime_prior",
            "post_cool_rebound_intraday_likelihood",
        ],
        "target_contract": "root_prior_weather_type_adjustment_or_metar_strong_likelihood",
        "cleanup_action": "assert_layer_specific_post_cool_rebound_mapping",
    },
    {
        "name": "legacy_atomic_display_meta",
        "tokens": [
            "atomic_temperature_bucket_display_enabled",
            "listed_atomic_temperature_conditional",
        ],
        "target_contract": "posterior_temperature_distribution.atomic_display",
        "cleanup_action": "assert_distribution_contract_instead_of_legacy_meta",
    },
]


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _line_starts(source: str) -> list[int]:
    starts = [0]
    for match in re.finditer(r"\n", source):
        starts.append(match.end())
    return starts


def _slice_for_node(source: str, starts: list[int], node: ast.AST) -> str:
    lineno = int(getattr(node, "lineno", 1) or 1)
    end_lineno = int(getattr(node, "end_lineno", lineno) or lineno)
    start = starts[max(0, lineno - 1)]
    end = starts[end_lineno] if end_lineno < len(starts) else len(source)
    return source[start:end]


def _test_functions(source: str) -> list[dict[str, Any]]:
    tree = ast.parse(source)
    starts = _line_starts(source)
    tests: list[dict[str, Any]] = []
    for class_node in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for node in class_node.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            tests.append(
                {
                    "class_name": class_node.name,
                    "test_name": node.name,
                    "line": int(node.lineno),
                    "source": _slice_for_node(source, starts, node),
                }
            )
    return tests


def build_legacy_additive_test_contract_audit(test_file: Path = DEFAULT_TEST_FILE) -> dict[str, Any]:
    path = Path(test_file)
    source = path.read_text(encoding="utf-8")
    tests = _test_functions(source)
    blocking_tests: list[dict[str, Any]] = []
    by_pattern: dict[str, int] = {}
    by_target_contract: dict[str, int] = {}

    for test in tests:
        matches: list[dict[str, str]] = []
        body = str(test["source"])
        for pattern in CONTRACT_PATTERNS:
            matched_tokens = [token for token in pattern["tokens"] if token in body]
            if not matched_tokens:
                continue
            by_pattern[pattern["name"]] = by_pattern.get(pattern["name"], 0) + 1
            target = str(pattern["target_contract"])
            by_target_contract[target] = by_target_contract.get(target, 0) + 1
            matches.append(
                {
                    "pattern": str(pattern["name"]),
                    "target_contract": target,
                    "cleanup_action": str(pattern["cleanup_action"]),
                    "matched_tokens": matched_tokens,
                }
            )
        if matches:
            blocking_tests.append(
                {
                    "class_name": str(test["class_name"]),
                    "test_name": str(test["test_name"]),
                    "line": int(test["line"]),
                    "matches": matches,
                }
            )
    unsafe_setup_token = 'os.environ["PM_BAYES_LEGACY_ADDITIVE_COMPAT_ALLOW_UNSAFE_RUNTIME"] = "1"'
    if unsafe_setup_token in source:
        pattern_name = "legacy_runtime_env_gate"
        target_contract = "hierarchical_runtime_always_on_env_ignored"
        by_pattern[pattern_name] = by_pattern.get(pattern_name, 0) + 1
        by_target_contract[target_contract] = by_target_contract.get(target_contract, 0) + 1
        blocking_tests.append(
            {
                "class_name": "module_or_setup",
                "test_name": "unsafe_legacy_runtime_env_gate",
                "line": source[: source.index(unsafe_setup_token)].count("\n") + 1,
                "matches": [
                    {
                        "pattern": pattern_name,
                        "target_contract": target_contract,
                        "cleanup_action": "remove_unsafe_legacy_env_expectations",
                        "matched_tokens": [unsafe_setup_token],
                    }
                ],
            }
        )

    return {
        "schema": SCHEMA_VERSION,
        "ok": not blocking_tests,
        "read_only": True,
        "test_file": str(path),
        "test_count": len(tests),
        "blocking_test_count": len(blocking_tests),
        "by_pattern": dict(sorted(by_pattern.items())),
        "by_target_contract": dict(sorted(by_target_contract.items())),
        "blocking_tests": blocking_tests,
        "guards": _guards(),
        "next_actions": (
            []
            if not blocking_tests
            else [
                "migrate_legacy_additive_tests_to_hierarchical_contracts",
                "delete_unsafe_legacy_delegate_after_blocking_tests_zero",
            ]
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit for legacy additive test contracts.")
    parser.add_argument("--test-file", default=str(DEFAULT_TEST_FILE))
    parser.add_argument("--summary", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_legacy_additive_test_contract_audit(Path(args.test_file))
    if args.summary:
        report = {
            "schema": report["schema"],
            "ok": report["ok"],
            "read_only": report["read_only"],
            "test_file": report["test_file"],
            "test_count": report["test_count"],
            "blocking_test_count": report["blocking_test_count"],
            "by_pattern": report["by_pattern"],
            "by_target_contract": report["by_target_contract"],
            "guards": report["guards"],
            "next_actions": report["next_actions"],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
