"""The paper ledger and production must not own two copies of one rule.

Standing user ruling (2026-08-22, restated 2026-08-26 after it was broken
again): any selection, sizing or execution rule moves on both sides or on
neither.  Promising to remember has now failed more than once, so this file
is the mechanism instead of the promise.

What actually goes wrong is not a name collision -- the ledger's copies were
all *renamed* (``_resting_ladder`` for ``_maker_ladder``, ``_leg_value_cap``
for ``_hedge_value_cap``), so a duplicate-name scan finds nothing and the two
drift in silence.  Measured on 2026-08-26: the two ladders disagreed on 37 of
64 sampled books and had done so since the day the ledger was told it was
"the same construction, same constants, so the two cannot drift".

So there are two devices here:

* **Equivalence** -- every pair that is one rule is exercised over a grid and
  must agree exactly.  This is what catches an edit to one side.
* **Completeness** -- every module-level rule constant the ledger declares is
  classified below as production's or as this lane's own, with a reason.  A
  new one fails until somebody says which it is.  This is what catches a rule
  that gets added to one side and never reaches the other.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import paper_trading_ledger as L  # noqa: E402
from bayes_basket_target import delta_v2_runtime as P  # noqa: E402
from bayes_basket_target.basket_kernel import delta_v2 as K  # noqa: E402


# --------------------------------------------------------------------------
# 1. Constants that are one value.  Written as (ledger name, owner, owner
#    name) so a reader can see where the single definition lives.
# --------------------------------------------------------------------------
BOUND_CONSTANTS = [
    ("KELLY_FRACTION", K, "DEFAULT_KELLY_FRACTION"),
    ("MAX_STAKE_FRACTION_PER_SCOPE", K, "MAX_STAKE_FRACTION_PER_SCOPE"),
    ("STAGE_BUDGET_SHARE", K, "STAGE_BUDGET_SHARE"),
    # Charter 70: one kernel constant, imported by both lanes.
    ("EXCHANGE_LOT_TOP_UP", K, "EXCHANGE_LOT_TOP_UP"),
    # User ruling 2026-09-08: the two-段 share shape's one dial.  Bound
    # explicitly because the ledger imports it rather than declaring it, and
    # an import binds the name without proving the two lanes read one value.
    ("KELLY_EDGE_TILT_FRACTION", K, "KELLY_EDGE_TILT_FRACTION"),
    ("MINIMUM_LEG_ALL_IN_ASK", P, "MINIMUM_LEG_ALL_IN_ASK"),
    ("MAKER_RESTING_LADDER_STEPS", P, "MAKER_LADDER_STEPS"),
    ("MAKER_RESTING_IMPROVE_TICKS", P, "MAKER_LADDER_IMPROVE_TICKS"),
    ("CERTAINTY_GATE", P, "CERTAINTY_GATE_SIGNALS"),
]


def test_the_two_sides_read_one_value_for_every_shared_constant() -> None:
    for ledger_name, owner, owner_name in BOUND_CONSTANTS:
        assert getattr(L, ledger_name) == getattr(owner, owner_name), (
            f"{ledger_name} has drifted from {owner.__name__}.{owner_name}"
        )


def test_the_stage_shares_cover_every_stage_the_ledger_walks() -> None:
    # A stage missing from the table silently falls back to the whole
    # allowance, which is not a default anybody chose.
    for stage in L.STAGES:
        assert stage in K.STAGE_BUDGET_SHARE, stage


# --------------------------------------------------------------------------
# 2. Functions that are one rule, checked by behaviour rather than by name.
# --------------------------------------------------------------------------
class _Row:
    def __init__(self, bid: float, ask: float, tick: float) -> None:
        self.bid_price = bid
        self.ask_price = ask
        self.price_tick_size = tick
        self.ask_available = True


_BOOKS = [
    (0.20, 0.23), (0.36, 0.37), (0.28, 0.32), (0.12, 0.18),
    (0.02, 0.05), (0.001, 0.02), (0.45, 0.55), (0.05, 0.30),
    (0.10, 0.11), (0.33, 0.90), (0.88, 0.90), (0.48, 0.49),
]
_TICKS = (0.001, 0.005, 0.01)
_CAPS = (None, 0.0795, 0.212, 0.005, 0.0106, 0.45)


def test_the_resting_ladder_is_productions_maker_ladder() -> None:
    """One band, one implementation.

    The ledger's copy rounded to six decimals where production floors to the
    tick, so this lane simulated orders at prices the exchange does not
    accept -- 0.2864 on a tick-0.001 book -- and every ROI figure it produced
    rested on them.
    """

    for tick in _TICKS:
        for bid, ask in _BOOKS:
            for cap in _CAPS:
                # The ledger folds its research-arm cap into the ceiling
                # before calling production's ladder, which no longer takes
                # one (charter 63 retired, 2026-09-03).
                # Charter 94: under the bid-anchored shape the cap is not
                # read at all -- the ladder is the anchor's on both sides.
                assert L._resting_ladder(
                    _Row(bid, ask, tick), value_cap=cap
                ) == P._maker_ladder(
                    bid=bid,
                    ask=(
                        ask if cap is None or P.MAKER_LADDER_SHAPE == "bid_anchored"
                        else min(ask, cap)
                    ),
                    tick=tick,
                ), (tick, bid, ask, cap)


def test_charter_63_is_retired_the_clamp_has_no_production_definition() -> None:
    """The per-leg value clamp left production on 2026-09-03.

    Under charter 65 (taker is the law) the taker candidate path never read
    it, and 133 missing legs after the 2026-09-02 arming were misattributed
    to it before charter 68 was found to own them.  What remains is the
    ledger's maker_resting research arm, which carries its own lambda and
    must answer None for every other execution family -- the family this
    lane scores production under.
    """

    for name in (
        "MAKER_HEDGE_VALUE_LAMBDA",
        "MAKER_SHOULDER_VALUE_EXEMPT",
        "MAKER_SHOULDER_VALUE_LAMBDA",
        "_hedge_value_cap",
    ):
        assert not hasattr(P, name), name
    import inspect

    assert "value_cap" not in inspect.signature(P._maker_ladder).parameters
    assert "value_cap" not in inspect.signature(
        P._lane_purchase_levels
    ).parameters

    # The ledger's research-arm cap still computes, but the family this
    # lane scores production under never reads it: a taker leg is priced
    # at the fee-adjusted ask whatever cap is handed in.
    # Charter 94 moved the family to the resting ladder; the taker arm is
    # still priced at the ask whatever cap is handed in.
    assert L.EXECUTION_FAMILY == P.EXECUTION_FAMILY == "maker"
    previous = L.EXECUTION_MODE
    L.EXECUTION_MODE = "taker"
    try:
        for bid, ask in _BOOKS:
            row = _Row(bid, ask, 0.001)
            for cap in _CAPS:
                assert L._execution_price(row, value_cap=cap) == pytest.approx(
                    ask * (1.0 + L.FEE_RATE)
                ), (bid, ask, cap)
    finally:
        L.EXECUTION_MODE = previous


def test_the_scope_stake_cap_is_the_kernels_on_both_sides() -> None:
    """Production capped a station at a flat 30 USDC; this book capped it at
    2% of its own equity.  Same station-day, two sizes.  The kernel owns the
    rule now and both sides call it (user ruling, 2026-08-26)."""

    import inspect

    from bayes_basket_target import runtime as R

    assert R.scope_stake_cap_usdc is K.scope_stake_cap_usdc
    assert L.scope_stake_cap_usdc is K.scope_stake_cap_usdc
    # And production's per-station gate must actually call it rather than
    # keep a flat number beside it.
    source = inspect.getsource(R._compile_target_plan)
    assert "scope_stake_cap_usdc(" in source
    assert "station_bankroll_usdc=float(policy.total_budget_usdc)" not in \
        source

    for equity in (100.0, 514.29, 1000.0):
        for stage in ("P0", "P0-M", "P1"):
            expected = (
                equity
                * K.MAX_STAKE_FRACTION_PER_SCOPE
                * K.STAGE_BUDGET_SHARE[stage]
            )
            assert K.scope_stake_cap_usdc(
                book_equity_usdc=equity, stage=stage
            ) == pytest.approx(expected)


# --------------------------------------------------------------------------
# 3. Completeness.  Every module-level rule constant the ledger declares is
#    classified.  A new one fails this test until somebody says which side
#    owns it -- which is the step that keeps getting skipped.
# --------------------------------------------------------------------------

# Assigned here, but the value comes from production -- an env override on
# top of a production constant, or a policy field there is no module symbol
# to import.  Weaker than an import, so each is pinned by an assertion above
# or below rather than by construction.
SHARED_WITH_PRODUCTION = {
    # Charter 110 (extended 2026-09-08): not rules of this lane's own but
    # the fingerprint OF production's claim transforms, read once from the
    # kernel so a candidate window records which vintage priced it.  They
    # move when production's tables move, which is the point.
    "CLAIM_TRANSFORM_SHA256_CENTRE",
    "CLAIM_TRANSFORM_SHA256_OFF_CENTRE",
    # Charter 94: one order family, spelled in each lane and asserted equal.
    "EXECUTION_FAMILY",
    # Charter 66 clause A: the same par bound is spelled in
    # delta_v2_runtime, asserted identical by the charter-66 binding test.
    "CHAIN_COST_PAR_BOUND",
    # Charter 67: one kernel constant, imported by both lanes.
    "P1_ENTRY_LOCAL_HOUR",
    # Charter 68: one switch, one table.  The table lives in the kernel and
    # both lanes import it; the switch is spelled in each and asserted equal.
    "EFFECTIVE_EDGE_MODE",
    # Charter 68 scope arm (2026-09-03): spelled in each lane, asserted
    # equal below; the per-leg table is the kernel's.
    "EFFECTIVE_EDGE_SCOPE",
    # Charter 69: the clamp's mode, spelled in each lane and asserted equal
    # below; the narrowing itself is one kernel function both import.
    "DECLINE_CLAMP",
    # Charter 70: the lot top-up mode is the kernel's; both lanes import it
    # and the raise itself is one kernel function both call.
    "EXCHANGE_LOT_TOP_UP",
    "CERTAINTY_GATE", "FEE_RATE", "KELLY_FRACTION",
    "MAKER_RESTING_IMPROVE_TICKS", "MAKER_RESTING_LADDER_STEPS",
    "MAXIMUM_SUPPORT_BUCKETS", "MINIMUM_SUPPORT_BUCKETS",
    "MAKER_RESTING_COVERAGE_BUDGET", "MAKER_RESTING_COVERAGE_LAMBDA",
}

# This lane's own, with the reason it has no production counterpart.
LEDGER_ONLY = {
    # Operational, not a rule: whether a pass repairs a book that holds more
    # than one rule set (charter 129's outstanding item, ruled 2026-09-12).
    # Production has no book to repair.  It deliberately stays out of the
    # selection fingerprint -- two books made by the same rules are the same
    # book whether or not one of them needed repairing.
    "AUTO_REDERIVE_ON_RULE_CHANGE": "operational: repair a mixed book, no production counterpart",
    # The simulator's adverse-selection calibration, and it must NEVER have
    # a counterpart in production.  Production's fill artifact
    # (``delta_v2_fill_artifact``, fitted by
    # ``fit_delta_v2_fill_from_maker_shadow.py``) is a DECISION-TIME
    # probability per station, price band and horizon: no outcome in it,
    # because the optimizer reads it before the day has happened.  These
    # two are conditioned on how the day settled, which is the only way to
    # model a counterparty better informed than we are -- and a strategy
    # holding that number would be betting on the future.  Their firewall
    # is structural (``adverse_fill_predicate`` closes over the truth so
    # the walk never receives it) and is asserted in
    # tests/test_adverse_fill_model_firewall.py.
    "ADVERSE_FILL_P_WIN": "simulator only: p(fill | crossed, leg wins)",
    "ADVERSE_FILL_P_LOSE": "simulator only: p(fill | crossed, leg loses)",
    # Charter 127 (七)(甲) replay arm: which resting lifecycle this book
    # simulates.  Production has no switch -- it IS charter 127's GTD, whose
    # expiry is the served belief's freshness -- so the arm is this lane's
    # alone, and since 2026-09-12 it is the default.  What the arm shares is asserted
    # below: the expiry column and the venue's grace floor are production's.
    "RESTING_LIFECYCLE": "mirror of production's GTD horizon (default since 09-12); production has no switch",
    # Not a rule: the inventory of tables this script creates and, for an
    # arm, discards.  Production writes none of them -- they are the book
    # this lane keeps -- so there is nothing on the other side to bind to.
    "PAPER_TRADE_TABLES": "this lane's own tables; production writes none",
    # Production's per-leg reservation veto (TAKER_LEG_VETO), replayed here
    # as an arm; the two are asserted equivalent below.
    "LEG_VETO": "production's TAKER_LEG_VETO, asserted equivalent",
    # The whole-basket judge arm (2026-09-03).  Production has no switch:
    # it IS the second judge.  What the arm shares is asserted below --
    # the solver it calls is production's own function.
    "JUDGE": "which judge seats and sizes; production is the solver itself",
    # Charter 71(b) tightening, replay arm: production spells nothing until
    # the increment-edge rule is ruled in.
    "TOPUP_INCREMENT_EDGE": "charter 71(b) tightening replay arm, off by default",
    # Which bankroll the production judge's curve reads; production spells
    # the station cap and this arm measures the alternatives.
    "JUDGE_BANKROLL": "production judge replay arm: station (production's) / scope / book",
    # Whether P0-M/P1 may open an empty chain; off is the book's rule and the
    # user's (only P0 opens), on is the replay arm that measures the other.
    "STAGE_SELF_OPEN": "replay arm: later stages opening an empty chain, off by default",
    # Whether a top-up must add to every leg; production's atomicity ruling
    # as a replay arm, off by default (the book buys the legs it is short of).
    "TOPUP_ATOMIC": "replay arm: production's atomic top-up, off by default",
    # Charter 63 retired (2026-09-03): production no longer defines a
    # per-leg value clamp, so the maker_resting research arm's lambdas are
    # this lane's alone.  They move nothing under the taker family.
    "MAKER_RESTING_VALUE_LAMBDA": "maker_resting research arm (production retired the clamp)",
    "MAKER_RESTING_SHOULDER_LAMBDA": "same",
    # The clamp's switch itself is shared (charter 69, above).  What is
    # this lane's own is the two extra arms it accepts -- "off", the
    # pre-2026-09-02 book, and "hold", the whole-basket refusal -- and the
    # threshold knob, none of which production spells.
    #
    # It is also the case this file's completeness device could not catch:
    # the clamp lived only in production from 2026-08-18 to 09-01, and a
    # rule that exists on one side has no constant on the other side to
    # classify.  See test_a_production_only_rule_has_no_constant_to_catch.
    "DECLINE_CLAMP_MODES": (
        "the six arms DECLINE_CLAMP accepts; production spells the four"
        " it may run (leg, guarded, held_only, after_open), off and hold"
        " are replay arms"
    ),
    "RESEAT_WITHIN_STAGE": (
        "arm knob: let a stage add to itself on later cycles, as production"
        " does; off is this ledger's historical one-seat-per-stage walk"
    ),
    "DECLINE_MIN_DROP": (
        "arm knob: how far a probability must fall to count as falling;"
        " 0 = production's rule, which has no threshold and never had one"
        " measured against it"
    ),
    # 2026-09-02 replay arm: a P1 effective-edge table handed in by path,
    # one set of bands per event date so each day reads only the days
    # before it.  Production reads the kernel's P1 constant through the same
    # dispatch both lanes bind above; unset here is that constant too.
    "P1_EFFECTIVE_EDGE_TABLE_PATH": (
        "replay arm; path to a per-event-date P1 lambda table, unset ="
        " the kernel's EFFECTIVE_EDGE_P1_LAMBDA, exactly as production"
    ),
    "P1_EFFECTIVE_EDGE_TABLE": (
        "the loaded form of P1_EFFECTIVE_EDGE_TABLE_PATH; empty when unset"
    ),
    # Charter 68(c) replay arm: ``off`` reads the kernel constants for every
    # decision (the book as priced before the rolling fit promoted itself),
    # ``as_of`` reads the store as of each decision's own moment, which is
    # production's read and the default.
    "EFFECTIVE_EDGE_TABLES_MODE": (
        "replay arm; off = kernel constants throughout, as_of = the store as"
        " of each decision, exactly as production reads it now"
    ),
    # Charter 66 replay arms: the bankroll frame and the miss-mass floor a
    # replay may vary; production's frame is its own capital plumbing.
    "CHAIN_TOPUP_BANKROLL_USDC": "replay arm; 0 = scope allowance",
    "CHAIN_TOPUP_MISS_FLOOR": "replay arm; off by default",
    # Measured 2026-08-30 and declined: every delay arm lost to entering at
    # the first seatable cycle.  Kept at 0 as the replayable record of that
    # ruling; production has no counterpart because the answer was no.
    "P0_ENTRY_DELAY_HOURS": "replay arm; measured and declined, default 0",
    # A replay simulates fills; production observes them.
    "MAKER_FILL_MODEL": "fill simulation, production reads real fills",
    "MAKER_RESTING_HORIZON_MINUTES": "how long a simulated order rests",
    "MAKER_RESTING_REPRICE_MINUTES": "re-post cadence; needs a cancel chain",
    "MAKER_RESTING_ESCALATE_SHARE": "re-post convergence; same",
    "MAKER_RESTING_CUTOFF_PERCENTILE": "when a simulated order gives up",
    "MAKER_RESTING_CUTOFF_BUFFER_MINUTES": "same",
    "MAKER_RESTING_CUTOFF_MIN_DAYS": "same",
    "MAKER_RESTING_GAP_MID": "simulated three-phase gap pressure",
    "MAKER_RESTING_GAP_LATE": "same",
    "MAKER_RESTING_BELIEF_GATE": (
        "withdraws resting legs; production's CANCEL_ACK has never run"
    ),
    "MAKER_RESTING_CRASH_TICKS": "control arm for the above",
    "CONSOLIDATION_TILT_FACTOR": (
        "stake tilt; production carries the same 0.05 in delta_v2_runtime"
        " (G1 port, both moved together 2026-08-30)"
    ),
    "CONSOLIDATION_TILT_MIN_SIGNALS": "same",
    "CONSOLIDATION_QUOTED_FRACTION": "annotation only",
    "CONSOLIDATION_SHARP_TOP2": "annotation only",
    "CONSOLIDATION_STABLE_LOOKBACK": "annotation only",
    # The exchange's stated lot for a snapshot taken before the official WS
    # feed carried ``min_order_size`` (user ruling 2026-09-04).  Production
    # fails closed on an unstated lot and never replays those days, so it
    # has no counterpart to bind to; the value is the registry's own, and
    # the assertion below pins it against the market ingress contract.
    "EXCHANGE_MINIMUM_LOT_SHARES_FALLBACK": (
        "replay-only fallback for pre-2026-08-31 snapshots; production"
        " fails closed instead, and the registry states 5.0 on every row"
    ),
    # Whether the leak-free replay layers reach the belief that seats.  Only
    # this lane's own judge can read them; production's snapshot builder
    # overwrites every bucket's probability with the envelope's.  Derived
    # from JUDGE, and pinned below.
    "REPLAY_LAYERS_APPLIED": (
        "derived: the replay ceiling/centre-width layers reach the solver"
        " only under the ledger judge; production reads its envelope"
    ),
    # Open ruling P1 (2026-09-04): whether charter 70's lift reads the
    # untilted allowance (production) or the tilted capital (the arm).
    "LOT_LIFT_BUDGET": (
        "replay arm: production lifts against the untilted allowance;"
        " 'tilted' measures the tilt the lift currently undoes"
    ),
    # Book-keeping for a paper book.  Production has a real capital pool.
    "INITIAL_CAPITAL_USDC": "paper book's opening equity",
    "BOOK_MODEL": "one book per station-day, a replay concept",
    "DUST_COST_FLOOR": "excludes dust fills from the paper equity curve",
    "LEDGER_EVENT_DATE_FLOOR": "which history this book admits",
    "LEDGER_STAGES": "replay bookkeeping",
    "SELECTION_RULE_LABEL": "fingerprint of this book's rule set",
    "SELECTION_RULE_SHA256": "same",
    "REFUSAL_DEPTH": "how the page reports a refusal",
    "REFUSAL_REASONS": "same",
    "GATE_LABEL": "same",
    "GATE_REFUSAL_REASON": "same",
    "STAGES": "replay bookkeeping",
    "DEFAULT_DB": "path",
    "OBSERVATION_DB": "path",
    "ROOT": "path",
    # Research arms.  Each is off by default; turning one on is a measurement
    # run, not a rule change, and the fingerprint records it.
    "ALLOCATION_SHAPE": "research arm (default follows production's switch)",
    "ALLOCATION_SHAPES": "research arm",
    "EXECUTION_MODE": "maker_resting by charter 94; taker stays as the arm",
    "EXECUTION_MODES": "same",
    "MAKER_RESTING_ANCHORED": (
        "charter 94: this lane's lifecycle arms off under production's shape"
    ),
    "MAKER_RESTING_BAND_ARM_DEFAULTS": (
        "charter 94: the band arm's own values, restorable in-process"
    ),
    "WINDOW_STRATEGY": "research arm",
    "WINDOW_STRATEGIES": "research arm",
    "SEAT_PRICING_BASIS": "crossing-priced gates, the 2026-08-27 ruling",
    "SEAT_PRICING_BASES": "same",
    "SEAT_PRICING_LANE_STAGES": "the ruling's P0-M exemption",
    "PAPER_LEDGER_FLEET_REGIME": (
        "research arm (fleet regime, measured -7 points and taken back out)"
    ),
    "PAPER_LEDGER_FLEET_REGIME_STAGES": "same",
    "SELECTION_DISCOUNT": "measurement lane; must not become a gate",
    "SHAPE_GAP_TILT": (
        "replay arm; scales the split-day opening stake instead of refusing,"
        " keeping the chain alive for its P1 top-ups; default 0"
    ),
    "SHAPE_GAP_LIMIT_BUCKETS": (
        "replay arm; admission gate on the served shape's mean-mode split,"
        " default 0 (off) pending the 2026-09-01 replay ruling"
    ),
    "MAKER_RESTING_BAND_TOP": "research arm",
    "MAKER_RESTING_CONFIRM_TAKE_TICKS": (
        "research arm pricing the 2026-08-20 market-moves-trigger-nothing"
        " ruling, take half"
    ),
    "MAKER_CENTRE_TAKER": "research arm: centre takes at entry, rest rests",
    "CONSOLIDATION_TILT_SCOPE": (
        "opener is the measured rule AND production's (consolidation_chain,"
        " 5f7d4987); the stage arm prices the rejected stateless reading"
    ),
    "MAKER_RESTING_CENTRE_LAMBDA": "research arm",
    "MAKER_RESTING_TAIL_LAMBDA": "research arm (rejected, kept for replay)",
    # Arm G (2026-09-08).  Production has no distance gate of any kind: its
    # leg screen is charter 91/103's belief-scaled price floor and nothing
    # reads the book's own centre.  Default 0 leaves this lane identical to
    # production, which is why it is an arm and not a divergence.
    "MARKET_DISTANCE_MAX": "research arm (rungs from the book's own centre)",
    "MAKER_RESTING_MAX_CLAIM_EDGE": "research arm (rejected)",
    "MAKER_MARKET_TOP_EXEMPT": "research arm",
    "MAKER_RESTING_PPOST": "research arm (archived)",
    "MAKER_RESTING_PPOST_COMPONENTS": "same",
    "PPOST_FILLED_MULT": "same",
    "PPOST_UNFILLED_MULT": "same",
    "PPOST_HIGH_CLAIM_FILLED_MULT": "same",
    "PPOST_HIGH_CLAIM_THRESHOLD": "same",
    "PPOST_LOW_ASK_VETO": "same",
    "REQUIRE_QUOTED_BOOK": "research arm, promotion-gated",
    # Layers the ledger re-applies from a leak-free replay table because
    # production's live artifact would be reading its own training set.
    "PAPER_LEDGER_CEILING": "replay curve substitution, tested idempotent",
    "PAPER_LEDGER_CENTER_WIDTH": "same, off by default",
    "PAPER_LEDGER_CENTER_WIDTH_STAGES": "same",
}


def _ledger_rule_constants() -> set[str]:
    tree = ast.parse(
        (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
            encoding="utf-8"
        )
    )
    assigned: set[str] = set()
    imported: dict[str, str] = {}

    def _keep(name: str) -> bool:
        return name.isupper() and not name.startswith("_")

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _keep(target.id):
                    assigned.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if _keep(bound):
                    imported[bound] = node.module or ""
    # An imported name cannot have drifted -- it is the same object.  A name
    # that moves from the imported set into the assigned set is exactly the
    # regression this file exists to catch, and it lands as "unclassified".
    return assigned - set(imported), imported


def test_the_two_sides_form_the_same_order_family() -> None:
    """The execution family is one rule, and it had no equivalence check.

    The completeness device classified the ledger's ``EXECUTION_MODE`` as
    "taker by charter 65" and was satisfied, because there was nothing on the
    production side to equate it to -- production's family was implicit in
    which candidate descriptors got enumerated, behind a parameter no caller
    passed.  That is how a ruling lived on one side for four days in silence.
    Production now declares the same constant, and as of 2026-08-31 the two
    agree: taker on both sides, charter 65 whole.  This assertion was
    xfail(strict) for the hours between production declaring the constant and
    the two filters coming out -- deliberately, so that closing the gap would
    force whoever closed it back here.
    """
    assert L.EXECUTION_MODES[:1] == P.EXECUTION_MODES[:1]
    # Charter 94: the ledger spells several resting arms, production one;
    # the family is what the two are held equal on.
    assert L.EXECUTION_FAMILY == P.EXECUTION_FAMILY == "maker"
    assert L.EXECUTION_MODE == "maker_resting" and P.EXECUTION_MODE == "maker"


def test_every_ledger_rule_constant_is_classified() -> None:
    """A new knob on one side must be classified before it can land.

    This is the step that keeps getting skipped: a rule is added to the
    ledger, measured, and never reaches production -- or the reverse -- and
    nothing says so until somebody audits by hand months later.  If you are
    reading this because the test failed, the question to answer is not "how
    do I make it pass" but "does production run this rule too?".
    """

    assigned, _imported = _ledger_rule_constants()
    declared = SHARED_WITH_PRODUCTION | set(LEDGER_ONLY)
    unclassified = assigned - declared
    assert not unclassified, (
        "these ledger constants are not classified as production's or as "
        "this lane's own -- say which, in "
        f"tests/{Path(__file__).name}: {sorted(unclassified)}"
    )
    # And the classification must stay honest: a name that has since been
    # deleted from the ledger should not linger here pretending to be bound.
    stale = declared - assigned
    assert not stale, f"classified but no longer defined: {sorted(stale)}"


def test_every_constant_the_ledger_imports_comes_from_production() -> None:
    """An imported rule cannot drift; that is the whole point of importing.

    So the strong form of this file's contract is "as much of the surface as
    possible is imported", and anything imported must come from the
    production package rather than from a third copy somewhere else.
    """

    _assigned, imported = _ledger_rule_constants()
    assert imported, "the ledger should be reading production's constants"
    for name, module in sorted(imported.items()):
        assert module.startswith("bayes_basket_target"), (name, module)


def test_the_support_width_bounds_match_productions_policy_defaults() -> None:
    """No module symbol to import -- these are policy fields -- so they are
    held to production's defaults by value instead."""

    from bayes_basket_target.basket_kernel.contracts import TargetBasketPolicy

    from bayes_basket_target import delta_v2_runtime as PROD  # noqa: PLC0415
    from bayes_basket_target.basket_kernel import delta_v2 as K2  # noqa: PLC0415

    # Charter 68 moves the objective's belief, so a split here would have the
    # two lanes trading different strategies while claiming to measure one.
    assert L.EFFECTIVE_EDGE_MODE == PROD.EFFECTIVE_EDGE_MODE
    assert L.effective_edge_lambda is K2.effective_edge_lambda
    assert PROD.effective_edge_lambda is K2.effective_edge_lambda
    assert L.EFFECTIVE_EDGE_STAGES is K2.EFFECTIVE_EDGE_STAGES
    # 2026-09-05: each stage reads its own table; P0-M's set is the
    # kernel's in both lanes, like P0's and P1's.
    assert L.EFFECTIVE_EDGE_P0M_STAGES is K2.EFFECTIVE_EDGE_P0M_STAGES
    # Charter 68(c), 2026-09-05: both lanes read the store's rolling tables
    # through one unpacking, production as of now and the ledger as of each
    # decision's own moment.  Source-level, like the pairs above: which
    # names the call sites use is the whole contract.
    from pathlib import Path as _P  # noqa: PLC0415
    _root = _P(__file__).resolve().parents[1]
    _io = (_root / "bayes_basket_target" / "production_io.py").read_text()
    _rt = (_root / "bayes_basket_target" / "delta_v2_runtime.py").read_text()
    _lg = (_root / "scripts" / "paper_trading_ledger.py").read_text()
    assert "effective_edge_tables=load_effective_edge_tables(conn)" in _io
    assert "effective_edge_lambda_from_tables(" in _rt
    assert "_effective_edge_tables_as_of(" in _lg
    assert L.effective_edge_lambda_from_tables is (
        K2.effective_edge_lambda_from_tables
    )
    # And the served tables leave a receipt on the decision row, so the
    # question "did production read the store" has an answer.
    _pr = (_root / "bayes_basket_target" / "production.py").read_text()
    assert '"effective_edge_tables": served_lambda_tables' in _rt
    assert 'support_selection.get("effective_edge_tables")' in _rt
    assert 'delta_v2_audit.get("effective_edge_tables")' in _pr
    # 2026-09-02: P1 flows through the same dispatch with its own table.  Both
    # lanes must call the one function and read the one stage set, or P1 is
    # exempt in one lane and shrunk in the other while both claim one book.
    assert L.effective_edge_lambda_for_stage is (
        K2.effective_edge_lambda_for_stage
    )
    assert PROD.effective_edge_lambda_for_stage is (
        K2.effective_edge_lambda_for_stage
    )
    assert L.EFFECTIVE_EDGE_P1_STAGES is K2.EFFECTIVE_EDGE_P1_STAGES
    assert PROD.EFFECTIVE_EDGE_P1_STAGES is K2.EFFECTIVE_EDGE_P1_STAGES
    assert L.MAXIMUM_SUPPORT_BUCKETS == (
        TargetBasketPolicy.maximum_support_buckets
    )
    assert L.MINIMUM_SUPPORT_BUCKETS == (
        TargetBasketPolicy.minimum_support_buckets
    )


def test_the_clamp_is_one_implementation_that_both_sides_import() -> None:
    """The declining-probability clamp is imported, not copied.

    It was inline in ``delta_v2_runtime`` from 2026-08-18 to 2026-09-01, so
    the ledger never ran it and the whole recorded book is silently the
    arm where it is off.  Extracted to the kernel rather than re-typed here
    because a hand-copy is how the maker ladder drifted on 37 of 64 books
    while both sides believed they were "the same construction".
    """

    from bayes_basket_target.basket_kernel import delta_v2 as kernel

    assert L.clamp_declining_gaps_v2 is kernel.clamp_declining_gaps_v2
    assert P.clamp_declining_gaps_v2 is kernel.clamp_declining_gaps_v2
    # Charter 69: the guarded narrowing is likewise one function, and the
    # two lanes default to the same mode.
    assert L.contiguous_edge_clamp_v2 is kernel.contiguous_edge_clamp_v2
    assert P.contiguous_edge_clamp_v2 is kernel.contiguous_edge_clamp_v2
    # Retired 2026-09-05: the clamp defaults off on both sides, and the
    # ledger reads production's value rather than spelling its own.
    assert L.DECLINE_CLAMP == P.DECLINE_CLAMP_MODE == "off"


def test_charter_69_the_guarded_clamp_never_drops_the_centre_or_opens_a_hole() -> None:
    """Ends only, never the centre: what "guarded" means on both sides.

    Charter 51's clamp runs before the complete-basket check, so a clamp
    that removes the centre or a middle rung produces a basket that passes
    as complete (measured 2026-09-01: 48 dropped centres, 10 holes in 581
    replay openings).  Charter 69 narrows it to what charter 49 and
    contiguity allow, and this pins the narrowing's shape on the grid the
    ledger and production both walk.
    """

    from bayes_basket_target.basket_kernel import delta_v2 as kernel

    support = ("18°C", "19°C", "20°C", "21°C", "22°C")
    narrow = kernel.contiguous_edge_clamp_v2
    # Both ends falling: both may go.
    assert narrow(declining=("18°C", "22°C"), support=support,
                  centre_bucket="20°C") == ("18°C", "22°C")
    # A run from one end stops at the first rung that is not falling; a
    # falling rung behind a rung that is not falling is not at an end, so
    # 21°C stays even though it fell.
    assert narrow(declining=("18°C", "19°C", "21°C"), support=support,
                  centre_bucket="20°C") == ("18°C", "19°C")
    # A falling rung in the middle is never dropped: no holes.
    assert narrow(declining=("19°C",), support=support,
                  centre_bucket="20°C") == ()
    # The centre is never dropped, even when everything is falling.
    assert narrow(declining=support, support=support,
                  centre_bucket="20°C") == ("18°C", "19°C", "21°C", "22°C")
    # ...and walking in from the ends stops AT the centre, on both sides.
    assert narrow(declining=("18°C", "19°C", "20°C"), support=support,
                  centre_bucket="20°C") == ("18°C", "19°C")
    assert narrow(declining=("20°C", "21°C", "22°C"), support=support,
                  centre_bucket="20°C") == ("21°C", "22°C")
    # Every result is a prefix plus a suffix of the window: what survives
    # is one contiguous run that contains the centre.
    for falling in (("18°C", "22°C"), ("18°C", "19°C", "21°C"), support):
        gone = set(narrow(declining=falling, support=support,
                          centre_bucket="20°C"))
        kept = [b for b in support if b not in gone]
        assert "20°C" in kept
        first, last = support.index(kept[0]), support.index(kept[-1])
        assert kept == list(support[first:last + 1])


def test_a_production_only_rule_has_no_constant_to_catch() -> None:
    """Name the hole this file's completeness device has.

    ``test_every_ledger_rule_constant_is_classified`` scans the *ledger's*
    module-level constants and asks which side owns each.  A rule added to
    production alone contributes no ledger constant, so the scan sees
    nothing to object to -- which is exactly how the clamp went thirteen
    days with the two lanes trading different strategies while every test
    here passed.  The docstring at the top of this file says the contract
    covers "or the reverse"; the mechanism does not.

    Asserted rather than fixed: closing it means enumerating production's
    selection surface, which is a larger piece of work than this test.  It
    fails loudly if somebody believes it is already covered.
    """

    assigned, _imported = _ledger_rule_constants()
    # The clamp is production's rule.  It reaches the ledger as an imported
    # function, so it is deliberately NOT among the ledger's own constants.
    assert "clamp_declining_gaps_v2" not in assigned
    # And that is the hole: nothing in the completeness scan would have
    # noticed its absence for the thirteen days it was production-only.
    assert "DECLINE_CLAMP" in assigned, (
        "only the arm switch is a ledger constant; the rule itself is not,"
        " which is why a production-only rule cannot be caught here"
    )


def test_the_whole_share_order_grid_is_read_from_the_kernel_by_both(
) -> None:
    """An order's size is a whole share, on both sides, from one place.

    User ruling, 2026-09-06.  The grid lives in the kernel next to the lot
    and the dollar, because those are the other two venue floors and all
    three answer "what will the exchange take".  Production's ``cash_phase``
    floors onto it; this book imports the same function and floors at the
    point its own comment already calls the order the exchange would judge
    -- after the seat is sized, before the lot and the dollar, which is
    production's order too.

    Not inside ``_fillable_weighted``.  That search probes a thousandth of
    a share for feasibility, so a floor there refuses every seat; production
    likewise floors the solver's answer rather than its search, and the grid
    is exempt from re-score because a single step cannot re-shape a basket.

    And not down to zero.  The kernel's raise skips a zero increment -- "a
    rung with no increment is not an order" -- so a rung floored to nothing
    would stand the basket down instead of being lifted to the venue
    minimum, which is the opposite of what charter 70 does with the same
    0.862-share rung on the production side.
    """

    import inspect

    from bayes_basket_target import cash_phase as CP
    from bayes_basket_target.basket_kernel import delta_v2 as K2

    assert K2.ORDER_SHARE_STEP_SHARES == 1.0
    assert K2.ORDER_SHARE_GRID in K2.ORDER_SHARE_GRID_MODES
    assert K2.order_share_step_v1(0.01) == 1.0
    assert K2.floor_to_order_share_grid_v1(13.66, 0.01) == 13.0

    # Production floors onto it, and charter 70's raise stays reachable
    # underneath: a leg the grid floors to zero is refused only where no
    # lot is stated.
    sizing = inspect.getsource(CP.OrderSizingDecision.compute)
    assert "floor_to_order_share_grid_v1(raw_q_order, quantum)" in sizing
    assert "q_order <= EPS and minimum_lot <= EPS" in sizing

    # The ledger imports the same function and the same step, so neither
    # can drift; the classification scan sees them as production's.
    _assigned, imported = _ledger_rule_constants()
    assert imported.get("ORDER_SHARE_GRID", "").startswith(
        "bayes_basket_target"
    ), imported.get("ORDER_SHARE_GRID")
    assert imported.get("ORDER_SHARE_STEP_SHARES", "").startswith(
        "bayes_basket_target"
    )
    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "floor_to_order_share_grid_v1(q)" in source
    # Floored before the venue's rules, which is production's order.
    assert source.index("floor_to_order_share_grid_v1(q)") < source.index(
        'if EXCHANGE_LOT_TOP_UP != "off" and legs:'
    )
    # A rung under one share is left for charter 70, and a rung that bought
    # nothing has no order to floor.
    assert 'if float(q) >= ORDER_SHARE_STEP_SHARES' in source
    assert "and float(c) > 0.0" in source
    # The grid is not inside the bisection.
    assert "floor_to_order_share_grid_v1" not in inspect.getsource(
        L._fillable_weighted
    )
    assert "floor_to_order_share_grid_v1" not in inspect.getsource(
        L._fillable_size
    )

    # And the book knows it changed.  Hashed unconditionally, like charter
    # 79's seat: rows sized on the old continuous grid and rows sized on
    # whole shares are not one book, and a digest that cannot tell them
    # apart is a digest that lets them be summed.
    assert "整股下单" in L.SELECTION_RULE_LABEL
    assert "order_share_grid" in source


def test_charter_68_scope_and_the_taker_leg_veto_move_together() -> None:
    """One belief on both sides (2026-09-03).

    The scope arm is spelled in each lane and must read the same word.
    The veto is production's TAKER_LEG_VETO and the ledger's LEG_VETO: the
    ledger has always judged a seat as one basket (no per-leg veto), so
    production's default is ``joint`` -- a rung the reservation inversion
    finds no positive price for still reaches the joint selector at the
    ask.  ``reservation`` / ``on`` is the arm that reproduces the taker
    path as shipped on 2026-08-31.
    """

    assert L.EFFECTIVE_EDGE_SCOPE == P.EFFECTIVE_EDGE_SCOPE
    # Charter 74's ticket is one switch on both sides; the solver offers
    # the candidate only while it is on.
    # Charter 96 (2026-09-06): the ticket is retired on both sides.  The
    # switch stays -- it is the replay arm -- and the two sides must still
    # read one value, which is what this assertion is for.
    import inspect as _inspect

    assert (L.LEG_VETO == "on") == (P.TAKER_LEG_VETO == "reservation")
    assert P.TAKER_LEG_VETO == "joint"
    # Charter 96: the lambda partition gained a boundary at 0.95, and the
    # three served scopes must carry the same boundaries -- the store fits
    # per scope but ``_band_index`` is one partition, so a scope on the old
    # bounds would look its lambda up in a band nobody fitted.
    #
    # Charter 87, second reading (2026-09-07): 1.00 and 1.10 join them.  The
    # open top band was measured on the enumerated windows and found
    # internally heterogeneous in the one way that makes a band average
    # unusable as a threshold -- lambda tracks ``a`` almost one for one
    # inside it, so the pooled 1.217 it fitted to would have admitted the
    # 1.00-1.22 slice, whose windows returned -2.3% and -2.7% when bought.
    assert K.EFFECTIVE_EDGE_BAND_BOUNDS == (
        0.50, 0.70, 0.80, 0.85, 0.95, 1.00, 1.10, float("inf")
    )
    # Charter 97: the claim-edge axis is one constant on both sides, and both
    # lanes resolve lambda through the same function -- the ledger judging a
    # seat on one axis while production gated it on two is the split this
    # test exists to stop.
    assert L.CLAIM_EDGE_THERMOMETER_MODE == P.CLAIM_EDGE_THERMOMETER_MODE == "on"
    assert K.CLAIM_EDGE_THIN_BAND_MAX == 0.05
    assert K.CLAIM_EDGE_THIN_BAND_LAMBDA == 1.00
    import inspect as _i
    assert "effective_edge_lambda_resolved" in _i.getsource(P._support_windows)
    _ledger_src = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "effective_edge_lambda_resolved" in _ledger_src
    _dear = {"P0": [(0.5, 0.10), (float("inf"), 1.20)], "P0-M": None, "P1": None}
    _cheap = {"P0": [(0.5, 0.10), (float("inf"), 0.80)], "P0-M": None, "P1": None}
    # Inside the thin band the thermometer serves its own value ...
    assert K.effective_edge_lambda_resolved(
        "P0", claim=0.30, price=0.28, tables=_cheap
    ) == (1.00, K.LAMBDA_SOURCE_CLAIM_EDGE)
    # ... but only to lift.  An agreement band that already trusts the claim
    # more keeps its number: the thermometer is evidence a claim arrives, not
    # evidence it does not.
    assert K.effective_edge_lambda_resolved(
        "P0", claim=1.0, price=0.97, tables=_dear
    ) == (1.20, K.LAMBDA_SOURCE_AGREEMENT)
    # Outside the band, and on a claim the book prices above, charter 68 is
    # the whole rule.
    assert K.effective_edge_lambda_resolved(
        "P0", claim=1.0, price=0.80, tables=_cheap
    ) == (0.80, K.LAMBDA_SOURCE_AGREEMENT)
    assert K.effective_edge_lambda_resolved(
        "P0", claim=0.30, price=0.33, tables=_cheap
    ) == (0.80, K.LAMBDA_SOURCE_AGREEMENT)
    for _table in (
        K.EFFECTIVE_EDGE_LAMBDA,
        K.EFFECTIVE_EDGE_P0M_LAMBDA,
        K.EFFECTIVE_EDGE_P1_LAMBDA,
    ):
        assert tuple(b for b, _ in _table) == K.EFFECTIVE_EDGE_BAND_BOUNDS
    # The bucket table's open top band, split 2026-09-09 the way charter 96
    # split the window table's, and for the reason that ruling gave: an open
    # band above 0.85 is heterogeneous in exactly the way that makes its
    # average unusable as a threshold, and this one pools to 1.38 -- the only
    # band in the table above one, so the only one that inflates a claim.
    # Measured the same day, running the bucket arm against the shipped one:
    # 68.1% of the rungs in the 33 station-days it wrongly opened sat in this
    # band, against 50.4% in the 57 profitable ones it refused in exchange.
    #
    # Splitting alone moves nothing -- every new band inherits 1.38, which is
    # what ``_project_onto_partition`` does with a stored table on the day a
    # boundary is added -- and the bounds now match the window table's, which
    # is what lets the two be compared band for band.
    assert K.EFFECTIVE_EDGE_BUCKET_LAMBDA == (
        (0.50, 0.22),
        (0.70, 0.43),
        (0.85, 0.80),
        (0.95, 1.38),
        (1.00, 1.38),
        (1.10, 1.38),
        (float("inf"), 1.38),
    )
    assert K.effective_edge_bucket_lambda_for_stage("P0", 1.27) == 1.38
    # And the dispatch prefers a fitted table over the constant, which is the
    # whole point of the lane added the same day: this arm read a hand-set
    # 2026-09-03 constant while the window arm read a nightly refit.
    assert K.effective_edge_bucket_lambda_for_stage(
        "P0", 1.27, bucket_table=((0.5, 0.10), (float("inf"), 0.90))
    ) == 0.90
    assert K.effective_edge_bucket_lambda_for_stage("P1", 1.27) == 1.0



def test_the_production_judge_arm_calls_productions_own_solver() -> None:
    """The arm's whole point is that nothing is re-implemented.

    ``PAPER_LEDGER_JUDGE=production`` must hand the book to the very function
    production seats and sizes with, resolve its policies with production's
    resolvers, and default to this lane's own judge so the standing book
    keeps its fingerprint.
    """
    import bayes_basket_target.delta_v2_artifact as A
    import bayes_basket_target.delta_v2_runtime as P
    import bayes_basket_target.policy as POL
    import bayes_basket_target.production_io as IO
    import bayes_basket_target.runtime as R

    # The standing book is judged by production's solver since 2026-09-04.
    assert L.JUDGE == "production"
    assert L.TOPUP_ATOMIC == "on"
    assert L._solve_over_support_windows is P._solve_over_support_windows
    assert L._support_windows is P._support_windows
    assert L.resolve_delta_v2_policy is A.resolve_delta_v2_policy
    assert L.resolve_station_policy is POL.resolve_station_policy
    assert L.load_production_artifacts is IO.load_production_artifacts
    assert L.derive_policy_maturity_bin is IO.derive_policy_maturity_bin
    assert L._PRODUCTION_DEFAULT_POLICY is R._DEFAULT_POLICY
    assert L.CONSOLIDATION_TILT_FLOOR_LEGS == P.CONSOLIDATION_TILT_FLOOR_LEGS
    # The tilt floor is the kernel's venue-minimum rule on both sides
    # (2026-09-06), and its version rides in the ledger's label.
    assert L.consolidation_tilt_floor_usdc is K.consolidation_tilt_floor_usdc
    assert L.venue_minimum_shares_v1 is K.venue_minimum_shares_v1
    assert L.CONSOLIDATION_TILT_FLOOR_VERSION == K.CONSOLIDATION_TILT_FLOOR_VERSION
    # The judge is the book's identity: hashed unconditionally.
    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert '"judge": JUDGE,' in source
    assert '"topup_atomic": TOPUP_ATOMIC,' in source


def test_the_chain_law_and_the_ticket_live_inside_the_solver() -> None:
    """Charters 66(A), 71 and 74 bind where production binds them.

    The arm called production's solver without the four inputs those
    clauses read -- the served identity, the chain's last-acted identity,
    the growth frame and the last decision signature -- so inside the
    solver they could not bind at all, and this lane re-judged the seated
    window afterwards.  The two are not the same rule: production tests
    every shortlisted window and seats the first that clears, so a head
    window refused on chain growth hands the seat to the next one, while
    a post-seat copy refuses the cycle outright.  Measured 2026-09-04 on
    the standing book: 105 P0-M/P1 cycles refused after the seat.

    The ticket is the same shape of error in the other direction.  It is
    charter 74's candidate *inside* the solver, offered only when the
    chain is empty at an opening stage, Kelly sized the seat to nothing,
    and the ticket's cash fits the capital the solver was handed.  This
    lane added a ticket of its own whenever the solver held -- capital or
    not -- so on a tilted day, where the capital floors at 0.75 and a
    four-rung ticket costs 1.00, it opened chains production does not.
    """

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # Every input the chain clauses read is handed to the solver.
    for argument in (
        "probability_identity=probability_identity or None,",
        "chain_growth_frame_usdc=growth_frame,",
        "last_decision_signature=last_decision_signature,",
    ):
        assert argument in source, argument
    assert "last_acted_probability_identity=(" in source
    # And the solver's own verdict is read back rather than recomputed.
    assert 'moment = dict(selection.get("decision_moment") or {})' in source
    assert 'moment.get("is_decision_moment") is False' in source
    # The ticket is retired (2026-09-08): neither lane reads it, and the
    # verdict no longer carries the flag that named it.
    assert 'verdict.get("ticket")' not in source
    assert "chain_ticket" not in source
    # The post-seat chain law is skipped for a production verdict.
    assert "chain_law_here = production_verdict is None" in source
    assert "if chain_law_here and not growth > 0.0:" in source
    assert "if chain_law_here and (" in source


def test_the_exchange_lot_and_the_station_dial_bind_where_production_binds() -> None:
    """Two venue rules, judged at production's own layer.

    Charter 70's raise is all-or-none: a leg the raise cannot lift to one
    lot cannot fill at any price, and production's ``cash_phase`` stands
    the whole basket down under ``exchange_minimum_lot_unaffordable``.
    This book recorded the fraction as filled instead -- 134 of the 209
    rows standing on 2026-09-04 were orders no exchange takes.

    The lift's budget is likewise production's: ``cash_phase`` sizes
    against ``remaining_budget``, which the consolidation tilt never
    touches, so a rung the tilted capital could not lift is lifted there
    anyway when the untilted allowance can pay.

    And the operator's station dial is not a sizing bound.
    ``_compile_target_plan`` hands the solver the scope's share of the
    book; folded into the solver's capital here, the dial sized P1 five
    USDC short of production on a 1000 book.  Where it *does* bind in
    production is the kernel's ``profit_floor_ok``: the worst settled
    outcome inside the seated support may not lose more than the dial.
    Not ``execution_gates.evaluate_execution_gate``'s per-leg ``budget_ok``
    -- that function has no caller on the live path, and the mirror this
    book carried for a few hours on 2026-09-04 copied it.
    """

    import inspect

    from bayes_basket_target import cash_phase as CP
    from bayes_basket_target.basket_kernel import delta_v2 as K2

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The reason production spells, spelled here by the same name.
    assert "exchange_minimum_lot_unaffordable" in inspect.getsource(
        CP.OrderSizingDecision.compute
    )
    assert "exchange_minimum_lot_unaffordable" in L.REFUSAL_REASONS
    # The dial's live gate is the kernel's profit floor on the worst
    # supported outcome, and the live budget gate reads the solver's
    # capital -- not the policy dial.
    kernel = inspect.getsource(K2.evaluate_action_v2)
    assert 'row["resulting_profit_floor"] + EPS < required_profit_floor' in (
        kernel
    )
    assert "clip > capital_available + EPS" in kernel
    assert "total_budget_usdc" not in kernel
    # And the per-leg execution gate is gone: it had no caller on the live
    # path, and a dead rule that reads a live dial gets mirrored as though
    # it ran (user ruling 2026-09-04: dead code is retired on sight).
    assert not (
        ROOT / "bayes_basket_target" / "basket_kernel" / "execution_gates.py"
    ).exists()
    from bayes_basket_target import basket_kernel as BK

    for name in (
        "evaluate_execution_gate", "evaluate_urgency",
        "expected_version_vector",
    ):
        assert not hasattr(BK, name), name
    assert "station_loss_floor_exceeded" in L.REFUSAL_REASONS
    assert "station_budget_exceeded" not in L.REFUSAL_REASONS
    # A leg still under its lot after the raise refuses the basket.
    assert "if _under_lot:" in source
    assert '"exchange_minimum_lot_unaffordable",' in source
    # The lift's budget is the untilted allowance under a production
    # verdict, and the dial is the floor on the worst supported outcome
    # after the buy: shares on the thinnest seated rung minus everything
    # the chain has paid.
    assert 'float(production_verdict["lot_room_usdc"])' in source
    assert '_floor = -float(production_verdict["total_budget_usdc"])' in (
        source
    )
    assert "if _worst + 1e-9 < _floor:" in source
    # The dial is no longer folded into the capital the solver is handed.
    assert "capital = scope_cap" in source
    assert "station_room = min(" not in source
    assert "capital = min(scope_cap, station_room)" not in source
    # The lot fallback is the exchange's stated minimum, and the identity
    # carries it: rows sized as if there were no lot are not comparable.
    assert L.EXCHANGE_MINIMUM_LOT_SHARES_FALLBACK == 5.0
    assert (
        '"production_judge": "solver_chain_gates_exchange_lot_centre_v12",'
        in source
    )
    assert "补足一手" in L.SELECTION_RULE_LABEL
    assert "不足一手不入座" in L.SELECTION_RULE_LABEL


def test_the_recorded_centre_comes_from_the_enumeration_that_seated() -> None:
    """A seat's centre is the centre of the run that chose it.

    Production's ``_choose_support`` reads the probability envelope --
    model centre, dispersion, rung width, formal members -- which this
    lane's own core resolver never opens, so the two centres disagree on
    real books.  The row recorded this lane's while the seat was
    production's, and every number derived from a centre went with it:
    the off-centre claim factor that prices the window, the centre-tilt
    weights, charter 69's guard on which rungs may be clamped, and the
    charter-49 audit itself.  Seven P1 rows of the rebuilt 2026-08-15
    book named a centre outside their own window -- EFHK 2026-08-16 sat
    22-23C against a recorded centre of 20C.

    One name, bound where the seat is chosen, read by all of them.
    """

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The judge returns the centre its own enumeration used.
    assert '"ladder_center_bucket": str(' in source
    assert "or centre_row.bucket" in source
    # The walk binds one name and the judge reassigns it when it seats.
    assert 'seat_center = str(\n                        audit.get(' in source
    assert 'verdict.get("ladder_center_bucket") or center' in source
    # And nothing derived from a seat reads the ledger's centre directly:
    # one read is left, the default binding itself.
    assert source.count('audit.get("ladder_center_bucket")') == 1
    for consumer in (
        "centre_bucket=seat_center,",
        "seat_centre = seat_center",
    ):
        assert consumer in source, consumer


def test_the_walk_carries_the_belief_the_solver_judged_on() -> None:
    """Row, clamp and lights read the same vector the solver did.

    Production's ``snapshot_from_probability_envelope`` sets every bucket's
    probability from the envelope, so a corrected vector handed in as the
    market mapping never reaches the solver.  This lane applied its
    leak-free ceiling and centre-width replays to the vector it *recorded*
    while the solver judged the envelope's -- SBGR 2026-08-17 P1 recorded a
    0.1995 claim against a 0.52 cost for a seat the solver had found
    positive.  The layers are applied only when this lane is the judge, and
    the fingerprint says which it was.

    Two more places read "the checkpoints before this one" and read them
    the way production does: the consolidation lookback (every stage, not
    P0 alone) and charter 69's previous vector (the previous committed
    checkpoint, any stage, not the previous cycle of this stage's walk).

    And the P0-M TAF TX bucket the enumerator's arms read is one function
    on both sides, handed to ``_support_windows`` by both compiles -- until
    2026-09-04 production's own compile never passed it, so the arm could
    only ever act in the replay.
    """

    import inspect

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert L.REPLAY_LAYERS_APPLIED == (L.JUDGE == "ledger")
    assert 'if stage == "P1" and REPLAY_LAYERS_APPLIED:' in source
    assert "        if REPLAY_LAYERS_APPLIED:\n            cw_p = " in source
    assert '"off:judge_reads_envelope"' in source
    # The lookback walks every stage.
    lookback = inspect.getsource(L._consolidation_signals)
    assert "stage='P0'" not in lookback
    assert "AND decision_at_utc<?" in lookback
    # The clamp's previous vector is the previous checkpoint, any stage.
    assert "_previous_checkpoint_probabilities(" in source
    previous = inspect.getsource(L._previous_checkpoint_probabilities)
    assert "stage" not in previous.split('"""')[2], (
        "the previous checkpoint must not be filtered by stage"
    )
    # One TAF helper, passed by both compiles.
    assert L._taf_tx_bucket is P.taf_tx_bucket_for
    production = (
        ROOT / "bayes_basket_target" / "delta_v2_runtime.py"
    ).read_text(encoding="utf-8")
    assert "taf_tx_bucket=taf_tx_bucket," in production
    runtime = (ROOT / "bayes_basket_target" / "runtime.py").read_text(
        encoding="utf-8"
    )
    assert "taf_tx_bucket=taf_tx_bucket_for(envelope, str(envelope.stage))," in (
        runtime
    )
    assert "taf_tx_bucket=_taf_tx_bucket(envelope, stage)," in source


def test_a_stage_is_judged_only_while_it_holds_the_days_authority() -> None:
    """One stage per cycle, and it is the highest one the day has reached.

    ``stages.select_current_stage`` walks ``reversed(STAGES)`` and returns
    the newest servable checkpoint of the highest stage present at or
    before the clock; a higher stage that is present but stale fails the
    cycle closed rather than falling back.  So from the first P0-M
    checkpoint onward production cannot compile P0 again.

    This lane walks a stage at a time and kept offering P0 books after
    that: 141 of 393 P0 seats on the standing book of 2026-09-04, against
    6 of 306 P0 compiles in production over the same days.  Under charter
    74 those are chains production never opened at all, and they settled
    at -25.1% against -17.1% for the seats taken inside authority.
    """

    import inspect

    horizon = inspect.getsource(L._stage_authority_horizon)
    # The horizon is the first checkpoint of any HIGHER stage, and the
    # comparison is strict because production takes the higher stage on a
    # tie.
    assert "LEDGER_STAGES.index(str(stage)) + 1" in horizon
    assert "MIN(decision_at_utc)" in horizon
    candidates = inspect.getsource(L._stage_entry_candidates)
    assert "AND (?4 IS NULL OR decision_at_utc < ?4)" in candidates
    assert "_stage_authority_horizon(conn, station_id, event_date, stage)" in (
        candidates
    )
    # P1 is the highest stage, so it is never bounded.
    from bayes_basket_target.stages import STAGES as PRODUCTION_STAGES

    assert tuple(L.LEDGER_STAGES) == tuple(PRODUCTION_STAGES)
    assert '"stage_authority": "highest_committed_stage_v1",' in (
        (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
            encoding="utf-8"
        )
    )


def test_a_taker_fills_at_the_top_of_the_offer_and_is_cut_to_depth() -> None:
    """One order, one price: the FOK's limit is the best ask.

    Production's taker candidate carries ``limit_price = row.ask_price``
    and sizes the leg against ``_executable_depth_cash``, which for a FOK
    counts only the offer at or below that limit; a thin top level cuts
    the leg (``sizing_clamped_below_authorized_clip``, then the bundle is
    re-scored at the cut sizes) and an empty one refuses it.  This lane's
    walker descended the book for the whole increment instead: 120 of 861
    legs on the 2026-09-04 book, 29.16 USDC paid above the top of book.
    """

    import inspect

    production = inspect.getsource(P)
    assert '(bucket, action_clip, row.ask_price, "FOK:0")' in production
    from bayes_basket_target import runtime as R

    depth = inspect.getsource(R._executable_depth_cash)
    assert "if float(level.price) <= float(limit_price) + 1e-9" in depth
    sized = inspect.getsource(L._production_sized_legs)
    assert "_walk_ask_shares_at_limit(leg, delta)" in sized
    assert "got + 1e-9 < delta" not in sized, "a short top level cuts, not refuses"
    # And the walker takes the best ask as its limit.
    levels = [
        {"price": 0.20, "shares": 3.0},
        {"price": 0.20, "shares": 2.0},
        {"price": 0.25, "shares": 50.0},
    ]
    got, paid = L._walk_ask_shares_at_limit(levels, 10.0)
    assert got == pytest.approx(5.0)
    assert paid == pytest.approx(1.0)
    assert L._walk_ask_shares_at_limit([], 10.0) == (0.0, 0.0)


def test_a_checkpoint_is_priced_on_the_book_production_compiled_it_on() -> None:
    """The first book at or after the checkpoint, never one before it.

    A checkpoint lands at its decision clock and production compiles it at
    its next cycle on that cycle's quote (p50 six to seven minutes later),
    and keeps compiling it until a newer same-or-higher checkpoint takes
    the book or the envelope stops being servable.  This lane priced a
    checkpoint on the last book *before* it -- a quote production had
    paired with the previous checkpoint -- on every P1 seat and 127 of 229
    P0 seats of the 2026-09-04 book.  The thirty-minute staleness bound
    that governed that choice is gone with it: nothing before the
    checkpoint is used, so there is nothing to bound.
    """

    import inspect

    pairing = inspect.getsource(L._books_for_checkpoint)
    assert "AND observed_at >= ?" in pairing
    assert "AND (?4 IS NULL OR observed_at < ?4)" in pairing
    # Every book while the checkpoint is current, not the first alone:
    # production judges each cycle and charter 77 picks the decisions.
    assert "LIMIT 1" not in pairing
    assert not hasattr(L, "MAX_ENTRY_CYCLES_PER_STAGE")
    # Bounded by the next same-or-higher checkpoint, which takes the book.
    assert "LEDGER_STAGES[LEDGER_STAGES.index(str(stage)):]" in pairing
    # And by the envelope's own servability, production's rule.
    assert "envelope.is_servable(observed_at)" in pairing
    from bayes_basket_target.contracts import ProbabilityEnvelope

    assert callable(getattr(ProbabilityEnvelope, "is_servable"))
    candidates = inspect.getsource(L._stage_entry_candidates)
    assert "_books_for_checkpoint(" in candidates
    assert "for quotes, observed_at in books:" in candidates
    assert not hasattr(L, "_quotes_at")
    assert not hasattr(L, "MAX_QUOTE_STALENESS_MINUTES")
    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # The P1 window is judged on the book's clock, as production judges it.
    assert 'str(payload["observed_at"])' in source
    assert "On the book's clock, which is the compile's clock" in source
    assert '"checkpoint_books": "every_committed_book_while_current_v1",' in source


def test_charter_73_authorizes_the_increment_on_the_seated_belief() -> None:
    """The execution layer's number is the kernel's, read on the seat's belief.

    Production's bundle re-score authorizes on ``whole_basket_edge_delta_v1``
    over ``seated.window_probabilities`` -- the charter-68 partition the
    solver judged the window on -- above ``execution_weights.epsilon_score``,
    with a ticket authorized on structure alone (charter 74).  The walk now
    applies the same function, on the same belief, to the increment it is
    about to book; 0 of 47 P1 top-ups on the 2026-09-04 book were refused by
    it, which is the point of carrying it: a rule that is dormant today and
    binding tomorrow must already be on both sides.
    """

    import inspect

    assert L.whole_basket_edge_delta_v1 is K.whole_basket_edge_delta_v1
    kernel = inspect.getsource(K)
    assert "score > execution_weights.epsilon_score + EPS" in kernel
    # No candidate origin is exempt from the score any more.
    assert "EXECUTION_AUTHORIZATION_CHAIN_TICKET" not in kernel
    judge = inspect.getsource(L._production_judge)
    assert '.get("window_probabilities")' in judge
    assert "delta_policy.execution_weights.epsilon_score" in judge
    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    # Charter 92: the ticket is no longer exempt from that check.
    assert "CHARTER_92_TICKET_EDGE" not in source
    assert '"ticket_authorization": (' in source
    assert "_edge = whole_basket_edge_delta_v1(" in source
    assert "if not _edge > _epsilon + 1e-9:" in source
    assert "execution_edge_not_authorized" in L.REFUSAL_REASONS
    assert '"execution_authorization": "whole_basket_edge_v1",' in source
    assert '"taker_fill": "top_of_offer_cut_to_depth_v1",' in source


def test_the_chains_tilt_is_the_openers_recorded_once() -> None:
    """One factor per chain, earned at the opening seat and inherited after.

    Production records the opener's consolidation tilt once
    (``record_chain_opener_tilt``: INSERT OR IGNORE at the first authorized
    P0 decision) and every later compile on the station-day inherits it
    outright through ``load_chain_tilt`` -- a P0 reseat as much as P0-M
    or P1.  This lane re-read each P0 cycle's own lights and overwrote the
    chain's factor at every seat, so the factor P1 inherited was the last
    P0 seat's, not the opener's.
    """

    import inspect

    from bayes_basket_target import consolidation_chain as CC

    record = inspect.getsource(CC.record_chain_opener_tilt)
    assert "INSERT OR IGNORE" in record
    load = inspect.getsource(CC.load_chain_tilt)
    assert "opener" in load
    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert 'if stage == "P0" and not held:' in source
    assert (
        "                    if seat_consolidation is not None:\n"
        "                        chain_tilt = tilt\n"
    ) in source
    assert "                    chain_tilt = tilt\n                    if seat_consolidation" not in source


def test_every_charter_clause_the_audit_prices_has_one_switch() -> None:
    """A clause that cannot be turned off cannot be measured.

    The 2026-09-05 gate audit priced every gate that had a switch and had
    to leave 77, 78, 66A, 71a, 71b, 72 and 70 unmeasured -- which is how a
    clause outlives the evidence it was ruled in on.  The clamp did: ruled
    in on 2026-09-02 against a book that priced every checkpoint on the
    book before it, and worth minus seven points once that advantage was
    removed.  Each clause now has a production switch defaulting to the
    shipped rule, and the ledger imports it, so an arm flips one rule on
    both sides at once.
    """

    import inspect

    for name in (
        "CHARTER_66A_CHAIN_PAR", "CHARTER_70_EXCHANGE_LOT",
        "CHARTER_71A_TOPUP_IDENTITY", "CHARTER_71B_CHAIN_GROWTH",
        "CHARTER_72_KELLY_SIZING", "CHARTER_77_DECISION_MOMENTS",
        "CHARTER_78_INCUMBENT_NO_EXEMPTION", "CHARTER_83_MIN_NOTIONAL",
        "CHARTER_84_LADDER_BALANCE",
    ):
        assert getattr(P, name) == "on", name
        assert getattr(L, name) is getattr(P, name), name
    # And each is read where its clause is applied.
    solver = inspect.getsource(P._solve_over_support_windows)
    assert 'CHARTER_77_DECISION_MOMENTS == "off"' in solver
    assert 'CHARTER_66A_CHAIN_PAR == "on"' in solver
    assert 'CHARTER_71A_TOPUP_IDENTITY == "on"' in solver
    assert 'CHARTER_71B_CHAIN_GROWTH == "on"' in solver
    windows = inspect.getsource(P._support_windows)
    assert 'CHARTER_78_INCUMBENT_NO_EXEMPTION == "off"' in windows
    compile_plan = inspect.getsource(P.compile_delta_v2_runtime_plan)
    assert 'CHARTER_72_KELLY_SIZING == "on"' in compile_plan
    assert 'CHARTER_70_EXCHANGE_LOT == "on"' in compile_plan
    assert 'CHARTER_83_MIN_NOTIONAL == "on"' in compile_plan
    candidates = inspect.getsource(P._target_candidates)
    assert 'CHARTER_84_LADDER_BALANCE == "on"' in candidates
    # The rulings of 2026-09-05/06 are the shipped defaults.
    assert P.DECLINE_CLAMP_MODE == "off"
    assert P.MARKET_LEAN_GATE_MODE == "on"
    # Charter 96 supersedes the 2026-09-05 ``ticket_only`` default: P0
    # sizes the seat it just proved an edge on.


def test_a_rebuild_can_be_taken_oldest_first_in_slices() -> None:
    """A seat is sized off the book the days before it left behind.

    So a rebuild that will not finish in one sitting has to advance a date
    bound upward: taken newest-first, the recent slice is sized off rows
    the older slice then re-derives, and the equity path is wrong in a way
    nothing downstream can see.  The bound only restricts which days the
    walk visits; rows above it keep whatever they carry.
    """

    source = (ROOT / "scripts" / "paper_trading_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "through_date: str | None = None," in source
    assert "AND (?3 IS NULL OR event_date <= ?3)" in source
    assert '"--through-date",' in source

def test_charter_110_both_lanes_read_one_ladder_and_one_rung_rule() -> None:
    """The ledger does not own a ladder.  It imports production's, so a
    charter-110 narrowing cannot land on one side only -- which is the
    failure charter 94(九) recorded when the ledger spent days scoring a
    strategy production does not run.
    """

    from bayes_basket_target.basket_kernel import delta_v2 as kernel
    from bayes_basket_target import resting_ladder_lifecycle as P_LIFECYCLE

    assert L.bid_anchored_ladder is P.bid_anchored_ladder
    assert L.bid_anchored_rungs_for_gap is P.bid_anchored_rungs_for_gap
    assert P.MAKER_LADDER_ANCHOR_OFFSETS == (1,)
    # And the per-rung decision is the kernel's single implementation, which
    # both the production lifecycle and the whole-basket verdict call.
    assert callable(kernel.resting_rung_action_v1)
    verdict = kernel.execution_completion_verdict_v1(
        window=("a", "b"),
        target_shares={"a": 5.0, "b": 5.0},
        held_shares={"a": 5.0, "b": 0.0},
        held_cost_usdc={"a": 1.0, "b": 0.0},
        claim_by_bucket={"a": 0.40, "b": 0.20},
        book_by_bucket={
            "a": {"bid": 0.20, "ask": 0.22, "tick": 0.01},
            "b": {"bid": 0.28, "ask": 0.30, "tick": 0.01},
        },
        rung_by_bucket={"b": {"limit": 0.29, "age_seconds": 1200.0}},
        hours_to_cutoff=8.0,
    )
    assert verdict["legs"]["b"]["action"] == kernel.LEG_ACTION_CROSS
    assert verdict["basket_state"] == kernel.BASKET_STATE_PARTIAL

    # And the ledger's own re-post asks the same function.  Before charter
    # 110 this lane re-derived the ladder from the current quote and
    # REPLACED the standing rungs with it, which follows a falling bid down
    # just as readily as a rising one up, and kept the standing rungs when
    # the derivation came back empty -- the stranding a collapsing ask
    # sweeps.  Production's lifecycle had the same two defects.  If only one
    # lane were fixed the ledger would once again be scoring a strategy
    # production does not run (charter 94(九)'s judgement).
    import inspect

    source = inspect.getsource(L)
    assert "resting_rung_action_v1(" in source
    assert source.count("if MAKER_RESTING_ANCHORED:\n                        # Charter 110") == 1
    assert L.RESTING_REPRICE_FAR_TICKS == P_LIFECYCLE.RESTING_REPRICE_FAR_TICKS
    assert L.RESTING_REPRICE_DWELL_SECONDS == (
        P_LIFECYCLE.RESTING_REPRICE_DWELL_SECONDS
    )

