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
                assert L._resting_ladder(
                    _Row(bid, ask, tick), value_cap=cap
                ) == P._maker_ladder(
                    bid=bid,
                    ask=ask if cap is None else min(ask, cap),
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
    assert L.EXECUTION_MODE == "taker"
    for bid, ask in _BOOKS:
        row = _Row(bid, ask, 0.001)
        for cap in _CAPS:
            assert L._execution_price(row, value_cap=cap) == pytest.approx(
                ask * (1.0 + L.FEE_RATE)
            ), (bid, ask, cap)


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
    # Production's per-leg reservation veto (TAKER_LEG_VETO), replayed here
    # as an arm; the two are asserted equivalent below.
    "LEG_VETO": "production's TAKER_LEG_VETO, asserted equivalent",
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
    # Book-keeping for a paper book.  Production has a real capital pool.
    "INITIAL_CAPITAL_USDC": "paper book's opening equity",
    "BOOK_MODEL": "one book per station-day, a replay concept",
    "DUST_COST_FLOOR": "excludes dust fills from the paper equity curve",
    "LEDGER_EVENT_DATE_FLOOR": "which history this book admits",
    "LEDGER_STAGES": "replay bookkeeping",
    "MAX_ENTRY_CYCLES_PER_STAGE": "replay walk bound",
    "MAX_QUOTE_STALENESS_MINUTES": "replay freshness bound",
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
    "EXECUTION_MODE": "taker by charter 65; the maker modes stay as arms",
    "EXECUTION_MODES": "same",
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
    "CHAIN_TICKET_MODE": (
        "ledger-only by design: it compensates for THIS book's one-book rule"
        " (only P0 opens), which production does not have -- its three stages"
        " compile independently, so a production P1 needs no ticket to act"
    ),
    "CHAIN_TICKET_MODE": (
        "replay arm; keeps a minimum-clip P0 opener when the shrunk claim"
        " sizes it at zero, so the chain survives for its P1 top-ups"
    ),
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
    assert L.EXECUTION_MODE == P.EXECUTION_MODE


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
    assert L.DECLINE_CLAMP == P.DECLINE_CLAMP_MODE == "after_open"


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
    assert (L.LEG_VETO == "on") == (P.TAKER_LEG_VETO == "reservation")
    assert P.TAKER_LEG_VETO == "joint"
    assert K.EFFECTIVE_EDGE_BUCKET_LAMBDA == (
        (0.50, 0.22),
        (0.70, 0.43),
        (0.85, 0.80),
        (float("inf"), 1.38),
    )
    assert K.effective_edge_bucket_lambda_for_stage("P0", 1.27) == 1.38
    assert K.effective_edge_bucket_lambda_for_stage("P1", 1.27) == 1.0

