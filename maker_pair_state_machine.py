"""Pure policy primitives for a YES-biased maker pair state machine.

The module deliberately has no network, database, filesystem, or order-entry
code.  It defines ranking, exposure invariants, passive prices, and the next
safe action for a coordinator that is hosted by an already-authorized
production service.  All exchange effects still require explicit order and
fill reconciliation outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Iterable, Sequence


EPSILON = 1e-12


class Phase(str, Enum):
    PAUSED = "PAUSED"
    IDLE = "IDLE"
    YES_WORKING = "YES_WORKING"
    YES_EXPOSED = "YES_EXPOSED"
    NO_HEDGING = "NO_HEDGING"
    BALANCED = "BALANCED"
    EXITING_YES = "EXITING_YES"
    YES_RESIDUAL_HOLD = "YES_RESIDUAL_HOLD"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"


class ActionKind(str, Enum):
    WAIT = "WAIT"
    PLACE = "PLACE"
    CANCEL = "CANCEL"
    HOLD = "HOLD"
    RECONCILE = "RECONCILE"


@dataclass(frozen=True)
class TopOfBook:
    best_bid: float | None
    best_ask: float | None
    tick_size: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    def complete(self) -> bool:
        bid, ask, tick = self.best_bid, self.best_ask, self.tick_size
        return bool(
            bid is not None
            and ask is not None
            and math.isfinite(bid)
            and math.isfinite(ask)
            and math.isfinite(tick)
            and 0.0 < bid < ask < 1.0
            and 0.0 < tick < 1.0
        )


@dataclass(frozen=True)
class BucketBook:
    outcome: str
    yes_token: str
    no_token: str
    yes: TopOfBook
    no: TopOfBook
    maker_fee_yes_per_share_usdc: float
    maker_fee_no_per_share_usdc: float
    fee_evidence_complete: bool

    def market_yes_probability(self) -> float | None:
        """Return the consensus of YES midpoint and complemented NO midpoint."""

        if not self.yes.complete() or not self.no.complete():
            return None
        yes_mid = (float(self.yes.best_bid) + float(self.yes.best_ask)) / 2.0
        yes_from_no = 1.0 - (
            float(self.no.best_bid) + float(self.no.best_ask)
        ) / 2.0
        probability = (yes_mid + yes_from_no) / 2.0
        return probability if 0.0 < probability < 1.0 else None

    def ranking_spread(self) -> float:
        if not self.yes.complete() or not self.no.complete():
            return math.inf
        return (float(self.yes.best_ask) - float(self.yes.best_bid)) + (
            float(self.no.best_ask) - float(self.no.best_bid)
        )


@dataclass(frozen=True)
class MakerPolicy:
    top_bucket_count: int = 2
    target_shares_per_bucket: float = 5.0
    minimum_order_shares: float = 5.0
    minimum_edge_per_share_usdc: float = 0.005
    per_bucket_unmatched_yes_cap: float = 5.0
    global_unmatched_yes_cap: float = 10.0
    selection_confirmation_versions: int = 3
    max_snapshot_age_seconds: float = 2.0
    minimum_rest_seconds: float = 1.5
    yes_work_timeout_seconds: float = 30.0
    hedge_soft_timeout_seconds: float = 15.0
    hedge_hard_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        positive = (
            self.target_shares_per_bucket,
            self.minimum_order_shares,
            self.per_bucket_unmatched_yes_cap,
            self.global_unmatched_yes_cap,
            self.max_snapshot_age_seconds,
            self.minimum_rest_seconds,
            self.yes_work_timeout_seconds,
            self.hedge_soft_timeout_seconds,
            self.hedge_hard_timeout_seconds,
        )
        if any(not math.isfinite(value) or value <= 0.0 for value in positive):
            raise ValueError("maker_policy_positive_value_invalid")
        if self.top_bucket_count != 2:
            raise ValueError("maker_policy_requires_exactly_two_top_buckets")
        if self.selection_confirmation_versions < 1:
            raise ValueError("selection_confirmation_versions_invalid")
        if self.minimum_edge_per_share_usdc < 0.0:
            raise ValueError("minimum_edge_invalid")
        if self.target_shares_per_bucket > self.per_bucket_unmatched_yes_cap + EPSILON:
            raise ValueError("target_exceeds_single_leg_cap")
        if self.target_shares_per_bucket + EPSILON < self.minimum_order_shares:
            raise ValueError("target_below_exchange_minimum")
        if self.hedge_soft_timeout_seconds >= self.hedge_hard_timeout_seconds:
            raise ValueError("hedge_timeout_order_invalid")


@dataclass(frozen=True)
class ExposureState:
    outcome: str
    phase: Phase = Phase.IDLE
    yes_filled_shares: float = 0.0
    no_filled_shares: float = 0.0
    yes_buy_open_shares: float = 0.0
    no_buy_open_shares: float = 0.0
    yes_sell_open_shares: float = 0.0
    yes_fill_vwap: float | None = None
    selection_streak: int = 0
    reconciliation_required: bool = False

    @property
    def unmatched_yes_shares(self) -> float:
        return self.yes_filled_shares - self.no_filled_shares

    @property
    def worst_case_yes_exposure(self) -> float:
        return self.yes_filled_shares + self.yes_buy_open_shares - self.no_filled_shares

    @property
    def worst_case_no_excess(self) -> float:
        return (
            self.no_filled_shares
            + self.no_buy_open_shares
            + self.yes_sell_open_shares
            - self.yes_filled_shares
        )


@dataclass(frozen=True)
class MakerQuote:
    side: str
    action: str
    price: float
    shares: float
    quote_mode: str
    edge_cap_price: float | None = None


@dataclass(frozen=True)
class StateAction:
    kind: ActionKind
    phase: Phase
    reason: str
    quote: MakerQuote | None = None


def select_top_buckets(
    books: Iterable[BucketBook], *, count: int = 2
) -> tuple[str, ...]:
    """Rank complete books by market YES probability, then tighter spread."""

    if count != 2:
        raise ValueError("top_bucket_count_must_equal_two")
    ranked: list[tuple[float, float, str]] = []
    seen: set[str] = set()
    for book in books:
        if book.outcome in seen:
            raise ValueError(f"duplicate_bucket_outcome:{book.outcome}")
        seen.add(book.outcome)
        probability = book.market_yes_probability()
        if probability is None:
            continue
        ranked.append((-probability, book.ranking_spread(), book.outcome))
    ranked.sort()
    return tuple(outcome for _probability, _spread, outcome in ranked[:count])


def exposure_violations(
    states: Sequence[ExposureState], policy: MakerPolicy
) -> tuple[str, ...]:
    """Validate both current and all-open-orders-fill exposure."""

    violations: list[str] = []
    global_worst_case_yes = 0.0
    for state in states:
        values = (
            state.yes_filled_shares,
            state.no_filled_shares,
            state.yes_buy_open_shares,
            state.no_buy_open_shares,
            state.yes_sell_open_shares,
        )
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            violations.append(f"negative_or_invalid_shares:{state.outcome}")
            continue
        if state.no_buy_open_shares > EPSILON and state.yes_sell_open_shares > EPSILON:
            violations.append(f"conflicting_risk_reducers:{state.outcome}")
        if state.worst_case_no_excess > EPSILON:
            violations.append(f"potential_no_directional_exposure:{state.outcome}")
        if state.worst_case_yes_exposure > policy.per_bucket_unmatched_yes_cap + EPSILON:
            violations.append(f"single_leg_yes_cap_exceeded:{state.outcome}")
        if state.no_filled_shares > state.yes_filled_shares + EPSILON:
            violations.append(f"realized_no_directional_exposure:{state.outcome}")
        global_worst_case_yes += max(0.0, state.worst_case_yes_exposure)
    if global_worst_case_yes > policy.global_unmatched_yes_cap + EPSILON:
        violations.append("global_yes_exposure_cap_exceeded")
    return tuple(violations)


def _tick_digits(tick_size: float) -> int:
    return max(0, min(9, int(math.ceil(-math.log10(tick_size)))))


def _floor_to_tick(value: float, tick_size: float) -> float:
    ticks = math.floor((value + EPSILON) / tick_size)
    return round(ticks * tick_size, _tick_digits(tick_size))


def _maker_buy_price(book: TopOfBook) -> tuple[float, str] | None:
    if not book.complete():
        return None
    bid, ask, tick = float(book.best_bid), float(book.best_ask), book.tick_size
    improved = round(bid + tick, _tick_digits(tick))
    if improved < ask - EPSILON:
        return improved, "improve_best_bid_one_tick"
    return bid, "join_best_bid"


def _maker_sell_price(book: TopOfBook) -> tuple[float, str] | None:
    if not book.complete():
        return None
    bid, ask, tick = float(book.best_bid), float(book.best_ask), book.tick_size
    improved = round(ask - tick, _tick_digits(tick))
    if improved > bid + EPSILON:
        return improved, "improve_best_ask_one_tick"
    return ask, "join_best_ask"


def maker_pair_prices(book: BucketBook) -> tuple[MakerQuote, MakerQuote] | None:
    yes = _maker_buy_price(book.yes)
    no = _maker_buy_price(book.no)
    if yes is None or no is None:
        return None
    return (
        MakerQuote("YES", "BUY", yes[0], 0.0, yes[1]),
        MakerQuote("NO", "BUY", no[0], 0.0, no[1]),
    )


def plan_yes_entry(
    *,
    book: BucketBook,
    state: ExposureState,
    selected_outcomes: Sequence[str],
    policy: MakerPolicy,
    global_worst_case_yes_shares: float,
    snapshot_age_seconds: float,
) -> StateAction:
    """Plan only the risk-opening YES leg; never place NO speculatively."""

    if state.reconciliation_required:
        return StateAction(ActionKind.RECONCILE, Phase.RECONCILE_REQUIRED, "order_outcome_unknown")
    if exposure_violations([state], policy):
        return StateAction(
            ActionKind.RECONCILE,
            Phase.RECONCILE_REQUIRED,
            "exposure_invariant_invalid",
        )
    if snapshot_age_seconds > policy.max_snapshot_age_seconds:
        return StateAction(ActionKind.WAIT, Phase.PAUSED, "market_ws_snapshot_stale")
    if len(set(selected_outcomes)) != policy.top_bucket_count:
        return StateAction(ActionKind.WAIT, Phase.PAUSED, "top_two_selection_incomplete")
    if state.outcome not in selected_outcomes:
        return StateAction(ActionKind.WAIT, Phase.IDLE, "outside_current_top_two")
    if state.selection_streak < policy.selection_confirmation_versions:
        return StateAction(ActionKind.WAIT, Phase.IDLE, "top_two_not_yet_stable")
    if any(
        value > EPSILON
        for value in (
            state.yes_filled_shares,
            state.no_filled_shares,
            state.yes_buy_open_shares,
            state.no_buy_open_shares,
            state.yes_sell_open_shares,
        )
    ):
        return StateAction(ActionKind.WAIT, state.phase, "cycle_already_started")
    pair = maker_pair_prices(book)
    if pair is None:
        return StateAction(ActionKind.WAIT, Phase.IDLE, "maker_quote_evidence_incomplete")
    fees = (book.maker_fee_yes_per_share_usdc, book.maker_fee_no_per_share_usdc)
    if (
        not book.fee_evidence_complete
        or any(not math.isfinite(fee) or fee < 0.0 for fee in fees)
    ):
        return StateAction(ActionKind.WAIT, Phase.PAUSED, "maker_fee_evidence_incomplete")
    yes_quote, no_reference = pair
    fee_per_set = sum(fees)
    edge = 1.0 - yes_quote.price - no_reference.price - fee_per_set
    if edge + EPSILON < policy.minimum_edge_per_share_usdc:
        return StateAction(ActionKind.WAIT, Phase.IDLE, "pair_edge_below_threshold")
    shares = policy.target_shares_per_bucket
    if shares > policy.per_bucket_unmatched_yes_cap + EPSILON:
        return StateAction(ActionKind.WAIT, Phase.PAUSED, "single_leg_yes_cap_would_be_exceeded")
    if global_worst_case_yes_shares + shares > policy.global_unmatched_yes_cap + EPSILON:
        return StateAction(ActionKind.WAIT, Phase.PAUSED, "global_yes_cap_would_be_exceeded")
    return StateAction(
        ActionKind.PLACE,
        Phase.YES_WORKING,
        "top_two_profitable_pair_yes_first",
        MakerQuote(
            side="YES",
            action="BUY",
            price=yes_quote.price,
            shares=shares,
            quote_mode=yes_quote.quote_mode,
            edge_cap_price=(
                1.0
                - no_reference.price
                - fee_per_set
                - policy.minimum_edge_per_share_usdc
            ),
        ),
    )


def hedge_edge_floor(age_seconds: float, policy: MakerPolicy) -> float | None:
    """Relax target edge linearly to break-even, never to an automatic loss."""

    age = max(0.0, float(age_seconds))
    if age >= policy.hedge_hard_timeout_seconds:
        return None
    if age <= policy.hedge_soft_timeout_seconds:
        return policy.minimum_edge_per_share_usdc
    duration = policy.hedge_hard_timeout_seconds - policy.hedge_soft_timeout_seconds
    remaining = policy.hedge_hard_timeout_seconds - age
    return policy.minimum_edge_per_share_usdc * remaining / duration


def plan_no_hedge(
    *,
    book: BucketBook,
    state: ExposureState,
    policy: MakerPolicy,
    exposure_age_seconds: float,
    snapshot_age_seconds: float,
) -> StateAction:
    """Plan an equal-or-smaller NO hedge only after verified YES fills."""

    if state.reconciliation_required:
        return StateAction(ActionKind.RECONCILE, Phase.RECONCILE_REQUIRED, "order_outcome_unknown")
    if exposure_violations([state], policy):
        return StateAction(
            ActionKind.RECONCILE,
            Phase.RECONCILE_REQUIRED,
            "exposure_invariant_invalid",
        )
    if snapshot_age_seconds > policy.max_snapshot_age_seconds:
        if any(
            value > EPSILON
            for value in (
                state.yes_buy_open_shares,
                state.no_buy_open_shares,
                state.yes_sell_open_shares,
            )
        ):
            return StateAction(ActionKind.CANCEL, Phase.PAUSED, "market_ws_snapshot_stale")
        return StateAction(ActionKind.WAIT, Phase.PAUSED, "market_ws_snapshot_stale")
    if state.yes_buy_open_shares > EPSILON:
        if (
            state.unmatched_yes_shares + EPSILON < policy.minimum_order_shares
            and exposure_age_seconds + EPSILON < policy.yes_work_timeout_seconds
        ):
            return StateAction(
                ActionKind.WAIT,
                Phase.YES_WORKING,
                "complete_minimum_yes_lot_before_hedging",
            )
        return StateAction(ActionKind.CANCEL, Phase.YES_EXPOSED, "cancel_yes_remainder_before_hedging")
    if state.no_buy_open_shares > EPSILON:
        return StateAction(ActionKind.WAIT, Phase.NO_HEDGING, "no_hedge_already_working")
    if state.yes_sell_open_shares > EPSILON:
        return StateAction(ActionKind.WAIT, Phase.EXITING_YES, "yes_exit_already_working")
    unmatched = state.unmatched_yes_shares
    if unmatched <= EPSILON:
        phase = Phase.BALANCED if state.yes_filled_shares > EPSILON else Phase.IDLE
        return StateAction(ActionKind.WAIT, phase, "no_unmatched_yes")
    if unmatched + EPSILON < policy.minimum_order_shares:
        return StateAction(
            ActionKind.HOLD,
            Phase.YES_RESIDUAL_HOLD,
            "subminimum_yes_residual_hold_to_settlement",
        )
    if state.yes_fill_vwap is None or not 0.0 < state.yes_fill_vwap < 1.0:
        return StateAction(ActionKind.RECONCILE, Phase.RECONCILE_REQUIRED, "yes_fill_vwap_missing")
    fees = (book.maker_fee_yes_per_share_usdc, book.maker_fee_no_per_share_usdc)
    if (
        not book.fee_evidence_complete
        or any(not math.isfinite(fee) or fee < 0.0 for fee in fees)
    ):
        return StateAction(ActionKind.WAIT, Phase.PAUSED, "maker_fee_evidence_incomplete")
    edge_floor = hedge_edge_floor(exposure_age_seconds, policy)
    if edge_floor is None:
        yes_exit = _maker_sell_price(book.yes)
        if yes_exit is None:
            return StateAction(
                ActionKind.HOLD,
                Phase.YES_RESIDUAL_HOLD,
                "hard_timeout_yes_exit_unavailable_hold_to_settlement",
            )
        return StateAction(
            ActionKind.PLACE,
            Phase.EXITING_YES,
            "hedge_hard_timeout_exit_yes_maker_only",
            MakerQuote("YES", "SELL", yes_exit[0], unmatched, yes_exit[1]),
        )
    passive_no = _maker_buy_price(book.no)
    if passive_no is None:
        return StateAction(ActionKind.WAIT, Phase.YES_EXPOSED, "no_maker_quote_missing")
    cap = 1.0 - state.yes_fill_vwap - sum(fees) - edge_floor
    price = min(passive_no[0], _floor_to_tick(cap, book.no.tick_size))
    if price <= EPSILON or price >= float(book.no.best_ask) - EPSILON:
        return StateAction(ActionKind.WAIT, Phase.YES_EXPOSED, "no_profitable_passive_price_available")
    mode = passive_no[1] if abs(price - passive_no[0]) <= EPSILON else "edge_capped_passive_bid"
    return StateAction(
        ActionKind.PLACE,
        Phase.NO_HEDGING,
        "verified_yes_fill_equal_no_hedge",
        MakerQuote("NO", "BUY", price, unmatched, mode, edge_cap_price=cap),
    )


def replacement_reason(
    *,
    side: str,
    action: str,
    current_price: float,
    desired_price: float,
    best_opposite_price: float,
    tick_size: float,
    rest_age_seconds: float,
    minimum_rest_seconds: float,
    selected: bool,
    economics_valid: bool,
    snapshot_fresh: bool,
) -> str | None:
    """Return why an open maker order must be cancelled/replaced.

    Safety cancellations bypass the minimum resting time.  A non-safety price
    chase requires at least ``minimum_rest_seconds`` at the caller and at
    least one full tick of drift.
    """

    normalized_side, normalized_action = side.upper(), action.upper()
    crossing = (
        normalized_action == "BUY" and current_price >= best_opposite_price - EPSILON
    ) or (
        normalized_action == "SELL" and current_price <= best_opposite_price + EPSILON
    )
    if not snapshot_fresh:
        return "market_ws_snapshot_stale"
    if crossing:
        return "maker_order_would_cross"
    if normalized_side == "YES" and normalized_action == "BUY" and not selected:
        return "bucket_left_top_two"
    if not economics_valid:
        return "complete_pair_economics_invalid"
    if rest_age_seconds < 0.0 or minimum_rest_seconds <= 0.0:
        return "order_rest_age_invalid"
    if rest_age_seconds + EPSILON < minimum_rest_seconds:
        return None
    if abs(desired_price - current_price) + EPSILON >= tick_size:
        return "price_drift_one_tick_or_more"
    return None


__all__ = [
    "ActionKind",
    "BucketBook",
    "ExposureState",
    "MakerPolicy",
    "MakerQuote",
    "Phase",
    "StateAction",
    "TopOfBook",
    "exposure_violations",
    "hedge_edge_floor",
    "maker_pair_prices",
    "plan_no_hedge",
    "plan_yes_entry",
    "replacement_reason",
    "select_top_buckets",
]
