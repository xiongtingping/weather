"""Shadow scoring for the fractional-Kelly (log-wealth) objective.

The production optimizer maximizes expected PnL, which loads size onto the
cheapest admitted bucket and leaves the settled-outcome PnL curve violently
skewed (one basket: +45.8 if the cheap tail settles, -2.3 if the likely
center does).  The approved replacement shapes that curve instead:

    max_q  G(q) = sum_o p_o * log(1 + s_o(q) / W)

over the mutually exclusive settlement outcomes ``o`` -- each admitted bucket
plus "none of ours settled" -- where ``s_o`` is the basket's PnL under that
outcome and ``W`` is the whole-portfolio bankroll.  Stakes are then scaled by
the Kelly fraction (0.25 to start).

This module carries **no authority**.  It computes, alongside every compiled
plan, what the log-wealth objective would have bought under the *same*
admitted buckets, the same limit prices, the same depth caps and the same
budget, and appends both PnL curves to an evidence table.  The accumulated
history is the input to the portfolio OOS replay that decides -- through the
usual gates -- whether this objective ever takes over.  Until then nothing
here touches a plan, an intent, or a reservation.

v1 deliberately re-uses the production plan's own candidate rows rather than
re-deriving admission from the raw book: the question the shadow phase must
answer is "given identical inputs, what does curve-shaping do differently",
and holding every input fixed is what makes the comparison an experiment
rather than an anecdote.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from .basket_kernel.delta_v2 import (
    CURVE_OBJECTIVE_FORM,
    DEFAULT_KELLY_FRACTION,
)
from .contracts import ContractError, content_sha256

SCHEMA = "basket_shadow_objective_score_v1"
# Charter clause 35 admits exactly two objective forms, and this module scores
# one of them -- the same curve, the same f, the same bankroll the live argmax
# reads.  What is distinct here is the *solver*: a closed-form active-set
# optimum with no clip grid, no atomicity and no support floor, against the
# live path's constrained candidate enumeration.  Naming that difference
# ``objective_form`` would put a third objective in the ledger and make the
# charter's "one objective" clause unprovable.  The form is therefore stated
# as what it is; the solver distinction is carried by the table this module
# writes -- every row in ``basket_shadow_objective_score`` is by construction
# the unconstrained reference, so it needs no column to say so.
#
# Post-floor the comparison earns its keep for a new reason: the gap between
# this unconstrained optimum and the seated basket is the measured price of
# the -10% floor, which is the number to look at before anyone retunes it.
OBJECTIVE_FORM = CURVE_OBJECTIVE_FORM
KELLY_FRACTION = DEFAULT_KELLY_FRACTION
FALLBACK_BANKROLL_USDC = 200.0
_MAX_ITERATIONS = 400
_TOLERANCE = 1e-10

SHADOW_OBJECTIVE_DDL = """
CREATE TABLE IF NOT EXISTS basket_shadow_objective_score (
  shadow_score_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  probability_stage TEXT NOT NULL,
  checkpoint_id TEXT NOT NULL,
  decision_at_utc TEXT NOT NULL,
  objective_form TEXT NOT NULL,
  kelly_fraction REAL NOT NULL,
  bankroll_usdc REAL NOT NULL,
  budget_usdc REAL NOT NULL,
  actual_basket_json TEXT NOT NULL,
  actual_curve_json TEXT NOT NULL,
  shadow_basket_json TEXT NOT NULL,
  shadow_curve_json TEXT NOT NULL,
  expected_growth_actual REAL NOT NULL,
  expected_growth_shadow REAL NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS basket_shadow_objective_scope_idx
  ON basket_shadow_objective_score(station_id,event_date,decision_at_utc);
CREATE TRIGGER IF NOT EXISTS basket_shadow_objective_no_update
  BEFORE UPDATE ON basket_shadow_objective_score
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
CREATE TRIGGER IF NOT EXISTS basket_shadow_objective_no_delete
  BEFORE DELETE ON basket_shadow_objective_score
  BEGIN SELECT RAISE(ABORT,'append_only'); END;
"""


def _curve(
    quantities: Sequence[float],
    prices: Sequence[float],
    probabilities: Sequence[float],
    *,
    bankroll: float,
) -> dict[str, Any]:
    """Outcome-indexed PnL curve plus its summary moments."""

    cost = sum(q * c for q, c in zip(quantities, prices))
    pnl = [q - cost for q in quantities]
    p_outside = max(0.0, 1.0 - sum(probabilities))
    outcomes = list(zip(probabilities, pnl)) + [(p_outside, -cost)]
    mean = sum(p * s for p, s in outcomes)
    variance = sum(p * (s - mean) ** 2 for p, s in outcomes)
    growth = sum(
        p * math.log(max(1e-12, 1.0 + s / bankroll)) for p, s in outcomes
    )
    return {
        "pnl_by_bucket": [round(value, 6) for value in pnl],
        "pnl_outside": round(-cost, 6),
        "p_outside": round(p_outside, 6),
        "cost_usdc": round(cost, 6),
        "expected_pnl": round(mean, 6),
        "variance": round(variance, 6),
        "expected_log_growth": growth,
    }


def solve_fractional_kelly(
    *,
    probabilities: Sequence[float],
    prices: Sequence[float],
    depth_caps: Sequence[float],
    budget_usdc: float,
    bankroll_usdc: float,
    kelly_fraction: float = KELLY_FRACTION,
    held_shares: Sequence[float] | None = None,
    sunk_cost_usdc: float = 0.0,
    held_outside: Sequence[Sequence[float]] = (),
) -> list[float]:
    """Log-optimal *increments* from the position already held.

    Unconstrained log-optimal betting on mutually exclusive outcomes has the
    classical solution: admit buckets in decreasing edge ``p/c`` while
    ``p_i/c_i > (1-P_S)/(1-C_S)``, hold reserve ``R = W(1-P_S)/(1-C_S)``, and
    stake ``q_i = R(p_i(1-C_S)/(c_i(1-P_S)) - 1)``.  Budget and depth then
    bind by clipping onto the feasible set and polishing with per-coordinate
    bisection on the (strictly concave) growth -- deterministic, no step
    sizes, and exact at the scale of eleven buckets.  Stakes scale by the
    Kelly fraction at the end.

    The position the station already carries is the *initial condition*, not
    an afterthought.  Solving from zero and adding the answer to inventory
    maximizes the curve of the increment, which is a different problem and a
    different answer: an outcome already covered by held shares has a higher
    settled wealth, so the marginal log value of buying more of it is lower,
    while an uncovered outcome is worth more than its price alone says.
    ``held_shares`` is the inventory on each buyable coordinate,
    ``sunk_cost_usdc`` what the whole position has already paid (a uniform
    shift of every outcome, and log wealth is not translation-invariant, so
    it belongs inside), and ``held_outside`` the ``(probability, shares)``
    of outcomes carrying inventory this solve cannot add to -- a bucket the
    seat has moved off, which still pays and still must be scored.  All
    three default to empty, which is exactly the from-flat problem.
    """

    n = len(prices)
    if not (
        n
        and len(probabilities) == n
        and len(depth_caps) == n
        and budget_usdc >= 0.0
        and bankroll_usdc > 0.0
        and 0.0 < kelly_fraction <= 1.0
    ):
        raise ContractError("shadow_objective_inputs_invalid")
    held = (
        [0.0] * n if held_shares is None
        else [float(value) for value in held_shares]
    )
    if len(held) != n:
        raise ContractError("shadow_objective_inputs_invalid")
    standing: list[tuple[float, float]] = []
    for row in held_outside:
        pair = tuple(float(value) for value in row)
        if len(pair) != 2:
            raise ContractError("shadow_objective_inputs_invalid")
        standing.append((pair[0], pair[1]))
    sunk = float(sunk_cost_usdc)
    for value in (
        *probabilities, *prices, *depth_caps, *held, sunk,
        *(item for pair in standing for item in pair),
    ):
        if not math.isfinite(float(value)) or float(value) < 0.0:
            raise ContractError("shadow_objective_inputs_invalid")
    if any(c <= 0.0 or c >= 1.0 for c in prices):
        raise ContractError("shadow_objective_price_invalid")

    w = float(bankroll_usdc)
    # What the bankroll still has at risk once the position is paid for.
    # Every outcome carries this shift alike, so it is the wealth the curve
    # is actually shaped against.
    free = w - sunk
    p_outside = max(
        0.0,
        1.0 - sum(probabilities) - sum(mass for mass, _ in standing),
    )

    # Closed-form warm start (unconstrained full Kelly).
    order = sorted(
        range(n), key=lambda i: probabilities[i] / prices[i], reverse=True
    )
    active: list[int] = []
    p_s = 0.0
    c_s = 0.0
    for index in order:
        trial_p = p_s + probabilities[index]
        trial_c = c_s + prices[index]
        # Both complements divide below.  A support set that covers the whole
        # probability simplex, or whose prices sum to one, leaves no residual
        # outcome to hold reserve against and the closed form degenerates --
        # stop admitting and let the bisection polish handle the rest.
        if trial_c >= 1.0 - _TOLERANCE or trial_p >= 1.0 - _TOLERANCE:
            break
        if probabilities[index] / prices[index] > (
            (1.0 - trial_p) / (1.0 - trial_c)
        ):
            active.append(index)
            p_s, c_s = trial_p, trial_c
    q = [0.0] * n
    if active and free > 0.0:
        reserve = free * (1.0 - p_s) / (1.0 - c_s)
        for index in active:
            stake = reserve * (
                probabilities[index] * (1.0 - c_s)
                / (prices[index] * (1.0 - p_s))
                - 1.0
            )
            # The closed form names the *total* exposure the outcome should
            # carry; inventory already delivers part of it and only the
            # remainder is for sale.  The polish below works from the exact
            # gradient, so this only has to start it in the right basin.
            q[index] = max(0.0, stake - held[index])

    # Onto the feasible set: depth box, then the budget face.
    q = [min(value, float(cap)) for value, cap in zip(q, depth_caps)]
    cost = sum(qi * ci for qi, ci in zip(q, prices))
    if cost > budget_usdc > 0.0:
        scale = budget_usdc / cost
        q = [value * scale for value in q]
    elif budget_usdc <= 0.0:
        q = [0.0] * n

    def _growth_gradient(index: int, stakes: Sequence[float]) -> float:
        total_cost = sum(qi * ci for qi, ci in zip(stakes, prices))
        reserve = free - total_cost
        if reserve <= 1e-9:
            return -1.0
        grad = probabilities[index] * (1.0 - prices[index]) / (
            reserve + held[index] + stakes[index]
        )
        for other in range(n):
            if other != index:
                grad -= (
                    probabilities[other] * prices[index]
                    / (reserve + held[other] + stakes[other])
                )
        for mass, shares in standing:
            grad -= mass * prices[index] / (reserve + shares)
        grad -= p_outside * prices[index] / reserve
        return grad

    def _coordinate_sweep() -> float:
        """One pass of exact per-coordinate maximization.  Returns the move."""

        moved = 0.0
        for index in range(n):
            cost_others = sum(
                qi * ci
                for position, (qi, ci) in enumerate(zip(q, prices))
                if position != index
            )
            upper = min(
                float(depth_caps[index]),
                max(0.0, (budget_usdc - cost_others) / prices[index]),
                (free - cost_others) / prices[index] - 1e-6,
            )
            if upper <= 0.0:
                moved = max(moved, q[index])
                q[index] = 0.0
                continue
            low, high = 0.0, upper
            trial = list(q)
            trial[index] = low
            if _growth_gradient(index, trial) <= 0.0:
                best = 0.0
            else:
                trial[index] = high
                if _growth_gradient(index, trial) >= 0.0:
                    best = high
                else:
                    for _ in range(60):
                        mid = 0.5 * (low + high)
                        trial[index] = mid
                        if _growth_gradient(index, trial) > 0.0:
                            low = mid
                        else:
                            high = mid
                    best = 0.5 * (low + high)
            moved = max(moved, abs(best - q[index]))
            q[index] = best
        return moved

    def _exchange_sweep() -> bool:
        """Move a dollar from the worst-paying leg to the best.  True if it did.

        Coordinate ascent cannot travel along the budget face: raising one
        leg there requires lowering another, and no single-coordinate step
        does both, so the sweep above stalls the moment the budget or the
        reserve binds -- which is every interesting day.  Measured on 600
        adversarial positions, every allocation the sweeps got wrong was on
        that face, one by 37% of its own score.

        The optimality condition is the same everywhere, budget-bound or
        not: differentiating the growth gives ``p_i / (c_i A_i) = S`` for
        every leg held strictly inside its bounds, so at the optimum the
        marginal growth per dollar is equal across them.  A transfer keeps
        the total cost -- and therefore the reserve -- fixed, so only the two
        legs' own wealth moves and the equalizing amount is one bisection.
        """

        total_cost = sum(qi * ci for qi, ci in zip(q, prices))
        reserve = free - total_cost
        if reserve <= 1e-9:
            return False

        def marginal(index: int) -> float:
            denom = reserve + held[index] + q[index]
            if denom <= 0.0:
                return math.inf
            return probabilities[index] / (prices[index] * denom)

        receivers = [
            index for index in range(n)
            if q[index] + 1e-12 < float(depth_caps[index])
        ]
        givers = [index for index in range(n) if q[index] > 1e-12]
        if not receivers or not givers:
            return False
        taker = max(receivers, key=marginal)
        maker = min(givers, key=marginal)
        if taker == maker or marginal(taker) <= marginal(maker) * (1.0 + 1e-12):
            return False
        ceiling = min(
            prices[taker] * (float(depth_caps[taker]) - q[taker]),
            prices[maker] * q[maker],
        )
        if ceiling <= 0.0:
            return False

        def slope(transfer: float) -> float:
            gained = reserve + held[taker] + q[taker] + transfer / prices[taker]
            given = reserve + held[maker] + q[maker] - transfer / prices[maker]
            if given <= 1e-12:
                return -math.inf
            return (
                probabilities[taker] / (prices[taker] * gained)
                - probabilities[maker] / (prices[maker] * given)
            )

        if slope(ceiling) >= 0.0:
            transfer = ceiling
        else:
            low, high = 0.0, ceiling
            for _ in range(60):
                mid = 0.5 * (low + high)
                if slope(mid) > 0.0:
                    low = mid
                else:
                    high = mid
            transfer = 0.5 * (low + high)
        if transfer <= 1e-15:
            return False
        q[taker] += transfer / prices[taker]
        q[maker] -= transfer / prices[maker]
        return True

    # Alternate until neither can improve: the sweeps set how much is spent,
    # the exchanges set how it is divided, and each unblocks the other.
    for _round in range(8):
        moved = 0.0
        for _sweep in range(24):
            step = _coordinate_sweep()
            moved = max(moved, step)
            if step < _TOLERANCE * max(1.0, w):
                break
        traded = False
        for _swap in range(4 * n):
            if not _exchange_sweep():
                break
            traded = True
        if not traded and moved < _TOLERANCE * max(1.0, w):
            break
    return [value * kelly_fraction for value in q]


def build_shadow_objective_score(
    *,
    station_id: str,
    event_date: str,
    probability_stage: str,
    checkpoint_id: str,
    decision_at_utc: str,
    candidate_rows: Sequence[Mapping[str, Any]],
    probabilities_by_bucket: Mapping[str, Any],
    budget_usdc: float,
    bankroll_usdc: float,
    created_at_utc: str,
) -> dict[str, Any] | None:
    """One shadow receipt, or None when the plan admitted nothing."""

    rows = [
        row for row in candidate_rows
        if row.get("eligible")
        and float(row.get("r_limit") or 0.0) > 0.0
    ]
    if not rows:
        return None
    buckets = [str(row["bucket"]) for row in rows]
    prices = [float(row["r_limit"]) for row in rows]
    probabilities = [
        float(probabilities_by_bucket.get(bucket) or 0.0)
        for bucket in buckets
    ]
    depth_caps = [
        float(row.get("depth_shares_cap") or 0.0) for row in rows
    ]
    actual = [float(row.get("q_order") or 0.0) for row in rows]
    bankroll = (
        float(bankroll_usdc)
        if bankroll_usdc and bankroll_usdc > 0.0
        else FALLBACK_BANKROLL_USDC
    )
    # The optimum itself (fraction 1) is the comparison object: on the same
    # feasible set its growth bounds every other allocation's, the actual
    # plan's included.  The stakes the objective would really bet are the
    # quarter-Kelly contraction of it -- deliberately below the optimum on
    # growth, in exchange for variance.
    full_kelly = solve_fractional_kelly(
        probabilities=probabilities,
        prices=prices,
        depth_caps=depth_caps,
        budget_usdc=float(budget_usdc),
        bankroll_usdc=bankroll,
        kelly_fraction=1.0,
    )
    shadow = [value * KELLY_FRACTION for value in full_kelly]
    actual_curve = _curve(
        actual, prices, probabilities, bankroll=bankroll
    )
    shadow_curve = _curve(
        shadow, prices, probabilities, bankroll=bankroll
    )
    full_kelly_curve = _curve(
        full_kelly, prices, probabilities, bankroll=bankroll
    )
    identity = {
        "schema": SCHEMA,
        "station_id": str(station_id),
        "event_date": str(event_date),
        "probability_stage": str(probability_stage),
        "checkpoint_id": str(checkpoint_id),
        "decision_at_utc": str(decision_at_utc),
        "objective_form": OBJECTIVE_FORM,
        "kelly_fraction": KELLY_FRACTION,
        "bankroll_usdc": round(bankroll, 6),
        "budget_usdc": round(float(budget_usdc), 6),
        "buckets": buckets,
        "prices": [round(value, 6) for value in prices],
        "probabilities": [round(value, 8) for value in probabilities],
        "actual_quantities": [round(value, 6) for value in actual],
        "shadow_quantities": [round(value, 6) for value in shadow],
    }
    return {
        **identity,
        "actual_curve": actual_curve,
        "shadow_curve": shadow_curve,
        "full_kelly_curve": full_kelly_curve,
        "content_sha256": content_sha256(identity),
        "created_at": str(created_at_utc),
    }


def persist_shadow_objective_score(
    conn: sqlite3.Connection,
    score: Mapping[str, Any],
) -> int:
    """Append one shadow receipt; identical evidence replays as a no-op."""

    conn.executescript(SHADOW_OBJECTIVE_DDL)
    sha = str(score["content_sha256"])
    conn.execute(
        "INSERT OR IGNORE INTO basket_shadow_objective_score("
        "shadow_score_id,station_id,event_date,probability_stage,"
        "checkpoint_id,decision_at_utc,objective_form,kelly_fraction,"
        "bankroll_usdc,budget_usdc,actual_basket_json,actual_curve_json,"
        "shadow_basket_json,shadow_curve_json,expected_growth_actual,"
        "expected_growth_shadow,content_sha256,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "shadow-" + sha[:32],
            str(score["station_id"]), str(score["event_date"]),
            str(score["probability_stage"]), str(score["checkpoint_id"]),
            str(score["decision_at_utc"]), str(score["objective_form"]),
            float(score["kelly_fraction"]), float(score["bankroll_usdc"]),
            float(score["budget_usdc"]),
            json.dumps({
                "buckets": score["buckets"],
                "prices": score["prices"],
                "quantities": score["actual_quantities"],
            }, sort_keys=True),
            json.dumps(score["actual_curve"], sort_keys=True),
            json.dumps({
                "buckets": score["buckets"],
                "prices": score["prices"],
                "quantities": score["shadow_quantities"],
            }, sort_keys=True),
            json.dumps(score["shadow_curve"], sort_keys=True),
            float(score["actual_curve"]["expected_log_growth"]),
            float(score["shadow_curve"]["expected_log_growth"]),
            sha, str(score["created_at"]),
        ),
    )
    return int(conn.execute("SELECT changes()").fetchone()[0])
