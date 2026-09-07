"""Charter 91: the leg floor falls with our belief in the rung.

User ruling 2026-09-06: "腿地板 0.03 不是固定不变，随着概率调整而调整".  The
2026-08-24 floor was measured on tail rungs -- cheap asks where our own claim
was small too.  ZGSZ 2026-09-06 held the day's high at 27°C with a 61.5% P1
belief and a 2.3-cent ask, and the fixed floor refused the one rung the
observations had confirmed.
"""
from __future__ import annotations

import pytest

from bayes_basket_target import delta_v2_runtime as P
from bayes_basket_target.basket_kernel import delta_v2 as K
from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot
from bayes_basket_target.delta_v2_runtime import _support_windows


def test_the_floor_is_whole_for_tail_claims_and_falls_above_the_anchor() -> None:
    assert K.LEG_FLOOR_RULE_VERSION == "belief_scaled_v1"
    assert K.LEG_FLOOR_BELIEF_ANCHOR == 0.30
    assert K.leg_floor_all_in_ask(0.03, None) == 0.03, "no belief, no relaxation"
    assert K.leg_floor_all_in_ask(0.03, 0.0) == 0.03
    assert K.leg_floor_all_in_ask(0.03, 0.05) == 0.03
    assert K.leg_floor_all_in_ask(0.03, 0.30) == 0.03
    assert K.leg_floor_all_in_ask(0.03, 0.45) == pytest.approx(0.02)
    assert K.leg_floor_all_in_ask(0.03, 0.615) == pytest.approx(0.014634, rel=1e-4)
    assert K.leg_floor_all_in_ask(0.03, 0.90) == pytest.approx(0.01)
    # The ZGSZ rung: 2.3 cents against 61.5% clears; the same ask at 5% does not.
    assert K.leg_below_floor(0.023, 0.615, 0.03) is False
    assert K.leg_below_floor(0.023, 0.05, 0.03) is True
    assert K.leg_below_floor(None, 0.05, 0.03) is False


def _snapshot(asks: dict[str, float], probabilities: dict[str, float]):
    ordered = sorted(asks, key=lambda b: int(b))
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6", "snapshot_id": "charter-91", "sequence": 1,
        "observed_at": "2026-09-06T06:50:00Z", "market_id": "m", "station_id": "ZGSZ",
        "event_date": "2026-09-06", "quote_version": "q", "quote_observed_at": "2026-09-06T06:50:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "inventory_version": "i", "order_state_version": "o",
        "evidence": {
            "probability_stage": "P1", "posterior_version": "p1", "posterior_sha256": "a" * 64,
            "reported_high_bucket": "27", "reported_high_ordinal": 27,
            "reported_high_observation_id": "obs", "hard_bound_id": "hb", "reported_high_advanced": False,
        },
        "buckets": [
            {
                "bucket": b, "ordinal": int(b), "probability": probabilities[b],
                "bid_price": max(0.001, round(asks[b] - 0.001, 3)),
                "bid_levels": [{"price": max(0.001, round(asks[b] - 0.001, 3)), "shares": 10.0}],
                "ask_levels": [{"price": asks[b], "shares": 100.0}],
                "price_tick_size": 0.001, "maker_queue_ahead_shares": 2.0, "maker_queue_known": True,
            }
            for b in ordered
        ],
        "checkpoint_changed": True, "quote_changed": True, "fill_changed": True,
        "time_gate_changed": True, "evidence_fresh": True, "contract_matches": True,
        "order_state_known": True, "risk_inputs_complete": True, "time_left_minutes": 540.0,
    })


def test_the_confirmed_high_at_two_cents_is_admitted_while_a_tail_at_two_cents_is_not(monkeypatch) -> None:
    """ZGSZ 2026-09-06 at 14:50 local, hard bound at 27: the market asks 2.3
    cents for 27°C against a 61.5% belief; 33°C asks 0.2 cents against 0.7%."""

    monkeypatch.setattr(P, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    asks = {"27": 0.023, "28": 0.283, "29": 0.61, "30": 0.18, "31": 0.04, "32": 0.023, "33": 0.002}
    probabilities = {"27": 0.615, "28": 0.002, "29": 0.014, "30": 0.109, "31": 0.180, "32": 0.073, "33": 0.007}
    snapshot = _snapshot(asks, probabilities)
    windows, audit = _support_windows(
        snapshot=snapshot, desired={"28"}, probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=3, center_bucket="28",
        probability_stage="P1",
    )
    assert "27" not in audit["leg_floor_excluded_buckets"], "61.5% at 2.3 cents is not a lottery"
    assert "33" in audit["leg_floor_excluded_buckets"], "0.7% at 0.2 cents still is"
    assert "32" in audit["leg_floor_excluded_buckets"], "7.3% at 2.3 cents still is: below the anchor"
    assert audit["leg_floor_rule"] == "belief_scaled_v1"
    assert audit["leg_floor_belief_anchor"] == 0.30
    # What still refuses {27, 28} on this day is not the floor: the bimodal
    # shape (mode 27, mean 28.5) reads as right-skewed, and charter 55 forbids
    # extending from the centre toward the cold side.  Named here so the next
    # blocker is on record rather than mistaken for the floor.
    assert audit["leg_floor_refused_window_count"] == 0
    assert audit["skew_refused_window_count"] == 1
    assert audit["skew_extension_sign"] == 1


def test_the_ledger_reads_the_kernels_floor_and_hashes_the_rule() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    import paper_trading_ledger as L  # noqa: PLC0415

    # Charter 103 moved the read point: both books ask the kernel the whole
    # question -- floor and the side of the book it is measured on -- rather
    # than each computing the comparison against the scaled floor itself.
    assert L.leg_below_floor is K.leg_below_floor
    assert L.LEG_FLOOR_BELIEF_ANCHOR == K.LEG_FLOOR_BELIEF_ANCHOR
    assert L.LEG_FLOOR_RULE_VERSION == K.LEG_FLOOR_RULE_VERSION
    assert L.LEG_FLOOR_PRICE_BASIS == K.LEG_FLOOR_PRICE_BASIS
    source = (root / "scripts" / "paper_trading_ledger.py").read_text(encoding="utf-8")
    assert '"leg_floor_rule": LEG_FLOOR_RULE_VERSION,' in source
    assert '"leg_floor_belief_anchor": LEG_FLOOR_BELIEF_ANCHOR,' in source
    assert '"leg_floor_price_basis": LEG_FLOOR_PRICE_BASIS,' in source
    runtime = (root / "bayes_basket_target" / "delta_v2_runtime.py").read_text(encoding="utf-8")
    # No production site compares a quote to the bare constant any more.
    assert "< MINIMUM_LEG_ALL_IN_ASK" not in runtime
    assert ">= MINIMUM_LEG_ALL_IN_ASK" not in runtime
    # And no site in either book reaches past the shared read point to the
    # scaled floor on its own -- that is how the two drifted onto different
    # sides of the book in the first place.
    assert "leg_floor_all_in_ask(" not in source
    assert "leg_floor_all_in_ask(" not in runtime
