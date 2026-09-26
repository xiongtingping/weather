"""Charter 131 (Q8): the leg floor is a flat three cents, read at the mid.

Charter 91 (2026-09-06) let the floor fall with belief -- 0.03 x min(1,
0.30/p) -- so ZGSZ 2026-09-06's confirmed 27°C at 2.3 cents could be bought.
User ruling 2026-09-12 ("0.03 地板：读中价") retired that: a rung the market
prices under three cents is not bought however much we believe in it, and
the reference price stays charter 103's mid.
"""
from __future__ import annotations

from bayes_basket_target import delta_v2_runtime as P
from bayes_basket_target.basket_kernel import delta_v2 as K
from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot
from bayes_basket_target.delta_v2_runtime import _support_windows


def test_the_floor_is_flat_whatever_the_belief() -> None:
    assert K.LEG_FLOOR_RULE_VERSION == "flat_mid_v2"
    assert not hasattr(K, "LEG_FLOOR_BELIEF_ANCHOR")
    for belief in (None, 0.0, 0.05, 0.30, 0.45, 0.615, 0.90):
        assert K.leg_floor_all_in_ask(0.03, belief) == 0.03
    # The ZGSZ rung: 2.3 cents is under the floor at 61.5% as at 5%.
    assert K.leg_below_floor(0.023, 0.615, 0.03) is True
    assert K.leg_below_floor(0.023, 0.05, 0.03) is True
    assert K.leg_below_floor(0.031, 0.05, 0.03) is False
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


def test_the_confirmed_high_at_two_cents_is_refused_like_any_tail(monkeypatch) -> None:
    """ZGSZ 2026-09-06 at 14:50 local: the market asks 2.3 cents for 27°C
    against a 61.5% belief.  Charter 91 admitted it; charter 131 does not."""

    asks = {"27": 0.023, "28": 0.283, "29": 0.61, "30": 0.18, "31": 0.04, "32": 0.023, "33": 0.002}
    probabilities = {"27": 0.615, "28": 0.002, "29": 0.014, "30": 0.109, "31": 0.180, "32": 0.073, "33": 0.007}
    snapshot = _snapshot(asks, probabilities)
    windows, audit = _support_windows(
        snapshot=snapshot, desired={"28"}, probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=3, center_bucket="28",
        probability_stage="P1",
    )
    assert "27" in audit["leg_floor_excluded_buckets"], "61.5% at 2.3 cents is under the flat floor"
    assert "33" in audit["leg_floor_excluded_buckets"]
    assert "32" in audit["leg_floor_excluded_buckets"]
    assert audit["leg_floor_rule"] == "flat_mid_v2"
    assert "leg_floor_belief_anchor" not in audit
    assert audit["leg_floor_refused_window_count"] == 0


def test_production_reads_the_kernels_floor() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    runtime = (root / "bayes_basket_target" / "delta_v2_runtime.py").read_text(encoding="utf-8")
    # No production site compares a quote to the bare constant any more.
    assert "< MINIMUM_LEG_ALL_IN_ASK" not in runtime
    assert ">= MINIMUM_LEG_ALL_IN_ASK" not in runtime
    assert "leg_floor_all_in_ask(" not in runtime


