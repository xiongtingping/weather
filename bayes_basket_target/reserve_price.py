"""Decimal-safe reserve-price pre-sizing primitive.

This module deliberately has no runtime, database, market, or gateway
dependencies.  A caller must use the returned price to refresh its captured
depth and sizing whenever that price changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    Decimal,
    InvalidOperation,
    ROUND_FLOOR,
    localcontext,
)
from typing import Any


class ReservePriceError(ValueError):
    """Raised when reserve-price inputs cannot safely produce a cap."""


def _decimal(value: Any, *, name: str) -> Decimal:
    """Convert a scalar without importing binary float error into Decimal."""

    if isinstance(value, bool) or value is None:
        raise ReservePriceError(f"{name}_invalid")
    try:
        converted = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReservePriceError(f"{name}_invalid") from exc
    if not converted.is_finite():
        raise ReservePriceError(f"{name}_nonfinite")
    if converted < 0:
        raise ReservePriceError(f"{name}_negative")
    return converted


def _precision(*values: Decimal) -> int:
    """Return enough precision for exact addition/subtraction/multiplication."""

    required = 1
    for value in values:
        digits = len(value.as_tuple().digits) or 1
        required = max(required, digits + max(value.as_tuple().exponent, 0) + 2)
    return max(28, required + 8)


def tick_down(value: Any, tick_size: Any) -> Decimal:
    """Floor a non-negative value to the exchange tick, using Decimal math."""

    amount = _decimal(value, name="value")
    tick = _decimal(tick_size, name="tick_size")
    if tick <= 0:
        raise ReservePriceError("tick_size_nonpositive")
    with localcontext() as context:
        context.prec = _precision(amount, tick)
        ticks = (amount / tick).to_integral_value(rounding=ROUND_FLOOR)
        return ticks * tick


@dataclass(frozen=True)
class ReservePriceResult:
    """Computed reserve cap and the re-sizing obligation it creates."""

    reserve_price: Decimal
    r_math: Decimal
    learned_cap: Decimal
    tick_size: Decimal
    price_changed: bool
    requires_depth_resizing: bool
    requires_sizing_re_evaluation: bool

    @property
    def requires_re_evaluation(self) -> bool:
        """Combined caller obligation for a changed reserve price."""

        return self.requires_depth_resizing or self.requires_sizing_re_evaluation


def presize_reserve_price(
    *,
    r_math: Any,
    p_eligible: Any,
    learned_margin: Any,
    tick_size: Any,
    previous_reserve_price: Any | None = None,
) -> ReservePriceResult:
    """Compute ``r_final = tick_down(min(r_math, p_eligible-margin))``.

    The learned cap can only tighten the mathematical cap.  Negative or
    non-finite values fail closed, including a margin that makes the learned
    cap negative.  ``previous_reserve_price`` is optional; when supplied it
    lets the caller receive an explicit re-sizing obligation only on change.
    The obligation is true for a first computation because no prior sizing is
    safe to reuse.
    """

    math_cap = _decimal(r_math, name="r_math")
    eligible = _decimal(p_eligible, name="p_eligible")
    margin = _decimal(learned_margin, name="learned_margin")
    tick = _decimal(tick_size, name="tick_size")
    if tick <= 0:
        raise ReservePriceError("tick_size_nonpositive")

    with localcontext() as context:
        context.prec = _precision(math_cap, eligible, margin, tick)
        learned_cap = eligible - margin
        if learned_cap < 0:
            raise ReservePriceError("learned_cap_negative")
        uncapped = min(math_cap, learned_cap)
        final_price = tick_down(uncapped, tick)

    if previous_reserve_price is None:
        changed = True
    else:
        previous = _decimal(previous_reserve_price, name="previous_reserve_price")
        changed = final_price != previous

    return ReservePriceResult(
        reserve_price=final_price,
        r_math=math_cap,
        learned_cap=learned_cap,
        tick_size=tick,
        price_changed=changed,
        requires_depth_resizing=changed,
        requires_sizing_re_evaluation=changed,
    )


compute_final_reserve_price = presize_reserve_price


__all__ = [
    "ReservePriceError",
    "ReservePriceResult",
    "compute_final_reserve_price",
    "presize_reserve_price",
    "tick_down",
]
