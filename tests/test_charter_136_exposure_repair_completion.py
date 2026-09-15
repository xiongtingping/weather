"""Charter 136: a short leg the market took is completed on the exposure.

User ruling 2026-09-15: "补单的负边际权重判断要小于篮子不完整的暴露风险".
The window gate refused completing EHAM 19C (ask 0.52, claim 0.505), ZGSZ
31C (0.35 against 0.307) and ZGGG 32C (0.31 against 0.303) for 2026-09-16
after follow-up cancels, each on its own edge under the 5% hurdle, and left
the basket one leg short.  Settled 2026-09-06..14, the eleven single short
legs that gate refused were all priced above our claim and landed 63.6%.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from bayes_basket_target import delta_v2_runtime as runtime
from bayes_basket_target.basket_kernel import delta_v2 as kernel
from tests import test_charter_84_ladder_balance as fixture

ROOT = Path(__file__).resolve().parents[1]


def _reading(**overrides):
    kwargs = dict(
        held_shares_by_bucket={"18°C": 5.0},
        held_cost_by_bucket={"18°C": 1.65},
        buyable_ask_by_bucket={"19°C": 0.52},
        buyable_claim_by_bucket={"19°C": 0.505},
        hours_to_cutoff=26.0,
    )
    kwargs.update(overrides)
    return kernel.exposure_repair_completion_v1(**kwargs)


def test_the_kernel_admits_only_a_leg_the_market_took_with_time_and_under_a_share() -> None:
    # EHAM 2026-09-16 at 2026-09-14 20:28Z: 18C held 5 @ 0.33, 19C 0.52 vs 0.505.
    took = _reading()
    assert (took["admits"], took["reason"]) == (True, "market_took_the_leg")
    assert took["completed_cost_per_share"] == pytest.approx(0.85)
    # A leg the market prices at or under our claim is one it is handing us.
    handed = _reading(buyable_ask_by_bucket={"19°C": 0.50})
    assert (handed["admits"], handed["reason"]) == (False, "market_did_not_take_every_leg")
    # Every short leg, not the basket's sum.
    mixed = _reading(
        buyable_ask_by_bucket={"19°C": 0.52, "20°C": 0.05},
        buyable_claim_by_bucket={"19°C": 0.505, "20°C": 0.08},
    )
    assert mixed["admits"] is False
    # Charter 110(六): not inside the cutoff; no clock is not a cutoff.
    assert _reading(hours_to_cutoff=3.0)["reason"] == "inside_completion_cutoff"
    assert _reading(hours_to_cutoff=None)["admits"] is True
    # The one hard cap.
    dear = _reading(held_cost_by_bucket={"18°C": 2.45})
    assert (dear["admits"], dear["reason"]) == (False, "completed_basket_costs_a_share")
    assert _reading(buyable_ask_by_bucket={"19°C": None})["reason"] == "short_leg_unpriced"
    assert _reading(held_shares_by_bucket={})["reason"] == "not_a_partial_basket"


def _compile(ask_32: float, hours: float | None) -> dict:
    return fixture.compile_delta_v2_runtime_plan(
        snapshot=fixture._held_snapshot(ask_32=ask_32),
        basket_policy=fixture._basket_policy(),
        delta_policy=fixture._delta_policy(),
        fill_resolver=fixture._fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
        hours_to_cutoff=hours,
    )


def _short(result: dict) -> list[tuple[str, str]]:
    return [
        (row["bucket"], row["execution_candidate"])
        for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(runtime, "EXPOSURE_REPAIR_COMPLETION", "on")


def test_the_arm_ships_off_until_the_whole_trade_off_is_ruled() -> None:
    assert runtime.EXPOSURE_REPAIR_COMPLETION == "off"


@pytest.mark.parametrize("hours", [None, 10.0])
def test_a_completion_the_market_took_is_bought_despite_its_edge(armed, hours) -> None:
    # 30C and 31C held at five shares; 32C claims 0.20 and the market asks 0.24.
    result = _compile(0.24, hours)
    completion = result["basket_support_selection"]["seated"]["completion"]
    assert completion["admitted_by"] == kernel.EXPOSURE_REPAIR_COMPLETION_RULE_VERSION
    assert result["basket_target_activated"] is True
    # Admitted despite the increment's edge, which is negative after the lift.
    assert result["whole_basket_edge_after_lift"] < 0.0
    assert result["post_lift_edge_exempt_rule"] == kernel.EXPOSURE_REPAIR_COMPLETION_RULE_VERSION
    assert result["support_scenario_floor_after_lift"]["legal"] is True
    assert _short(result) == [("32", "TAKER_FOK")]


@pytest.mark.parametrize(
    "ask_32, hours",
    [(0.18, None), (0.24, 2.0)],
    ids=["market_did_not_take_it", "inside_the_cutoff"],
)
def test_a_completion_outside_the_rule_still_answers_to_the_hurdle(armed, ask_32, hours) -> None:
    result = _compile(ask_32, hours)
    assert result["basket_target_activated"] is False
    assert _short(result) == []
    assert result["post_lift_edge_exempt_rule"] is None


def test_a_completion_that_clears_the_hurdle_is_admitted_by_the_hurdle(armed) -> None:
    completion = _compile(0.15, None)["basket_support_selection"]["seated"]["completion"]
    assert completion["admitted_by"] == "hurdle"


def test_off_reproduces_the_charter_130_book(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "EXPOSURE_REPAIR_COMPLETION", "off")
    result = _compile(0.24, None)
    assert result["basket_target_activated"] is False
    assert _short(result) == []


def test_the_ledger_hashes_the_rule_and_reads_productions_switch() -> None:
    spec = importlib.util.spec_from_file_location(
        "ledger_136", ROOT / "scripts" / "paper_trading_ledger.py"
    )
    ledger = importlib.util.module_from_spec(spec)
    sys.modules["ledger_136"] = ledger
    spec.loader.exec_module(ledger)
    assert ledger._EXPOSURE_REPAIR_COMPLETION is runtime.EXPOSURE_REPAIR_COMPLETION
    assert ledger._SELECTION_RULE_IDENTITY["exposure_repair_completion"] == "off"
