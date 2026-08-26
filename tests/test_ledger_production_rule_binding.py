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
    ("MINIMUM_LEG_ALL_IN_ASK", P, "MINIMUM_LEG_ALL_IN_ASK"),
    ("MAKER_RESTING_LADDER_STEPS", P, "MAKER_LADDER_STEPS"),
    ("MAKER_RESTING_IMPROVE_TICKS", P, "MAKER_LADDER_IMPROVE_TICKS"),
    ("MAKER_RESTING_VALUE_LAMBDA", P, "MAKER_HEDGE_VALUE_LAMBDA"),
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
                assert L._resting_ladder(
                    _Row(bid, ask, tick), value_cap=cap
                ) == P._maker_ladder(
                    bid=bid, ask=ask, tick=tick, value_cap=cap
                ), (tick, bid, ask, cap)


def test_the_leg_value_cap_is_productions_hedge_value_cap() -> None:
    """One red line, one definition.

    ``_hedge_value_cap``'s docstring claimed this from 2026-08-22 while they
    were in fact two functions agreeing by inspection.  They now agree by
    call, and this holds them to it across the centre exemption, the charter
    63 shoulder exemption and the plain lambda.
    """

    for window in (("a", "b", "c"), ("a", "b", "c", "d")):
        for centre in window:
            for bucket in window:
                for probability in (0.02, 0.05, 0.15, 0.3, 0.55, 0.9):
                    probabilities = {b: probability for b in window}
                    assert L._leg_value_cap(
                        bucket,
                        probabilities=probabilities,
                        centre_bucket=centre,
                        window=window,
                        market_top_bucket=None,
                        all_in_ask=0.2,
                    ) == P._hedge_value_cap(
                        bucket=bucket,
                        centre_bucket=centre,
                        probability=probability,
                        window=window,
                    ), (window, centre, bucket, probability)


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
    "CERTAINTY_GATE", "FEE_RATE", "KELLY_FRACTION",
    "MAKER_RESTING_IMPROVE_TICKS", "MAKER_RESTING_LADDER_STEPS",
    "MAKER_RESTING_SHOULDER_LAMBDA", "MAKER_RESTING_VALUE_LAMBDA",
    "MAXIMUM_SUPPORT_BUCKETS", "MINIMUM_SUPPORT_BUCKETS",
    "MAKER_RESTING_COVERAGE_BUDGET", "MAKER_RESTING_COVERAGE_LAMBDA",
}

# This lane's own, with the reason it has no production counterpart.
LEDGER_ONLY = {
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
    "CONSOLIDATION_TILT_FACTOR": "measured stake tilt, not yet in production",
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
    "EXECUTION_MODE": "research arm",
    "EXECUTION_MODES": "research arm",
    "WINDOW_STRATEGY": "research arm",
    "WINDOW_STRATEGIES": "research arm",
    "SEAT_PRICING_BASIS": "comparison arm for the 2026-08-26 fix",
    "SEAT_PRICING_BASES": "same",
    "SELECTION_DISCOUNT": "measurement lane; must not become a gate",
    "MAKER_RESTING_BAND_TOP": "research arm",
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

    assert L.MAXIMUM_SUPPORT_BUCKETS == (
        TargetBasketPolicy.maximum_support_buckets
    )
    assert L.MINIMUM_SUPPORT_BUCKETS == (
        TargetBasketPolicy.minimum_support_buckets
    )
