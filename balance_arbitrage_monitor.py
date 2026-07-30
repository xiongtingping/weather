"""Pure maker-limit calculations for the ZUUU complete-set monitor.

This module has no network, database, persistence, or order-entry code.  A
``signal`` means that all proposed limit orders can rest as makers and would
have positive complete-set economics *if every leg eventually fills*.  It is
not an atomic-fill guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping, Sequence


EPSILON = 1e-12


@dataclass(frozen=True)
class FeeSpec:
    """Official CLOB fee evidence for one token."""

    rate: float
    exponent: float
    taker_only: bool

    def __post_init__(self) -> None:
        if not math.isfinite(self.rate) or not 0.0 <= self.rate < 1.0:
            raise ValueError("fee_rate_invalid")
        if not math.isfinite(self.exponent) or self.exponent < 0.0:
            raise ValueError("fee_exponent_invalid")

    def maker_fee_per_share(self, price: float) -> float:
        value = float(price)
        if not math.isfinite(value) or not 0.0 < value < 1.0:
            raise ValueError("fee_price_invalid")
        if not self.taker_only:
            raise ValueError("maker_fee_zero_not_proven")
        # Do not count a possible maker rebate as profit.  Official
        # ``taker_only=true`` is sufficient evidence for a zero maker fee.
        return 0.0


@dataclass(frozen=True)
class MarketBinding:
    slug: str
    event_id: str
    event_date: str
    outcome: str
    condition_id: str
    yes_token: str
    no_token: str
    kind: str
    lower: float | None
    upper: float | None


@dataclass(frozen=True)
class PriceLevel:
    price: float
    size: float


@dataclass(frozen=True)
class MakerLeg:
    outcome: str
    side: str
    token_id: str
    order_price: float
    quote_mode: str
    best_bid: float | None
    best_ask: float | None
    queue_ahead_shares: float
    fee: FeeSpec


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def normalize_levels(
    rows: Iterable[Mapping[str, Any]], *, descending: bool = False
) -> tuple[PriceLevel, ...]:
    by_price: dict[float, float] = {}
    for raw in rows:
        price = _finite_float(raw.get("price"))
        size = _finite_float(raw.get("size"))
        if price is None or size is None or not 0.0 < price < 1.0 or size <= 0.0:
            continue
        by_price[price] = by_price.get(price, 0.0) + size
    return tuple(
        PriceLevel(price=price, size=by_price[price])
        for price in sorted(by_price, reverse=descending)
    )


def validate_exhaustive_partition(markets: Sequence[MarketBinding]) -> tuple[bool, str]:
    rows = list(markets)
    if len(rows) < 2:
        return False, "partition_requires_at_least_two_buckets"
    if len({row.slug for row in rows}) != 1 or len({row.event_id for row in rows}) != 1:
        return False, "partition_event_identity_mismatch"
    if len({row.condition_id for row in rows}) != len(rows):
        return False, "partition_condition_not_unique"
    tokens = [token for row in rows for token in (row.yes_token, row.no_token)]
    if any(not token for token in tokens) or len(set(tokens)) != len(tokens):
        return False, "partition_token_mapping_invalid"
    lower_tails = [row for row in rows if row.kind == "lower_tail"]
    upper_tails = [row for row in rows if row.kind == "upper_tail"]
    singles = [row for row in rows if row.kind == "single"]
    if len(lower_tails) != 1 or len(upper_tails) != 1:
        return False, "partition_tail_count_invalid"
    lower_end, upper_start = lower_tails[0].upper, upper_tails[0].lower
    if lower_end is None or upper_start is None:
        return False, "partition_tail_boundary_missing"
    if not float(lower_end).is_integer() or not float(upper_start).is_integer():
        return False, "partition_not_whole_degree"
    if upper_start <= lower_end:
        return False, "partition_tail_order_invalid"
    expected = list(range(int(lower_end) + 1, int(upper_start)))
    actual: list[int] = []
    for row in singles:
        if row.lower is None or row.upper is None or row.lower != row.upper:
            return False, "partition_single_boundary_invalid"
        if not float(row.lower).is_integer():
            return False, "partition_not_whole_degree"
        actual.append(int(row.lower))
    if sorted(actual) != expected or len(actual) != len(set(actual)):
        return False, "partition_has_gap_or_overlap"
    return True, "ok"


def _tick_add(price: float, tick_size: float) -> float:
    digits = max(0, min(9, int(math.ceil(-math.log10(tick_size))) if tick_size < 1 else 0))
    return round(price + tick_size, digits)


def _tick_subtract(price: float, tick_size: float) -> float:
    digits = max(0, min(9, int(math.ceil(-math.log10(tick_size))) if tick_size < 1 else 0))
    return round(price - tick_size, digits)


def propose_maker_leg(
    *,
    outcome: str,
    side: str,
    token_id: str,
    book: Mapping[str, Any],
    fee: FeeSpec,
    tick_size: float,
) -> MakerLeg | None:
    """Propose a non-crossing maker BUY, improving best bid by one tick."""

    tick = float(tick_size)
    if not math.isfinite(tick) or not 0.0 < tick < 1.0:
        raise ValueError(f"tick_size_invalid:{token_id}")
    bids = normalize_levels(book.get("bids") or [], descending=True)
    asks = normalize_levels(book.get("asks") or [])
    best_bid = bids[0].price if bids else None
    best_ask = asks[0].price if asks else None
    order_price: float | None = None
    quote_mode = ""
    queue_ahead = 0.0
    if best_bid is not None:
        improved = _tick_add(best_bid, tick)
        if improved < 1.0 - EPSILON and (best_ask is None or improved < best_ask - EPSILON):
            order_price = improved
            quote_mode = "improve_best_bid_one_tick"
        else:
            order_price = best_bid
            quote_mode = "join_best_bid"
            queue_ahead = bids[0].size
    elif best_ask is not None:
        seeded = _tick_subtract(best_ask, tick)
        if seeded > EPSILON:
            order_price = seeded
            quote_mode = "seed_one_tick_below_best_ask"
    if order_price is None or not 0.0 < order_price < 1.0:
        return None
    if best_ask is not None and order_price >= best_ask - EPSILON:
        return None
    # Calling this also proves the official fee evidence is maker-safe.
    fee.maker_fee_per_share(order_price)
    return MakerLeg(
        outcome=outcome,
        side=side,
        token_id=token_id,
        order_price=order_price,
        quote_mode=quote_mode,
        best_bid=best_bid,
        best_ask=best_ask,
        queue_ahead_shares=queue_ahead,
        fee=fee,
    )


def evaluate_complete_set(
    *,
    name: str,
    legs: Sequence[MakerLeg | None],
    payout_per_share: float,
    target_shares: float,
    minimum_edge: float,
    minimum_order_shares: float,
) -> dict[str, Any]:
    if target_shares <= 0.0 or minimum_order_shares <= 0.0:
        raise ValueError("share_size_invalid")
    if minimum_edge < 0.0 or payout_per_share <= 0.0:
        raise ValueError("complete_set_threshold_invalid")
    complete = bool(legs) and all(leg is not None for leg in legs)
    result: dict[str, Any] = {
        "name": name,
        "leg_count": len(legs),
        "order_style": "maker_limit_buy",
        "fill_contingent": True,
        "payout_per_complete_set_usdc": payout_per_share,
        "target_shares": target_shares,
        "minimum_edge_per_set_usdc": minimum_edge,
        "minimum_order_shares": minimum_order_shares,
        "maker_quotes_complete": complete,
        "signal": False,
        "legs": [],
    }
    if not complete:
        result["block_reason"] = "maker_quote_evidence_incomplete"
        return result
    complete_legs = [leg for leg in legs if leg is not None]
    leg_rows = [
        {
            "outcome": leg.outcome,
            "side": leg.side,
            "token_id": leg.token_id,
            "order_price": leg.order_price,
            "quote_mode": leg.quote_mode,
            "best_bid": leg.best_bid,
            "best_ask": leg.best_ask,
            "queue_ahead_shares": leg.queue_ahead_shares,
            "maker_fee_per_share_usdc": leg.fee.maker_fee_per_share(leg.order_price),
        }
        for leg in complete_legs
    ]
    price_sum = sum(leg.order_price for leg in complete_legs)
    fee_per_set = sum(leg.fee.maker_fee_per_share(leg.order_price) for leg in complete_legs)
    all_in_per_set = price_sum + fee_per_set
    edge = payout_per_share - all_in_per_set
    notional = price_sum * target_shares
    fee = fee_per_set * target_shares
    result.update(
        {
            "legs": leg_rows,
            "maker_price_sum": price_sum,
            "maker_fee_per_set_usdc": fee_per_set,
            "maker_all_in_per_set_usdc": all_in_per_set,
            "net_edge_per_set_usdc": edge,
            "notional_usdc": notional,
            "fee_usdc": fee,
            "payout_usdc": payout_per_share * target_shares,
            "net_profit_if_all_filled_usdc": edge * target_shares,
            "queue_ahead_shares_total": sum(leg.queue_ahead_shares for leg in complete_legs),
            "signal": edge + EPSILON >= minimum_edge and target_shares + EPSILON >= minimum_order_shares,
            "block_reason": None if edge + EPSILON >= minimum_edge else "maker_edge_below_threshold",
        }
    )
    return result


def build_complete_sets(
    markets: Sequence[MarketBinding],
    books_by_token: Mapping[str, Mapping[str, Any]],
    fees_by_token: Mapping[str, FeeSpec],
    ticks_by_token: Mapping[str, float],
    *,
    target_shares: float,
    minimum_edge: float,
    minimum_order_shares: float,
) -> dict[str, Any]:
    rows = list(markets)

    def leg(row: MarketBinding, side: str) -> MakerLeg | None:
        token = row.yes_token if side == "YES" else row.no_token
        fee = fees_by_token.get(token)
        tick = ticks_by_token.get(token)
        if fee is None:
            raise ValueError(f"fee_evidence_missing:{token}")
        if tick is None:
            raise ValueError(f"tick_evidence_missing:{token}")
        return propose_maker_leg(
            outcome=row.outcome,
            side=side,
            token_id=token,
            book=books_by_token.get(token, {}),
            fee=fee,
            tick_size=tick,
        )

    pairs = [
        {
            "outcome": row.outcome,
            **evaluate_complete_set(
                name=f"PAIR:{row.outcome}",
                legs=[leg(row, "YES"), leg(row, "NO")],
                payout_per_share=1.0,
                target_shares=target_shares,
                minimum_edge=minimum_edge,
                minimum_order_shares=minimum_order_shares,
            ),
        }
        for row in rows
    ]
    partition_ok, partition_reason = validate_exhaustive_partition(rows)
    result: dict[str, Any] = {
        "pairs": pairs,
        "partition_ok": partition_ok,
        "partition_reason": partition_reason,
        "all_yes": None,
        "all_no": None,
    }
    if not partition_ok:
        return result
    result["all_yes"] = evaluate_complete_set(
        name="ALL_YES",
        legs=[leg(row, "YES") for row in rows],
        payout_per_share=1.0,
        target_shares=target_shares,
        minimum_edge=minimum_edge,
        minimum_order_shares=minimum_order_shares,
    )
    result["all_no"] = evaluate_complete_set(
        name="ALL_NO",
        legs=[leg(row, "NO") for row in rows],
        payout_per_share=float(len(rows) - 1),
        target_shares=target_shares,
        minimum_edge=minimum_edge,
        minimum_order_shares=minimum_order_shares,
    )
    return result


__all__ = [
    "FeeSpec",
    "MakerLeg",
    "MarketBinding",
    "PriceLevel",
    "build_complete_sets",
    "evaluate_complete_set",
    "normalize_levels",
    "propose_maker_leg",
    "validate_exhaustive_partition",
]
