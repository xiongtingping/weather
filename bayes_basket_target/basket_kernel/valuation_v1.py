"""Charter 132 kernel half: the valuation lane's pure arithmetic.

Everything here is a function of its arguments -- no database, no clock --
because the runtime that prices a snapshot must not import a store (the rule
``BETA_TABLE_KEY`` already lives under).  The store
(``bayes_basket_target.valuation_store``) fits, persists and loads; this module
is what both the fit and the enumeration compute with, so the two cannot hold
two copies of the market reference or the pool.

See ``valuation_store``'s module docstring for the design: the market's book as
a second forecast, one pooled vector per snapshot, conservatism on the action.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

VALUATION_RULE = "linear_pool_market_weather_v1"
MARKET_REFERENCE_RULE = "normalised_mid_v1"
# Where the valuation lane travels in the served-tables mapping.  Not a stage
# and not a band table; readers that iterate the mapping as band tables skip it.
VALUATION_TABLE_KEY = "valuation"
LCB_QUANTILE = 0.05
# ``off``: not read.  ``shadow`` (default): every priced window records the
# pooled mass, the market mass, the action's value bounds, its capacity and the
# verdict the gate would reach on them; nothing gates.  ``on``: the gate reads
# the action's lower bound and the solver sizes on the pooled mass -- and a
# stage without a served, non-accumulating fit AND an ``admitted`` walk-forward
# verdict holds entirely.  There is no fallback to the claim chain under ``on``:
# a stage the valuation lane cannot vouch for is a stage that does not trade.
VALUATION_MODE = (
    os.environ.get("PM_VALUATION_MODE", "").strip().lower() or "shadow"
)
# Charter 230: the L4 posterior is the basket's one belief; the pooled
# valuation is a shadow reading only (its ``on`` gate is retired).
if VALUATION_MODE not in {"off", "shadow"}:
    raise RuntimeError(f"unknown valuation mode: {VALUATION_MODE}")

# Market reference completeness.  A tail bucket quoted only on the ask at a
# price this small is effectively a zero-probability outcome with a lottery
# ticket on it; its mid is bounded in [0, ask] and taken as ask/2, an error of
# at most half the ask.  Measured 2026-09-13 on the last 3,000 official WS
# snapshots: 12,531 one-sided-ask buckets, p50 0.001, p95 0.03, p99 0.05, max
# 0.20.  At 0.02 one such bucket refused 15% of whole snapshots and the fit's
# sample became "days whose tails happened to be quoted tightly"; at 0.05 it
# admits 99.2% of them with a per-bucket error bound of 0.025.  The mirror for
# a nearly settled bucket quoted only on the bid.  Anything else one-sided or
# empty means the book does not describe the whole market, and the reference
# refuses rather than normalising the rest into "the market".
ONE_SIDED_TAIL_MAX_ASK = 0.05
NEAR_CERTAIN_MIN_BID = 0.98
MAX_BOOK_SKEW_SECONDS = 120.0


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for level in raw or ():
        price = _field(level, "price")
        shares = _field(level, "shares", 0.0)
        try:
            price_f = float(price)
            shares_f = float(shares or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price_f) and price_f > 0.0 and shares_f > 0.0:
            out.append((price_f, shares_f))
    return out


def _parse_instant(text: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def market_reference_v1(buckets: Iterable[Any]) -> dict[str, Any]:
    """The market's probability vector, or a refusal that says why.

    ``buckets`` is the whole contract's ladder -- ``BucketObservation`` rows at
    run time, or the official WS snapshot's bucket mappings in history; both
    carry ask/bid levels and the unmaterialised flag, and the snapshot rows
    additionally carry each token's own book time, which is checked when every
    bucket has one.  The runtime rows have no per-token time; the snapshot's
    freshness contract has already bounded the whole quote there.
    """

    mids: dict[str, float] = {}
    spreads: list[float] = []
    # What crossing the book costs on each bucket, from the mid to the side a
    # trade would hit: half the spread when both sides quote, and the whole
    # distance from the synthesised mid to the one side that does.  Read by
    # the charter EV page to price the disagreement strategy's spread cost
    # beside its expected profit; nothing gates on it.
    half_spread: dict[str, float] = {}
    one_sided: list[str] = []
    stamps: list[datetime] = []
    missing_stamp = False
    count = 0
    for row in buckets:
        count += 1
        bucket = str(_field(row, "bucket"))
        if bool(_field(row, "unmaterialized_book", False)):
            return {"state": "incomplete", "reason": f"unmaterialized:{bucket}"}
        asks = _levels(_field(row, "ask_levels"))
        ask_available = _field(row, "ask_available", bool(asks))
        best_ask = min((p for p, _s in asks), default=None) if ask_available else None
        bids = _levels(_field(row, "bid_levels"))
        best_bid = max((p for p, _s in bids), default=None)
        if best_bid is None:
            try:
                raw_bid = float(_field(row, "bid_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                raw_bid = 0.0
            if _field(row, "bid_available", raw_bid > 0.0) and raw_bid > 0.0:
                best_bid = raw_bid
        if best_ask is not None and best_bid is not None:
            if best_bid > best_ask + 1e-12:
                return {"state": "incomplete", "reason": f"crossed_book:{bucket}"}
            mids[bucket] = 0.5 * (best_ask + best_bid)
            spreads.append(best_ask - best_bid)
            half_spread[bucket] = 0.5 * (best_ask - best_bid)
        elif best_ask is not None:
            if best_ask > ONE_SIDED_TAIL_MAX_ASK:
                return {"state": "incomplete", "reason": f"one_sided_ask:{bucket}"}
            mids[bucket] = 0.5 * best_ask
            half_spread[bucket] = 0.5 * best_ask
            one_sided.append(bucket)
        elif best_bid is not None:
            if best_bid < NEAR_CERTAIN_MIN_BID:
                return {"state": "incomplete", "reason": f"one_sided_bid:{bucket}"}
            mids[bucket] = 0.5 * (best_bid + 1.0)
            half_spread[bucket] = 0.5 * (1.0 - best_bid)
            one_sided.append(bucket)
        else:
            return {"state": "incomplete", "reason": f"empty_book:{bucket}"}
        stamp = _parse_instant(_field(row, "book_observed_at"))
        if stamp is None:
            missing_stamp = True
        else:
            stamps.append(stamp)
    if count == 0:
        return {"state": "incomplete", "reason": "no_buckets"}
    skew = None
    if stamps and not missing_stamp:
        skew = (max(stamps) - min(stamps)).total_seconds()
        if skew > MAX_BOOK_SKEW_SECONDS:
            return {
                "state": "incomplete", "reason": "book_skew",
                "book_skew_seconds": skew,
            }
    total = sum(mids.values())
    if not (total > 0.0 and math.isfinite(total)):
        return {"state": "incomplete", "reason": "no_mass"}
    return {
        "state": "complete",
        "reason": None,
        "rule": MARKET_REFERENCE_RULE,
        "vector": {bucket: mid / total for bucket, mid in mids.items()},
        "mid_sum": total,
        "max_spread": max(spreads) if spreads else None,
        # On the vector's scale (divided by the same total), so a cost read
        # against ``vector`` is in the same units as the vector.
        "half_spread": {
            bucket: value / total for bucket, value in half_spread.items()
        },
        "one_sided_buckets": one_sided,
        "book_skew_seconds": skew,
    }


def valuation_vector_v1(
    p_weather: Mapping[str, float],
    m_market: Mapping[str, float],
    beta: float,
    *,
    excluded: Iterable[str] = (),
) -> dict[str, float] | None:
    """``(1 - beta) * m + beta * p`` on the market's contract, hard facts at 0.

    The contract is the market's bucket set: a bucket the book lists is an
    outcome that can settle.  A bucket a hard fact has excluded -- the running
    high already past its upper edge -- is zeroed on BOTH sources before the
    pool and the result renormalised, so a lagging book cannot hand a settled
    loss any probability.  None when nothing is left.
    """

    b = min(max(float(beta), 0.0), 1.0)
    dead = {str(x) for x in excluded}
    raw: dict[str, float] = {}
    for bucket, m in m_market.items():
        key = str(bucket)
        if key in dead:
            raw[key] = 0.0
            continue
        p = float(p_weather.get(key, 0.0) or 0.0)
        raw[key] = max(0.0, (1.0 - b) * float(m) + b * max(0.0, p))
    total = sum(raw.values())
    if not (total > 0.0 and math.isfinite(total)):
        return None
    return {bucket: value / total for bucket, value in raw.items()}


def window_mass(vector: Mapping[str, float], window: Iterable[str]) -> float:
    return float(sum(float(vector.get(str(bucket), 0.0) or 0.0) for bucket in window))


def ladder_cost_v1(
    ask_levels: Sequence[tuple[float, float]], shares: float,
) -> tuple[float, float]:
    """Cash to buy ``shares`` walking the ask ladder, and the shares it holds.

    Returns ``(cost, fillable)``; ``fillable < shares`` means the visible book
    ends first, and the cost is for what it holds.  Fees are zero on this
    venue's weather markets (config/trading/basket_yes_v1.json); a non-zero fee
    belongs to the caller's cost, never to a belief.
    """

    want = max(0.0, float(shares))
    cost = 0.0
    got = 0.0
    for price, depth in sorted(ask_levels):
        if got >= want - 1e-12:
            break
        take = min(depth, want - got)
        cost += take * price
        got += take
    return cost, got


def action_value_v1(
    vectors: Sequence[Mapping[str, float]],
    shares_by_bucket: Mapping[str, float],
    cost: float,
    *,
    quantile: float = LCB_QUANTILE,
) -> dict[str, float] | None:
    """An action's net value on each vector in the uncertainty set.

    ``sum_b p[b] * n[b] - C`` per vector; the lower bound is a quantile across
    them.  Every vector is a whole distribution from the same fit, so the
    worst case of one action and of another may come from different draws --
    that is what an uncertainty set is -- but never from a vector the fit did
    not produce.
    """

    if not vectors:
        return None
    values = sorted(
        sum(float(v.get(str(b), 0.0) or 0.0) * float(n) for b, n in shares_by_bucket.items())
        - float(cost)
        for v in vectors
    )
    q = min(max(float(quantile), 0.0), 1.0)
    return {
        "value_point": sum(values) / len(values),
        "value_lcb": values[min(len(values) - 1, int(q * len(values)))],
        "value_ucb": values[max(0, int((1.0 - q) * len(values)) - 1)],
    }


def capacity_v1(
    *,
    window: Sequence[str],
    ask_levels_by_bucket: Mapping[str, Sequence[tuple[float, float]]],
    mass_lcb: float,
    hurdle: float,
    min_order_size: float = 0.0,
) -> dict[str, float]:
    """The most equal shares per leg the window can take and still clear.

    ``mass_lcb * n >= (1 + h) * C(n)`` with ``C`` walked down every leg's
    ladder.  The ask side only rises with n, so the largest n that clears is
    found by walking the union of level breakpoints; the exchange minimum is a
    floor below which nothing is buyable at all.  Reported beside the edge so
    "an edge on one share" is never read as "an edge on the station".
    """

    legs = [str(b) for b in window]
    breakpoints: set[float] = set()
    for bucket in legs:
        cumulative = 0.0
        for _price, depth in sorted(ask_levels_by_bucket.get(bucket) or ()):
            cumulative += depth
            breakpoints.add(cumulative)
    best_n = 0.0
    best_cost = 0.0
    floor = max(0.0, float(min_order_size))
    for n in sorted(x for x in breakpoints if x >= floor and x > 0.0):
        total = 0.0
        full = True
        for bucket in legs:
            cost, got = ladder_cost_v1(ask_levels_by_bucket.get(bucket) or (), n)
            if got < n - 1e-9:
                full = False
                break
            total += cost
        if not full:
            break
        if mass_lcb * n >= (1.0 + float(hurdle)) * total - 1e-12:
            best_n, best_cost = n, total
        else:
            break
    return {"capacity_shares": best_n, "capacity_cost": best_cost}


def beta_draw_vectors(
    p_weather: Mapping[str, float],
    m_market: Mapping[str, float],
    draws: Sequence[float],
    *,
    excluded: Iterable[str] = (),
) -> list[dict[str, float]]:
    dead = tuple(excluded)
    out = []
    for beta in draws:
        vector = valuation_vector_v1(p_weather, m_market, beta, excluded=dead)
        if vector is not None:
            out.append(vector)
    return out


