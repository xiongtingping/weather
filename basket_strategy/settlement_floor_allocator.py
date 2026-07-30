"""Settlement-floor biased basket allocation.

This module is the calculation path for the manually specified basket rules.
It is intentionally independent from the weather/Bayes/runtime selectors and
from the order gateway.  Callers provide the selected buckets, their explicit
roles, current YES asks/depth and (optionally) already held positions.

The hard invariant is the settlement floor for every non-lottery bucket::

    P_i = final_shares_i - settlement_basket_cost >= safety_pad

M/S/A/B ratios are targets, not gates.  When a ratio conflicts with the
settlement floor, the floor wins and the result reports the relaxed target.
Lottery buckets receive a fixed notional and are excluded from that floor.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SETTLEMENT_FLOOR_POLICY_ID = "weather_settlement_floor_biased_basket_v1"
SETTLEMENT_FLOOR_POLICY_SCHEMA = "settlement_floor_biased_basket_policy_v1"
DEFAULT_SETTLEMENT_FLOOR_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "trading"
    / "settlement_floor_basket_policy_v1.json"
)

EPS = 1e-9
# Five-leg baskets can have a near-unit fixed-point slope when an additional
# floor bucket is introduced.  Keep iterating to convergence instead of
# falsely treating the slow but valid solution as a hard failure.
MAX_SOLVER_ITERATIONS = 4096

# The requested reference allocation is M:S:A:B = 12:6:2:1.  The resulting
# relative capital shares at a complete 200U budget are 57.14%, 28.57%,
# 9.52% and 4.76%.  Missing roles do not create placeholder buckets and do
# not cause their reference weight to be silently reassigned.
REFERENCE_FUNDING_RATIOS: dict[str, float] = {
    "M": 12.0,
    "S": 6.0,
    "A": 2.0,
    "B": 1.0,
}

FLOOR_ROLES = frozenset({"M", "S", "A", "B", "F", "L"})
# C is retained in the basket cost ledger but is not a floor leg and receives
# no new allocation.  This is used when an existing bucket is deliberately
# excluded from the settlement-floor guarantee without pretending its
# historical cost disappeared.
COST_ONLY_ROLES = frozenset({"C"})
ALL_ROLES = FLOOR_ROLES | COST_ONLY_ROLES
UNIQUE_ROLES = frozenset({"M", "S", "A", "B"})
# Role-rebalance sales are deliberately narrower than ordinary inventory
# management.  They may trim a lower-priority/secondary exposure that is
# preventing the declared profit order, but may not sell the main core,
# primary share bucket, or a floor bucket merely to control budget.
REBALANCE_SELLABLE_ROLES = frozenset({"S", "B", "L", "C"})
REBALANCE_PROTECTED_ROLES = frozenset({"M", "A", "F"})

ROLE_ALIASES = {
    "M": "M",
    "主": "M",
    "主核心": "M",
    "主核心资金": "M",
    "主核心资金桶": "M",
    "MAIN": "M",
    "PRIMARY": "M",
    "S": "S",
    "副": "S",
    "次": "S",
    "副核心": "S",
    "次核心": "S",
    "副核心资金": "S",
    "副核心资金桶": "S",
    "SECONDARY": "S",
    "A": "A",
    "主份额": "A",
    "主核心份额": "A",
    "主核心份额桶": "A",
    "核心份额": "A",
    "主份额桶": "A",
    "B": "B",
    "副份额": "B",
    "副核心份额": "B",
    "副核心份额桶": "B",
    "副份额桶": "B",
    "F": "F",
    "保本": "F",
    "保本桶": "F",
    "成本": "F",
    "成本桶": "F",
    "L": "L",
    "彩票": "L",
    "彩票桶": "L",
    "LOTTERY": "L",
    "C": "C",
    "成本计入": "C",
    "成本计入桶": "C",
    "仅计成本": "C",
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bucket_key(value: Any) -> str:
    return str(value).strip()


def _temperature_value(value: Any) -> float | None:
    raw = _bucket_key(value).replace("℃", "°C")
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(?:°?C)?", raw, flags=re.IGNORECASE)
    return _finite(match.group(1)) if match else None


def _mapping_value(mapping: Mapping[str, Any], bucket: str, default: Any = None) -> Any:
    """Match 37, 37C and 37°C without collapsing tail bucket labels."""

    if bucket in mapping:
        return mapping[bucket]
    numeric = _temperature_value(bucket)
    if numeric is None:
        return default
    for key, value in mapping.items():
        if _temperature_value(key) == numeric:
            return value
    return default


def _normalise_role(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return ROLE_ALIASES.get(raw.upper(), ROLE_ALIASES.get(raw))


def _position_values(raw: Any) -> tuple[float, float | None]:
    if isinstance(raw, Mapping):
        shares = raw.get("shares", raw.get("quantity", raw.get("size", raw.get("position_shares", 0.0))))
        cost = raw.get(
            "cost_usdc",
            raw.get("cost", raw.get("notional_usdc", raw.get("position_cost_usdc"))),
        )
        if cost is None and raw.get("average_cost") is not None:
            average = _finite(raw.get("average_cost"))
            shares_value = _finite(shares)
            cost = average * shares_value if average is not None and shares_value is not None else None
    else:
        shares, cost = raw, None
    shares_value = _finite(shares)
    if shares_value is None or shares_value < 0:
        return 0.0, None
    cost_value = _finite(cost)
    if cost_value is not None and cost_value < 0:
        cost_value = None
    return shares_value, cost_value


@dataclass(frozen=True)
class _AskLevel:
    price: float
    size: float


@dataclass(frozen=True)
class _AskBook:
    levels: tuple[_AskLevel, ...]

    @property
    def best_price(self) -> float:
        return self.levels[0].price

    @property
    def total_depth(self) -> float:
        return sum(level.size for level in self.levels)

    def cost_for(self, quantity: float, *, consumed_quantity: float = 0.0) -> float | None:
        """Return the cost of buying quantity after consumed_quantity shares."""

        remaining = max(0.0, float(quantity))
        skip = max(0.0, float(consumed_quantity))
        cost = 0.0
        for level in self.levels:
            if skip >= level.size - EPS:
                skip -= level.size
                continue
            available = level.size - skip
            skip = 0.0
            take = min(remaining, available)
            cost += take * level.price
            remaining -= take
            if remaining <= EPS:
                return cost
        return cost if remaining <= EPS else None

    def quantity_for_cost(self, notional: float, *, consumed_quantity: float = 0.0) -> tuple[float, float] | None:
        """Return (shares, cost) for a notional, consuming asks in price order."""

        remaining = max(0.0, float(notional))
        skip = max(0.0, float(consumed_quantity))
        quantity = 0.0
        cost = 0.0
        for level in self.levels:
            if skip >= level.size - EPS:
                skip -= level.size
                continue
            available = level.size - skip
            skip = 0.0
            level_cost = available * level.price
            if level_cost <= remaining + EPS:
                quantity += available
                cost += level_cost
                remaining -= level_cost
                continue
            take = remaining / level.price
            quantity += take
            cost += remaining
            return quantity, cost
        if remaining <= EPS:
            return quantity, cost
        return None


def build_staged_depth_slice(
    *,
    target_shares: float,
    quote: Any,
    minimum_order_shares: float = 0.0,
) -> dict[str, Any]:
    """Split a target into immediately marketable FAK and a GTC remainder.

    The visible ask book is the only source for the FAK slice.  When the
    target exceeds visible depth, the last visible ask is returned as the
    protected GTC limit and its notional is only a reserve estimate.  Pending
    GTC shares are deliberately *not* treated as held shares or as settled
    basket cost; callers must recompute the floor after every fill.
    """

    target = _finite(target_shares)
    minimum_order = _finite(minimum_order_shares)
    if target is None or target < 0 or minimum_order is None or minimum_order < 0:
        return {"status": "blocked", "reason_codes": ["staged_target_invalid"]}
    book = _quote_levels(quote)
    if book is None:
        return {"status": "blocked", "reason_codes": ["settlement_floor_quote_missing"]}
    if target <= EPS:
        return {
            "status": "complete",
            "target_shares": 0.0,
            "immediate_fak_shares": 0.0,
            "immediate_fak_cost_usdc": 0.0,
            "immediate_fak_limit_price": None,
            "pending_gtc_shares": 0.0,
            "gtc_limit_price": None,
            "pending_gtc_reserve_usdc": 0.0,
            "visible_depth_shares": book.total_depth,
        }

    immediate = min(target, book.total_depth)
    immediate_cost = book.cost_for(immediate) if immediate > EPS else 0.0
    if immediate_cost is None:
        return {"status": "blocked", "reason_codes": ["settlement_floor_depth_insufficient"]}
    remaining = max(0.0, target - immediate)
    last_price = book.levels[-1].price
    if remaining > EPS and not 0.0 < last_price < 1.0:
        return {"status": "blocked", "reason_codes": ["staged_gtc_limit_invalid"]}
    if immediate > EPS and minimum_order > EPS and immediate < minimum_order:
        # A partial visible slice smaller than the exchange minimum is not a
        # valid FAK.  Leave the whole target to a protected GTC instead.
        immediate = 0.0
        immediate_cost = 0.0
        remaining = target
    return {
        "status": "pending" if remaining > EPS else "complete",
        "target_shares": target,
        "immediate_fak_shares": immediate,
        "immediate_fak_cost_usdc": float(immediate_cost),
        "immediate_fak_limit_price": last_price if immediate > EPS else None,
        "pending_gtc_shares": remaining,
        "gtc_limit_price": last_price if remaining > EPS else None,
        "pending_gtc_reserve_usdc": remaining * last_price,
        "visible_depth_shares": book.total_depth,
    }


def _with_staged_tail(book: _AskBook) -> _AskBook:
    """Use best ask as the planning cap for an eventual GTC tail.

    The visible book can contain a high, thin tail.  Using that last level as
    the solver's infinite tail can make the estimated sum exceed one and
    reject the whole staged plan before the executable FAK slice is built.
    The actual GTC price is still selected by ``build_staged_depth_slice``
    from the last visible level; this synthetic tail is only a planning cap.
    """

    # This synthetic tail is used only by the staged planner to solve an
    # estimated target.  The original visible book remains available for the
    # actual FAK/GTC split and for reporting depth.
    return _AskBook(book.levels + (_AskLevel(book.best_price, 10**15),))


def _quote_levels(raw: Any) -> _AskBook | None:
    if not isinstance(raw, Mapping):
        return None
    raw_levels = raw.get("asks", raw.get("sell_asks", raw.get("yes_asks")))
    candidates: list[Any]
    if isinstance(raw_levels, Mapping):
        candidates = [{"price": price, "size": size} for price, size in raw_levels.items()]
    elif isinstance(raw_levels, Sequence) and not isinstance(raw_levels, (str, bytes)):
        candidates = list(raw_levels)
    else:
        candidates = [
            {
                "price": raw.get("yes_ask", raw.get("ask", raw.get("best_ask", raw.get("price")))),
                "size": raw.get("yes_ask_size", raw.get("ask_size", raw.get("depth", raw.get("size")))),
            }
        ]
    levels: list[_AskLevel] = []
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            price = _finite(candidate.get("price", candidate.get("ask")))
            size = _finite(candidate.get("size", candidate.get("quantity", candidate.get("depth"))))
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)) and len(candidate) >= 2:
            price, size = _finite(candidate[0]), _finite(candidate[1])
        else:
            continue
        if price is None or size is None or not 0.0 < price < 1.0 or size <= 0.0:
            continue
        levels.append(_AskLevel(price, size))
    if not levels:
        return None
    return _AskBook(tuple(sorted(levels, key=lambda level: level.price)))


def _quote_bid_levels(raw: Any) -> _AskBook | None:
    """Parse official YES bid depth for a controlled rebalance sale.

    The same book type is reused with descending prices: ``cost_for`` then
    represents executable sale proceeds from the best bid down.  This keeps
    the calculation path separate from asks while preserving full depth and
    partial-fill accounting.
    """

    if not isinstance(raw, Mapping):
        return None
    raw_levels = raw.get("bids", raw.get("buy_bids", raw.get("yes_bids")))
    candidates: list[Any]
    if isinstance(raw_levels, Mapping):
        candidates = [{"price": price, "size": size} for price, size in raw_levels.items()]
    elif isinstance(raw_levels, Sequence) and not isinstance(raw_levels, (str, bytes)):
        candidates = list(raw_levels)
    else:
        candidates = [
            {
                "price": raw.get("yes_bid", raw.get("bid", raw.get("best_bid"))),
                "size": raw.get("yes_bid_size", raw.get("bid_size", raw.get("depth"))),
            }
        ]
    levels: list[_AskLevel] = []
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            price = _finite(candidate.get("price", candidate.get("bid")))
            size = _finite(candidate.get("size", candidate.get("quantity", candidate.get("depth"))))
        elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)) and len(candidate) >= 2:
            price, size = _finite(candidate[0]), _finite(candidate[1])
        else:
            continue
        if price is None or size is None or not 0.0 < price < 1.0 or size <= 0.0:
            continue
        levels.append(_AskLevel(price, size))
    if not levels:
        return None
    return _AskBook(tuple(sorted(levels, key=lambda level: level.price, reverse=True)))


def build_role_rebalance_plan(
    *,
    buckets: Sequence[Any],
    roles: Mapping[Any, Any] | None,
    existing_positions: Mapping[Any, Any] | None,
    sell_quotes: Mapping[Any, Any] | None,
    safety_pad_usdc: float = 0.01,
    minimum_strict_share_gap: float = 1.0,
    minimum_order_shares: float = 0.01,
    sellable_roles: Sequence[str] = tuple(sorted(REBALANCE_SELLABLE_ROLES)),
) -> dict[str, Any]:
    """Plan a constrained role-order rebalance using executable YES bids.

    This function never sells to cap a budget.  It only trims a sellable
    lower-priority role when its current inventory prevents the declared
    ``M>S>A>B>F`` order.  Proceeds are included in the net basket cost and the
    hard floor is recomputed for the post-sale inventory.  It is calculation
    only; the caller remains responsible for gateway submission and fill
    reconciliation.
    """

    safety_pad = _finite(safety_pad_usdc)
    strict_gap = _finite(minimum_strict_share_gap)
    minimum_order = _finite(minimum_order_shares)
    if safety_pad is None or safety_pad < 0 or strict_gap is None or strict_gap < 0 or minimum_order is None or minimum_order <= 0:
        return {"status": "blocked", "reason_codes": ["rebalance_parameters_invalid"]}
    rows, role_issues = _normalise_buckets(buckets, roles)
    if role_issues:
        return {"status": "blocked", "reason_codes": role_issues}
    positions = {_bucket_key(key): value for key, value in (existing_positions or {}).items()}
    sell_quote_map = {_bucket_key(key): value for key, value in (sell_quotes or {}).items()}
    inventory: dict[str, tuple[float, float]] = {}
    missing_cost: list[str] = []
    for row in rows:
        bucket = str(row["bucket"])
        shares, cost = _position_values(_mapping_value(positions, bucket, {}))
        if shares > EPS and cost is None:
            missing_cost.append(bucket)
        inventory[bucket] = shares, float(cost or 0.0)
    if missing_cost:
        return {"status": "blocked", "reason_codes": ["existing_position_cost_missing"], "missing_cost_buckets": missing_cost}

    by_role = {
        str(row["role"]): str(row["bucket"])
        for row in rows
        if row.get("role") in {"M", "S", "A", "B", "F"}
    }
    ordered_roles = [role for role in ("M", "S", "A", "B", "F") if role in by_role]
    sellable = {str(role) for role in sellable_roles}
    final_shares = {bucket: values[0] for bucket, values in inventory.items()}
    sell_by_bucket: dict[str, float] = {bucket: 0.0 for bucket in inventory}
    unsatisfied: list[dict[str, Any]] = []
    for index in range(len(ordered_roles) - 1):
        higher, lower = ordered_roles[index], ordered_roles[index + 1]
        higher_bucket, lower_bucket = by_role[higher], by_role[lower]
        higher_shares = final_shares[higher_bucket]
        lower_shares = final_shares[lower_bucket]
        if higher_shares > lower_shares + strict_gap + EPS:
            continue
        if lower not in sellable:
            unsatisfied.append({"higher_role": higher, "lower_role": lower, "reason": "lower_role_protected"})
            continue
        desired_lower = max(0.0, higher_shares - strict_gap)
        sell_quantity = max(0.0, lower_shares - desired_lower)
        if sell_quantity > EPS and minimum_order > EPS:
            sell_quantity = math.ceil((sell_quantity - EPS) / minimum_order) * minimum_order
        sell_quantity = min(sell_quantity, lower_shares)
        sell_by_bucket[lower_bucket] = max(sell_by_bucket[lower_bucket], sell_quantity)
        final_shares[lower_bucket] = lower_shares - sell_quantity

    if unsatisfied:
        return {
            "status": "needs_additive_topup",
            "reason_codes": ["role_order_requires_buy_not_protected_sell"],
            "unsatisfied_role_pairs": unsatisfied,
            "sell_orders": [],
        }

    sell_orders: list[dict[str, Any]] = []
    realized_proceeds = 0.0
    for row in rows:
        bucket = str(row["bucket"])
        quantity = float(sell_by_bucket.get(bucket, 0.0))
        if quantity <= EPS:
            continue
        book = _quote_bid_levels(_mapping_value(sell_quote_map, bucket))
        if book is None:
            return {
                "status": "blocked",
                "reason_codes": ["rebalance_bid_quote_missing"],
                "failed_bucket": bucket,
                "requested_sell_shares": quantity,
            }
        proceeds = book.cost_for(quantity)
        if proceeds is None:
            return {
                "status": "blocked",
                "reason_codes": ["rebalance_bid_depth_insufficient"],
                "failed_bucket": bucket,
                "requested_sell_shares": quantity,
                "available_bid_depth_shares": book.total_depth,
            }
        realized_proceeds += proceeds
        sell_orders.append({
            "bucket": bucket,
            "role": row.get("role"),
            "requested_sell_shares": quantity,
            "best_bid": book.best_price,
            "estimated_proceeds_usdc": proceeds,
            "remaining_shares": final_shares[bucket],
            "sell_quote_depth_shares": book.total_depth,
        })

    original_cost = sum(inventory[str(row["bucket"])][1] for row in rows if row.get("role") in ALL_ROLES)
    net_cost = original_cost - realized_proceeds
    floor_profit_by_bucket = {
        str(row["bucket"]): final_shares[str(row["bucket"])] - net_cost
        for row in rows
        if row.get("role") in FLOOR_ROLES
    }
    if floor_profit_by_bucket and min(floor_profit_by_bucket.values()) < safety_pad - 1e-7:
        return {
            "status": "blocked",
            "reason_codes": ["rebalance_would_break_settlement_floor"],
            "sell_orders": sell_orders,
            "original_basket_cost_usdc": original_cost,
            "realized_proceeds_usdc": realized_proceeds,
            "net_settlement_cost_usdc": net_cost,
            "settlement_profit_by_bucket": floor_profit_by_bucket,
        }

    final_by_role = {role: final_shares[bucket] for role, bucket in by_role.items()}
    role_order_ok = all(
        final_by_role[ordered_roles[index]] > final_by_role[ordered_roles[index + 1]] + EPS
        for index in range(len(ordered_roles) - 1)
    )
    return {
        "status": "ready",
        "rebalance_required": bool(sell_orders),
        "sell_orders": sell_orders,
        "sellable_roles": sorted(sellable),
        "protected_roles": sorted(REBALANCE_PROTECTED_ROLES),
        "roles": {str(row["bucket"]): row.get("role") for row in rows if row.get("role")},
        "original_basket_cost_usdc": original_cost,
        "realized_proceeds_usdc": realized_proceeds,
        "net_settlement_cost_usdc": net_cost,
        "final_shares_by_bucket": final_shares,
        "final_shares_by_role": final_by_role,
        "settlement_profit_by_bucket": floor_profit_by_bucket,
        "minimum_settlement_profit_usdc": min(floor_profit_by_bucket.values()) if floor_profit_by_bucket else None,
        "maximum_settlement_profit_usdc": max(floor_profit_by_bucket.values()) if floor_profit_by_bucket else None,
        "role_order_ok": role_order_ok,
        "minimum_strict_share_gap": strict_gap,
        "safety_pad_usdc": safety_pad,
        "recompute_after_fill": True,
        "unknown_fill_action": "reconcile_required_stop",
    }


def _normalise_buckets(buckets: Sequence[Any], roles: Mapping[Any, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    role_map = {_bucket_key(key): value for key, value in (roles or {}).items()}
    rows: list[dict[str, Any]] = []
    issues: list[str] = []
    seen: set[str] = set()
    for raw in buckets:
        if isinstance(raw, Mapping):
            bucket = raw.get("bucket", raw.get("outcome", raw.get("label")))
            inline_role = raw.get("role")
        else:
            bucket, inline_role = raw, None
        key = _bucket_key(bucket)
        if not key:
            issues.append("bucket_label_missing")
            continue
        if key in seen:
            issues.append("bucket_label_duplicate")
            continue
        seen.add(key)
        role_raw = _mapping_value(role_map, key, inline_role)
        role = _normalise_role(role_raw)
        if role_raw is not None and role is None:
            issues.append(f"role_invalid:{key}")
        rows.append({"bucket": key, "role": role})
    role_seen: dict[str, str] = {}
    for row in rows:
        role = row.get("role")
        if role in UNIQUE_ROLES:
            previous = role_seen.get(str(role))
            if previous is not None:
                issues.append(f"role_duplicate:{role}")
            role_seen[str(role)] = str(row["bucket"])
    return rows, issues


def _soft_profit_weights(rows: Sequence[Mapping[str, Any]], books: Mapping[str, _AskBook]) -> dict[str, float]:
    """Build price-aware weights while preserving M>S>A>B target order.

    The unadjusted quantity weight is funding_ratio / best_ask.  That is the
    requested real-price correction for the 12:6:2:1 funding profile.  A
    monotone projection prevents an unusually cheap lower-priority bucket from
    becoming the profit leader.  Both effects remain soft and are reported.
    """

    raw: dict[str, float] = {}
    for row in rows:
        role = str(row.get("role") or "")
        bucket = str(row.get("bucket") or "")
        if role in REFERENCE_FUNDING_RATIOS and bucket in books:
            raw[role] = REFERENCE_FUNDING_RATIOS[role] / books[bucket].best_price
    ordered = [role for role in ("M", "S", "A", "B") if role in raw]
    result: dict[str, float] = {}
    previous: float | None = None
    for role in ordered:
        candidate = max(raw[role], EPS)
        if previous is not None:
            candidate = min(candidate, previous * (1.0 - 1e-6))
        result[role] = candidate
        previous = candidate
    return result


def _solve_quantities(
    *,
    rows: Sequence[Mapping[str, Any]],
    books: Mapping[str, _AskBook],
    existing: Mapping[str, tuple[float, float]],
    weights: Mapping[str, float],
    safety_pad: float,
    profit_scale: float,
    minimum_order_shares: float,
    profit_order_step_shares: float,
    minimum_order_notional_usdc: float,
) -> dict[str, Any]:
    """Solve C = C0 + cost(required shares) for one profit scale."""

    floor_rows = [row for row in rows if row.get("role") in FLOOR_ROLES]
    cost_rows = [row for row in rows if row.get("role") in (FLOOR_ROLES | COST_ONLY_ROLES)]
    c0 = sum(existing[str(row["bucket"])][1] for row in cost_rows)
    current_cost = c0

    def _requested_quantity(bucket: str, shortfall: float) -> float:
        quantity = shortfall
        if quantity > EPS and minimum_order_shares > 0.0 and quantity < minimum_order_shares:
            quantity = minimum_order_shares
        if quantity > EPS and minimum_order_notional_usdc > EPS:
            current_cost = books[bucket].cost_for(quantity)
            if current_cost is not None and current_cost + EPS < minimum_order_notional_usdc:
                minimum_purchase = books[bucket].quantity_for_cost(minimum_order_notional_usdc)
                if minimum_purchase is None:
                    return math.inf
                quantity = max(quantity, minimum_purchase[0])
        return quantity

    for _ in range(MAX_SOLVER_ITERATIONS):
        required: dict[str, float] = {}
        quantities: dict[str, float] = {}
        costs: dict[str, float] = {}
        for row in floor_rows:
            bucket = str(row["bucket"])
            role = str(row["role"])
            held_shares, _ = existing[bucket]
            required_shares = current_cost + safety_pad + profit_scale * float(weights.get(role, 0.0))
            required[bucket] = required_shares
            shortfall = _requested_quantity(bucket, max(0.0, required_shares - held_shares))
            if not math.isfinite(shortfall):
                return {
                    "status": "blocked",
                    "reason_codes": ["settlement_floor_minimum_order_notional_depth_insufficient"],
                    "failed_bucket": bucket,
                    "required_shares": required_shares,
                }
            quantities[bucket] = shortfall
            cost = books[bucket].cost_for(shortfall)
            if cost is None:
                return {
                    "status": "blocked",
                    "reason_codes": ["settlement_floor_depth_insufficient"],
                    "failed_bucket": bucket,
                    "required_shares": required_shares,
                    "requested_new_shares": shortfall,
                }
            costs[bucket] = cost

        # Existing inventory can be materially asymmetric.  The floor is the
        # hard constraint, but the declared role order still needs to be
        # restored using additive buys when it is feasible.  Work upwards from
        # the weakest declared role so M>S>A>B>F is respected without selling
        # any existing inventory or inventing a missing role.
        if profit_order_step_shares > EPS:
            role_buckets: dict[str, list[str]] = {}
            for row in floor_rows:
                role = str(row.get("role") or "")
                if role in {"M", "S", "A", "B", "F"}:
                    role_buckets.setdefault(role, []).append(str(row["bucket"]))
            # F may intentionally contain more than one保本桶.  Compare a
            # higher role against the largest lower-role inventory, rather
            # than silently retaining only the last F bucket in a dict.
            target_by_role = {
                role: max(existing[bucket][0] + quantities[bucket] for bucket in buckets)
                for role, buckets in role_buckets.items()
            }
            ordered_roles = [role for role in ("M", "S", "A", "B", "F") if role in role_buckets]
            for index in range(len(ordered_roles) - 2, -1, -1):
                higher, lower = ordered_roles[index], ordered_roles[index + 1]
                target_by_role[higher] = max(
                    target_by_role[higher],
                    target_by_role[lower] + profit_order_step_shares,
                )
            # UNIQUE_ROLES are single buckets by contract; duplicate F
            # buckets stay independently floor-sized and only constrain the
            # higher role through target_by_role above.
            for role, buckets in role_buckets.items():
                if len(buckets) != 1:
                    continue
                bucket = buckets[0]
                target = target_by_role[role]
                held_shares = existing[bucket][0]
                shortfall = _requested_quantity(bucket, max(0.0, target - held_shares))
                if not math.isfinite(shortfall):
                    return {
                        "status": "blocked",
                        "reason_codes": ["settlement_floor_minimum_order_notional_depth_insufficient"],
                        "failed_bucket": bucket,
                        "required_shares": target,
                    }
                quantities[bucket] = shortfall
                cost = books[bucket].cost_for(shortfall)
                if cost is None:
                    return {
                        "status": "blocked",
                        "reason_codes": ["settlement_floor_depth_insufficient"],
                        "failed_bucket": bucket,
                        "required_shares": target,
                        "requested_new_shares": shortfall,
                    }
                costs[bucket] = cost
        next_cost = c0 + sum(costs.values())
        if abs(next_cost - current_cost) <= 1e-8:
            final_cost = next_cost
            final_shares = {
                str(row["bucket"]): existing[str(row["bucket"])][0] + quantities[str(row["bucket"])]
                for row in floor_rows
            }
            profits = {bucket: shares - final_cost for bucket, shares in final_shares.items()}
            if any(value < safety_pad - 1e-7 for value in profits.values()):
                # One more pass with the new C handles the boundary created by
                # a minimum-order jump.
                current_cost = next_cost
                continue
            return {
                "status": "ready",
                "total_cost_usdc": final_cost,
                "existing_cost_usdc": c0,
                "new_cost_by_bucket": costs,
                "new_shares_by_bucket": quantities,
                "final_shares_by_bucket": final_shares,
                "required_shares_by_bucket": required,
                "settlement_profit_by_bucket": profits,
                "profit_scale": profit_scale,
            }
        current_cost = next_cost
    return {"status": "blocked", "reason_codes": ["settlement_floor_solver_not_converged"]}


def _lottery_target(
    *,
    bucket: str,
    book: _AskBook,
    held_shares: float,
    held_cost: float,
    target_cost: float,
    minimum_order_shares: float,
    minimum_order_notional_usdc: float,
) -> dict[str, Any]:
    required_cost = max(0.0, target_cost - held_cost)
    if required_cost <= EPS:
        return {
            "bucket": bucket,
            "role": "L",
            "new_shares": 0.0,
            "new_cost_usdc": 0.0,
            "final_shares": held_shares,
            "final_cost_usdc": held_cost,
            "settlement_profit_usdc": None,
            "participates_in_settlement_floor": False,
        }
    if minimum_order_notional_usdc > EPS and required_cost + EPS < minimum_order_notional_usdc:
        return {
            "bucket": bucket,
            "role": "L",
            "new_shares": 0.0,
            "new_cost_usdc": 0.0,
            "final_shares": held_shares,
            "final_cost_usdc": held_cost,
            "settlement_profit_usdc": None,
            "participates_in_settlement_floor": False,
            "allocation": "lottery_increment_below_minimum_notional",
        }
    purchased = book.quantity_for_cost(required_cost)
    if purchased is None:
        return {"status": "blocked", "reason_codes": ["lottery_depth_insufficient"], "failed_bucket": bucket}
    new_shares, new_cost = purchased
    if minimum_order_shares > 0.0 and new_shares + EPS < minimum_order_shares:
        return {"status": "blocked", "reason_codes": ["lottery_below_minimum_order"], "failed_bucket": bucket}
    return {
        "bucket": bucket,
        "role": "L",
        "new_shares": new_shares,
        "new_cost_usdc": new_cost,
        "final_shares": held_shares + new_shares,
        "final_cost_usdc": held_cost + new_cost,
        "settlement_profit_usdc": None,
        "participates_in_settlement_floor": False,
    }


def build_settlement_floor_plan(
    *,
    budget_usdc: float,
    buckets: Sequence[Any],
    roles: Mapping[Any, Any] | None = None,
    quotes: Mapping[Any, Any] | None = None,
    existing_positions: Mapping[Any, Any] | None = None,
    safety_pad_usdc: float = 0.01,
    lottery_budget_usdc: float = 0.0,
    minimum_order_shares: float = 1.0,
    budget_is_incremental: bool = False,
    profit_order_step_shares: float = 1.0,
    minimum_order_notional_usdc: float = 0.0,
    allow_staged_depth: bool = False,
    allow_pending_only_when_ask_sum_ge_one: bool = False,
) -> dict[str, Any]:
    """Build a settlement-floor plan from explicit roles and real asks.

    ``quotes`` accepts either a scalar best-ask/depth mapping, for example
    ``{"yes_ask": 0.20, "yes_ask_size": 100}``, or an ``asks`` list of
    ``{"price": ..., "size": ...}`` levels.  ``existing_positions`` accepts
    ``shares`` plus ``cost_usdc`` (or an ``average_cost``).

    The supplied budget is an upper bound.  By default it covers the final
    basket cost, including historical cost.  ``budget_is_incremental=True``
    makes it cover only new buys, which is the execution meaning of a
    top-up request.  The planner maximises the common soft profit scale until
    budget or depth is reached; it never sells or invents a missing role.
    Lottery (``L``) buckets are ordinary settlement-floor legs: their amount
    is solved from the same floor and budget equations, rather than fixed at
    a notional.  ``lottery_budget_usdc`` remains as a compatibility parameter
    and is intentionally ignored.
    """

    budget = _finite(budget_usdc)
    safety_pad = _finite(safety_pad_usdc)
    lottery_budget = _finite(lottery_budget_usdc)
    minimum_order = _finite(minimum_order_shares)
    if budget is None or budget <= 0:
        return {"status": "blocked", "reason_codes": ["allocation_budget_invalid"]}
    if safety_pad is None or safety_pad < 0 or lottery_budget is None or lottery_budget < 0:
        return {"status": "blocked", "reason_codes": ["allocation_parameters_invalid"]}
    if minimum_order is None or minimum_order < 0:
        return {"status": "blocked", "reason_codes": ["allocation_minimum_order_invalid"]}
    profit_step = _finite(profit_order_step_shares)
    if profit_step is None or profit_step < 0:
        return {"status": "blocked", "reason_codes": ["allocation_profit_order_step_invalid"]}
    minimum_notional = _finite(minimum_order_notional_usdc)
    if minimum_notional is None or minimum_notional < 0:
        return {"status": "blocked", "reason_codes": ["allocation_minimum_order_notional_invalid"]}

    rows, role_issues = _normalise_buckets(buckets, roles)
    if role_issues:
        return {"status": "blocked", "reason_codes": role_issues}
    if not rows:
        return {"status": "blocked", "reason_codes": ["basket_buckets_missing"]}
    if not any(row.get("role") in FLOOR_ROLES for row in rows):
        return {"status": "blocked", "reason_codes": ["settlement_floor_roles_missing"]}

    quote_map = {_bucket_key(key): value for key, value in (quotes or {}).items()}
    visible_books: dict[str, _AskBook] = {}
    books: dict[str, _AskBook] = {}
    missing_quotes: list[str] = []
    for row in rows:
        bucket = str(row["bucket"])
        role = row.get("role")
        if role not in ALL_ROLES:
            continue
        book = _quote_levels(_mapping_value(quote_map, bucket))
        if book is None:
            missing_quotes.append(bucket)
        else:
            visible_books[bucket] = book
            # Every selected leg, including L, participates in the floor.
            books[bucket] = (
                _with_staged_tail(book)
                if allow_staged_depth and role in FLOOR_ROLES
                else book
            )
    if missing_quotes:
        return {
            "status": "blocked",
            "reason_codes": ["settlement_floor_quote_missing"],
            "missing_quote_buckets": missing_quotes,
        }

    floor_rows = [row for row in rows if row.get("role") in FLOOR_ROLES]
    best_ask_sum = sum(books[str(row["bucket"])].best_price for row in floor_rows)
    pending_only_gate_bypassed = bool(
        allow_staged_depth
        and allow_pending_only_when_ask_sum_ge_one
        and best_ask_sum >= 1.0 - EPS
    )
    if best_ask_sum >= 1.0 - EPS and not pending_only_gate_bypassed:
        return {
            "status": "blocked",
            "reason_codes": ["settlement_floor_best_ask_sum_ge_one"],
            "best_ask_sum": sum(visible_books[str(row["bucket"])].best_price for row in floor_rows),
        }

    positions = {_bucket_key(key): value for key, value in (existing_positions or {}).items()}
    existing: dict[str, tuple[float, float]] = {}
    missing_cost: list[str] = []
    for row in rows:
        bucket = str(row["bucket"])
        shares, cost = _position_values(_mapping_value(positions, bucket, {}))
        if shares > EPS and cost is None:
            missing_cost.append(bucket)
        existing[bucket] = (shares, float(cost or 0.0))
    if missing_cost:
        return {
            "status": "blocked",
            "reason_codes": ["existing_position_cost_missing"],
            "missing_cost_buckets": missing_cost,
        }

    weights = _soft_profit_weights(rows, books)
    # L is solved by _solve_quantities together with every other floor leg.
    # These zero-valued fields remain only for report/schema compatibility.
    lottery_rows: list[dict[str, Any]] = []
    lottery_total_cost = 0.0
    lottery_new_cost = 0.0

    floor = _solve_quantities(
        rows=rows,
        books=books,
        existing=existing,
        weights=weights,
        safety_pad=safety_pad,
        profit_scale=0.0,
        minimum_order_shares=minimum_order,
        profit_order_step_shares=profit_step,
        minimum_order_notional_usdc=minimum_notional,
    )
    if floor.get("status") != "ready":
        return floor
    floor_total = float(floor["total_cost_usdc"])
    floor_new_cost = sum(float(value) for value in floor.get("new_cost_by_bucket", {}).values())
    floor_spend = floor_new_cost + lottery_new_cost if budget_is_incremental else floor_total + lottery_total_cost
    if floor_spend > budget + 1e-7:
        return {
            "status": "blocked",
            "reason_codes": ["allocation_budget_below_settlement_floor"],
            "settlement_floor_cost_usdc": floor_total,
            "lottery_reserve_usdc": lottery_total_cost,
            "new_cost_usdc": floor_new_cost + lottery_new_cost,
            "budget_usdc": budget,
            "budget_is_incremental": bool(budget_is_incremental),
        }

    def evaluate(scale: float) -> dict[str, Any]:
        result = _solve_quantities(
            rows=rows,
            books=books,
            existing=existing,
            weights=weights,
            safety_pad=safety_pad,
            profit_scale=scale,
            minimum_order_shares=minimum_order,
            profit_order_step_shares=profit_step,
            minimum_order_notional_usdc=minimum_notional,
        )
        if result.get("status") != "ready":
            return result
        result_floor_new_cost = sum(float(value) for value in result.get("new_cost_by_bucket", {}).values())
        result_spend = (
            result_floor_new_cost + lottery_new_cost
            if budget_is_incremental
            else float(result["total_cost_usdc"]) + lottery_total_cost
        )
        result["new_cost_usdc"] = result_floor_new_cost + lottery_new_cost
        result["within_budget"] = result_spend <= budget + 1e-7
        return result

    best = floor
    lower = 0.0
    upper = 1.0
    upper_result: dict[str, Any] | None = None
    for _ in range(80):
        upper_result = evaluate(upper)
        if upper_result.get("status") != "ready" or not upper_result.get("within_budget"):
            break
        best = upper_result
        lower = upper
        upper *= 2.0
    else:
        upper_result = evaluate(upper)

    if upper_result is not None and (upper_result.get("status") != "ready" or not upper_result.get("within_budget")):
        for _ in range(80):
            midpoint = (lower + upper) / 2.0
            candidate = evaluate(midpoint)
            if candidate.get("status") == "ready" and candidate.get("within_budget"):
                lower = midpoint
                best = candidate
            else:
                upper = midpoint

    active_cost = float(best["total_cost_usdc"])
    total_cost = active_cost + lottery_total_cost
    total_incremental_cost = sum(float(value) for value in best.get("new_cost_by_bucket", {}).values()) + lottery_new_cost
    # Bisection can leave a sub-micro-unit overrun.  It is not an executable
    # spend and should not make a budget-bound result appear over budget.
    if total_cost > budget and total_cost - budget <= 1e-6:
        total_cost = budget
    target_rows: list[dict[str, Any]] = []
    for row in rows:
        bucket = str(row["bucket"])
        role = row.get("role")
        held_shares, held_cost = existing[bucket]
        if role in FLOOR_ROLES:
            staged_slice = build_staged_depth_slice(
                target_shares=float(best["new_shares_by_bucket"][bucket]),
                quote=_mapping_value(quote_map, bucket),
                minimum_order_shares=minimum_order,
            )
            if staged_slice.get("status") == "blocked":
                return staged_slice
            target_rows.append(
                {
                    "bucket": bucket,
                    "role": role,
                    "participates_in_settlement_floor": True,
                    "existing_shares": held_shares,
                    "existing_cost_usdc": held_cost,
                    "new_shares": float(best["new_shares_by_bucket"][bucket]),
                    "new_cost_usdc": float(best["new_cost_by_bucket"][bucket]),
                    "estimated_new_cost_usdc": float(best["new_cost_by_bucket"][bucket]),
                    "immediate_fak_shares": float(staged_slice["immediate_fak_shares"]),
                    "immediate_fak_cost_usdc": float(staged_slice["immediate_fak_cost_usdc"]),
                    "pending_gtc_shares": float(staged_slice["pending_gtc_shares"]),
                    "pending_gtc_reserve_usdc": float(staged_slice["pending_gtc_reserve_usdc"]),
                    "immediate_fak_limit_price": staged_slice["immediate_fak_limit_price"],
                    "gtc_limit_price": staged_slice["gtc_limit_price"],
                    "final_shares": float(best["final_shares_by_bucket"][bucket]),
                    "final_cost_usdc": held_cost + float(best["new_cost_by_bucket"][bucket]),
                    "settlement_profit_usdc": float(best["settlement_profit_by_bucket"][bucket]),
                    "projected_final_shares": float(best["final_shares_by_bucket"][bucket]),
                    "estimated_final_cost_usdc": held_cost + float(best["new_cost_by_bucket"][bucket]),
                    "projected_settlement_profit_usdc": float(best["settlement_profit_by_bucket"][bucket]),
                    "best_ask": books[bucket].best_price,
                    "available_depth_shares": visible_books[bucket].total_depth,
                }
            )
        elif role in COST_ONLY_ROLES:
            target_rows.append(
                {
                    "bucket": bucket,
                    "role": role,
                    "participates_in_settlement_floor": False,
                    "existing_shares": held_shares,
                    "existing_cost_usdc": held_cost,
                    "new_shares": 0.0,
                    "new_cost_usdc": 0.0,
                    "final_shares": held_shares,
                    "final_cost_usdc": held_cost,
                    "settlement_profit_usdc": None,
                    "allocation": "cost_only_no_floor_no_topup",
                }
            )
        else:
            target_rows.append(
                {
                    "bucket": bucket,
                    "role": None,
                    "participates_in_settlement_floor": False,
                    "existing_shares": held_shares,
                    "existing_cost_usdc": held_cost,
                    "new_shares": 0.0,
                    "new_cost_usdc": 0.0,
                    "final_shares": held_shares,
                    "final_cost_usdc": held_cost,
                    "settlement_profit_usdc": None,
                    "allocation": "not_allocated_role_missing",
                }
            )

    pending_floor_rows = [
        item for item in target_rows
        if item.get("participates_in_settlement_floor")
        and float(item.get("pending_gtc_shares") or 0.0) > EPS
    ]
    pending_gtc_shares = sum(float(item.get("pending_gtc_shares") or 0.0) for item in pending_floor_rows)
    pending_gtc_reserve = sum(float(item.get("pending_gtc_reserve_usdc") or 0.0) for item in pending_floor_rows)
    actual_floor_cost = sum(
        float(item.get("existing_cost_usdc") or 0.0)
        + float(item.get("immediate_fak_cost_usdc") or 0.0)
        for item in target_rows
        if item.get("participates_in_settlement_floor")
    )
    actual_floor_profit_by_bucket = {
        str(item["bucket"]): float(item.get("existing_shares") or 0.0)
        + float(item.get("immediate_fak_shares") or 0.0)
        - actual_floor_cost
        for item in target_rows
        if item.get("participates_in_settlement_floor")
    }
    actual_floor_profits = list(actual_floor_profit_by_bucket.values())
    staged_execution = bool(allow_staged_depth and pending_floor_rows)
    for item in target_rows:
        if not item.get("participates_in_settlement_floor"):
            continue
        if staged_execution:
            # A pending GTC is a plan/reserve only.  The current position,
            # current cost and current profit exclude it until a later run
            # confirms the fill and recomputes the basket.
            item["new_cost_usdc"] = float(item.get("immediate_fak_cost_usdc") or 0.0)
            item["final_shares"] = float(item.get("existing_shares") or 0.0) + float(item.get("immediate_fak_shares") or 0.0)
            item["final_cost_usdc"] = float(item.get("existing_cost_usdc") or 0.0) + float(item.get("immediate_fak_cost_usdc") or 0.0)
            item["settlement_profit_usdc"] = actual_floor_profit_by_bucket[str(item["bucket"])]

    # Keep the floor-local proof separate from the basket-wide settlement
    # result.  Lottery legs are excluded from the floor constraint, but their
    # cost is still part of the basket cost when reporting the profit of every
    # possible winning bucket.
    floor_profits = [
        float(item["settlement_profit_usdc"])
        for item in target_rows
        if item.get("participates_in_settlement_floor")
    ]
    actual_total_cost = sum(
        float(item.get("final_cost_usdc") or 0.0)
        for item in target_rows
        if item.get("role") in ALL_ROLES
    )
    projected_total_cost = total_cost
    for item in target_rows:
        if item.get("role") not in ALL_ROLES:
            continue
        floor_profit = item.get("settlement_profit_usdc") if item.get("participates_in_settlement_floor") else None
        projected_floor_profit = item.get("projected_settlement_profit_usdc") if item.get("participates_in_settlement_floor") else None
        item["floor_settlement_profit_usdc"] = floor_profit
        item["projected_floor_settlement_profit_usdc"] = projected_floor_profit
        actual_shares = float(item.get("final_shares") or 0.0)
        projected_shares = float(item.get("projected_final_shares", item.get("final_shares")) or 0.0)
        item["settlement_profit_usdc"] = actual_shares - actual_total_cost
        item["projected_settlement_profit_usdc"] = projected_shares - projected_total_cost

    basket_profits = [
        float(item["settlement_profit_usdc"])
        for item in target_rows
        if item.get("role") in ALL_ROLES and item.get("settlement_profit_usdc") is not None
    ]
    projected_basket_profits = [
        float(item["projected_settlement_profit_usdc"])
        for item in target_rows
        if item.get("role") in ALL_ROLES and item.get("projected_settlement_profit_usdc") is not None
    ]
    target_cost_by_role: dict[str, float] = {}
    new_cost_by_role: dict[str, float] = {}
    final_shares_by_role: dict[str, float] = {}
    profit_by_role: dict[str, list[float]] = {}
    for item in target_rows:
        role = item.get("role")
        if role not in ALL_ROLES:
            continue
        target_cost_by_role[role] = target_cost_by_role.get(role, 0.0) + float(item.get("final_cost_usdc", 0.0))
        new_cost_by_role[role] = new_cost_by_role.get(role, 0.0) + float(item.get("new_cost_usdc", 0.0))
        final_shares_by_role[role] = final_shares_by_role.get(role, 0.0) + float(item.get("final_shares", 0.0))
        profit = item.get("settlement_profit_usdc")
        if profit is not None:
            profit_by_role.setdefault(str(role), []).append(float(profit))

    actual_profit_by_role = {
        role: (max(values) if role == "F" else values[0])
        for role, values in profit_by_role.items()
        if values
    }

    def _ratio(numerator: float | None, denominator: float | None) -> float | None:
        if numerator is None or denominator is None or abs(denominator) <= EPS:
            return None
        return numerator / denominator

    ratio_actual = {
        "M_to_S_funding": _ratio(target_cost_by_role.get("M"), target_cost_by_role.get("S")),
        "M_to_S_profit": _ratio(
            next((item["settlement_profit_usdc"] for item in target_rows if item.get("role") == "M"), None),
            next((item["settlement_profit_usdc"] for item in target_rows if item.get("role") == "S"), None),
        ),
        "A_to_B_shares": _ratio(final_shares_by_role.get("A"), final_shares_by_role.get("B")),
        "A_to_S_funding": _ratio(target_cost_by_role.get("A"), target_cost_by_role.get("S")),
    }
    soft_relaxations: list[str] = []
    if ratio_actual["M_to_S_funding"] is not None and abs(ratio_actual["M_to_S_funding"] - 2.0) > 0.10:
        soft_relaxations.append("M_to_S_funding_ratio_relaxed")
    if ratio_actual["M_to_S_profit"] is not None and abs(ratio_actual["M_to_S_profit"] - 2.0) > 0.10:
        soft_relaxations.append("M_to_S_profit_ratio_relaxed")
    if ratio_actual["A_to_B_shares"] is not None and abs(ratio_actual["A_to_B_shares"] - 2.0) > 0.10:
        soft_relaxations.append("A_to_B_share_ratio_relaxed")
    profit_order_roles = [role for role in ("M", "S", "A", "B", "F") if role in actual_profit_by_role]
    profit_order_ok = all(
        actual_profit_by_role[profit_order_roles[index]]
        > actual_profit_by_role[profit_order_roles[index + 1]] + 1e-7
        for index in range(len(profit_order_roles) - 1)
    )
    if not profit_order_ok and len(profit_order_roles) > 1:
        soft_relaxations.append("profit_order_relaxed_by_floor_or_existing_position")

    return {
        "status": "ready",
        "policy_id": SETTLEMENT_FLOOR_POLICY_ID,
        "mode": "settlement_floor_biased_basket",
        "budget_usdc": budget,
        "budget_is_upper_bound": True,
        "budget_is_incremental": bool(budget_is_incremental),
        "safety_pad_usdc": safety_pad,
        "lottery_budget_per_bucket_usdc": lottery_budget,
        "minimum_order_shares": minimum_order,
        "minimum_order_notional_usdc": minimum_notional,
        "allow_staged_depth": bool(allow_staged_depth),
        "allow_pending_only_when_ask_sum_ge_one": bool(allow_pending_only_when_ask_sum_ge_one),
        "pending_only_gate_bypassed": pending_only_gate_bypassed,
        "execution_mode": "staged_fak_then_gtc" if staged_execution else "fak_complete",
        "target_rows": target_rows,
        "roles": {item["bucket"]: item.get("role") for item in target_rows if item.get("role")},
        "settlement_floor_cost_usdc": floor_total,
        "settlement_basket_cost_usdc": actual_floor_cost,
        "estimated_settlement_basket_cost_usdc": active_cost,
        "lottery_reserve_usdc": lottery_total_cost,
        "lottery_new_cost_usdc": lottery_new_cost,
        "total_cost_usdc": total_cost,
        "total_incremental_cost_usdc": total_incremental_cost,
        "actual_incremental_cost_usdc": sum(float(item.get("new_cost_usdc") or 0.0) for item in target_rows),
        "remaining_budget_usdc": max(0.0, budget - (total_incremental_cost if budget_is_incremental else total_cost)),
        "unused_budget_usdc": max(0.0, budget - (total_incremental_cost if budget_is_incremental else total_cost)),
        "reference_residual_budget_usdc": max(0.0, budget - (floor_new_cost + lottery_new_cost if budget_is_incremental else floor_total + lottery_total_cost)),
        "reference_residual_budget_by_role": {
            role: max(
                0.0,
                budget - (
                    floor_new_cost + lottery_new_cost
                    if budget_is_incremental
                    else floor_total + lottery_total_cost
                ),
            )
            * ratio
            / sum(REFERENCE_FUNDING_RATIOS.values())
            for role, ratio in REFERENCE_FUNDING_RATIOS.items()
            if any(item.get("role") == role for item in target_rows)
        },
        "best_ask_sum_floor_buckets": best_ask_sum,
        "profit_scale": float(best.get("profit_scale", 0.0)),
        "settlement_profit_by_bucket": {
            item["bucket"]: item["settlement_profit_usdc"]
            for item in target_rows
            if item.get("role") in ALL_ROLES
        },
        "projected_settlement_profit_by_bucket": {
            item["bucket"]: item.get("projected_settlement_profit_usdc", item.get("settlement_profit_usdc"))
            for item in target_rows
            if item.get("role") in ALL_ROLES
        },
        "minimum_settlement_profit_usdc": min(floor_profits) if floor_profits else None,
        "maximum_settlement_profit_usdc": max(floor_profits) if floor_profits else None,
        "projected_minimum_settlement_profit_usdc": min(
            [
                float(item.get("projected_floor_settlement_profit_usdc"))
                for item in target_rows
                if item.get("participates_in_settlement_floor") and item.get("projected_floor_settlement_profit_usdc") is not None
            ]
        ) if any(item.get("participates_in_settlement_floor") and item.get("projected_floor_settlement_profit_usdc") is not None for item in target_rows) else None,
        "projected_maximum_settlement_profit_usdc": max(
            [
                float(item.get("projected_floor_settlement_profit_usdc"))
                for item in target_rows
                if item.get("participates_in_settlement_floor") and item.get("projected_floor_settlement_profit_usdc") is not None
            ]
        ) if any(item.get("participates_in_settlement_floor") and item.get("projected_floor_settlement_profit_usdc") is not None for item in target_rows) else None,
        "minimum_floor_settlement_profit_usdc": min(floor_profits) if floor_profits else None,
        "maximum_floor_settlement_profit_usdc": max(floor_profits) if floor_profits else None,
        "actual_total_basket_cost_usdc": actual_total_cost,
        "projected_total_basket_cost_usdc": projected_total_cost,
        "actual_basket_settlement_profit_by_bucket": {
            item["bucket"]: item["settlement_profit_usdc"]
            for item in target_rows
            if item.get("role") in ALL_ROLES
        },
        "projected_basket_settlement_profit_by_bucket": {
            item["bucket"]: item["projected_settlement_profit_usdc"]
            for item in target_rows
            if item.get("role") in ALL_ROLES
        },
        # Compatibility fields: these remain floor-only because callers use
        # them for the hard floor gate.  The basket-wide values above include
        # lottery legs and their cost, and are the reporting values for total
        # minimum/maximum settlement profit.
        "floor_minimum_settlement_profit_usdc": min(floor_profits) if floor_profits else None,
        "floor_maximum_settlement_profit_usdc": max(floor_profits) if floor_profits else None,
        "basket_minimum_settlement_profit_usdc": min(basket_profits) if basket_profits else None,
        "basket_maximum_settlement_profit_usdc": max(basket_profits) if basket_profits else None,
        "projected_basket_minimum_settlement_profit_usdc": min(projected_basket_profits) if projected_basket_profits else None,
        "projected_basket_maximum_settlement_profit_usdc": max(projected_basket_profits) if projected_basket_profits else None,
        "actual_settlement_floor_cost_usdc": actual_floor_cost,
        "actual_settlement_profit_by_bucket_excluding_pending": actual_floor_profit_by_bucket,
        "actual_minimum_settlement_profit_usdc": min(actual_floor_profits) if actual_floor_profits else None,
        "actual_maximum_settlement_profit_usdc": max(actual_floor_profits) if actual_floor_profits else None,
        "settlement_floor_proof_status": (
            "pending_orders_excluded_not_proven"
            if staged_execution
            else "proven_on_current_positions_and_cost"
        ),
        "pending_gtc_shares": pending_gtc_shares,
        "pending_gtc_reserve_usdc": pending_gtc_reserve,
        "pending_orders_count_as_floor": False,
        "pending_orders_reserve_budget": True,
        "recompute_after_fill": True,
        "target_cost_by_role": target_cost_by_role,
        "new_cost_by_role": new_cost_by_role,
        "final_shares_by_role": final_shares_by_role,
        "actual_profit_by_role": actual_profit_by_role,
        "profit_order_roles_present": profit_order_roles,
        "profit_order_ok": profit_order_ok,
        "core_profit_sources": [role for role in ("M", "S", "A") if role in actual_profit_by_role],
        "reference_funding_ratios": dict(REFERENCE_FUNDING_RATIOS),
        "actual_ratios": ratio_actual,
        "soft_targets": {
            "M_to_S_funding": 2.0,
            "M_to_S_profit": 2.0,
            "A_to_B_shares": 2.0,
            "A_to_S_funding": 1.0 / 3.0,
            "profit_order": ["M", "S", "A", "B", "F"],
        },
        "soft_relaxations": soft_relaxations,
        "unassigned_buckets": [item["bucket"] for item in target_rows if item.get("role") is None],
        "reason_codes": soft_relaxations,
    }


def load_settlement_floor_policy(path: Path | str = DEFAULT_SETTLEMENT_FLOOR_POLICY_PATH) -> dict[str, Any]:
    """Load the persisted calculation rules without making them executable."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    if not isinstance(value, Mapping):
        return {}
    return dict(value)


__all__ = [
    "ALL_ROLES",
    "DEFAULT_SETTLEMENT_FLOOR_POLICY_PATH",
    "FLOOR_ROLES",
    "REFERENCE_FUNDING_RATIOS",
    "SETTLEMENT_FLOOR_POLICY_ID",
    "SETTLEMENT_FLOOR_POLICY_SCHEMA",
    "build_staged_depth_slice",
    "build_settlement_floor_plan",
    "load_settlement_floor_policy",
]
