#!/usr/bin/env python3
"""Read-only audit for production EV/Edge gate-zero and exact exit authorization."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Dict


ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "scan_weather_market.py"
STRATEGY = ROOT / "strong_posterior_three_leg_strategy.py"
TARGET_RUNTIME = ROOT / "target_realization_v3_runtime.py"
HARD_SOURCE_POLLER = ROOT / "scripts" / "hard_source_direct_poller.py"
UI_SERVER = ROOT / "ui" / "server.py"
REALIZATION = ROOT / "realization_state_projection_runtime.py"


def _function_source(path: Path, function_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            lines = source.splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise RuntimeError(f"function_not_found:{path.name}:{function_name}")


def run_audit() -> Dict[str, object]:
    scan_source = SCAN.read_text(encoding="utf-8")
    strategy_file_source = STRATEGY.read_text(encoding="utf-8")
    realization_source = REALIZATION.read_text(encoding="utf-8")
    ui_source = UI_SERVER.read_text(encoding="utf-8")
    target_entry = _function_source(SCAN, "_target_realization_v3_entry_block_reason")
    entry_policy = _function_source(SCAN, "_entry_order_policy_block_reason")
    execute_source = _function_source(SCAN, "execute_auto_trades")
    strategy_source = _function_source(STRATEGY, "build_strong_posterior_three_leg_strategy")
    retirement_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (STRATEGY, TARGET_RUNTIME, SCAN, HARD_SOURCE_POLLER, UI_SERVER)
    )

    checks = {
        "target_entry_has_no_posterior_price_value_gate": all(
            token not in target_entry
            for token in (
                "q_settlement",
                "nonpositive_posterior_value",
                "FEE_RESERVE_PER_SHARE",
            )
        ),
        "target_entry_has_no_buy_price_cap_gate": all(
            token not in target_entry
            for token in (
                "target_realization_order_price_cap",
                "target_realization_v3_entry_price_cap_exceeded",
            )
        ),
        "entry_submit_policy_has_no_edge_gate": all(
            token not in entry_policy
            for token in (
                "entry_usable_edge",
                "exec_edge",
                "signal_edge",
                "LCB",
            )
        ),
        "entry_size_has_no_usable_edge_scale": all(
            token not in execute_source
            for token in (
                'candidate_ref.get("entry_usable_edge_size_scale")',
                'PM_ENTRY_USABLE_EDGE_SIZE_SCALE_ENABLED',
                "_recover_positive_usable_edge_size_in_place",
            )
        ),
        "buy_submit_calls_have_policy_guard": (
            scan_source.count('_entry_order_policy_block_reason(candidate)') >= 2
            and scan_source.count('"place-order",\n                    token_id,\n                    "BUY"') >= 1
        ),
        "strategy_has_no_fair_value_or_unapproved_sell_reason": all(
            token not in strategy_source
            for token in (
                "target_realization_yes_nonpositive_posterior_value",
                "target_realization_no_nonpositive_posterior_value",
                "target_realization_top_yes_profit_lock",
                "target_realization_no_single_bucket_cap_rebalance",
                "target_realization_yes_cap_rebalance",
                "target_realization_no_capital_recycle",
            )
        ),
        "yes_budget_does_not_use_expected_no_profit": (
            "TOP_YES_MAX_NOTIONAL_USDC = 20.0" in strategy_file_source
            and "SECOND_YES_MAX_NOTIONAL_USDC = 4.99" in strategy_file_source
            and "LEAST_NO_MAX_NOTIONAL_USDC = 100.0" in strategy_file_source
            and "expected_no_profit" not in strategy_file_source
        ),
        "exit_classifier_has_no_prefix_authority": (
            "startswith(TARGET_REALIZATION" not in scan_source
            and "code.startswith(\"target_realization_\")" not in scan_source
        ),
        "market_alpha_runtime_default_deny": (
            "MARKET_ALPHA_TAKE_PROFIT_RUNTIME_ENABLED = False"
            in (ROOT / "trade_exit_reason_contract.py").read_text(encoding="utf-8")
        ),
        "retired_recycle_and_profit_lock_paths_absent": all(
            token not in retirement_sources
            for token in (
                "no_recycle",
                "top_yes_profit_lock",
                "top_yes_take_profit_points",
                "capital_recycle",
            )
        ),
        "retired_remote_and_trajectory_no_generators_absent": all(
            token not in retirement_sources
            for token in (
                '"remote_no"',
                "target_realization_remote_no",
                "PM_TARGET_REALIZATION_REMOTE_NO",
                "safe_remote_no",
                "trajectory_no_current_report",
                "current_report_temperature_no",
                "next_temperature_bucket_no",
            )
        ),
        "physical_report_to_market_bucket_direct_mapping_retired": (
            "def _current_bucket" not in realization_source
            and '"current_report_bucket": ""' in realization_source
            and "physical_x_must_not_be_directly_mapped_to_market_bucket_b" in realization_source
            and '"physical_report_to_market_bucket_direct_mapping_retired": True'
            in realization_source
            and "_ui_snapshot_hard_temperature_floor_c" not in ui_source
            and "_ui_snapshot_bucket_below_hard_temperature_floor" not in ui_source
            and "posterior_target_bucket_below_hard_temperature_floor" not in ui_source
            and "_invalidate_posterior_ssot_fact_by_hard_observation" not in ui_source
        ),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "schema": "production_trade_gate_zero_audit_v1",
        "ok": not blockers,
        "read_only": True,
        "checks": checks,
        "blockers": blockers,
        "guards": {
            "places_orders": False,
            "writes_archive_db": False,
            "restarts_services": False,
            "changes_parameters": False,
        },
    }


def main() -> int:
    payload = run_audit()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
