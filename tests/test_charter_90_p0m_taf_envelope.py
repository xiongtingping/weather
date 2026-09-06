"""Charter 90: on the P0-M stage the seat holds the TAF TX bucket.

The TAF is not a probability update -- it never touches the served vector --
but it is part of the selection envelope the P0-M seat must cover (user
ruling 2026-09-06: "TAF 不和模型争权重，但可以在 P0-M 阶段进篮子选桶包络").
The charter-79 symmetric seat widens from centre+-1 to the contiguous span
that also holds the TX bucket, up to the width cap; a TX the cap cannot reach
leaves no admissible window and the stage holds.
"""
from __future__ import annotations

import pytest

from bayes_basket_target import delta_v2_runtime as P
from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot
from bayes_basket_target.delta_v2_runtime import _support_windows


def _snapshot(probabilities: tuple[float, ...], stage: str, ask: float = 0.10):
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


@pytest.fixture(autouse=True)
def _plain_edge_gate(monkeypatch):
    # These tests are about which windows may seat, not about the charter-87
    # lambda gate; the fixture book is cheap enough for the plain edge gate.
    monkeypatch.setattr(P, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")


def _seats(stage: str, tx_bucket: str | None, probabilities=SYMMETRIC):
    snapshot = _snapshot(probabilities, stage)
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
    return admissible, audit


def test_the_shipped_mode_is_the_envelope() -> None:
    assert P.P0M_TAF_MODE == "tx_envelope"
    assert P.P0M_TAF_ENVELOPE_RULE_VERSION == "p0m_seat_holds_taf_tx_union_v1"


def test_a_tx_inside_the_symmetric_seat_changes_nothing(monkeypatch) -> None:
    """The TAF confirming the model is the quiet case: centre+-1 stands."""

    monkeypatch.setattr(P, "P0M_TAF_MODE", "tx_envelope")
    seats, audit = _seats("P0-M", "31")
    assert seats == [("29", "30", "31")]
    assert audit["symmetric_seat_forced"] is True
    assert audit["p0m_taf"]["seat_expanded_to_tx"] is False
    assert audit["p0m_taf"]["seat_holds_tx"] is True


def test_a_tx_one_rung_past_the_seat_widens_it_to_four(monkeypatch) -> None:
    """Union only expands: centre+-1 plus the TX bucket, nothing dropped."""

    monkeypatch.setattr(P, "P0M_TAF_MODE", "tx_envelope")
    seats, audit = _seats("P0-M", "28")
    assert seats == [("28", "29", "30", "31")]
    assert audit["p0m_taf"]["seat_expanded_to_tx"] is True
    assert audit["p0m_taf"]["tx_unreachable"] is False
    assert audit["p0m_taf"]["seat_holds_tx"] is True


def test_a_tx_the_cap_cannot_reach_leaves_no_seat(monkeypatch) -> None:
    """Model at 30, TAF at 27: five rungs to hold both -- the stage holds."""

    monkeypatch.setattr(P, "P0M_TAF_MODE", "tx_envelope")
    seats, audit = _seats("P0-M", "27")
    assert seats == []
    assert audit["p0m_taf"]["tx_unreachable"] is True
    assert audit["p0m_taf"]["seat_holds_tx"] is False


def test_the_rule_is_p0m_only(monkeypatch) -> None:
    """P0 has no TAF yet and P1 reads observations; neither seat moves."""

    monkeypatch.setattr(P, "P0M_TAF_MODE", "tx_envelope")
    p0_seats, p0_audit = _seats("P0", "28")
    assert p0_seats == [("29", "30", "31")]
    assert p0_audit["p0m_taf"] == {}


def test_on_a_skewed_day_every_admissible_window_holds_the_tx(monkeypatch) -> None:
    """No fixed seat to widen: the windows that lack the TX bucket cannot seat."""

    monkeypatch.setattr(P, "P0M_TAF_MODE", "tx_envelope")
    # Right-skewed: mass leans hot of the centre bucket 30.
    skewed = (0.01, 0.02, 0.05, 0.12, 0.30, 0.25, 0.15, 0.10)
    seats, audit = _seats("P0-M", "32", probabilities=skewed)
    assert seats, "a window holding both the centre and the TX exists"
    assert all("32" in seat for seat in seats)
    assert all("30" in seat for seat in seats), "charter 49 still holds"
    assert audit["symmetric_seat_forced"] is False


def test_off_is_the_pre_ruling_arm(monkeypatch) -> None:
    monkeypatch.setattr(P, "P0M_TAF_MODE", "off")
    seats, audit = _seats("P0-M", "28")
    assert seats == [("29", "30", "31")]
    assert audit["p0m_taf"] == {}


def test_the_ledger_hashes_the_mode_unconditionally() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "paper_trading_ledger.py"
    ).read_text(encoding="utf-8")
    assert '"p0m_taf": P0M_TAF_MODE,' in source
    assert 'if P0M_TAF_MODE != "off"' not in source
