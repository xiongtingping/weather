"""Charter 90: the TAF TX is the P0 basket's second confirmation at P0-M.

User rulings 2026-09-06.  The TAF is a point forecast, not a bucket
distribution, but it carries information the models do not have, so it
enters exactly once -- on the P0-M stage, as the basket-selection envelope --
and never touches the served vector.  If the basket P0 holds already contains
the TX bucket, the TAF changes nothing.  If it does not, the TX bucket must
enter the basket: the P0-M seat becomes the span from the centre to the TX,
keeps as many held rungs as the width cap allows, and P0-M may buy that leg
or open the day on it.
"""
from __future__ import annotations

import inspect

import pytest

from bayes_basket_target import delta_v2_runtime as P
from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot
from bayes_basket_target.delta_v2_runtime import _support_windows


def _snapshot(
    probabilities: tuple[float, ...],
    stage: str,
    held: dict[str, float] | None = None,
    ask: float = 0.10,
):
    held = held or {}
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "charter-90",
        "sequence": 1,
        "observed_at": "2026-09-06T03:00:00Z",
        "market_id": "charter-90-market",
        "station_id": "ZGSZ",
        "event_date": "2026-09-06",
        "quote_version": "quote-v1",
        "quote_observed_at": "2026-09-06T03:00:00Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-v1",
        "evidence": {
            "probability_stage": stage,
            "posterior_version": "p0m-v1",
            "posterior_sha256": "a" * 64,
        },
        "buckets": [
            {
                "bucket": str(26 + index),
                "ordinal": 26 + index,
                "probability": probability,
                "bid_price": round(ask - 0.01, 4),
                "bid_levels": [{"price": round(ask - 0.01, 4), "shares": 10.0}],
                "ask_levels": [{"price": ask, "shares": 100.0}],
                "price_tick_size": 0.01,
                "maker_queue_ahead_shares": 2.0,
                "maker_queue_known": True,
                "filled_shares": float(held.get(str(26 + index), 0.0)),
                "filled_cost_usdc": float(held.get(str(26 + index), 0.0)) * ask,
            }
            for index, probability in enumerate(probabilities)
        ],
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
    })


# Buckets 26..33; a symmetric day centred on 30 (mean == mode).
SYMMETRIC = (0.01, 0.03, 0.08, 0.18, 0.40, 0.18, 0.08, 0.04)
P0_BASKET = {"29": 5.0, "30": 5.0, "31": 5.0}


@pytest.fixture(autouse=True)
def _plain_edge_gate(monkeypatch):
    # These tests are about which windows may seat, not about the charter-87
    # lambda gate; the fixture book is cheap enough for the plain edge gate.
    monkeypatch.setattr(P, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    monkeypatch.setattr(P, "P0M_TAF_MODE", "tx_envelope")


def _seats(stage: str, tx_bucket: str | None, *, held=None, probabilities=SYMMETRIC):
    snapshot = _snapshot(probabilities, stage, held=held)
    by_bucket = {row.bucket: float(row.probability) for row in snapshot.buckets}
    windows, audit = _support_windows(
        snapshot=snapshot,
        desired={"29", "30", "31"},
        probabilities=by_bucket,
        fee_rate=0.0,
        maximum_support_buckets=4,
        minimum_support_buckets=3,
        center_bucket="30",
        probability_stage=stage,
        taf_tx_bucket=tx_bucket,
    )
    admissible = [
        tuple(row["buckets"]) for row in windows if row.get("admissible", True)
    ]
    return admissible, audit, windows


def test_the_shipped_mode_is_the_second_confirmation() -> None:
    assert P.P0M_TAF_MODE == "tx_envelope"
    assert P.P0M_TAF_ENVELOPE_RULE_VERSION == "p0m_taf_second_confirmation_v3"


def test_a_tx_inside_the_held_basket_confirms_it_and_constrains_nothing() -> None:
    """P0 holds 29/30/31, the TAF says 31: the standing rules seat as before."""

    with_taf, audit, _ = _seats("P0-M", "31", held=P0_BASKET)
    without, _, _ = _seats("P0-M", None, held=P0_BASKET)
    assert with_taf == without == [("29", "30", "31")]
    assert audit["p0m_taf"]["confirmed"] is True
    assert audit["p0m_taf"]["entry_required"] is False
    assert audit["p0m_taf"]["held_buckets"] == ["29", "30", "31"]


def test_a_tx_outside_the_held_basket_must_enter_it() -> None:
    """P0 holds 29/30/31, the TAF says 28: the seat is 28..31, nothing dropped."""

    seats, audit, windows = _seats("P0-M", "28", held=P0_BASKET)
    assert seats == [("28", "29", "30", "31")]
    assert audit["p0m_taf"]["entry_required"] is True
    assert audit["p0m_taf"]["entry_window"] == ["28", "29", "30", "31"]
    assert audit["p0m_taf"]["tx_unreachable"] is False
    # Charter 131: the entry span is the one shape the day may seat.
    assert audit["seat_shape"]["rule"] == "seat_shape_mean_anchored_v1"
    entry_rows = [row for row in windows if row.get("taf_tx_entry")]
    assert [tuple(row["buckets"]) for row in entry_rows] == [("28", "29", "30", "31")]


def test_the_entry_seat_keeps_the_held_rungs_the_cap_allows() -> None:
    """Held 30/31/32, TAF 28, centre 30: 28..30 plus the held 31 -- four rungs."""

    seats, audit, _ = _seats("P0-M", "28", held={"30": 5.0, "31": 5.0, "32": 5.0})
    assert seats == [("28", "29", "30", "31")]
    assert audit["p0m_taf"]["entry_window"] == ["28", "29", "30", "31"]


def test_on_an_empty_chain_the_taf_constrains_nothing_and_p0m_does_not_open() -> None:
    """Charter 131 (user, 2026-09-12 evening): P0-M does not open a chain,
    there must be a P0.  Nothing held means no basket for the TX to confirm
    or enter: the day seats its ordinary shape and charter 74 holds it."""

    for tx in ("28", "31"):
        seats, audit, windows = _seats("P0-M", tx)
        assert seats == [("29", "30", "31")]
        assert audit["p0m_taf"]["held_buckets"] == []
        assert audit["p0m_taf"]["no_p0_basket"] is True
        assert audit["p0m_taf"]["entry_required"] is False
        assert audit["p0m_taf"]["confirmed"] is False
        assert audit["p0m_taf"]["entry_window"] is None
        assert not any(row.get("taf_tx_entry") for row in windows)


def test_a_tx_the_cap_cannot_reach_from_the_centre_leaves_no_seat() -> None:
    """Model at 30, TAF at 25: six rungs to hold both -- the stage holds."""

    probabilities = (0.005, 0.01, 0.03, 0.08, 0.18, 0.40, 0.18, 0.115)  # 26..33, centre 31
    snapshot = _snapshot(probabilities, "P0-M", held=P0_BASKET)
    by_bucket = {row.bucket: float(row.probability) for row in snapshot.buckets}
    windows, audit = _support_windows(
        snapshot=snapshot, desired={"30", "31", "32"}, probabilities=by_bucket,
        fee_rate=0.0, maximum_support_buckets=4, minimum_support_buckets=3,
        center_bucket="31", probability_stage="P0-M", taf_tx_bucket="26",
    )
    assert [r for r in windows if r.get("admissible", True)] == []
    assert audit["p0m_taf"]["tx_unreachable"] is True
    assert audit["p0m_taf"]["entry_window"] is None


def test_the_rule_is_p0m_only() -> None:
    p0_seats, p0_audit = _seats("P0", "28", held=P0_BASKET)[:2]
    assert p0_seats == [("29", "30", "31")]
    assert p0_audit["p0m_taf"] == {}


def test_off_is_the_pre_ruling_arm(monkeypatch) -> None:
    monkeypatch.setattr(P, "P0M_TAF_MODE", "off")
    seats, audit, _ = _seats("P0-M", "28", held=P0_BASKET)
    assert seats == [("29", "30", "31")]
    assert audit["p0m_taf"] == {}


def test_the_solver_treats_the_entry_as_a_decision_moment_and_lets_the_leg_through() -> None:
    """The wiring the ruling needs: a decision moment of its own and gate
    71(a) yielding to the entry leg.  Charter 131 (2026-09-12 evening)
    retired the third wire -- P0-M opening the day on the TX -- so the
    empty-chain refusal has no TAF exemption left anywhere."""

    source = inspect.getsource(P._solve_over_support_windows)
    assert 'get("p0m_taf") or {}).get("entry_required")' in source
    assert "or taf_tx_entry or (" in source
    assert 'and not bool(row.get("taf_tx_entry"))' in source
    candidates = inspect.getsource(P._target_candidates)
    assert "taf_opening" not in candidates
    assert "later_stage_on_empty_chain = (" in candidates
    assert "and not later_stage_on_empty_chain" in candidates
    plan = inspect.getsource(P.compile_delta_v2_runtime_plan)
    assert "chain_not_opened_by_p0" in plan
    assert 'get("p0m_taf") or {}).get("entry_required")' not in plan


def test_the_ledger_hashes_mode_and_rule_unconditionally() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "paper_trading_ledger.py"
    ).read_text(encoding="utf-8")
    assert '"p0m_taf": P0M_TAF_MODE,' in source
    assert '"p0m_taf_rule": P0M_TAF_ENVELOPE_RULE_VERSION,' in source
    assert 'if P0M_TAF_MODE != "off"' not in source


# --- Charter 145 (2026-09-17, user "打通p0m的taf动态调仓"): the entry span is
# bought through charter 143's completion channel, on the structure it
# completes, never on the model's claim for the TX rung.  Under v2 the span
# had to clear the whole-window gate on that claim, and from charter 131 to
# this ruling every held-basket entry the book computed was refused there.

from bayes_basket_target.basket_kernel import delta_v2 as kernel  # noqa: E402
from tests import test_charter_84_ladder_balance as fixture  # noqa: E402
from tests.charter_143_switch import set_charter_143  # noqa: E402

RULE_143 = kernel.COMPLETION_143_RULE_VERSION


def _book(price: float, shares: float = 100.0):
    return ({"price": price, "shares": shares},)


def _entry_targets(**overrides):
    """ZBAA 2026-09-18's shape: 30 held at 2.15 for five, TAF says 31 which
    the book offers at 0.60 against a 0.20 claim, 29 pads the span."""

    kwargs = dict(
        held_by_bucket={"30": 5.0},
        support=("29", "30", "31"),
        held_cost_by_bucket={"30": 2.15},
        depth_by_bucket={"29": _book(0.20), "30": _book(0.45), "31": _book(0.60)},
        capital_available_usdc=10.0,
        claim_by_bucket={"29": 0.20, "30": 0.50, "31": 0.20},
        hours_to_cutoff=27.0,
        taf_entry=True,
    )
    kwargs.update(overrides)
    return kernel.completion_143_targets_v1(**kwargs)


def test_145_the_entry_span_is_completed_by_structure_whatever_it_holds() -> None:
    reading = _entry_targets()
    assert reading["refused"] is None
    assert reading["taf_entry"] is True
    assert reading["basket_type"] == "multi"
    assert reading["level_rule"] == "taf_entry_span_to_held_level"
    assert reading["buy_buckets"] == ["29", "31"]
    assert reading["targets"] == {"29": 5.0, "30": 5.0, "31": 5.0}
    assert reading["floor_before"] == pytest.approx(-2.15)
    assert reading["floor_after"] == pytest.approx(-1.15)
    # The same window read as an ordinary single-leg basket: the TX rung
    # fails the hurdle on the model's claim, which is what v2 asked of it.
    plain = _entry_targets(taf_entry=False)
    assert plain["refused"] == "no_leg_clears_hurdle"
    assert plain["taf_entry"] is False


def test_145_a_span_holding_no_rung_is_levelled_to_the_chain() -> None:
    """RKPK 2026-09-18: 27/28 held, the model's centre at 24, TAF 25 -- the
    span 24..25 touches nothing held.  Its level is the chain's cheapest."""

    kwargs = dict(
        held_by_bucket={"24": 0.0, "25": 0.0},
        support=("24", "25"),
        held_cost_by_bucket={},
        depth_by_bucket={"24": _book(0.31), "25": _book(0.35)},
        claim_by_bucket={"24": 0.28, "25": 0.18},
    )
    reading = _entry_targets(chain_level_shares=5.0, **kwargs)
    assert reading["refused"] is None
    assert reading["buy_buckets"] == ["24", "25"]
    assert reading["targets"] == {"24": 5.0, "25": 5.0}
    assert reading["floor_after"] == pytest.approx(5.0 - 3.30)
    assert _entry_targets(**kwargs)["refused"] == "no_held_level"
    # A span whose asks sum past one is a loss wherever the day lands.
    dear = _entry_targets(
        chain_level_shares=5.0,
        **dict(kwargs, depth_by_bucket={"24": _book(0.55), "25": _book(0.55)}),
    )
    assert dear["refused"] == "worst_not_improved"


def test_145_the_entry_is_a_must_its_size_is_not() -> None:
    """A level the capital cannot carry steps down a share at a time, to the
    venue lot and no further; an ordinary completion is still all-or-none."""

    fitted = _entry_targets(capital_available_usdc=3.0)
    assert fitted["refused"] is None
    assert fitted["level_rule"] == "taf_entry_span_to_held_level_fit_capital"
    assert fitted["level_shares"] == 3.0
    assert fitted["top_level_shares"] == 5.0
    assert fitted["targets"] == {"29": 3.0, "30": 5.0, "31": 3.0}
    assert fitted["spend_usdc"] == pytest.approx(2.40)
    lot = _entry_targets(capital_available_usdc=3.0, minimum_level_shares=5.0)
    assert lot["refused"] == "capital"
    assert lot["level_shares"] == 5.0
    ordinary = kernel.completion_143_targets_v1(
        held_by_bucket={"30": 5.0, "31": 5.0}, support=("30", "31", "32"),
        held_cost_by_bucket={"30": 1.25, "31": 2.15},
        depth_by_bucket={"30": _book(0.20), "31": _book(0.45), "32": _book(0.24)},
        capital_available_usdc=1.0, hours_to_cutoff=20.0,
    )
    assert ordinary["refused"] == "capital"


def test_145_the_exemption_reads_the_span_as_multi_leg() -> None:
    kwargs = dict(
        support=("29", "30", "31"),
        held_shares={"30": 5.0}, held_cost_usdc={"30": 2.15},
        target_shares={"29": 5.0, "30": 5.0, "31": 5.0},
        target_cost_usdc={"29": 1.0, "30": 2.15, "31": 3.0},
        claim_by_bucket={"29": 0.20, "31": 0.20},
        hours_to_cutoff=27.0,
    )
    entry = kernel.completion_143_exemption_v1(taf_entry=True, **kwargs)
    assert entry["exempt"] is True
    assert entry["basket_type"] == "multi"
    assert entry["taf_entry"] is True
    plain = kernel.completion_143_exemption_v1(**kwargs)
    assert plain["reason"] == "leg_below_hurdle"
    assert plain["taf_entry"] is False
    # The structure still binds: a span that leaves the window worse is out.
    worse = kernel.completion_143_exemption_v1(
        taf_entry=True,
        **dict(kwargs, target_cost_usdc={"29": 1.0, "30": 2.15, "31": 5.2}),
    )
    assert worse["reason"] == "worst_not_improved"


def _p0m_entry_snapshot(*, ask_31: float = 0.60):
    """The ZBAA shape as a P0-M snapshot: 30 held, TAF 31 dear, 29 pads."""
    from dataclasses import replace

    snapshot = fixture._partly_held(
        rungs=[("28", 28, 0.05, 0.08), ("29", 29, 0.20, 0.20), ("30", 30, 0.50, 0.45),
               ("31", 31, 0.20, ask_31), ("32", 32, 0.05, 0.08)],
        held={"30": 5.0}, cost={"30": 2.15},
    )
    return replace(
        snapshot, evidence=replace(snapshot.evidence, probability_stage="P0-M"),
    )


def test_145_the_span_is_admitted_by_charter_143_not_by_the_whole_window() -> None:
    snapshot = _p0m_entry_snapshot()
    by_bucket = {row.bucket: float(row.probability) for row in snapshot.buckets}
    windows, audit = _support_windows(
        snapshot=snapshot, desired={"29", "30", "31"}, probabilities=by_bucket,
        fee_rate=0.0, maximum_support_buckets=4, minimum_support_buckets=3,
        center_bucket="30", probability_stage="P0-M", taf_tx_bucket="31",
        hours_to_cutoff=27.0, completion_budget_usdc=10.0,
    )
    assert audit["p0m_taf"]["entry_required"] is True
    assert audit["p0m_taf"]["entry_window"] == ["29", "30", "31"]
    assert audit["p0m_taf"]["seat_holds_tx"] is True
    assert audit["p0m_taf"]["tx_unreachable_reason"] is None
    entry = [row for row in windows if row.get("taf_tx_entry")]
    assert [tuple(row["buckets"]) for row in entry] == [("29", "30", "31")]
    completion = entry[0]["completion"]
    assert entry[0]["admissible"] is True
    assert completion["admitted_by"] == RULE_143
    assert completion["taf_tx_entry"] is True
    # The whole-window gate on the model's claim (0.90 against 1.25) is
    # recorded, and no longer decides.
    assert completion["whole_window_admissible"] is False
    assert completion["completion_143"]["taf_entry"] is True
    assert completion["completion_143"]["basket_type"] == "multi"


def test_145_with_charter_143_off_the_span_answers_to_the_whole_window(monkeypatch) -> None:
    set_charter_143(monkeypatch, "off")
    snapshot = _p0m_entry_snapshot()
    by_bucket = {row.bucket: float(row.probability) for row in snapshot.buckets}
    windows, audit = _support_windows(
        snapshot=snapshot, desired={"29", "30", "31"}, probabilities=by_bucket,
        fee_rate=0.0, maximum_support_buckets=4, minimum_support_buckets=3,
        center_bucket="30", probability_stage="P0-M", taf_tx_bucket="31",
        hours_to_cutoff=27.0, completion_budget_usdc=10.0,
    )
    assert audit["p0m_taf"]["entry_required"] is True
    assert audit["p0m_taf"]["seat_holds_tx"] is False
    assert not any(row.get("taf_tx_entry") for row in windows)


def test_145_the_solver_offers_the_entry_at_the_held_level_and_seats_it() -> None:
    snapshot = _p0m_entry_snapshot()
    rows = runtime_candidates = P._target_candidates(
        snapshot=snapshot, desired=("29", "30", "31"),
        artifact=fixture._delta_policy(), capital_available_usdc=10.0,
        minimum_clip_notional_usdc=0.01,
        bankroll_usdc=fixture._basket_policy().total_budget_usdc,
        probabilities_by_bucket={r.bucket: r.probability for r in snapshot.buckets},
        book_equity_usdc=514.29, taf_tx_entry=True, completion_143_admitted=True,
        taf_entry_level_shares=5.0, taf_entry_minimum_level_shares=5.0,
        hours_to_cutoff=27.0,
    )
    del runtime_candidates
    balance = [r for r in rows if r["candidate_origin"] == kernel.LADDER_BALANCE_CANDIDATE_ORIGIN]
    assert len(balance) == 1
    assert balance[0]["completion_143"]["exempt"] is True
    assert balance[0]["completion_143"]["taf_entry"] is True
    assert {b: balance[0]["shares_by_bucket"][b] for b in ("29", "30", "31")} == {
        "29": 5.0, "30": 5.0, "31": 5.0,
    }
    # Without the entry flag the same window is a single-leg basket whose
    # missing rungs fail the hurdle: no completion is offered.
    plain = P._target_candidates(
        snapshot=snapshot, desired=("29", "30", "31"),
        artifact=fixture._delta_policy(), capital_available_usdc=10.0,
        minimum_clip_notional_usdc=0.01,
        bankroll_usdc=fixture._basket_policy().total_budget_usdc,
        probabilities_by_bucket={r.bucket: r.probability for r in snapshot.buckets},
        book_equity_usdc=514.29, hours_to_cutoff=27.0,
    )
    assert [r for r in plain if r["candidate_origin"] == kernel.LADDER_BALANCE_CANDIDATE_ORIGIN] == []
    solved = kernel.solve_target_inventory_v2(
        probabilities={r.bucket: r.probability for r in snapshot.buckets},
        feasible_inventory_candidates=rows,
        protected_buckets=("29", "30", "31"),
        shortfall_buckets=(), breakthrough_buckets=(),
        weights=fixture._delta_policy().value_weights,
        objective_form=kernel.KELLY_OBJECTIVE_FORM, bankroll_usdc=514.29,
    )
    assert solved["winner_candidate_origin"] == kernel.LADDER_BALANCE_CANDIDATE_ORIGIN
    assert solved["winner_completion_rule"] == RULE_143


def _p0m_delta_policy():
    from bayes_basket_target.basket_kernel.contracts import content_sha256
    from bayes_basket_target.delta_v2_artifact import DeltaV2PolicyArtifact

    policy = fixture._delta_policy()
    raw = {k: v for k, v in policy.to_dict().items() if k != "content_sha256"}
    raw["stage"] = "P0-M"
    raw["content_sha256"] = content_sha256(raw)
    return DeltaV2PolicyArtifact.from_mapping(raw)


def _compile_entry(snapshot, *, taf_tx_bucket="31", hours=27.0):
    return fixture.compile_delta_v2_runtime_plan(
        snapshot=snapshot, basket_policy=fixture._basket_policy(),
        delta_policy=_p0m_delta_policy(), fill_resolver=fixture._fill_resolver,
        capital_available_usdc=10.0, minimum_profit_floor=-5.0,
        resource_probe_ok=True, book_equity_usdc=514.29,
        taf_tx_bucket=taf_tx_bucket, hours_to_cutoff=hours,
        completion_budget_usdc=10.0,
    )


def _bought(result: dict) -> list[tuple[str, str]]:
    return [
        (row["bucket"], row["execution_candidate"])
        for row in result["targets"]
        if float(row.get("delta_shares") or 0.0) > 0.0
    ]


def test_145_end_to_end_the_tx_leg_is_bought_as_the_fok_with_the_waiver(monkeypatch) -> None:
    fixture._production_arms(monkeypatch)
    result = _compile_entry(_p0m_entry_snapshot())
    seat = result["basket_support_selection"]["seated"]
    # The fixture policy's seating floor is one rung, so the span is not
    # padded: the seat is the held 30 and the TX 31 -- and 31 alone is bought.
    assert seat["buckets"] == ["30", "31"]
    assert seat["completion"]["taf_tx_entry"] is True
    assert seat["completion"]["admitted_by"] == RULE_143
    assert seat["completion"]["whole_window_admissible"] is False
    assert result["basket_support_selection"]["p0m_taf"]["seat_holds_tx"] is True
    assert result["basket_target_activated"] is True
    assert _bought(result) == [("31", "TAKER_FOK")]
    assert result["completion_143_after_lift"]["taf_entry"] is True
    assert result["completion_143_after_lift"]["exempt"] is True
    assert result["completion_repair"]["exempt"] is True
    assert result["post_lift_edge_exempt_rule"] == RULE_143
    # Inside the completion cutoff the entry waits, like any completion.
    late = _compile_entry(_p0m_entry_snapshot(), hours=2.0)
    assert late["basket_target_activated"] is False
    assert _bought(late) == []


def test_145_end_to_end_with_charter_143_off_the_entry_holds_as_before(monkeypatch) -> None:
    fixture._production_arms(monkeypatch)
    set_charter_143(monkeypatch, "off")
    result = _compile_entry(_p0m_entry_snapshot())
    assert result["basket_target_activated"] is False
    assert _bought(result) == []
    assert result["basket_support_selection"]["p0m_taf"]["seat_holds_tx"] is False


def test_145_the_ledger_hands_its_walk_the_entry_flag() -> None:
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "ledger_145", root / "scripts" / "paper_trading_ledger.py"
    )
    ledger = importlib.util.module_from_spec(spec)
    sys.modules["ledger_145"] = ledger
    spec.loader.exec_module(ledger)
    assert ledger._SELECTION_RULE_IDENTITY["p0m_taf_rule"] == "p0m_taf_second_confirmation_v3"
    seat = {"buckets": ["29", "30", "31"], "completion": {"market_dead_buckets": []}}
    verdict = {"winner_completion_rule": RULE_143, "edge_probabilities": {"29": 0.2, "31": 0.2},
               "hours_to_cutoff": 27.0, "completion_143_taf_entry": True}
    kwargs = dict(seat=seat, held={"30": 5.0}, held_cost={"30": 2.15},
                  leg_shares=[5.0, 0.0, 5.0], leg_cash=[1.0, 0.0, 3.0])
    assert ledger._completion_143_exempt_on_legs(verdict=verdict, **kwargs) is True
    assert ledger._completion_143_exempt_on_legs(
        verdict=dict(verdict, completion_143_taf_entry=False), **kwargs
    ) is False
