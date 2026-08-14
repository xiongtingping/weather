"""Pure production adapter for the unique DeltaBasketScore v2 objective.

The adapter deliberately reuses only the probability/support diagnostics from
the basket state machine.  It never calls the v1 optimizer or strict-delta
authorization path.  Every target and action candidate is bounded by the
activated v2 artifact, official WS depth, confirmed inventory/order state and
the caller-provided fill-distribution authority.
"""

from __future__ import annotations

from itertools import product
import math
from typing import Any, Callable, Mapping, Sequence

from .basket_kernel.contracts import (
    EPS,
    ContractError,
    Direction,
    TargetBasketPolicy,
    TargetBasketSnapshot,
    content_sha256,
)
from .basket_kernel.delta_v2 import (
    SUPPORT_EDGE_FRICTION_TICKS_PER_LEG,
    CANDIDATE_KIND_BASELINE,
    CANDIDATE_KIND_COMPLETE,
    CANDIDATE_KINDS,
    CURVE_OBJECTIVE_FORM,
    VALUE_OBJECTIVE_FORM,
    evaluate_action_v2,
    evaluate_basket_value_v2,
    select_action_v2,
    select_complete_maker_bundle_v2,
    solve_reservation_price_v2,
    solve_target_inventory_v2,
)
from .basket_kernel.support import (
    _choose_support,
    _derive_dual_risk_scope,
    probability_center_diagnostics,
)
from .basket_kernel.state_machine import resolve_basket_state
from .delta_v2_artifact import DeltaV2PolicyArtifact
from .shadow_objective import solve_fractional_kelly


SCHEMA = "delta_basket_score_runtime_plan_v2"
MAKER_EXECUTION_CONTRACT = "maker_post_only"
FOK_EXECUTION_CONTRACT = "single_bucket_fok"
FillResolver = Callable[..., Mapping[str, Any]]


def _floor_to_tick(price: float, tick: float) -> float:
    steps = math.floor((price + EPS) / tick)
    return round(steps * tick, 12)


def _maker_market_anchor(*, bid: float, ask: float, tick: float) -> float | None:
    """Return the most competitive non-crossing official-WS Maker price."""

    highest_post_only = _floor_to_tick(ask - tick, tick)
    if highest_post_only <= EPS:
        return None
    improve_bid = _floor_to_tick(bid + tick, tick)
    anchor = min(highest_post_only, improve_bid)
    if anchor + EPS < bid:
        return None
    return anchor


def _ask_depth_purchase(
    *,
    levels: Sequence[Any],
    cash_limit: float,
) -> tuple[float, float]:
    """Conservatively price target inventory against official ask depth."""

    remaining = cash_limit
    shares = 0.0
    spent = 0.0
    for level in levels:
        if remaining <= EPS:
            break
        level_cash = float(level.price) * float(level.shares)
        take_cash = min(remaining, level_cash)
        shares += take_cash / float(level.price)
        spent += take_cash
        remaining -= take_cash
    return shares, spent


def _reservation_grid(*, row: Any, artifact: DeltaV2PolicyArtifact) -> tuple[float, ...]:
    """Bind economic inversion to the live spread without making it a quote."""

    tick = float(row.price_tick_size)
    highest_post_only = _floor_to_tick(float(row.ask_price) - tick, tick)
    if highest_post_only <= EPS:
        return ()
    market_points = {
        _floor_to_tick(float(row.bid_price), tick),
        _floor_to_tick(float(row.bid_price) + tick, tick),
        highest_post_only,
    }
    values = {
        _floor_to_tick(float(price), tick)
        for price in (*artifact.price_grid, *market_points)
        if float(price) > EPS and float(price) <= highest_post_only + EPS
    }
    return tuple(sorted(price for price in values if price > EPS))


def _support_windows(
    *,
    snapshot: TargetBasketSnapshot,
    desired: set[str],
    probabilities: Mapping[str, float],
    fee_rate: float,
    maximum_support_buckets: int,
) -> list[dict[str, Any]]:
    """Contiguous windows admissible as a support, ranked by belief divergence.

    The incumbent support stays in the list unconditionally.  It is what the
    continuous-core resolver admitted -- evidence, model agreement, hard
    bounds, peripheral reliability -- and this function is a price filter, not
    a second opinion on any of that.  Windows are drawn from the same admitted
    set, so nothing here can seat a bucket the core refused.

    Ranking is by ``p - all_in_ask`` because that is the quantity the log
    objective maximises anyway; it only orders the shortlist, and the seat is
    decided by the objective itself on the full curve.
    """

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    incumbent_buckets = sorted(
        (bucket for bucket in desired if bucket in by_bucket),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    if not incumbent_buckets:
        raise ContractError("delta_v2_support_empty")
    incumbent = tuple(incumbent_buckets)
    # The domain is every outcome the day can still reach, not the window
    # where the probability happens to pile up.
    #
    # Drawing candidates from the probability core made the optimizer a
    # formality: the core is chosen by mass, mass is what the market prices
    # richest, and no sub-window of four expensive buckets clears the win
    # rail -- 34 of 40 production plans had exactly one admissible window,
    # their own incumbent.  ZUUU 2026-08-14 seated a support whose four
    # buckets priced at 0.93 against our 0.585, while 33C (edge +0.092),
    # 38C (+0.048) and 32C (+0.052) sat outside it unconsidered.  Over 53
    # station-days a full-domain search finds a window clearing both rails on
    # 24 of them at a mean edge of +0.318; ZUUU's own was {33,34,35,36} at
    # +0.154, one bucket below the incumbent.
    #
    # The safety property the core carried is preserved by the filter below:
    # a bucket the hard lower bound has excluded holds zero probability and
    # is not a candidate.  Evidence still decides what is possible; the
    # objective decides only what is worth buying among the possible.
    admitted = sorted(
        (
            row.bucket
            for row in snapshot.buckets
            if float(row.probability) > EPS
        ),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    for bucket in incumbent:
        if bucket not in admitted:
            admitted.append(bucket)
    admitted.sort(key=lambda bucket: (by_bucket[bucket].ordinal, bucket))

    def _all_in(bucket: str) -> float | None:
        """All-in ask, or None when the bucket has no offer to price.

        A support may legitimately contain a bucket with no ask -- it was
        admitted on evidence and is held or waited on, not bought now.  Such a
        window cannot be scored on price, so it is not *ranked* against the
        priced ones; the incumbent still passes through unconditionally, which
        keeps a bid-only bucket exactly as available as it was before.
        """

        row = by_bucket[bucket]
        if not row.ask_available:
            return None
        return float(row.ask_price) * (1.0 + float(fee_rate))

    windows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    # Contiguous and at most ``maximum_support_buckets`` -- the same structural
    # cap the continuous core enforces.  Widening the *domain* must not widen
    # the basket: a five-bucket support would break the atomicity contract
    # downstream and the clip lattice it feeds.
    max_size = max(1, int(maximum_support_buckets))
    for size in range(1, min(max_size, len(admitted)) + 1):
        for start in range(0, len(admitted) - size + 1):
            window = tuple(admitted[start:start + size])
            if window in seen:
                continue
            seen.add(window)
            win = sum(float(probabilities[bucket]) for bucket in window)
            quotes = [_all_in(bucket) for bucket in window]
            priced = all(quote is not None for quote in quotes)
            price = sum(quote for quote in quotes if quote is not None)
            edge = win - price if priced else None
            is_incumbent = window == incumbent
            # The incumbent is always offered so the optimizer can keep it.
            # The only hard admission floor is friction: the all-in price
            # already carries the fee, so an edge smaller than tick-rounding
            # across the legs is quantisation noise, not signal.  Everything
            # else is ranking -- the seat belongs to the curve score alone.
            friction = SUPPORT_EDGE_FRICTION_TICKS_PER_LEG * sum(
                float(by_bucket[bucket].price_tick_size)
                for bucket in window
            )
            admissible = is_incumbent or (
                priced and edge is not None and edge > friction + EPS
            )
            if not admissible:
                continue
            kl = None
            if priced:
                # Clipping implements the 0*log(0)=0 limits: a full-partition
                # window (win=1) is the certainty-harvest shape and must rank
                # by log(1/cost), not fall off the shortlist as unrankable.
                clipped_win = min(max(win, EPS), 1.0 - EPS)
                cost = min(max(price, EPS), 1.0 - EPS)
                kl = (
                    clipped_win * math.log(clipped_win / cost)
                    + (1.0 - clipped_win)
                    * math.log((1.0 - clipped_win) / (1.0 - cost))
                )
            windows.append({
                "buckets": window,
                "win_probability": win,
                "all_in_price": price if priced else None,
                "edge": edge,
                "log_growth_rank": kl,
                "is_incumbent": is_incumbent,
            })
    if incumbent not in seen:
        # Enumeration can miss the incumbent outright: it is bounded to
        # contiguous runs of at most ``maximum_support_buckets``, and an
        # inherited support may be neither -- a live ZHHH core sat on
        # {31C, 33C or higher} with 32C alive between them.  Relying on the
        # ``is_incumbent`` branch inside the loop to carry it through is
        # therefore not enough; that branch only fires on a window the loop
        # actually produced.  Dropping it would take "keep what we hold" off
        # the menu, which is the same escape the floor's live-bucket scope
        # exists to close, so it is appended here instead.
        quotes = [_all_in(bucket) for bucket in incumbent]
        priced = all(quote is not None for quote in quotes)
        price = sum(quote for quote in quotes if quote is not None)
        win = sum(float(probabilities[bucket]) for bucket in incumbent)
        incumbent_kl = None
        if priced:
            clipped_win = min(max(win, EPS), 1.0 - EPS)
            cost = min(max(price, EPS), 1.0 - EPS)
            incumbent_kl = (
                clipped_win * math.log(clipped_win / cost)
                + (1.0 - clipped_win)
                * math.log((1.0 - clipped_win) / (1.0 - cost))
            )
        windows.append({
            "buckets": incumbent,
            "win_probability": win,
            "all_in_price": price if priced else None,
            "edge": (win - price) if priced else None,
            "log_growth_rank": incumbent_kl,
            "is_incumbent": True,
        })
    # An unpriced window has no edge to rank on, so it sorts last rather than
    # being compared against numbers it does not have; the objective still
    # scores it, and its own curve decides whether it is seated.
    # Rank by the objective's own quantity: the curve score at optimal stake
    # is the KL divergence between our belief and the market's implied odds
    # (charter 35), so the shortlist order is the seat contest itself run on
    # the cheap.  Raw edge over-ranks penny lotteries; KL prices the whole
    # win/lose geometry.
    windows.sort(
        key=lambda row: (
            row["log_growth_rank"] is None,
            -(row["log_growth_rank"] or 0.0),
            len(row["buckets"]),
            row["buckets"],
        )
    )
    return windows


# Enumerating quantities is exponential in the window's width, so the shortlist
# is bounded rather than the whole lattice being expanded.  The ordering above
# is by the same quantity the objective maximises -- but that ranking is
# depth-blind: on a listing-thin book the highest-KL windows can all contain
# a leg whose entire visible book is smaller than the smallest cash clip, and
# such a window solves to exactly the baseline.  At four, ZUUU 2026-08-15's
# top three alternatives all carried the 0.75-USDC 33C book and scored zero
# while the depth-feasible {34,35} pair (edge +0.18) sat unsolved at rank
# five -- a HOLD manufactured by the cutoff, not by the objective.  Twelve
# keeps the solve bounded (each window's own candidate lattice is already
# capped at 4096) while letting the first depth-feasible alternatives reach
# the solver.
MAX_SUPPORT_WINDOWS_SOLVED = 12


def _solve_over_support_windows(
    *,
    windows: Sequence[Mapping[str, Any]],
    live_buckets: Sequence[str],
    snapshot: TargetBasketSnapshot,
    probabilities: Mapping[str, float],
    shortfall: set[str],
    breakthrough: set[str],
    delta_policy: DeltaV2PolicyArtifact,
    basket_policy: TargetBasketPolicy,
    capital_available_usdc: float,
    objective_form: str,
    bankroll_usdc: float,
) -> tuple[tuple[str, ...], dict[str, Any], dict[str, Any]]:
    """Run the objective on each shortlisted window; the best curve is seated.

    Scores are comparable across windows because every candidate's scenario
    curve spans the whole bucket space -- an unheld bucket settles at minus the
    basket's cost -- so two windows are scored on the same outcome partition
    rather than on their own supports.
    """

    shortlist = list(windows[:MAX_SUPPORT_WINDOWS_SOLVED])
    incumbent = next(
        (row for row in windows if row["is_incumbent"]), None
    )
    if incumbent is not None and incumbent not in shortlist:
        shortlist.append(incumbent)
    if not shortlist:
        raise ContractError("delta_v2_support_empty")

    scored: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for row in shortlist:
        window = tuple(row["buckets"])
        try:
            solution = solve_target_inventory_v2(
                probabilities=probabilities,
                feasible_inventory_candidates=_target_candidates(
                    snapshot=snapshot,
                    desired=window,
                    artifact=delta_policy,
                    capital_available_usdc=capital_available_usdc,
                    minimum_clip_notional_usdc=(
                        basket_policy.clip_min_notional_usdc
                    ),
                ),
                # The floor guards the whole live set exactly when full
                # coverage is structurally possible (charter 38, amended
                # 2026-08-14).  While the hard bound still holds more live
                # buckets than a window may span, demanding every one be
                # protected is not a floor but a ban: eleven live buckets
                # against a four-bucket support means some uncovered bucket
                # always settles at minus the whole cost, so every spending
                # candidate was illegal and the opening stage could never
                # buy -- a direct collision with charters 46/48.  The risk
                # the floor was double-charging is already priced inside the
                # objective: an uncovered live bucket enters the log-wealth
                # curve at minus the basket's cost with its own probability,
                # and the fractional-Kelly stake shrinks accordingly.  So:
                # live set coverable (<= the window width cap) -> protect
                # all of it, which keeps the anti-narrowing rule exactly
                # where it can bind; wider than coverable -> protect the
                # window's own tail, and let sizing price the rest.  The
                # hard bound pruning live buckets flips this back to
                # full-scope automatically, which is clause 38's original
                # "the floor relaxes as certainty arrives" reading run in
                # both directions.
                protected_buckets=(
                    live_buckets
                    if len(live_buckets)
                    <= max(1, int(basket_policy.maximum_support_buckets))
                    else window
                ),
                shortfall_buckets=tuple(sorted(shortfall & set(window))),
                breakthrough_buckets=tuple(
                    sorted(breakthrough & set(window))
                ),
                weights=delta_policy.value_weights,
                objective_form=objective_form,
                bankroll_usdc=bankroll_usdc,
            )
        except ContractError as exc:
            # One infeasible window -- no legal shape at these prices, or a
            # candidate lattice too wide -- must not deny the others their
            # comparison.  It is recorded, not silently dropped.
            failures.append({
                "buckets": list(window),
                "reason": f"ContractError:{exc}",
            })
            continue
        score = (
            solution["target_curve_score"]
            if objective_form == CURVE_OBJECTIVE_FORM
            else solution["target_value"]
        )
        scored.append({
            "window": row,
            "solution": solution,
            "score": float(score),
        })
    if not scored:
        raise ContractError("delta_v2_no_solvable_support_window")
    best = max(
        scored,
        key=lambda item: (
            item["score"],
            # Ties go to the cheaper basket, then to the incumbent, then by
            # bucket order: three deterministic keys so an equal-scoring pair
            # never resolves on dict ordering.
            -sum(item["solution"]["target_cost_by_bucket"].values()),
            item["window"]["is_incumbent"],
            item["window"]["buckets"],
        ),
    )
    selection = {
        "rule": "MAX_CURVE_SCORE_OVER_CONTIGUOUS_SUPPORT_WINDOWS_V2",
        "edge_friction_ticks_per_leg": SUPPORT_EDGE_FRICTION_TICKS_PER_LEG,
        "admissible_window_count": len(windows),
        "solved_window_count": len(scored),
        "unsolvable_windows": failures,
        "seated": {
            "buckets": list(best["window"]["buckets"]),
            "win_probability": best["window"]["win_probability"],
            "all_in_price": best["window"]["all_in_price"],
            "edge": best["window"]["edge"],
            "curve_score": best["score"],
            "is_incumbent": best["window"]["is_incumbent"],
        },
        "considered": [
            {
                "buckets": list(item["window"]["buckets"]),
                "win_probability": item["window"]["win_probability"],
                "all_in_price": item["window"]["all_in_price"],
                "edge": item["window"]["edge"],
                "curve_score": item["score"],
            }
            for item in sorted(
                scored, key=lambda item: -item["score"]
            )
        ],
    }
    return tuple(best["window"]["buckets"]), best["solution"], selection


def _target_candidates(
    *,
    snapshot: TargetBasketSnapshot,
    desired: Sequence[str],
    artifact: DeltaV2PolicyArtifact,
    capital_available_usdc: float,
    minimum_clip_notional_usdc: float,
) -> list[dict[str, Any]]:
    """Build a bounded q-grid priced from official WS ask depth."""

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    current_shares = {row.bucket: row.filled_shares for row in snapshot.buckets}
    current_cost = {row.bucket: row.filled_cost_usdc for row in snapshot.buckets}
    pending_shares = {row.bucket: row.pending_shares for row in snapshot.buckets}
    pending_cost = {
        row.bucket: row.pending_notional_usdc for row in snapshot.buckets
    }
    ordered = tuple(sorted(desired, key=lambda bucket: (by_bucket[bucket].ordinal, bucket)))
    per_bucket_levels: list[tuple[float, ...]] = []
    quote_levels: dict[str, dict[float, tuple[float, float]]] = {}
    for bucket in ordered:
        row = by_bucket[bucket]
        quote_levels[bucket] = {}
        affordable: list[float] = []
        for clip in artifact.cash_clip_grid:
            if clip > capital_available_usdc + EPS:
                continue
            # The clip floor is deliberately *not* applied here.  Dropping a
            # sub-floor clip from the grid does stop an unfillable q*, but it
            # also removes the bucket from the sizing decisions downstream,
            # and ``clip_below_minimum`` is raised there -- so filtering early
            # trades one silent hold for another while destroying the only
            # reason code that explains it.  ``cash_phase`` owns this refusal
            # and says so per bucket; leave the candidate visible to it.
            shares, spent = _ask_depth_purchase(
                levels=row.ask_levels,
                cash_limit=float(clip),
            )
            if (
                shares <= EPS
                or spent + EPS < float(clip)
            ):
                continue
            quote_levels[bucket][float(clip)] = (shares, spent)
            affordable.append(float(clip))
        per_bucket_levels.append((0.0, *affordable))
    candidate_count = math.prod(len(levels) for levels in per_bucket_levels)
    if candidate_count > 4096:
        raise ContractError("delta_v2_target_candidate_bound_exceeded")
    candidates: list[dict[str, Any]] = []
    for clips in product(*per_bucket_levels):
        if sum(clips) > capital_available_usdc + EPS:
            continue
        is_baseline = all(clip <= EPS for clip in clips)
        if not is_baseline and any(
            (
                current_shares[bucket]
                + pending_shares[bucket]
                <= EPS
                and clip <= EPS
            )
            for bucket, clip in zip(ordered, clips)
        ):
            # A basket activation is atomic at the target level: either every
            # selected support bucket receives a positive target, or the
            # optimizer compares against the no-new-action baseline and
            # HOLDS.  Every positive gap is emitted in one complete Maker
            # intent bundle; exchange fills may arrive independently, but no
            # proper subset is an authorized initial action.
            continue
        shares = {
            bucket: current_shares[bucket] + pending_shares[bucket]
            for bucket in current_shares
        }
        costs = {
            bucket: current_cost[bucket] + pending_cost[bucket]
            for bucket in current_cost
        }
        for bucket, clip in zip(ordered, clips):
            if clip <= EPS:
                continue
            acquired_shares, acquired_cost = quote_levels[bucket][float(clip)]
            shares[bucket] += acquired_shares
            costs[bucket] += acquired_cost
        candidates.append({
            "candidate_id": "q-" + content_sha256({
                "ordered_support": ordered,
                "clips": clips,
                "quote_version": snapshot.quote_version,
            })[:24],
            "shares_by_bucket": shares,
            "cost_by_bucket": costs,
            "pricing_basis": "official_ws_ask_depth",
            "candidate_kind": (
                CANDIDATE_KIND_BASELINE
                if is_baseline
                else CANDIDATE_KIND_COMPLETE
            ),
            "candidate_origin": "CLIP_GRID_PRODUCT",
        })
    candidates.extend(_continuous_optimum_candidates(
        snapshot=snapshot,
        ordered=ordered,
        current_shares=current_shares,
        current_cost=current_cost,
        pending_shares=pending_shares,
        pending_cost=pending_cost,
        capital_available_usdc=capital_available_usdc,
        minimum_clip_notional_usdc=minimum_clip_notional_usdc,
    ))
    if not candidates:
        raise ContractError("delta_v2_target_candidate_set_empty")
    return _assert_candidate_support_contract(
        candidates, ordered_support=ordered
    )


def _continuous_optimum_candidates(
    *,
    snapshot: TargetBasketSnapshot,
    ordered: Sequence[str],
    current_shares: Mapping[str, float],
    current_cost: Mapping[str, float],
    pending_shares: Mapping[str, float],
    pending_cost: Mapping[str, float],
    capital_available_usdc: float,
    minimum_clip_notional_usdc: float,
) -> list[dict[str, Any]]:
    """Offer the split the clip grid cannot express.

    An exhaustive product over the grid costs ``(levels+1) ** buckets``, so
    six support buckets exhaust the 4096 bound at three levels and the
    objective is left choosing between a handful of corners.  Resolution
    cannot come from a denser grid; it comes from solving the continuous
    problem, which is concave here, and offering the answer alongside.

    These are ordinary complete-basket candidates -- same kind, same audit,
    same contract -- so the argmax is still exact over the set it is given.
    They differ only in provenance, which is why provenance is a separate
    field: a generator must be able to say where a candidate came from
    without changing what the state machine reads.

    Sizes span rather than pre-judge.  Using the serving fraction here would
    count the same risk aversion twice, since the curve score already
    expresses it, and the smallest candidate would always win.
    """

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    prices: list[float] = []
    caps: list[float] = []
    probabilities: list[float] = []
    for bucket in ordered:
        row = by_bucket[bucket]
        levels = list(row.ask_levels)
        if not levels:
            return []
        prices.append(float(levels[0].price))
        caps.append(sum(float(level.shares) for level in levels))
        probabilities.append(float(row.probability))
    if any(price <= EPS or price >= 1.0 for price in prices):
        return []
    floor = max(float(minimum_clip_notional_usdc), 0.0)
    if floor * len(ordered) > capital_available_usdc + EPS:
        return []

    results: list[dict[str, Any]] = []
    seen: set[tuple[float, ...]] = set()
    for fraction in (1.0, 0.5):
        try:
            stakes = solve_fractional_kelly(
                probabilities=probabilities,
                prices=prices,
                depth_caps=caps,
                budget_usdc=float(capital_available_usdc),
                bankroll_usdc=float(capital_available_usdc),
                kelly_fraction=fraction,
            )
        except ContractError:
            continue
        cash = [
            max(floor, round(stake * price, 2))
            for stake, price in zip(stakes, prices)
        ]
        total = sum(cash)
        if total > capital_available_usdc + EPS:
            scale = capital_available_usdc / total
            cash = [max(floor, round(value * scale, 2)) for value in cash]
            if sum(cash) > capital_available_usdc + EPS:
                continue
        key = tuple(cash)
        if key in seen:
            continue
        seen.add(key)
        shares = {
            bucket: current_shares[bucket] + pending_shares[bucket]
            for bucket in current_shares
        }
        costs = {
            bucket: current_cost[bucket] + pending_cost[bucket]
            for bucket in current_cost
        }
        usable = True
        for bucket, clip in zip(ordered, cash):
            acquired_shares, spent = _ask_depth_purchase(
                levels=by_bucket[bucket].ask_levels,
                cash_limit=float(clip),
            )
            if acquired_shares <= EPS or spent + EPS < float(clip):
                usable = False
                break
            shares[bucket] += acquired_shares
            costs[bucket] += spent
        if not usable:
            continue
        results.append({
            "candidate_id": "k-" + content_sha256({
                "ordered_support": tuple(ordered),
                "cash": key,
                "quote_version": snapshot.quote_version,
            })[:24],
            "shares_by_bucket": shares,
            "cost_by_bucket": costs,
            "pricing_basis": "official_ws_ask_depth",
            "candidate_kind": CANDIDATE_KIND_COMPLETE,
            "candidate_origin": "CONTINUOUS_LOG_OPTIMUM",
        })
    return results




def _assert_candidate_support_contract(
    candidates: Sequence[Mapping[str, Any]],
    *,
    ordered_support: Sequence[str],
) -> list[dict[str, Any]]:
    """Hold every candidate to what the stages downstream assume of it.

    ``candidate_kind`` is not a label.  ``basket_target_activated`` is a
    literal comparison against ``COMPLETE_BASKET_TARGET``, and the support
    that the gap, the Maker bundle and the emitted target rows are all
    computed over collapses to empty when that comparison fails.  A generator
    that produced a structurally perfect basket under a new name would
    therefore not be rejected -- it would silently turn a trade into a hold,
    which is what happened the first time a second generator was tried.

    So the kind stays a closed semantic class that the state machine reads,
    provenance moves to ``candidate_origin`` where it can vary freely, and
    the three invariants the rest of the pipeline relies on are checked here,
    at the boundary where a new generator is introduced, rather than assumed:

    - a complete basket gives every support bucket a positive increment,
      because activation is atomic;
    - no candidate moves a bucket outside the support, because the gap is
      only ever computed over the support and anything bought outside it
      would never be reconciled;
    - exactly one baseline exists, because it is the anchor every other
      candidate is compared against.

    Increments are measured against the baseline rather than read absolutely:
    the absolute figures carry existing inventory, which every candidate
    shares and none of them chose.
    """

    support = tuple(ordered_support)
    baselines = [
        row for row in candidates
        if row.get("candidate_kind") == CANDIDATE_KIND_BASELINE
    ]
    if len(baselines) != 1:
        raise ContractError("delta_v2_candidate_baseline_not_unique")
    baseline_shares = dict(baselines[0].get("shares_by_bucket") or {})
    for row in candidates:
        kind = str(row.get("candidate_kind") or "")
        if kind not in CANDIDATE_KINDS:
            raise ContractError("delta_v2_candidate_kind_invalid")
        shares = dict(row.get("shares_by_bucket") or {})
        for bucket, value in shares.items():
            increment = float(value) - float(
                baseline_shares.get(bucket, 0.0)
            )
            if bucket not in support and increment > EPS:
                raise ContractError(
                    "delta_v2_candidate_moves_bucket_outside_support"
                )
        if kind == CANDIDATE_KIND_COMPLETE and any(
            float(shares.get(bucket, 0.0))
            - float(baseline_shares.get(bucket, 0.0))
            <= EPS
            for bucket in support
        ):
            raise ContractError(
                "delta_v2_candidate_complete_basket_incomplete"
            )
        if kind == CANDIDATE_KIND_BASELINE and any(
            abs(
                float(shares.get(bucket, 0.0))
                - float(baseline_shares.get(bucket, 0.0))
            )
            > EPS
            for bucket in shares
        ):
            raise ContractError("delta_v2_candidate_baseline_not_flat")
    return [dict(row) for row in candidates]


def _hard_gates(
    snapshot: TargetBasketSnapshot,
    *,
    positive_gap: bool,
    budget_ok: bool,
    reservation_price_ok: bool = True,
    resource_probe_ok: bool,
) -> dict[str, bool]:
    return {
        "unique_ssot": True,
        "lineage_complete": bool(
            snapshot.evidence.posterior_sha256
            and snapshot.quote_version
            and snapshot.inventory_version
            and snapshot.order_state_version
        ),
        "registry_timezone_matches": True,
        "bucket_contract_matches": snapshot.contract_matches,
        "official_ws_fresh": snapshot.evidence_fresh,
        "inventory_known": True,
        "order_state_known": snapshot.order_state_known,
        "positive_inventory_gap": positive_gap,
        "budget_ok": budget_ok,
        "profit_floor_ok": True,
        "resource_probe_ok": resource_probe_ok,
        "reservation_price_ok": reservation_price_ok,
    }


def _market_center_ordinal(
    snapshot: TargetBasketSnapshot,
) -> tuple[int | None, str | None]:
    """The bucket the book prices richest, or None on an unquoted market.

    Prices are implied probabilities, so the richest mid is where the market
    puts the day.  A one-sided book still carries an opinion (its one side);
    a book with no quotes at all carries none, and the gate that consumes
    this cannot evaluate -- which is harmless, because there is also nothing
    to buy.
    """

    mids: list[tuple[float, int, str]] = []
    for row in snapshot.buckets:
        bid = float(row.bid_price) if row.bid_levels else None
        ask = float(row.ask_price) if row.ask_available else None
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask
        else:
            continue
        if mid > EPS:
            mids.append((mid, int(row.ordinal), row.bucket))
    if not mids:
        return None, None
    top = max(mid for mid, _, _ in mids)
    leaders = [item for item in mids if item[0] >= top - EPS]
    if len(leaders) != 1:
        # A tied top is a market without a unique opinion, not a market
        # disagreeing with the model; the gate has nothing to act on.
        return None, None
    return leaders[0][1], leaders[0][2]


def compile_delta_v2_runtime_plan(
    *,
    snapshot: TargetBasketSnapshot,
    basket_policy: TargetBasketPolicy,
    delta_policy: DeltaV2PolicyArtifact,
    fill_resolver: FillResolver,
    capital_available_usdc: float,
    minimum_profit_floor: float,
    resource_probe_ok: bool,
    regime_direction_allowed: bool = True,
    confirmed_cancel_for_fok: bool = False,
) -> dict[str, Any]:
    """Compile the single-event q* and best no-live action under v2."""

    basket_policy.validate()
    if not resource_probe_ok:
        raise ContractError("delta_v2_resource_probe_failed")
    if delta_policy.station_id not in {"*", snapshot.station_id}:
        raise ContractError("delta_v2_policy_station_mismatch")
    if delta_policy.stage not in {"*", snapshot.evidence.probability_stage}:
        raise ContractError("delta_v2_policy_stage_mismatch")
    (
        center,
        direction,
        base,
        bias,
        edge,
        desired,
        scores,
        candidate_lower,
        candidate_upper,
        expansion_layers,
        core_transition,
    ) = _choose_support(snapshot, basket_policy)
    market_center_ordinal, market_center_bucket = _market_center_ordinal(
        snapshot
    )
    # Hard entry gate: when the market's own center sits outside the model
    # envelope (the disagreement-expanded candidate boundary, which already
    # unions TAF TX), the market is telling us it holds information our
    # evidence layer does not.  ZUUU 2026-08-13 is the type specimen: a
    # starved two-system root centred at 35 while the book centred on 37 --
    # the "+0.154 edge" window it implied was phantom belief, not mispricing.
    # The gate blocks NEW money only, same philosophy as the breaker's
    # STOP_NEW_INTENTS_ONLY and the hard bound's "constrain additions":
    # probabilities are untouched, held inventory is untouched, and the
    # baseline (hold, add nothing) remains the seat.
    envelope_entry_blocked = (
        market_center_ordinal is not None
        and not (
            int(candidate_lower)
            <= market_center_ordinal
            <= int(candidate_upper)
        )
    )
    shortfall, breakthrough = _derive_dual_risk_scope(snapshot, center)
    # Risk families classify already-admitted support; they never bypass the
    # continuous-core candidate boundary or its evidence/price/Maker gates.
    shortfall &= desired
    breakthrough &= desired
    if not desired:
        raise ContractError("delta_v2_support_empty")
    (
        diagnosed_center,
        runner_up,
        center_lead,
        center_stable,
    ) = probability_center_diagnostics(
        {row.bucket: row.probability for row in snapshot.buckets},
        {row.bucket: row.ordinal for row in snapshot.buckets},
        basket_policy,
    )
    # The diagnostic center is the probability argmax.  It is the anchor only
    # for stages that publish no point estimate of their own; when one exists
    # the anchor is deliberately not the argmax, because an open-ended edge
    # bucket spans an unbounded interval and outranks the 1°C buckets it is
    # compared against without the forecast pointing anywhere near it.  Keeping
    # the equality unconditional would reject exactly the cases the anchor was
    # introduced to fix (ZUUU: anchor 33°C, argmax "35°C or higher").  The
    # runner-up/lead/stability diagnostics below remain informative either way.
    if (
        snapshot.evidence.model_center_ordinal is None
        and diagnosed_center != center.bucket
    ):
        raise ContractError("delta_v2_center_diagnostic_mismatch")
    probabilities = {row.bucket: row.probability for row in snapshot.buckets}
    by_bucket = {row.bucket: row for row in snapshot.buckets}
    ordered_support = tuple(
        sorted(desired, key=lambda bucket: (by_bucket[bucket].ordinal, bucket))
    )
    # Rank by the settled-outcome curve rather than by expected value; see
    # ``curve_score`` for why a symmetric variance charge flattens the curve
    # where log-wealth alone would sharpen it.
    #
    # The bankroll is the station's allotted risk capital, not the budget
    # still unspent.  Using the remainder would make the charge grow as the
    # book fills and diverge as it empties -- the last dollar priced as
    # infinitely precious -- so the risk preference would drift within a day
    # for no reason the evidence supports.  A depleted budget still compiles:
    # the feasible set collapses to the zero basket on its own, and the
    # ranking simply has nothing left to choose between.
    bankroll_usdc = float(basket_policy.total_budget_usdc)
    objective_form = (
        CURVE_OBJECTIVE_FORM
        if bankroll_usdc > 0.0
        else VALUE_OBJECTIVE_FORM
    )
    # One argmax over (support, quantities), not two stages.
    #
    # The support used to be fixed by probability mass alone and handed to the
    # optimizer already chosen, which threw the book away before the objective
    # ever saw it.  Measured over 37 station-days with live asks, that support
    # carried our probability 0.688 against a market price of 0.883 -- an
    # expected value of -0.195, because the buckets we rate highest are the
    # ones the market prices richest.  The objective never needed a new term
    # to fix this: at its optimal stake the log-wealth score *is* the
    # divergence between our belief and the price (measured correlation 0.833
    # against KL), so simply letting it see more than one window makes it
    # choose on edge.  Given the choice it agreed with the mispricing-maximal
    # window on 31 of 37 days and with the incumbent probability-maximal one
    # on 3.
    support_windows = _support_windows(
        snapshot=snapshot,
        desired=desired,
        probabilities=probabilities,
        fee_rate=basket_policy.fee_rate,
        maximum_support_buckets=basket_policy.maximum_support_buckets,
    )
    # The floor's live set is the evidence's, not the candidate support's.
    # ``ordered_support`` here is the continuous-core candidate boundary --
    # four buckets on an ordinary day -- and handing it to the solver as
    # "live" made the coverability test at the floor always pass in
    # full-protection mode: every window that did not blanket the whole
    # candidate core was floor-illegal, which re-manufactured the listing-day
    # ban that the clause 38 amendment removed.  Live means what the hard
    # bound still holds reachable: every bucket with probability mass.
    evidence_live_buckets = tuple(
        row.bucket
        for row in snapshot.buckets
        if row.probability > EPS
    )
    seated_support, target_solution, support_selection = (
        _solve_over_support_windows(
            windows=support_windows,
            live_buckets=evidence_live_buckets,
            snapshot=snapshot,
            probabilities=probabilities,
            shortfall=shortfall,
            breakthrough=breakthrough,
            delta_policy=delta_policy,
            basket_policy=basket_policy,
            capital_available_usdc=capital_available_usdc,
            objective_form=objective_form,
            bankroll_usdc=bankroll_usdc,
        )
    )
    ordered_support = seated_support
    desired = set(seated_support)
    target_shares = dict(target_solution["target_shares_by_bucket"])
    current_shares = {row.bucket: row.filled_shares for row in snapshot.buckets}
    current_cost = {row.bucket: row.filled_cost_usdc for row in snapshot.buckets}
    pending_shares = {row.bucket: row.pending_shares for row in snapshot.buckets}
    pending_cost = {
        row.bucket: row.pending_notional_usdc for row in snapshot.buckets
    }
    basket_target_activated = (
        target_solution.get("winner_candidate_kind")
        == "COMPLETE_BASKET_TARGET"
    )
    final_support = ordered_support if basket_target_activated else ()
    # The positive gap is a per-bucket quantity and every consumer below must
    # read the same one.  Deriving it once keeps candidate admission, Maker
    # clip sizing and the emitted target rows from drifting apart.
    gap_by_bucket = {
        bucket: (
            target_shares[bucket]
            - current_shares[bucket]
            - pending_shares[bucket]
        )
        for bucket in final_support
    }
    execution_gap_support = tuple(
        bucket for bucket in final_support if gap_by_bucket[bucket] > EPS
    )
    event_id = f"{snapshot.station_id}-{snapshot.event_date}"
    actions: list[dict[str, Any]] = []
    candidate_descriptors: list[tuple[str, float, float, str]] = []
    maker_candidate_diagnostics: dict[str, str] = {
        bucket: "official_ws_ask_unavailable"
        for bucket in ordered_support
        if not by_bucket[bucket].ask_available
    }
    for bucket in execution_gap_support:
        row = by_bucket[bucket]
        if not row.ask_available:
            maker_candidate_diagnostics[bucket] = (
                "official_ws_ask_unavailable"
            )
            continue
        gap = gap_by_bucket[bucket]
        if gap <= EPS:
            continue
        reservation_grid = _reservation_grid(row=row, artifact=delta_policy)
        maker_anchor = _maker_market_anchor(
            bid=row.bid_price,
            ask=row.ask_price,
            tick=row.price_tick_size,
        )
        if maker_anchor is None:
            maker_candidate_diagnostics[bucket] = (
                "maker_post_only_price_unavailable_at_min_tick"
                if row.ask_price <= row.price_tick_size + EPS
                else "maker_post_only_market_anchor_unavailable"
            )
        elif not reservation_grid:
            maker_candidate_diagnostics[bucket] = (
                "maker_reservation_grid_empty"
            )
        for clip in delta_policy.cash_clip_grid:
            if (
                clip > capital_available_usdc + EPS
                or not reservation_grid
                or maker_anchor is None
            ):
                continue
            action_clip = min(float(clip), gap * maker_anchor)
            if action_clip <= EPS:
                continue
            for timeout in delta_policy.timeout_grid_seconds:
                candidate_descriptors.append(
                    (bucket, action_clip, maker_anchor, f"MAKER:{timeout}")
                )
        # FOK is intentionally a single zero-time candidate family and is
        # admitted only in the existing urgency window with sufficient depth.
        if (
            confirmed_cancel_for_fok
            and snapshot.time_left_minutes
            <= basket_policy.taker_time_threshold_minutes + EPS
        ):
            for clip in delta_policy.cash_clip_grid:
                if clip > capital_available_usdc + EPS:
                    continue
                shares, spent = _ask_depth_purchase(
                    levels=row.ask_levels,
                    cash_limit=float(clip),
                )
                if spent + EPS >= clip and shares <= gap + EPS:
                    candidate_descriptors.append(
                        (bucket, clip, row.ask_price, "FOK:0")
                    )
    if len(candidate_descriptors) > delta_policy.execution_weights.maximum_candidates:
        raise ContractError("delta_v2_action_candidate_bound_exceeded")
    for bucket, clip, price, mode_timeout in candidate_descriptors:
        mode, timeout_raw = mode_timeout.split(":", 1)
        timeout = float(timeout_raw)
        row = by_bucket[bucket]
        gap = gap_by_bucket[bucket]
        initial_fill = fill_resolver(
            station_id=snapshot.station_id,
            stage=snapshot.evidence.probability_stage,
            bucket=bucket,
            order_mode=mode,
            limit_price=price,
            timeout_seconds=timeout,
            queue_ahead_shares=row.maker_queue_ahead_shares,
            spread=max(0.0, row.ask_price - row.bid_price),
            depth=row.executable_depth_shares,
            quote_version=snapshot.quote_version,
        )
        distribution = initial_fill.get("fill_distribution")
        if not isinstance(distribution, Sequence):
            raise ContractError("delta_v2_fill_distribution_missing")
        costs = initial_fill.get("execution_costs")
        if not isinstance(costs, Mapping):
            raise ContractError("delta_v2_execution_costs_missing")
        action_kwargs = {
            "event_id": event_id,
            "bucket": bucket,
            "cash_clip": clip,
            "timeout_seconds": timeout,
            "order_mode": mode,
            "fill_distribution": distribution,
            "probabilities": probabilities,
            "current_shares_by_bucket": current_shares,
            "current_cost_by_bucket": current_cost,
            "confirmed_pending_shares_by_bucket": pending_shares,
            "confirmed_pending_cost_by_bucket": pending_cost,
            "target_shares_by_bucket": target_shares,
            "protected_buckets": ordered_support,
            "shortfall_buckets": tuple(sorted(shortfall & desired)),
            "breakthrough_buckets": tuple(sorted(breakthrough & desired)),
            "value_weights": delta_policy.value_weights,
            "execution_weights": delta_policy.execution_weights,
            "hard_gates": _hard_gates(
                snapshot,
                positive_gap=True,
                budget_ok=clip <= capital_available_usdc + EPS,
                resource_probe_ok=resource_probe_ok,
            ),
            "capital_available_usdc": capital_available_usdc,
            "minimum_profit_floor": minimum_profit_floor,
            "reservation_price": max(delta_policy.price_grid),
            "wait_cost": costs.get("wait", 0.0),
            "queue_risk": costs.get("queue", 0.0),
            "adverse_selection": costs.get("adverse_selection", 0.0),
            "reprice_risk": costs.get("reprice", 0.0),
            "capital_lock_cost": costs.get("capital_lock", 0.0),
            "fill_artifact_content_sha256": initial_fill.get(
                "fill_artifact_content_sha256"
            ),
            "fee_rate": basket_policy.fee_rate,
        }
        price_grid = (
            _reservation_grid(row=row, artifact=delta_policy)
            if mode == "MAKER"
            else tuple(
                sorted(
                    set(
                        price
                        for price in (*delta_policy.price_grid, row.ask_price)
                        if price + EPS >= row.ask_price
                    )
                )
            )
        )
        reservation = solve_reservation_price_v2(
            cash_clip=clip,
            price_grid=price_grid,
            action_kwargs={
                key: value
                for key, value in action_kwargs.items()
                if key not in {"cash_clip", "limit_price"}
            },
        )
        reserve = reservation.get("reservation_price")
        if reserve is None and mode != "MAKER":
            continue
        # A Maker leg may be individually negative while the complete basket
        # is jointly positive because floor/CVaR/concentration are nonlinear.
        # Keep the non-crossing market anchor as a bounded bundle candidate;
        # only the joint bundle selector may authorize it.
        reservation_price = (
            float(reserve) if reserve is not None else float(price)
        )
        if mode == "MAKER":
            maker_limit = min(
                float(price),
                _floor_to_tick(reservation_price, row.price_tick_size),
            )
            clip = min(float(clip), gap * maker_limit)
            if (
                clip <= EPS
                or maker_limit + EPS < row.bid_price
                or maker_limit >= row.ask_price - EPS
            ):
                continue
            price = maker_limit
            action_kwargs["cash_clip"] = clip
        elif (
            price > reservation_price + EPS
            or clip / price > gap + EPS
        ):
            continue
        resolved_fill = fill_resolver(
            station_id=snapshot.station_id,
            stage=snapshot.evidence.probability_stage,
            bucket=bucket,
            order_mode=mode,
            limit_price=price,
            timeout_seconds=timeout,
            queue_ahead_shares=row.maker_queue_ahead_shares,
            spread=max(0.0, row.ask_price - row.bid_price),
            depth=row.executable_depth_shares,
            quote_version=snapshot.quote_version,
        )
        final_distribution = resolved_fill.get("fill_distribution")
        final_costs = resolved_fill.get("execution_costs")
        if not isinstance(final_distribution, Sequence):
            raise ContractError("delta_v2_fill_distribution_missing")
        if not isinstance(final_costs, Mapping):
            raise ContractError("delta_v2_execution_costs_missing")
        action_kwargs.update({
            "fill_distribution": final_distribution,
            "reservation_price": reservation_price,
            "limit_price": price,
            "wait_cost": final_costs.get("wait", 0.0),
            "queue_risk": final_costs.get("queue", 0.0),
            "adverse_selection": final_costs.get("adverse_selection", 0.0),
            "reprice_risk": final_costs.get("reprice", 0.0),
            "capital_lock_cost": final_costs.get("capital_lock", 0.0),
            "fill_artifact_content_sha256": resolved_fill.get(
                "fill_artifact_content_sha256"
            ),
        })
        actions.append(evaluate_action_v2(**action_kwargs))
    maker_actions = [
        action for action in actions
        if str(dict(action.get("action") or {}).get("order_mode") or "").upper()
        == "MAKER"
    ]
    maker_action_buckets = {
        str(dict(action.get("action") or {}).get("bucket") or "")
        for action in maker_actions
    }
    for bucket in execution_gap_support:
        if bucket not in maker_action_buckets:
            maker_candidate_diagnostics.setdefault(
                bucket,
                "maker_candidate_rejected_by_reservation_or_size",
            )
    selection = select_complete_maker_bundle_v2(
        maker_actions,
        required_buckets=execution_gap_support,
        probabilities=probabilities,
        current_shares_by_bucket=current_shares,
        current_cost_by_bucket=current_cost,
        target_shares_by_bucket=target_shares,
        protected_buckets=ordered_support,
        shortfall_buckets=tuple(sorted(shortfall & desired)),
        breakthrough_buckets=tuple(sorted(breakthrough & desired)),
        value_weights=delta_policy.value_weights,
        execution_weights=delta_policy.execution_weights,
        capital_available_usdc=capital_available_usdc,
        minimum_profit_floor=minimum_profit_floor,
        fee_rate=basket_policy.fee_rate,
    )
    # Urgent FOK remains a single-bucket close-out mechanism.  It is never
    # allowed to coexist with a Maker bundle or bypass an incomplete basket.
    if (
        selection.get("authorized") is not True
        and len(execution_gap_support) == 1
        and confirmed_cancel_for_fok
    ):
        fok_selection = select_action_v2(
            [
                action for action in actions
                if str(
                    dict(action.get("action") or {}).get("order_mode") or ""
                ).upper() == "FOK"
            ],
            execution_weights=delta_policy.execution_weights,
        )
        fok_selected = fok_selection["selected_action"]
        if fok_selected.get("authorized") is True:
            selection = {
                "schema": "delta_basket_bundle_selection_v2",
                "required_buckets": list(execution_gap_support),
                "selected_actions": [fok_selected],
                "selected_bundle": None,
                "selected_action_sha256s": [
                    str(fok_selected.get("content_sha256") or "")
                ],
                "selected_score": float(
                    fok_selected.get("delta_basket_score_v2") or 0.0
                ),
                "capital_reserved_usdc": float(
                    fok_selected.get("capital_reserved_usdc") or 0.0
                ),
                "basket_complete": True,
                "authorized": True,
                "blockers": [],
                "hold_selected": False,
                "execution_mode": "SINGLE_BUCKET_FOK",
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
                "content_sha256": content_sha256({
                    "schema": "delta_basket_bundle_selection_v2",
                    "selected_action_sha256s": [
                        str(fok_selected.get("content_sha256") or "")
                    ],
                    "execution_mode": "SINGLE_BUCKET_FOK",
                }),
            }
    if envelope_entry_blocked and selection.get("authorized") is True:
        selection = {
            **selection,
            "selected_actions": [],
            "selected_bundle": None,
            "selected_action_sha256s": [],
            "selected_score": 0.0,
            "capital_reserved_usdc": 0.0,
            "basket_complete": False,
            "authorized": False,
            "blockers": ["market_center_outside_model_envelope"],
            "hold_selected": True,
        }
    if not regime_direction_allowed and selection.get("authorized") is True:
        selection = {
            **selection,
            "selected_actions": [],
            "selected_bundle": None,
            "selected_action_sha256s": [],
            "selected_score": 0.0,
            "capital_reserved_usdc": 0.0,
            "basket_complete": False,
            "authorized": False,
            "blockers": ["regime_direction_not_allowed"],
            "hold_selected": True,
        }
    if snapshot.terminal and selection.get("authorized") is True:
        selection = {
            **selection,
            "selected_actions": [],
            "selected_bundle": None,
            "selected_action_sha256s": [],
            "selected_score": 0.0,
            "capital_reserved_usdc": 0.0,
            "basket_complete": False,
            "authorized": False,
            "blockers": ["terminal_stop_adding"],
            "hold_selected": True,
        }
    realised = evaluate_basket_value_v2(
        probabilities=probabilities,
        shares_by_bucket=current_shares,
        cost_by_bucket=current_cost,
        protected_buckets=ordered_support,
        shortfall_buckets=tuple(sorted(shortfall & desired)),
        breakthrough_buckets=tuple(sorted(breakthrough & desired)),
        weights=delta_policy.value_weights,
        target_shares_by_bucket=target_shares,
    )
    targets: list[dict[str, Any]] = []
    selected_evaluations = [
        dict(row) for row in selection.get("selected_actions", ())
        if isinstance(row, Mapping)
    ]
    selected_actions_by_bucket = {
        str(dict(row.get("action") or {}).get("bucket") or ""): dict(
            row.get("action") or {}
        )
        for row in selected_evaluations
    }
    selected_buckets = tuple(sorted(
        bucket for bucket in selected_actions_by_bucket if bucket
    ))
    if set(selected_buckets) - set(execution_gap_support):
        raise ContractError(
            "delta_v2_selected_action_outside_execution_gap_support"
        )
    if selection.get("authorized") is True and set(selected_buckets) != set(
        execution_gap_support
    ):
        raise ContractError("delta_v2_selected_bundle_incomplete")
    for bucket in final_support:
        row = by_bucket[bucket]
        is_selected = (
            selection.get("authorized") is True
            and bucket in selected_actions_by_bucket
        )
        action = selected_actions_by_bucket.get(bucket, {}) if is_selected else {}
        targets.append({
            "bucket": bucket,
            "ordinal": row.ordinal,
            "role": (
                "BASE" if bucket in base else "BIAS" if bucket in bias else "EDGE"
            ),
            "base_score": scores.get(bucket, (0.0, 0.0, 0.0))[0],
            "bias_score": scores.get(bucket, (0.0, 0.0, 0.0))[1],
            "edge_score": scores.get(bucket, (0.0, 0.0, 0.0))[2],
            "target_shares": target_shares[bucket],
            "filled_shares": row.filled_shares,
            "filled_cost_usdc": row.filled_cost_usdc,
            "pending_shares": row.pending_shares,
            "pending_notional_usdc": row.pending_notional_usdc,
            "delta_shares": max(0.0, gap_by_bucket[bucket]),
            "clip_shares": float(action.get("maximum_shares") or 0.0),
            "clip_notional_usdc": float(action.get("cash_clip") or 0.0),
            "candidate_price": float(action.get("limit_price") or 0.0),
            "limit_price": float(action.get("limit_price") or 0.0),
            "effective_price": float(action.get("limit_price") or 0.0),
            "execution_candidate": (
                "TAKER_FOK"
                if action.get("order_mode") == "FOK"
                else "MAKER"
                if action.get("order_mode") == "MAKER"
                else "NONE"
            ),
            "execution_contract": (
                FOK_EXECUTION_CONTRACT
                if action.get("order_mode") == "FOK"
                else MAKER_EXECUTION_CONTRACT
                if action.get("order_mode") == "MAKER"
                else ""
            ),
            "post_only": action.get("order_mode") == "MAKER",
            "cancel_acknowledged": (
                bool(confirmed_cancel_for_fok)
                if action.get("order_mode") == "FOK"
                else False
            ),
            "target_price_id": (
                "delta_v2_price_"
                + str(
                    next(
                        (
                            value.get("content_sha256")
                            for value in selected_evaluations
                            if str(
                                dict(value.get("action") or {}).get("bucket")
                                or ""
                            )
                            == bucket
                        ),
                        "",
                    )
                )[:32]
                if is_selected
                else ""
            ),
            "execution_reason_codes": (
                [] if is_selected else ["delta_v2_action_not_selected"]
            ),
        })
    reason_codes: list[str] = []
    if selection.get("authorized") is not True:
        reason_codes.extend(str(value) for value in selection.get("blockers", ()))
        reason_codes.extend(
            f"{reason}:{bucket}"
            for bucket, reason in sorted(
                maker_candidate_diagnostics.items()
            )
        )
        reason_codes.append("delta_v2_complete_basket_hold_selected")
    if not center_stable:
        reason_codes.append("center_probability_lead_unstable")
    if not regime_direction_allowed:
        reason_codes.append("regime_direction_not_allowed")
    if envelope_entry_blocked:
        reason_codes.append("market_center_outside_model_envelope")
    delta_v2_audit = {
        "schema": "delta_basket_score_authorization_v2",
        "selected_actions": selected_evaluations,
        "selected_bundle_sha256": selection.get("selected_bundle_sha256"),
        "authorized": selection.get("authorized") is True,
        "blockers": list(selection.get("blockers", ())),
        "delta_basket_score_v2": float(
            selection.get("selected_score", 0.0)
        ),
        "policy_artifact_id": delta_policy.artifact_id,
        "policy_artifact_sha256": delta_policy.content_sha256,
        "fill_distribution_artifact_sha256": (
            delta_policy.fill_distribution_artifact_sha256
        ),
        "realised_value_sha256": realised["content_sha256"],
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    delta_v2_audit["content_sha256"] = content_sha256(delta_v2_audit)
    selected_before = tuple(
        row.bucket for row in snapshot.buckets if row.selected_before
    )
    ordered_core = tuple(
        str(bucket)
        for bucket in core_transition["next_core_buckets"]
    )
    basket_state = resolve_basket_state(
        bucket_ordinals={
            row.bucket: row.ordinal for row in snapshot.buckets
        },
        desired_support=final_support,
        selected_before=selected_before,
        positive_gap_buckets=execution_gap_support,
        execution_authorized=selection.get("authorized") is True,
        order_state_known=snapshot.order_state_known,
        stop_adding=snapshot.terminal,
    )
    identity = {
        "schema": SCHEMA,
        "snapshot_sha256": snapshot.content_sha256,
        "quote_version": snapshot.quote_version,
        "inventory_version": snapshot.inventory_version,
        "order_state_version": snapshot.order_state_version,
        "station_id": snapshot.station_id,
        "event_date": snapshot.event_date,
        "observed_at": snapshot.observed_at,
        "market_id": snapshot.market_id,
        "state": "TRADE" if selection.get("authorized") is True else "HOLD",
        "basket_state": basket_state.state.value,
        "basket_state_reason": basket_state.reason,
        "basket_state_transition": basket_state.to_dict(),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "center_bucket": center.bucket,
        "runner_up_bucket": runner_up,
        "center_lead_probability": center_lead,
        "center_lead_threshold_probability": (
            basket_policy.center_lead_min_probability
        ),
        "center_stable": center_stable,
        "direction": direction.value if isinstance(direction, Direction) else str(direction),
        "diagnostic_lower_ordinal": snapshot.evidence.model_envelope_lower_ordinal,
        "diagnostic_upper_ordinal": snapshot.evidence.model_envelope_upper_ordinal,
        "candidate_lower_ordinal": candidate_lower,
        "candidate_upper_ordinal": candidate_upper,
        "market_center_ordinal": market_center_ordinal,
        "market_center_bucket": market_center_bucket,
        "envelope_entry_gate": {
            "blocked": envelope_entry_blocked,
            "market_center_ordinal": market_center_ordinal,
            "market_center_bucket": market_center_bucket,
            "candidate_lower_ordinal": int(candidate_lower),
            "candidate_upper_ordinal": int(candidate_upper),
            "blocks_new_intents_only": True,
        },
        "disagreement_expansion_layers": expansion_layers,
        "underreach_risk_buckets": sorted(shortfall),
        "upside_breakthrough_risk_buckets": sorted(breakthrough),
        "current_core_buckets": list(snapshot.current_core_buckets),
        "current_direction": (
            snapshot.current_direction.value
            if snapshot.current_direction is not None
            else None
        ),
        "core_buckets": list(ordered_core),
        "core_transition": core_transition,
        "candidate_support": list(ordered_support),
        "final_support": list(final_support),
        "basket_completion_required": True,
        "basket_target_activated": basket_target_activated,
        "execution_gap_support": list(execution_gap_support),
        "selected_buckets": list(selected_buckets),
        "excluded_candidate_buckets": [
            {
                "bucket": bucket,
                "reason": "zero_joint_target_inventory",
            }
            for bucket in ordered_support
            if bucket not in final_support
        ],
        "selected_before": list(selected_before),
        "targets": targets,
        "target_solution": target_solution,
        "delta_v2_action_selection": selection,
        "maker_candidate_diagnostics": dict(
            sorted(maker_candidate_diagnostics.items())
        ),
        "delta_v2_audit": delta_v2_audit,
        "scenario_pnl_usdc": realised["scenario_pnl"],
        "expected_pnl_usdc": realised["expected_pnl"],
        "profit_floor_usdc": realised["profit_floor"],
        "cvar_usdc": realised["cvar"],
        "basket_objective_form": objective_form,
        "basket_curve_variance": target_solution.get("target_variance"),
        "basket_curve_score": target_solution.get("target_curve_score"),
        # The curve dials the charter is steered by.  They exist in the solve
        # identity either way; carrying them onto the plan is what makes the
        # win-rate target an auditable daily number instead of an intention --
        # the whole reason the 21-plan measurement that exposed the lottery
        # shape had to be reconstructed by hand from scenario curves.
        "basket_win_probability": target_solution.get(
            "target_win_probability"
        ),
        "basket_odds_ratio": target_solution.get("target_odds_ratio"),
        "basket_support_floor_pnl_usdc": target_solution.get(
            "target_support_floor_pnl"
        ),
        "basket_support_floor_fraction": target_solution.get(
            "support_scenario_floor_fraction"
        ),
        "basket_floor_rejected_candidate_count": target_solution.get(
            "floor_rejected_candidate_count"
        ),
        # Which windows were on the table, what each was worth, and why this
        # one was seated.  Without it a support decision is unreviewable: the
        # incumbent rule was wrong for months precisely because nothing
        # recorded the alternatives it never considered.
        "basket_support_selection": support_selection,
        "total_target_shares": sum(target_shares.values()),
        "total_delta_notional_usdc": float(
            selection.get("capital_reserved_usdc") or 0.0
        ),
        "basket_optimizer_id": "DeltaBasketScoreV2",
        "basket_optimizer_converged": True,
        "fok_cancel_acknowledged": bool(confirmed_cancel_for_fok),
        "target_only": True,
        "places_orders": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "plan_id": "delta_v2_plan_" + content_sha256(identity)[:32],
        "content_sha256": content_sha256(identity),
    }


__all__ = ["SCHEMA", "compile_delta_v2_runtime_plan"]
