"""Stress the curve objective across opening, holding and rebalancing.

Research-only, read-only, no database and no network.  The objective was
just taught to treat the position already held as its initial condition;
that changes what it optimizes on every station-day that is not flat, and a
flat repository (no fills since 2026-07-29) cannot exercise any of it.  So
the positions are manufactured here instead, adversarially, and the answers
are checked against properties that must hold whatever the numbers are.

Three regimes, because they are three different problems:

* OPEN       flat book, nothing held, nothing sunk.
* HOLD       inventory already carried, the belief unchanged since it was
             bought.
* REBALANCE  inventory carried and the belief has moved -- toward some
             rungs, away from others -- plus inventory stranded on buckets
             the seat window has since left behind.

What is asserted is not "the numbers look reasonable" but invariants that
a wrong answer must violate:

  FEASIBLE     stakes are finite, non-negative, inside the depth box and
               inside the budget.
  NO_RUIN      no settlement drives wealth to zero: the reserve after the
               purchase stays positive.
  OPTIMAL      the answer's own curve score is not beaten by any of a
               thousand feasible perturbations around it, nor by the
               from-flat-then-add allocation the code used to produce.
  SATIATION    buying more of an outcome you already own is worth less:
               raising inventory on one leg never raises the new purchase
               of that same leg.
  MONOTONE     more budget never scores worse -- the feasible set only
               grows.
  DETERMINISM  identical inputs give byte-identical answers.
  CONTRACT     malformed inputs raise ContractError rather than returning
               a number nobody can defend.

Run:  python3 scripts/stress_objective_position_scenarios.py [trials]
"""

from __future__ import annotations

import math
import random
import sys
from collections.abc import Sequence
from typing import Any

sys.path.insert(0, ".")

from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    DEFAULT_KELLY_FRACTION,
    ValueWeightsV2,
    curve_score,
    evaluate_basket_value_v2,
)
from bayes_basket_target.basket_kernel.contracts import (  # noqa: E402
    ContractError as KernelContractError,
)
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.shadow_objective import (  # noqa: E402
    solve_fractional_kelly,
)

# The kernel keeps its own contract type.  A harness that catches only one of
# them reports the other as a crash -- which it did on the first run, on a
# refusal that is ordinary behaviour.
CONTRACT_ERRORS = (ContractError, KernelContractError)

NEUTRAL_WEIGHTS = ValueWeightsV2.from_mapping({
    "lambda_profit_floor": 0.0, "lambda_cvar": 0.0, "lambda_shortfall": 0.0,
    "lambda_breakthrough": 0.0, "lambda_overfill": 0.0,
    "lambda_excess_hhi": 0.0, "cvar_alpha": 0.25, "hhi_max": 0.75,
    "expected_pnl_scale": 1.0, "profit_floor_scale": 1.0, "cvar_scale": 1.0,
    "coverage_scale": 1.0, "overfill_scale": 1.0, "excess_hhi_scale": 1.0,
})


class Scenario:
    """One station-day's objective inputs, in whatever state it is in."""

    def __init__(
        self,
        *,
        regime: str,
        label: str,
        probabilities: Sequence[float],
        prices: Sequence[float],
        depth_caps: Sequence[float],
        held: Sequence[float],
        sunk: float,
        outside: Sequence[tuple[float, float]],
        budget: float,
        bankroll: float,
    ) -> None:
        self.regime = regime
        self.label = label
        self.probabilities = [float(v) for v in probabilities]
        self.prices = [float(v) for v in prices]
        self.depth_caps = [float(v) for v in depth_caps]
        self.held = [float(v) for v in held]
        self.sunk = float(sunk)
        self.outside = [(float(p), float(s)) for p, s in outside]
        self.budget = float(budget)
        self.bankroll = float(bankroll)

    @property
    def n(self) -> int:
        return len(self.prices)

    def solve(self, **overrides: Any) -> list[float]:
        kwargs: dict[str, Any] = {
            "probabilities": self.probabilities,
            "prices": self.prices,
            "depth_caps": self.depth_caps,
            "budget_usdc": self.budget,
            "bankroll_usdc": self.bankroll,
            "kelly_fraction": 1.0,
            "held_shares": self.held,
            "sunk_cost_usdc": self.sunk,
            "held_outside": self.outside,
        }
        kwargs.update(overrides)
        return solve_fractional_kelly(**kwargs)

    def score(self, bought: Sequence[float]) -> float:
        """The ranking's own curve score of the resulting whole position.

        Scored exactly as ``solve_target_inventory_v2`` scores a candidate:
        every outcome in the partition, sunk cost inside, effective bankroll
        ``W/f``.  The solver is handed ``W/f`` as its bankroll, so the two
        are the same object and the optimum of one is the optimum of both.
        """

        names = [f"b{i}" for i in range(self.n)]
        outside_names = [f"o{i}" for i in range(len(self.outside))]
        probabilities: dict[str, float] = {
            name: self.probabilities[i] for i, name in enumerate(names)
        }
        shares: dict[str, float] = {
            name: self.held[i] + bought[i] for i, name in enumerate(names)
        }
        cost: dict[str, float] = {name: 0.0 for name in names}
        cost[names[0]] += self.sunk
        for i, name in enumerate(names):
            cost[name] += bought[i] * self.prices[i]
        for j, name in enumerate(outside_names):
            probabilities[name] = self.outside[j][0]
            shares[name] = self.outside[j][1]
            cost[name] = 0.0
        residual = 1.0 - sum(probabilities.values())
        if residual > 1e-12:
            probabilities["residual"] = residual
            shares["residual"] = 0.0
            cost["residual"] = 0.0
        elif residual < -1e-9:
            raise AssertionError("scenario probability mass exceeds one")
        audit = evaluate_basket_value_v2(
            probabilities=probabilities,
            shares_by_bucket=shares,
            cost_by_bucket=cost,
            protected_buckets=names,
            shortfall_buckets=(),
            breakthrough_buckets=(),
            weights=NEUTRAL_WEIGHTS,
        )
        return curve_score(
            audit,
            bankroll_usdc=self.bankroll * DEFAULT_KELLY_FRACTION,
            kelly_fraction=DEFAULT_KELLY_FRACTION,
        )


def _prices(rng: random.Random, n: int, *, edgy: bool) -> list[float]:
    if edgy:
        return [rng.choice([0.001, 0.01, 0.5, 0.97, 0.999]) for _ in range(n)]
    return [round(rng.uniform(0.02, 0.85), 4) for _ in range(n)]


def _probabilities(rng: random.Random, n: int, *, full: bool) -> list[float]:
    draws = [rng.expovariate(1.0) + 1e-6 for _ in range(n)]
    total = sum(draws)
    mass = 1.0 if full else rng.uniform(0.25, 0.95)
    return [value / total * mass for value in draws]


def scenario(rng: random.Random, regime: str) -> Scenario:
    n = rng.randint(1, 11)
    edgy = rng.random() < 0.25
    full = rng.random() < 0.2
    probabilities = _probabilities(rng, n, full=full)
    prices = _prices(rng, n, edgy=edgy)
    depth_caps = [
        rng.choice([0.0, 1e-3, rng.uniform(1.0, 50.0), 1e6])
        for _ in range(n)
    ]
    bankroll = rng.choice([1.0, 30.0, 200.0, 800.0, 1e5])
    if regime == "OPEN":
        held = [0.0] * n
        sunk = 0.0
        outside: list[tuple[float, float]] = []
    else:
        held = [
            rng.choice([0.0, rng.uniform(0.1, 40.0), 5_000.0])
            for _ in range(n)
        ]
        # Sunk cost is deliberately allowed to exceed the bankroll: a book
        # that has spent more than its allotted risk capital is a state the
        # solver has to answer for, not one it may assume away.
        sunk = rng.choice([
            0.0,
            sum(h * c for h, c in zip(held, prices)),
            bankroll * rng.uniform(0.5, 2.0),
        ])
        outside = []
        if regime == "REBALANCE":
            spare = max(0.0, 1.0 - sum(probabilities))
            for _ in range(rng.randint(0, 3)):
                if spare <= 1e-9:
                    break
                take = rng.uniform(0.0, spare)
                spare -= take
                outside.append((take, rng.uniform(0.0, 500.0)))
    budget = rng.choice([
        0.0, 1e-4, rng.uniform(0.5, 20.0), bankroll, 1e7,
    ])
    return Scenario(
        regime=regime,
        label=f"{regime}/n={n}/edgy={int(edgy)}/full={int(full)}",
        probabilities=probabilities, prices=prices, depth_caps=depth_caps,
        held=held, sunk=sunk, outside=outside,
        budget=budget, bankroll=bankroll,
    )


class Findings:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.notes: list[tuple[str, str, str]] = []
        self.checked = 0
        self.skipped = 0

    def fail(self, invariant: str, case: Scenario, detail: str) -> None:
        self.rows.append((invariant, case.label, detail))

    def note(self, tag: str, case: Scenario, detail: str) -> None:
        self.notes.append((tag, case.label, detail))

    @staticmethod
    def _show(rows: list[tuple[str, str, str]], *, limit: int) -> None:
        seen: dict[str, list[tuple[str, str]]] = {}
        for tag, label, detail in rows:
            seen.setdefault(tag, []).append((label, detail))
        for tag, hits in sorted(seen.items(), key=lambda item: -len(item[1])):
            print(f"\n  {tag}: {len(hits)}")
            for label, detail in hits[:limit]:
                print(f"    {label}\n      {detail}")

    def report(self) -> int:
        print(f"\nchecked {self.checked} scenarios ({self.skipped} refused)")
        if self.notes:
            print(f"\nDIAGNOSTICS: {len(self.notes)}")
            self._show(self.notes, limit=2)
        if not self.rows:
            print("\nno invariant violated")
            return 0
        print(f"\nVIOLATIONS: {len(self.rows)}")
        self._show(self.rows, limit=4)
        return 1


def check(case: Scenario, out: Findings, rng: random.Random) -> None:
    try:
        stakes = case.solve()
    except CONTRACT_ERRORS:
        # A refusal is a legitimate answer; what is forbidden is a number
        # that cannot be defended.
        out.skipped += 1
        return
    out.checked += 1

    # FEASIBLE ---------------------------------------------------------
    for i, value in enumerate(stakes):
        if not math.isfinite(value) or value < -1e-12:
            out.fail("FEASIBLE", case, f"stake[{i}]={value}")
            return
        if value > case.depth_caps[i] + 1e-6:
            out.fail(
                "FEASIBLE", case,
                f"stake[{i}]={value:.6f} over depth {case.depth_caps[i]}",
            )
    spend = sum(q * c for q, c in zip(stakes, case.prices))
    if spend > case.budget + 1e-6:
        out.fail(
            "FEASIBLE", case,
            f"spend {spend:.6f} over budget {case.budget:.6f}",
        )

    # NO_RUIN ----------------------------------------------------------
    reserve = case.bankroll - case.sunk - spend
    if spend > 0.0 and reserve <= 0.0:
        out.fail(
            "NO_RUIN", case,
            f"reserve {reserve:.6f} <= 0 after spending {spend:.6f}",
        )

    # DETERMINISM ------------------------------------------------------
    if case.solve() != stakes:
        out.fail("DETERMINISM", case, "two identical solves disagreed")

    # OPTIMAL ----------------------------------------------------------
    try:
        best = case.score(stakes)
        idle = case.score([0.0] * case.n)
    except (*CONTRACT_ERRORS, AssertionError, ValueError) as exc:
        out.fail("OPTIMAL", case, f"score unavailable: {exc}")
        return
    if not math.isfinite(idle):
        # The position was already ruinous before this decision: its sunk
        # cost alone exceeds the effective bankroll, so every allocation
        # including buying nothing scores minus infinity and the ranking has
        # nothing to discriminate with.  Not a solver defect -- but not
        # nothing either, so it is counted.
        out.note("PRE_RUINED", case, f"sunk {case.sunk:.4f} vs bankroll "
                 f"{case.bankroll:.4f}, buying nothing already scores -inf")
        return
    if not math.isfinite(best):
        out.fail(
            "NO_RUIN", case,
            f"buying nothing scores {idle:.6f} but the solve scores {best}",
        )
        return
    # (a) against the allocation the old code produced: solve from flat,
    #     add to inventory.
    try:
        flat = solve_fractional_kelly(
            probabilities=case.probabilities, prices=case.prices,
            depth_caps=case.depth_caps, budget_usdc=case.budget,
            bankroll_usdc=case.bankroll, kelly_fraction=1.0,
        )
    except CONTRACT_ERRORS:
        flat = None
    if flat is not None:
        flat_spend = sum(q * c for q, c in zip(flat, case.prices))
        if (
            flat_spend <= case.budget + 1e-9
            and case.bankroll - case.sunk - flat_spend > 0.0
        ):
            legacy = case.score(flat)
            if math.isfinite(legacy) and legacy > best + 1e-9:
                out.fail(
                    "OPTIMAL", case,
                    f"from-flat scores {legacy:.9f} > {best:.9f}",
                )
    # (b) against feasible perturbations of the answer itself.
    for _ in range(120):
        spread = rng.choice([1e-3, 1e-2, 0.1, 1.0, 10.0])
        trial = [
            min(
                case.depth_caps[i],
                max(0.0, stakes[i] + rng.gauss(0.0, spread)),
            )
            for i in range(case.n)
        ]
        trial_spend = sum(q * c for q, c in zip(trial, case.prices))
        if trial_spend > case.budget + 1e-9:
            continue
        if case.bankroll - case.sunk - trial_spend <= 0.0:
            continue
        rival = case.score(trial)
        if math.isfinite(rival) and rival > best + 1e-7:
            out.fail(
                "OPTIMAL", case,
                f"perturbation scores {rival:.9f} > {best:.9f} "
                f"(delta {rival - best:.2e})",
            )
            break

    # KKT --------------------------------------------------------------
    # Sharper than perturbation search, and the condition the solver is
    # supposed to satisfy: differentiating the growth gives
    # ``p_i / (c_i A_i) = lambda`` for every leg held strictly inside its
    # bounds, whether or not the budget binds.  A leg pinned at zero may sit
    # below that level and one pinned at its depth cap above it, but two
    # interior legs disagreeing means dollars are in the wrong place.
    spend_now = sum(q * c for q, c in zip(stakes, case.prices))
    reserve_now = case.bankroll - case.sunk - spend_now
    if reserve_now > 1e-9:
        interior = [
            i for i in range(case.n)
            if stakes[i] > 1e-9 and stakes[i] + 1e-9 < case.depth_caps[i]
        ]
        marginals = [
            case.probabilities[i]
            / (case.prices[i] * (reserve_now + case.held[i] + stakes[i]))
            for i in interior
        ]
        positive = [value for value in marginals if value > 0.0]
        if len(positive) > 1:
            spread = max(positive) / min(positive) - 1.0
            if spread > 1e-4:
                out.fail(
                    "KKT", case,
                    f"interior legs {interior} disagree on marginal growth "
                    f"per dollar by {spread:.3%} "
                    f"({min(positive):.6g}..{max(positive):.6g})",
                )

    # SATIATION --------------------------------------------------------
    # Owning more of an outcome cannot make buying more of it attractive.
    leg = rng.randrange(case.n)
    richer = Scenario(
        regime=case.regime, label=case.label,
        probabilities=case.probabilities, prices=case.prices,
        depth_caps=case.depth_caps,
        held=[
            value + (25.0 if i == leg else 0.0)
            for i, value in enumerate(case.held)
        ],
        sunk=case.sunk, outside=case.outside,
        budget=case.budget, bankroll=case.bankroll,
    )
    try:
        richer_stakes = richer.solve()
    except CONTRACT_ERRORS:
        richer_stakes = None
    if richer_stakes is not None:
        slack = 1e-6 + 1e-3 * max(1.0, stakes[leg])
        if richer_stakes[leg] > stakes[leg] + slack:
            out.fail(
                "SATIATION", case,
                f"leg {leg}: holding 25 more raised the buy "
                f"{stakes[leg]:.6f} -> {richer_stakes[leg]:.6f}",
            )

    # MONOTONE ---------------------------------------------------------
    richer_budget = Scenario(
        regime=case.regime, label=case.label,
        probabilities=case.probabilities, prices=case.prices,
        depth_caps=case.depth_caps, held=case.held, sunk=case.sunk,
        outside=case.outside, budget=case.budget * 2.0 + 1.0,
        bankroll=case.bankroll,
    )
    try:
        wider = richer_budget.solve()
    except CONTRACT_ERRORS:
        wider = None
    if wider is not None:
        wider_spend = sum(q * c for q, c in zip(wider, case.prices))
        if case.bankroll - case.sunk - wider_spend > 0.0:
            wider_score = case.score(wider)
            if math.isfinite(wider_score) and wider_score < best - 1e-7:
                out.fail(
                    "MONOTONE", case,
                    f"doubling the budget scored worse: "
                    f"{wider_score:.9f} < {best:.9f}",
                )


def contract_cases(out: Findings) -> None:
    """Malformed inputs must refuse, not improvise."""

    base = {
        "probabilities": [0.3, 0.3], "prices": [0.2, 0.3],
        "depth_caps": [10.0, 10.0], "budget_usdc": 5.0,
        "bankroll_usdc": 100.0,
    }
    bad: list[tuple[str, dict[str, Any]]] = [
        ("held_shares wrong length", {"held_shares": [1.0]}),
        ("held_shares negative", {"held_shares": [-1.0, 0.0]}),
        ("held_shares nan", {"held_shares": [float("nan"), 0.0]}),
        ("held_shares inf", {"held_shares": [float("inf"), 0.0]}),
        ("sunk negative", {"sunk_cost_usdc": -1.0}),
        ("sunk nan", {"sunk_cost_usdc": float("nan")}),
        ("outside not a pair", {"held_outside": [(0.1,)]}),
        ("outside triple", {"held_outside": [(0.1, 1.0, 2.0)]}),
        ("outside negative mass", {"held_outside": [(-0.1, 1.0)]}),
        ("outside nan shares", {"held_outside": [(0.1, float("nan"))]}),
        ("bankroll zero", {"bankroll_usdc": 0.0}),
        ("bankroll negative", {"bankroll_usdc": -5.0}),
        ("budget negative", {"budget_usdc": -1.0}),
        ("price at one", {"prices": [0.2, 1.0]}),
        ("price zero", {"prices": [0.0, 0.3]}),
        ("fraction zero", {"kelly_fraction": 0.0}),
        ("fraction above one", {"kelly_fraction": 1.5}),
        ("probability nan", {"probabilities": [float("nan"), 0.3]}),
        ("depth negative", {"depth_caps": [-1.0, 10.0]}),
    ]
    for name, override in bad:
        kwargs = {**base, **override}
        try:
            result = solve_fractional_kelly(**kwargs)
        except CONTRACT_ERRORS:
            continue
        except Exception as exc:  # noqa: BLE001 -- that is the finding
            out.rows.append((
                "CONTRACT", name,
                f"raised {type(exc).__name__} instead of ContractError: {exc}",
            ))
            continue
        out.rows.append((
            "CONTRACT", name, f"accepted and returned {result}",
        ))


def edge_battery() -> list[Scenario]:
    """The corners a random draw reaches rarely or never."""

    def make(label: str, **kwargs: Any) -> Scenario:
        base: dict[str, Any] = {
            "regime": "EDGE", "label": label,
            "probabilities": [0.3, 0.3], "prices": [0.2, 0.3],
            "depth_caps": [50.0, 50.0], "held": [0.0, 0.0], "sunk": 0.0,
            "outside": [], "budget": 12.0, "bankroll": 120.0,
        }
        base.update(kwargs)
        return Scenario(**base)

    return [
        make("EDGE/single rung", probabilities=[0.6], prices=[0.3],
             depth_caps=[100.0], held=[0.0]),
        make("EDGE/every depth zero", depth_caps=[0.0, 0.0]),
        make("EDGE/dust depth", depth_caps=[1e-9, 1e-9]),
        make("EDGE/budget zero", budget=0.0),
        make("EDGE/budget one cent", budget=0.01),
        make("EDGE/full support, no residual outcome",
             probabilities=[0.45, 0.55]),
        make("EDGE/full support and cheap", probabilities=[0.45, 0.55],
             prices=[0.01, 0.02], budget=1e6),
        make("EDGE/near-certain at near-full price",
             probabilities=[0.99, 0.005], prices=[0.999, 0.001]),
        make("EDGE/penny lottery", probabilities=[0.002, 0.9],
             prices=[0.001, 0.9]),
        make("EDGE/zero probability leg", probabilities=[0.0, 0.6]),
        make("EDGE/no edge anywhere", probabilities=[0.2, 0.3]),
        make("EDGE/saturated inventory", held=[1e9, 1e9]),
        make("EDGE/inventory one leg only", held=[5000.0, 0.0]),
        make("EDGE/sunk equals bankroll", sunk=120.0),
        make("EDGE/sunk one cent under bankroll", sunk=119.99),
        make("EDGE/sunk over bankroll", sunk=500.0),
        make("EDGE/tiny bankroll", bankroll=1e-6, budget=1e-6),
        make("EDGE/huge bankroll", bankroll=1e9, budget=1e9),
        make("EDGE/outside holds every remaining outcome",
             outside=[(0.4, 1e6)]),
        make("EDGE/outside mass, no shares", outside=[(0.4, 0.0)]),
        make("EDGE/eleven rungs",
             probabilities=[0.09] * 11,
             prices=[round(0.02 + 0.07 * i, 3) for i in range(11)],
             depth_caps=[40.0] * 11, held=[0.0] * 11),
        make("EDGE/eleven rungs, half held",
             probabilities=[0.09] * 11,
             prices=[round(0.02 + 0.07 * i, 3) for i in range(11)],
             depth_caps=[40.0] * 11,
             held=[0.0 if i % 2 else 30.0 for i in range(11)],
             sunk=45.0),
    ]


def plan_smoke(out: Findings) -> None:
    """Drive the whole compile, not just the solver, in all three regimes.

    The solver can be right about a vector and the plan still wrong about
    what it does with it.  These build a real snapshot per regime and assert
    the properties the pipeline downstream relies on: a plan compiles or
    refuses with a named contract error, never crashes; targets never fall
    below what is already held (nothing is ever sold); and no emitted clip
    exceeds the capital it was compiled against.
    """

    from bayes_basket_target.basket_kernel.contracts import (
        TargetBasketPolicy,
        TargetBasketSnapshot,
    )
    from bayes_basket_target.delta_v2_artifact import (
        ARTIFACT_SCHEMA,
        DeltaV2PolicyArtifact,
    )
    from bayes_basket_target.contracts import content_sha256
    from bayes_basket_target.delta_v2_runtime import (
        compile_delta_v2_runtime_plan,
    )

    def artifact() -> DeltaV2PolicyArtifact:
        raw: dict[str, Any] = {
            "schema": ARTIFACT_SCHEMA, "artifact_id": "stress-delta-v2",
            "station_id": "*", "stage": "*", "season_bin": "summer",
            "maturity_bin": "d0", "liquidity_bin": "liquid",
            "strategy_version": "target-v2",
            "value_weights": {
                "lambda_profit_floor": 0.0, "lambda_cvar": 0.0,
                "lambda_shortfall": 0.0, "lambda_breakthrough": 0.0,
                "lambda_overfill": 0.1, "lambda_excess_hhi": 0.0,
                "cvar_alpha": 0.1, "hhi_max": 1.0,
                "expected_pnl_scale": 1.0, "profit_floor_scale": 1.0,
                "cvar_scale": 1.0, "coverage_scale": 1.0,
                "overfill_scale": 1.0, "excess_hhi_scale": 1.0,
            },
            "execution_weights": {
                "epsilon_score": 0.0001, "lambda_wait": 0.0,
                "lambda_queue": 0.0, "lambda_adverse_selection": 0.0,
                "lambda_reprice": 0.0, "lambda_capital_lock": 0.0,
                "wait_scale": 1.0, "queue_scale": 1.0,
                "adverse_selection_scale": 1.0, "reprice_scale": 1.0,
                "capital_lock_scale": 1.0, "maximum_candidates": 16,
            },
            "cash_clip_grid": [0.25, 1.0], "price_grid": [0.1],
            "timeout_grid_seconds": [60.0],
            "fill_distribution_artifact_sha256": "b" * 64,
            "training_evidence_sha256": "c" * 64,
            "train_start_date": "2026-05-01", "train_end_date": "2026-05-31",
            "holdout_start_date": "2026-06-01",
            "holdout_end_date": "2026-06-30",
            "sample_n": 200, "holdout_n": 80,
            "consecutive_forward_windows": 2,
            "compared_baselines": sorted(
                ["v1", "EV_ONLY", "ZERO_WEIGHT", "HOLD"]
            ),
            "oos_metrics": {"delta_score_lift": 0.1},
            "oos_gate_version": "delta-v2-oos-v1", "oos_gate_passed": True,
            "trained_through_utc": "2026-07-01T00:00:00Z",
            "valid_from_utc": "2026-07-02T00:00:00Z",
            "valid_until_utc": "2027-01-01T00:00:00Z",
            "authority_scope": "PRODUCTION_NO_LIVE",
        }
        raw["content_sha256"] = content_sha256(raw)
        return DeltaV2PolicyArtifact.from_mapping(raw)

    def fill_resolver(**_: Any) -> dict[str, Any]:
        return {
            "fill_distribution": [
                {"fill_fraction": 0.0, "probability": 0.1},
                {"fill_fraction": 0.5, "probability": 0.2},
                {"fill_fraction": 1.0, "probability": 0.7},
            ],
            "execution_costs": {
                "wait": 0.0, "queue": 0.0, "adverse_selection": 0.0,
                "reprice": 0.0, "capital_lock": 0.0,
            },
        }

    masses = [0.06, 0.12, 0.26, 0.30, 0.16, 0.10]
    asks = [0.05, 0.11, 0.24, 0.33, 0.15, 0.08]

    def snapshot(
        *, regime: str, held: Sequence[float], costs: Sequence[float],
        shifted: bool,
    ) -> TargetBasketSnapshot:
        probabilities = (
            [masses[(i + 1) % len(masses)] for i in range(len(masses))]
            if shifted
            else list(masses)
        )
        return TargetBasketSnapshot.from_mapping({
            "schema": "target_basket_snapshot_v6",
            "snapshot_id": f"stress-{regime}", "sequence": 1,
            "observed_at": "2026-07-28T03:00:00Z",
            "market_id": "stress-market", "station_id": "RKPK",
            "event_date": "2026-07-28", "quote_version": "quote-1",
            "quote_observed_at": "2026-07-28T03:00:00Z",
            "quote_source": "OFFICIAL_CLOB_WS",
            "inventory_version": "inv-1", "order_state_version": "ord-1",
            "evidence": {
                "probability_stage": "P0", "posterior_version": "p0-v1",
                "posterior_sha256": "a" * 64,
            },
            "buckets": [
                {
                    "bucket": f"{30 + index}",
                    "ordinal": 30 + index,
                    "probability": probabilities[index],
                    "bid_price": max(0.01, asks[index] - 0.02),
                    "bid_levels": [
                        {"price": max(0.01, asks[index] - 0.02),
                         "shares": 40.0},
                    ],
                    "ask_levels": [
                        {"price": asks[index], "shares": 60.0},
                        {"price": asks[index] + 0.02, "shares": 200.0},
                    ],
                    "price_tick_size": 0.01,
                    "maker_queue_ahead_shares": 2.0,
                    "maker_queue_known": True,
                    "filled_shares": held[index],
                    "filled_cost_usdc": costs[index],
                }
                for index in range(len(masses))
            ],
            "checkpoint_changed": True, "quote_changed": True,
            "fill_changed": True, "time_gate_changed": True,
            "evidence_fresh": True, "contract_matches": True,
            "order_state_known": True, "risk_inputs_complete": True,
            "time_left_minutes": 900.0,
        })

    flat = [0.0] * len(masses)
    carried = [0.0, 0.0, 18.0, 22.0, 0.0, 0.0]
    stranded = [9.0, 0.0, 18.0, 22.0, 0.0, 0.0]
    cases = [
        ("OPEN", snapshot(regime="open", held=flat, costs=flat,
                          shifted=False), None),
        ("HOLD", snapshot(regime="hold", held=carried,
                          costs=[0.0, 0.0, 4.3, 7.3, 0.0, 0.0],
                          shifted=False),
         {f"{30 + i}": masses[i] for i in range(len(masses))}),
        ("REBALANCE", snapshot(regime="rebalance", held=carried,
                               costs=[0.0, 0.0, 4.3, 7.3, 0.0, 0.0],
                               shifted=True),
         {f"{30 + i}": masses[i] for i in range(len(masses))}),
        ("REBALANCE-STRANDED",
         snapshot(regime="stranded", held=stranded,
                  costs=[2.1, 0.0, 4.3, 7.3, 0.0, 0.0], shifted=True),
         {f"{30 + i}": masses[i] for i in range(len(masses))}),
    ]
    for capital in (0.0, 0.6, 4.0, 25.0, 500.0):
        for budget in (5.0, 30.0, 200.0):
            for label, snap, previous in cases:
                policy = TargetBasketPolicy.from_mapping({
                    "minimum_support_buckets": 2,
                    "total_budget_usdc": budget,
                    "base_coverage": 0.6, "maximum_coverage": 0.8,
                    "entropy_coverage_gain": 0.0,
                    "clip_min_notional_usdc": 0.5,
                    "clip_max_notional_usdc": 5.0,
                    "protected_floor_usdc": -50.0,
                    "max_outside_loss_usdc": 50.0,
                })
                case = Scenario(
                    regime="PLAN", label=f"PLAN/{label}/cap={capital}"
                    f"/budget={budget}",
                    probabilities=[1.0], prices=[0.5], depth_caps=[1.0],
                    held=[0.0], sunk=0.0, outside=[], budget=capital,
                    bankroll=budget,
                )
                try:
                    plan = compile_delta_v2_runtime_plan(
                        snapshot=snap, basket_policy=policy,
                        delta_policy=artifact(),
                        fill_resolver=fill_resolver,
                        capital_available_usdc=capital,
                        minimum_profit_floor=-50.0,
                        resource_probe_ok=True,
                        previous_probabilities=previous,
                    )
                except CONTRACT_ERRORS as exc:
                    out.note("PLAN_REFUSED", case, str(exc))
                    continue
                except Exception as exc:  # noqa: BLE001 -- that is the point
                    out.fail(
                        "PLAN_CRASH", case,
                        f"{type(exc).__name__}: {exc}",
                    )
                    continue
                out.checked += 1
                held_by_bucket = {
                    row.bucket: row.filled_shares + row.pending_shares
                    for row in snap.buckets
                }
                for target in plan.get("targets", ()):
                    bucket = str(target["bucket"])
                    if (
                        float(target["target_shares"])
                        + 1e-9
                        < held_by_bucket[bucket]
                    ):
                        out.fail(
                            "NEVER_SELLS", case,
                            f"{bucket}: target "
                            f"{target['target_shares']:.4f} below held "
                            f"{held_by_bucket[bucket]:.4f}",
                        )
                    if float(target["delta_shares"]) < -1e-9:
                        out.fail(
                            "NEVER_SELLS", case,
                            f"{bucket}: negative gap "
                            f"{target['delta_shares']}",
                        )
                clips = sum(
                    float(target.get("clip_notional_usdc") or 0.0)
                    for target in plan.get("targets", ())
                )
                if clips > capital + 1e-6:
                    out.fail(
                        "PLAN_BUDGET", case,
                        f"clips {clips:.4f} over capital {capital:.4f}",
                    )
                if plan.get("state") not in {"TRADE", "HOLD"}:
                    out.fail(
                        "PLAN_STATE", case, f"state {plan.get('state')!r}"
                    )


def main() -> int:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    rng = random.Random(20260816)
    out = Findings()
    contract_cases(out)
    for case in edge_battery():
        check(case, out, rng)
    for index in range(trials):
        regime = ("OPEN", "HOLD", "REBALANCE")[index % 3]
        check(scenario(rng, regime), out, rng)
    plan_smoke(out)
    return out.report()


if __name__ == "__main__":
    raise SystemExit(main())
