#!/usr/bin/env python3
"""Read-only behavioral audit for the sole production three-leg strategy."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dynamic_three_leg_transition_runtime import MANAGEMENT_SCOPE, build_dynamic_three_leg_transition_plan
from dynamic_top_yes_wave_exit_runtime import build_dynamic_top_yes_wave_exit_candidate
from scripts.production_trade_gate_zero_audit import run_audit as run_gate_zero_audit
from second_yes_entry_window_runtime import build_second_yes_entry_window_state
from strong_posterior_three_leg_strategy import build_strong_posterior_three_leg_strategy


NOW = "2026-07-16T07:30:00.000Z"


def _hard_certificate(bucket: str) -> dict[str, Any]:
    core = {
        "state": "valid", "bucket": bucket,
        "resolution_contract_id": "resolution-current", "posterior_checkpoint_id": "posterior-current",
        "bridge_artifact_sha256": "a" * 64, "target_contract_sha256": "b" * 64,
        "physical_distribution_sha256": "c" * 64, "physical_hard_support_sha256": "d" * 64,
        "hard_residual_support_authority_sha256": "e" * 64,
        "physical_hard_support_c": [35.0], "hard_residual_support_c": [0.0],
        "settlement_support_witness_c": [35.0], "full_support_exclusion_verified": True,
    }
    raw = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {**core, "certificate_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest()}


def _selector() -> dict[str, Any]:
    return build_strong_posterior_three_leg_strategy(
        posterior_buckets=[
            {"bucket": "34C", "probability": 0.20, "yes_bid": 0.18, "no_bid": 0.81},
            {"bucket": "35C", "probability": 0.55, "yes_bid": 0.52, "no_bid": 0.47},
            {"bucket": "36C", "probability": 0.25, "yes_bid": 0.23, "no_bid": 0.76},
        ],
        strong_physical_evidence={
            "joint_feature_model_applied": True,
            "top_bucket": "35C",
            "top_bucket_joint_strong_support": True,
            "safe_no_observed_max_c": 35.0,
            "bucket_exclusions": [
                {
                    "bucket": "34C",
                    "excluded": True,
                    "kind": "observed_daily_high_exceeded",
                    "composer_joint_strong_model": True,
                    "settlement_exclusion_certificate_state": "valid",
                    "full_support_exclusion_verified": True,
                    "settlement_exclusion_certificate": _hard_certificate("34C"),
                    "confidence": 1.0,
                }
            ],
        },
        settlement_window_open=True,
        available_cash_usdc=200.0,
    )


def _plan() -> dict[str, Any]:
    return {
        "ok": True,
        "trading_plan_id": "plan-current",
        "posterior_run_id": "posterior-current",
        "realization_state_id": "realization-current",
        "strategy_artifact_sha256": "artifact-current",
        "market_snapshot_id": "market-current",
        "capital_snapshot_id": "capital-current",
        "risk_snapshot_id": "risk-current",
        "capital_epoch_id": "capital-epoch-current",
        "risk_reservation_id": "reservation-current",
        "second_yes_entry_window_state_id": "second-window-current",
        "candidates": [
            {"candidate_id": "top", "strategy_leg": "top_yes_wave", "bucket": "35C", "side": "YES", "notional": 20.0},
            {"candidate_id": "second", "strategy_leg": "second_yes", "bucket": "36C", "side": "YES", "notional": 4.99},
            {"candidate_id": "no", "strategy_leg": "strongly_excluded_no", "bucket": "34C", "side": "NO", "notional": 100.0},
        ],
    }


def _lot(lot: str, leg: str, bucket: str, side: str, notional: float, size: float) -> dict[str, Any]:
    return {
        "management_scope": MANAGEMENT_SCOPE,
        "position_lot_id": lot,
        "position_lot_version": "1",
        "token_id": f"token-{bucket}-{side}",
        "bucket": bucket,
        "side": side,
        "strategy_leg": leg,
        "position_size": size,
        "entry_notional_usdc": notional,
        "average_cost": notional / size,
    }


def _snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "position_snapshot_id": "positions-current",
        "as_of": NOW,
        "complete": True,
        "positions": rows,
        "unresolved_intents": [],
    }


def run_audit() -> dict[str, Any]:
    selected = _selector()
    legs = {row["strategy_leg"]: row for row in selected.get("legs", [])}
    selector_contract = (
        selected.get("ok") is True
        and legs.get("top_yes_wave", {}).get("bucket") == "35C"
        and legs.get("second_yes", {}).get("bucket") == "36C"
        and legs.get("strongly_excluded_no", {}).get("bucket") == "34C"
        and legs.get("top_yes_wave", {}).get("target_notional_usdc") == 20.0
        and legs.get("second_yes", {}).get("target_notional_usdc") == 4.99
        and legs.get("strongly_excluded_no", {}).get("target_notional_usdc") == 100.0
        and selected.get("boundary", {}).get("uses_ev_edge_lcb") is False
    )

    swapped_positions = [
        _lot("lot-promoted", "second_yes", "35C", "YES", 4.99, 9.98),
        _lot("lot-demoted", "top_yes_wave", "36C", "YES", 20.0, 40.0),
        _lot("lot-no", "strongly_excluded_no", "34C", "NO", 100.0, 125.0),
    ]
    swap = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(), position_snapshot=_snapshot(swapped_positions), decision_as_of=NOW
    )
    dynamic_swap = (
        swap.get("phase") == "SELL_OLD"
        and len(swap.get("sell_intents", [])) == 1
        and swap["sell_intents"][0].get("position_lot_id") == "lot-demoted"
        and swap["sell_intents"][0].get("full_exit") is False
        and swap["sell_intents"][0].get("target_notional_after_fill_usdc") == 4.99
        and not any(row.get("position_lot_id") == "lot-promoted" for row in swap.get("sell_intents", []))
    )

    after_trim = [
        _lot("lot-promoted", "second_yes", "35C", "YES", 4.99, 9.98),
        _lot("lot-demoted", "top_yes_wave", "36C", "YES", 4.99, 9.98),
        _lot("lot-no", "strongly_excluded_no", "34C", "NO", 100.0, 125.0),
    ]
    top_up = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=_plan(), position_snapshot=_snapshot(after_trim), decision_as_of=NOW
    )
    promoted_buy = next((row for row in top_up.get("buy_candidates", []) if row.get("strategy_leg") == "top_yes_wave"), {})
    dynamic_top_up = (
        top_up.get("phase") == "BUY_NEW"
        and promoted_buy.get("bucket") == "35C"
        and abs(float(promoted_buy.get("notional") or 0.0) - 15.01) < 1e-9
    )

    window = build_second_yes_entry_window_state(
        realization_state={
            "posterior_run_id": "posterior-current",
            "realization_state_id": "realization-current",
            "peak_timing_state": {
                "schema": "peak_timing_state_l3_v1",
                "ok": True,
                "state": "available",
                "peak_timing_state_id": "peak-current",
                "entry_peak_at": "2026-07-16T08:00:00.000Z",
            },
        },
        decision_as_of=NOW,
    )
    second_window = window.get("entry_allowed") is True and window.get("entry_window_minutes") == 30
    after_peak_window = build_second_yes_entry_window_state(
        realization_state={
            "posterior_run_id": "posterior-current",
            "realization_state_id": "realization-current",
            "peak_timing_state": {
                "schema": "peak_timing_state_l3_v1",
                "ok": True,
                "state": "available",
                "peak_timing_state_id": "peak-current",
                "entry_peak_at": "2026-07-16T08:00:00.000Z",
            },
        },
        decision_as_of="2026-07-16T08:00:00.001Z",
    )
    after_peak_closes_second = (
        after_peak_window.get("entry_allowed") is False
        and after_peak_window.get("entry_block_reason") == "predicted_peak_time_reached_or_passed"
    )

    promoted_exit = build_dynamic_top_yes_wave_exit_candidate(
        posterior_ssot_fact={
            "posterior_run_id": "posterior-current",
            "realization_state_id": "realization-current",
            "target_bucket": "35C",
        },
        position={
            "management_scope": MANAGEMENT_SCOPE,
            "strategy_leg": "top_yes_wave",
            "entry_strategy_leg": "second_yes",
            "bucket": "35C",
            "side": "YES",
            "token_id": "token-35C-YES",
            "position_lot_id": "lot-promoted",
            "position_lot_version": "1",
            "position_size": 9.98,
            "entry_strategy_artifact_sha256": "artifact-current",
            "effective_take_profit_price": 0.90,
            "take_profit_price_source": "promoted_current_top_yes_policy",
        },
        executable_sell_quote={
            "market_snapshot_id": "market-exit",
            "token_id": "token-35C-YES",
            "requested_size": 9.98,
            "price_for_requested_size": 0.91,
            "as_of": NOW,
            "expires_at": "2026-07-16T07:30:01.000Z",
        },
        approved_strategy_artifact_sha256="artifact-current",
        decision_as_of=NOW,
    )
    dynamic_exit = (
        promoted_exit.get("state") == "candidate"
        and promoted_exit.get("limit_price") == 0.90
        and promoted_exit.get("entry_strategy_leg") == "second_yes"
        and promoted_exit.get("bucket_selection_dynamic") is True
    )

    malformed = _plan()
    malformed["candidates"][1]["notional"] = 5.0
    rejected = build_dynamic_three_leg_transition_plan(
        desired_trading_plan=malformed, position_snapshot=_snapshot([]), decision_as_of=NOW
    )
    cap_fail_closed = (
        rejected.get("phase") == "BLOCKED"
        and "dynamic_transition_desired_candidate_notional_cap_exceeded" in rejected.get("block_reasons", [])
    )
    gate_zero = run_gate_zero_audit()

    checks = {
        "posterior_rank_and_joint_physical_exclusion_select_exact_three_legs": selector_contract,
        "top_and_second_roles_reselect_without_promoted_lot_churn": dynamic_swap,
        "promoted_second_lot_tops_up_to_current_top_20": dynamic_top_up,
        "second_yes_entry_is_only_in_final_30_minutes_before_predicted_peak": second_window,
        "second_yes_entry_closes_immediately_when_predicted_peak_is_reached": after_peak_closes_second,
        "current_top_exit_is_dynamic_and_uses_090_wave_target": dynamic_exit,
        "malformed_second_yes_five_usdc_plan_fails_closed": cap_fail_closed,
        "legacy_ev_edge_lcb_and_retired_no_paths_are_non_executable": gate_zero.get("ok") is True,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "schema": "production_dynamic_three_leg_strategy_contract_audit_v1",
        "ok": not blockers,
        "state": "strategy_contract_aligned" if not blockers else "blocked",
        "checks": checks,
        "blockers": blockers,
        "limits_usdc": {"top_yes_wave": 20.0, "second_yes": 4.99, "strongly_excluded_no": 100.0},
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


def main() -> int:
    result = run_audit()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
