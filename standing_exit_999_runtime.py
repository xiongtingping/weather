"""Durable-identity standing-exit plan semantics for Target Realization v6."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


SCHEMA_VERSION = "standing_exit_999_plan_v1"
TARGET_PRICE = Decimal("0.999")
APPROVED_LEGS = {"main_yes", "second_yes", "trajectory_no", "hard_exclusion_no"}
MARKET_ALPHA_CODE_BY_LEG = {
    "main_yes": "market_alpha_take_profit_main_yes_999",
    "second_yes": "market_alpha_take_profit_second_yes_999",
    "trajectory_no": "market_alpha_take_profit_trajectory_no_999",
    "hard_exclusion_no": "market_alpha_take_profit_hard_exclusion_no_999",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _identity(prefix: str, value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def tick_supports_0999(tick_size: Any) -> bool:
    try:
        tick = Decimal(str(tick_size))
    except (InvalidOperation, ValueError):
        return False
    if tick <= 0:
        return False
    return TARGET_PRICE % tick == 0


def build_standing_exit_plan(
    *,
    fill: Mapping[str, Any],
    strategy_artifact_sha256: str,
    tick_size: Any,
) -> dict[str, Any]:
    leg = _text(fill.get("strategy_leg"))
    size = _finite(fill.get("filled_size"))
    required = {
        "posterior_run_id": _text(fill.get("posterior_run_id")),
        "realization_state_id": _text(fill.get("realization_state_id")),
        "position_lot_id": _text(fill.get("position_lot_id")),
        "position_lot_version": _text(fill.get("position_lot_version")),
        "token_id": _text(fill.get("token_id")),
        "bucket": _text(fill.get("bucket")),
        "side": _text(fill.get("side")).upper(),
        "buy_intent_id": _text(fill.get("buy_intent_id")),
        "fill_identity": _text(fill.get("fill_identity")),
    }
    blockers: list[str] = []
    if leg not in APPROVED_LEGS:
        blockers.append("standing_exit_strategy_leg_invalid")
    if size is None or size <= 0:
        blockers.append("standing_exit_filled_size_invalid")
    if not _text(strategy_artifact_sha256) or not all(required.values()):
        blockers.append("standing_exit_lineage_missing")
    if required["side"] not in {"YES", "NO"}:
        blockers.append("standing_exit_position_side_invalid")
    if not tick_supports_0999(tick_size):
        blockers.append("standing_exit_999_tick_unsupported")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers)), "places_orders": False}
    identity = {**required, "strategy_leg": leg, "strategy_artifact_sha256": _text(strategy_artifact_sha256), "filled_size": size}
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "coverage_required",
        "standing_exit_plan_id": _identity("standplan_", identity),
        **identity,
        "sell_side": "SELL",
        "order_type": "GTC",
        "post_only": False,
        "limit_price": float(TARGET_PRICE),
        "target_size": size,
        "covered_size": 0.0,
        "uncovered_size": size,
        "stage_released_size": 0.0,
        "coverage_version": 1,
        "exit_reason_family": "market_alpha_take_profit",
        "exit_reason_code": MARKET_ALPHA_CODE_BY_LEG[leg],
        "places_orders": False,
    }


def release_standing_coverage(
    *,
    plan: Mapping[str, Any],
    release_size: float,
    target_realization_reason_code: str,
    coverage_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    size = _finite(release_size)
    covered = _finite(plan.get("covered_size"))
    released = _finite(plan.get("stage_released_size"))
    blockers: list[str] = []
    if plan.get("schema") != SCHEMA_VERSION or not _text(plan.get("standing_exit_plan_id")):
        blockers.append("standing_exit_release_plan_invalid")
    if size is None or size <= 0 or covered is None or covered < size or released is None:
        blockers.append("standing_exit_release_size_exceeds_coverage")
    if not _text(target_realization_reason_code).startswith("target_realization_main_yes_"):
        blockers.append("standing_exit_release_reason_not_approved_main_yes_target_realization")
    if coverage_snapshot.get("complete") is not True:
        blockers.append("standing_exit_coverage_snapshot_incomplete")
    if coverage_snapshot.get("cancel_or_remaining_reconciled") is not True:
        blockers.append("standing_exit_cancel_or_remaining_not_reconciled")
    if coverage_snapshot.get("ack_unknown") is True:
        blockers.append("standing_exit_cancel_ack_unknown")
    if blockers:
        return {"schema": "standing_exit_999_release_v1", "ok": False, "state": "blocked", "block_reasons": sorted(set(blockers)), "places_orders": False}
    identity = {
        "standing_exit_plan_id": plan["standing_exit_plan_id"],
        "coverage_version": int(plan.get("coverage_version") or 1),
        "release_size": size,
        "target_realization_reason_code": _text(target_realization_reason_code),
    }
    return {
        "schema": "standing_exit_999_release_v1",
        "ok": True,
        "state": "released_for_target_realization",
        "standing_exit_release_id": _identity("standrel_", identity),
        **identity,
        "covered_size_before": covered,
        "covered_size_after": covered - size,
        "stage_released_size_after": released + size,
        "next_coverage_version": int(plan.get("coverage_version") or 1) + 1,
        "places_orders": False,
    }


__all__ = [
    "APPROVED_LEGS", "MARKET_ALPHA_CODE_BY_LEG", "SCHEMA_VERSION", "TARGET_PRICE",
    "build_standing_exit_plan", "release_standing_coverage", "tick_supports_0999",
]
