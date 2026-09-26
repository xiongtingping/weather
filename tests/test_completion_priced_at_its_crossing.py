"""A completion that crosses is authorized at the price it crosses at.

Charter 127 (五) sends the designated completion as a limit FOK capped at the
cash its authorization was struck on.  The authorization is the completion
candidate's own cost, and ``_lane_purchase_levels`` prices every candidate at
P0-M on the maker resting band (``TARGET_PRICING_LANE_STAGES``) -- bid+1t
clamped under the ask, always at least a tick under the offer the FOK has to
take.  So at P0-M the walk always cost more than the authorization and every
completion was refused ``completion_take_above_authorized_cost``, with no
resting rung in its place.

Live case, LFPB 2026-09-14: 27C and 28C held at five shares, 29C short.  At
2026-09-13 09:47Z (P0-M) the book was 0.08/0.09 for 29C with 72.88 shares at
0.09; the candidate authorized five shares at the band (0.40) and the take
walked 0.45.  Fourteen compiles through 16:07Z and three more that evening
refused the same leg; the event-day high went through 29C.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from bayes_basket_target import delta_v2_runtime
from tests import test_charter_84_ladder_balance as fixture


def _compile(monkeypatch: pytest.MonkeyPatch, *, stage: str) -> dict:
    monkeypatch.setattr(
        delta_v2_runtime, "CONSOLIDATION_TILT_SIGNALS", ("stable", "sharp")
    )
    monkeypatch.setattr(delta_v2_runtime, "CHARTER_83_MIN_NOTIONAL", "off")
    if stage != "P0":
        # Charter 229 (2026-09-26): the basket buys in P0 only; a later
        # stage's completion is priced under the three-stage research arm.
        monkeypatch.setattr(
            delta_v2_runtime,
            "BASKET_BUYING_STAGES",
            frozenset({"P0", "P0-M", "P1"}),
        )
    solve = delta_v2_runtime.solve_reservation_price_v2
    monkeypatch.setattr(
        delta_v2_runtime,
        "solve_reservation_price_v2",
        lambda **kwargs: {**solve(**kwargs), "reservation_price": None},
    )
    # Charter 230: the whole window refused, the increment admitted.
    snapshot = fixture._held_snapshot(ask_31=0.60)
    snapshot = replace(
        snapshot, evidence=replace(snapshot.evidence, probability_stage=stage)
    )
    return fixture.compile_delta_v2_runtime_plan(
        snapshot=snapshot,
        basket_policy=fixture._basket_policy(),
        # Production's policies are stage "*" (AGENTS.md); the fixture's is P0.
        delta_policy=replace(fixture._delta_policy(), stage="*"),
        fill_resolver=fixture._fill_resolver,
        capital_available_usdc=10.0,
        minimum_profit_floor=-5.0,
        resource_probe_ok=True,
        book_equity_usdc=514.29,
    )


@pytest.mark.parametrize("stage", ["P0", "P0-M"])
def test_the_completion_crosses_at_every_stage_the_band_prices(
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    assert "P0-M" in delta_v2_runtime.TARGET_PRICING_LANE_STAGES
    assert delta_v2_runtime.CHARTER_127_COMPLETION_TAKE == "on"
    result = _compile(monkeypatch, stage=stage)

    assert result["target_solution"]["winner_candidate_origin"] == (
        fixture.LADDER_BALANCE_CANDIDATE_ORIGIN
    )
    diagnostics = result.get("maker_candidate_diagnostics") or {}
    assert diagnostics.get("32") != "completion_take_above_authorized_cost", (
        stage, diagnostics,
    )
    short = [
        row for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]
    assert [row["bucket"] for row in short] == ["32"], (stage, short)
    leg = short[0]
    assert leg["execution_candidate"] == "TAKER_FOK", (stage, leg)
    # 32C is 0.13/0.15 in the fixture: the band is 0.14, the take is 0.15.
    assert leg["candidate_price"] == pytest.approx(0.15)
    assert leg["clip_shares"] == pytest.approx(leg["delta_shares"])
