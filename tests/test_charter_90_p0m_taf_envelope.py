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
    assert P.P0M_TAF_ENVELOPE_RULE_VERSION == "p0m_taf_second_confirmation_v2"


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
    assert audit["symmetric_seat_forced"] is False, "the entry seat supersedes 79"
    entry_rows = [row for row in windows if row.get("taf_tx_entry")]
    assert [tuple(row["buckets"]) for row in entry_rows] == [("28", "29", "30", "31")]


def test_the_entry_seat_keeps_the_held_rungs_the_cap_allows() -> None:
    """Held 30/31/32, TAF 28, centre 30: 28..30 plus the held 31 -- four rungs."""

    seats, audit, _ = _seats("P0-M", "28", held={"30": 5.0, "31": 5.0, "32": 5.0})
    assert seats == [("28", "29", "30", "31")]
    assert audit["p0m_taf"]["entry_window"] == ["28", "29", "30", "31"]


def test_on_an_empty_chain_the_entry_seat_is_the_span_padded_to_the_floor() -> None:
    """Nothing held, TAF 28, centre 30: 28..30 is already three rungs."""

    seats, audit, _ = _seats("P0-M", "28")
    assert seats == [("28", "29", "30")]
    assert audit["p0m_taf"]["held_buckets"] == []
    assert audit["p0m_taf"]["entry_required"] is True
    # TAF 31 next to the centre: the two-rung span pads toward the heavier side.
    seats, audit, _ = _seats("P0-M", "31")
    assert seats == [("29", "30", "31")]


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
    """The wiring the ruling needs: a decision moment of its own, gate 71(a)
    yielding to the entry leg, and P0-M allowed to open the day on it."""

    source = inspect.getsource(P._solve_over_support_windows)
    assert 'get("p0m_taf") or {}).get("entry_required")' in source
    assert "or taf_tx_entry or (" in source
    assert 'and not bool(row.get("taf_tx_entry"))' in source
    candidates = inspect.getsource(P._target_candidates)
    assert 'taf_opening = bool(taf_tx_entry) and stage_name == "P0-M"' in candidates
    # The entry opens the day by exempting P0-M from the empty-chain refusal,
    # so the Kelly candidate is offered to it.  Until 2026-09-08 the same flag
    # also let the ticket open the day; the ticket is retired and this is the
    # one surviving path, which is what makes asserting it worth doing.
    assert "and not taf_opening" in candidates
    assert "later_stage_on_empty_chain = (" in candidates
    assert "and not later_stage_on_empty_chain" in candidates
    plan = inspect.getsource(P.compile_delta_v2_runtime_plan)
    assert 'get("p0m_taf") or {}).get("entry_required")' in plan


def test_the_ledger_hashes_mode_and_rule_unconditionally() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "paper_trading_ledger.py"
    ).read_text(encoding="utf-8")
    assert '"p0m_taf": P0M_TAF_MODE,' in source
    assert '"p0m_taf_rule": P0M_TAF_ENVELOPE_RULE_VERSION,' in source
    assert 'if P0M_TAF_MODE != "off"' not in source
