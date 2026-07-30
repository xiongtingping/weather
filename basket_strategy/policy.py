"""Persisted basket selection, allocation and two-scale execution policy.

This module is deliberately pure: it never reads weather data, SQLite or a
gateway.  The planner may use it to turn a contiguous selected window into
stable role labels and profit-weighted quantities.  The production adapter is
responsible for persisting the resulting parent/child order audit rows.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


POLICY_SCHEMA_VERSION = "basket_selection_execution_policy_v2"
POLICY_ID = "weather_basket_selection_execution_v2"
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "trading" / "basket_strategy_policy_v2.json"
EPS = 1e-9


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def policy_sha256(value: Mapping[str, Any]) -> str:
    body = {key: item for key, item in dict(value).items() if key != "policy_sha256"}
    return hashlib.sha256(_json(body).encode("utf-8")).hexdigest()


def bucket_roles(width: int) -> list[str]:
    """Return fixed positional roles; roles are never swapped by price."""

    roles = {
        4: ["non_core_edge_left", "primary_core", "secondary_core", "non_core_edge_right"],
        3: ["non_core_edge_left", "core", "non_core_edge_right"],
        2: ["primary_core", "secondary_core"],
    }.get(int(width))
    if roles is None:
        raise ValueError("basket_width_role_contract_missing")
    return roles


def role_map(selected: Sequence[Mapping[str, Any]], width: int | None = None) -> list[dict[str, Any]]:
    rows = list(selected)
    actual_width = int(width or len(rows))
    roles = bucket_roles(actual_width)
    if len(rows) != len(roles):
        raise ValueError("basket_selected_width_role_mismatch")
    return [
        {
            "position": index + 1,
            "bucket": str(row.get("bucket") or row.get("outcome") or ""),
            "bucket_c": row.get("bucket_c"),
            "role": roles[index],
            "core": roles[index] in {"primary_core", "secondary_core", "core"},
            "profit_weight": 2.0 if roles[index] == "primary_core" else 1.0 if roles[index] in {"secondary_core", "core"} else 0.0,
        }
        for index, row in enumerate(rows)
    ]


def validate_window_roles(
    selected: Sequence[Mapping[str, Any]],
    probabilities_by_c: Mapping[int, float] | None = None,
    *,
    tolerance: float = 0.0,
) -> list[str]:
    """Validate contiguity and fixed main/sub-core ordering.

    A low b2 probability never causes a silent b2/b3 role exchange.  The
    caller must move the complete window or fail closed.
    """

    rows = list(selected)
    issues: list[str] = []
    try:
        roles = role_map(rows)
    except ValueError as exc:
        return [str(exc)]
    values = [row.get("bucket_c") for row in rows]
    if any(not isinstance(value, (int, float)) for value in values):
        issues.append("basket_bucket_coordinate_missing")
    elif any(int(values[index]) != int(values[index - 1]) + 1 for index in range(1, len(values))):
        issues.append("basket_window_not_contiguous")
    probabilities = probabilities_by_c or {}
    if len(rows) >= 4:
        p2 = float(probabilities.get(int(rows[1].get("bucket_c")), 0.0) or 0.0)
        p3 = float(probabilities.get(int(rows[2].get("bucket_c")), 0.0) or 0.0)
        if p2 + float(tolerance) < p3:
            issues.append("primary_core_probability_below_secondary_move_window")
    return issues


def allocate_profit_weighted(
    selected: Sequence[Mapping[str, Any]],
    *,
    budget_usdc: float,
    fee_rate: float = 0.0,
    minimum_order_shares: float = 1.0,
    edge_profit_floor_usdc: float = 0.000001,
    max_leg_notional_usdc: float | None = None,
) -> dict[str, Any]:
    """Allocate a budget while making every bucket non-negative at settlement.

    For bucket i, ``q_i = C + g_i`` and ``C = sum(q_i p_i)``.  The profit
    weights are [0,2,1,0] for four buckets and [0,1,0] for three buckets.
    Scaling by depth preserves the 2:1 core profit ratio and never removes an
    edge bucket solely because it is non-core.
    """

    rows = list(selected)
    roles = role_map(rows)
    budget = float(budget_usdc)
    fee = float(fee_rate)
    if not math.isfinite(budget) or budget <= 0:
        return {"status": "blocked", "reason_codes": ["allocation_budget_invalid"]}
    prices = []
    depths = []
    for row in rows:
        try:
            price = float(row.get("yes_ask")) * (1.0 + fee)
            depth = float(row.get("yes_ask_size"))
        except (TypeError, ValueError):
            return {"status": "blocked", "reason_codes": ["allocation_quote_invalid"]}
        if not 0 < price < 1 or not math.isfinite(depth) or depth <= 0:
            return {"status": "blocked", "reason_codes": ["allocation_quote_invalid"]}
        prices.append(price)
        depths.append(depth)
    total_price = sum(prices)
    if total_price >= 1.0 - EPS:
        return {"status": "blocked", "reason_codes": ["basket_worst_case_floor_not_profitable"]}
    weights = [float(item["profit_weight"]) for item in roles]
    edge_floor = max(0.0, float(edge_profit_floor_usdc))
    weighted_price = sum(price * weight for price, weight in zip(prices, weights))
    if weighted_price <= EPS:
        return {"status": "blocked", "reason_codes": ["allocation_profit_weight_degenerate"]}
    # C is the basket cost; q_i-C is the target settlement profit.
    # Add the same tiny non-core floor to every scenario, then scale the
    # 2:1/1:1 core bonuses around it.  This keeps every leg strictly above
    # break-even after rounding while preserving the requested core ratio to
    # numerical precision.
    profit_unit = (budget * (1.0 - total_price) - total_price * edge_floor) / weighted_price
    if profit_unit < -EPS:
        return {"status": "blocked", "reason_codes": ["allocation_budget_below_edge_floor"]}
    quantities = [budget + edge_floor + profit_unit * weight for weight in weights]
    depth_scale = min(depth / quantity for depth, quantity in zip(depths, quantities))
    scale = min(1.0, depth_scale)
    if max_leg_notional_usdc is not None:
        leg_cap = float(max_leg_notional_usdc)
        if not math.isfinite(leg_cap) or leg_cap <= 0:
            return {"status": "blocked", "reason_codes": ["allocation_parent_order_cap_invalid"]}
        scale = min(
            scale,
            min(leg_cap / (quantity * price) for quantity, price in zip(quantities, prices)),
        )
    quantities = [quantity * scale for quantity in quantities]
    cost = sum(quantity * price for quantity, price in zip(quantities, prices))
    profits = [quantity - cost for quantity in quantities]
    if min(quantities) + EPS < float(minimum_order_shares):
        return {
            "status": "blocked",
            "reason_codes": ["allocation_depth_below_minimum_order"],
            "quantities": quantities,
            "total_cost_usdc": cost,
        }
    return {
        "status": "ready",
        "mode": "profit_weighted_settlement_floor",
        "roles": roles,
        "profit_weights": weights,
        "edge_profit_floor_usdc": edge_floor,
        "quantities": quantities,
        "prices": prices,
        "total_cost_usdc": cost,
        "settlement_profit_by_bucket": profits,
        "primary_secondary_profit_ratio": 2.0 if len(rows) == 4 else 1.0,
        "depth_scale": scale,
        "max_leg_notional_usdc": max_leg_notional_usdc,
        "worst_case_profit_usdc": min(profits),
        "best_case_profit_usdc": max(profits),
    }


def compute_macro_topup(
    *,
    target_quantities: Mapping[str, float],
    filled_quantities: Mapping[str, float] | None = None,
    pending_quantities: Mapping[str, float] | None = None,
    available_budget_usdc: float,
    ask_by_bucket: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Recompute target at the macro level and emit additive deltas only."""

    filled = filled_quantities or {}
    pending = pending_quantities or {}
    deltas = {
        bucket: max(0.0, float(target) - float(filled.get(bucket, 0.0) or 0.0) - float(pending.get(bucket, 0.0) or 0.0))
        for bucket, target in target_quantities.items()
    }
    asks = ask_by_bucket or {}
    estimated = sum(float(deltas[bucket]) * float(asks.get(bucket, 0.0) or 0.0) for bucket in deltas)
    budget = max(0.0, float(available_budget_usdc))
    if estimated > budget + EPS and estimated > EPS:
        scale = budget / estimated
        deltas = {bucket: value * scale for bucket, value in deltas.items()}
        estimated = budget
    return {
        "schema": "basket_macro_topup_v1",
        "mode": "macro_recompute_target",
        "delta_rule": "max(target - filled - pending, 0)",
        "target_quantities": dict(target_quantities),
        "filled_quantities": dict(filled),
        "pending_quantities": dict(pending),
        "delta_quantities": deltas,
        "estimated_notional_usdc": estimated,
        "available_budget_usdc": budget,
        "additive_only": True,
        "recompute_weather_snapshot": True,
    }


def split_micro_parent_order(
    *,
    bucket: str,
    quantity: float,
    asks: Sequence[Mapping[str, Any] | Sequence[Any]],
    child_max_notional_usdc: float,
    max_children: int = 8,
) -> dict[str, Any]:
    """Split one macro delta across price levels without changing its target."""

    remaining = max(0.0, float(quantity))
    cap = float(child_max_notional_usdc)
    children: list[dict[str, Any]] = []
    if cap <= 0 or max_children <= 0:
        return {"status": "blocked", "reason_codes": ["micro_split_limits_invalid"]}
    for index, raw in enumerate(asks):
        if remaining <= EPS or len(children) >= max_children:
            break
        if isinstance(raw, Mapping):
            price, depth = raw.get("price"), raw.get("size")
        elif isinstance(raw, Sequence) and len(raw) >= 2:
            price, depth = raw[0], raw[1]
        else:
            continue
        try:
            price, depth = float(price), float(depth)
        except (TypeError, ValueError):
            continue
        if not 0 < price < 1 or depth <= 0:
            continue
        child_quantity = min(remaining, depth, cap / price)
        if child_quantity <= EPS:
            continue
        children.append({"bucket": bucket, "price": price, "quantity": child_quantity, "notional_usdc": child_quantity * price, "level": index})
        remaining -= child_quantity
    return {
        "status": "ready" if children else "blocked",
        "reason_codes": [] if children else ["micro_split_depth_missing"],
        "schema": "basket_micro_split_v1",
        "mode": "micro_child_orders",
        "parent_bucket": bucket,
        "requested_quantity": float(quantity),
        "child_orders": children,
        "unallocated_quantity": max(0.0, remaining),
        "target_recomputed": False,
        "weather_recomputed": False,
    }


def default_policy() -> dict[str, Any]:
    return {
        "schema": POLICY_SCHEMA_VERSION,
        "policy_id": POLICY_ID,
        "state": "prepared",
        "selection": {
            "allowed_widths": [4, 3, 2],
            "four_bucket_roles": bucket_roles(4),
            "three_bucket_roles": bucket_roles(3),
            "two_bucket_roles": bucket_roles(2),
            "primary_secondary_profit_ratio": "2:1",
            "roles_fixed_by_position": True,
            "move_window_instead_of_role_swap": True,
            "edge_bucket_can_be_center": True,
            "probability_tolerance_for_b2_b3": 0.0,
        },
        "allocation": {
            "mode": "profit_weighted_settlement_floor",
            "edge_profit_floor_usdc": 0.0,
            "primary_core_profit_weight": 2.0,
            "secondary_core_profit_weight": 1.0,
            "core_role_is_not_price_rank": True,
        },
        "execution": {
            "macro": {
                "delta_rule": "max(target - filled - pending, 0)",
                "recompute_on_new_posterior_or_quote": True,
                "additive_only": True,
                "parent_order_cap_usdc": 2.0,
            },
            "micro": {
                "split_by_ask_level": True,
                "child_order_cap_usdc": 2.0,
                "max_children_per_leg": 8,
                "does_not_recompute_target": True,
            },
            "order_type": "FOK",
            "all_legs_required": True,
            "partial_or_unknown_fill": "reconcile_required_stop_new_basket",
        },
        "risk_budget": {
            "production_total_usdc": 1000.0,
            "test_total_usdc": 100.0,
            "test_daily_loss_cap_usdc": 5.0,
            "unallocated_yes_pool_usdc": 30.0,
            "city_day_cap_usdc": 10.0,
            "single_event_cap_usdc": 5.0,
            "parent_order_cap_usdc": 2.0,
        },
        "fact_semantics": {
            "amsc_whole_hour_is_settlement_fact": True,
            "amsc_half_hour_is_reference_only": True,
            "speci_can_fill_settlement_slot": False,
        },
    }


def load_policy(path: Path | str = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = default_policy()
    if not isinstance(value, Mapping):
        raise ValueError("basket_policy_not_mapping")
    policy = dict(value)
    supplied = str(policy.get("policy_sha256") or "")
    if supplied and supplied != policy_sha256(policy):
        raise ValueError("basket_policy_hash_mismatch")
    policy.setdefault("policy_sha256", policy_sha256(policy))
    return policy


__all__ = [
    "DEFAULT_POLICY_PATH",
    "POLICY_ID",
    "POLICY_SCHEMA_VERSION",
    "allocate_profit_weighted",
    "bucket_roles",
    "compute_macro_topup",
    "default_policy",
    "load_policy",
    "policy_sha256",
    "role_map",
    "split_micro_parent_order",
    "validate_window_roles",
]
