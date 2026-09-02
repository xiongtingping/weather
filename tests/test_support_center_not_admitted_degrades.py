"""An anchor the gates refuse degrades the incumbent; it no longer aborts.

Measured 2026-09-02 (see ``_choose_support``): the served P1 afternoon shape
-- 0.669 on the reported-high bucket, the point-estimate anchor one rung up,
no per-bucket model reliability anywhere -- made ``support_center_not_admitted``
a compile exception on ten to twenty-three station-days a day.
"""
from __future__ import annotations

from dataclasses import replace

from bayes_basket_target.basket_kernel.contracts import (
    TargetBasketPolicy,
    TargetBasketSnapshot,
)
from bayes_basket_target.basket_kernel.support import _choose_support


def _bucket(label: str, ordinal: int, probability: float, ask: float | None) -> dict[str, object]:
    return {
        "bucket": label,
        "ordinal": ordinal,
        "probability": probability,
        "bid_price": 0.001,
        "bid_levels": [{"price": 0.001, "shares": 10.0}],
        "ask_levels": [] if ask is None else [{"price": ask, "shares": 100.0}],
        "price_tick_size": 0.001,
        "maker_queue_ahead_shares": 0.0,
        "maker_queue_known": True,
    }


def _snapshot(*, anchor_ordinal: int) -> TargetBasketSnapshot:
    # FACT 2026-09-02 13:06Z as served: running high 19C, model centre 20C.
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "fact-snapshot",
        "sequence": 1,
        "observed_at": "2026-09-02T13:22:30Z",
        "market_id": "fact-market",
        "station_id": "FACT",
        "event_date": "2026-09-02",
        "quote_version": "quote-v1",
        "quote_observed_at": "2026-09-02T13:22:30Z",
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "inventory-v1",
        "order_state_version": "order-v1",
        "evidence": {
            "probability_stage": "P0",
            "posterior_version": "p0-v1",
            "posterior_sha256": "a" * 64,
            "model_center_ordinal": anchor_ordinal,
        },
        "buckets": [
            _bucket("18°C", 3, 0.0, 0.002),
            _bucket("19°C", 4, 0.669, None),
            _bucket("20°C", 5, 0.115, 0.03),
            _bucket("21°C", 6, 0.110, 0.01),
            _bucket("22°C", 7, 0.064, 0.002),
            _bucket("23°C", 8, 0.020, 0.002),
            _bucket("24°C", 9, 0.022, 0.002),
        ],
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 300.0,
    })


def _policy() -> TargetBasketPolicy:
    # Production's target is 0.55 + 0.20 x entropy, which on the measured
    # shape lands at 0.65-0.67 against a 0.669 top bucket -- the knife-edge
    # that decided EGLC one way and FACT the other.  Pin the target below the
    # top bucket so this test is about the anchor, not about the edge.
    return replace(
        TargetBasketPolicy(), base_coverage=0.6, entropy_coverage_gain=0.0
    )


def test_unadmitted_anchor_grows_the_incumbent_from_the_heaviest_bucket() -> None:
    center, _direction, base, _bias, _edge, desired, _scores, _lo, _hi, _layers, transition = (
        _choose_support(_snapshot(anchor_ordinal=5), _policy())
    )

    # The anchor is still reported as the stage's centre ...
    assert center.bucket == "20°C"
    # ... but the incumbent is the contiguous run around the heaviest
    # admitted bucket, and nothing the gates refused was let in.
    assert desired == {"19°C"}
    assert base == {"19°C"}
    assert transition["center_admitted"] is False
    assert transition["center_bucket"] == "20°C"
    assert transition["incumbent_anchor_bucket"] == "19°C"
    assert transition["center_not_admitted_reason"] == "anchor_outside_admitted_support"


def test_admitted_anchor_leaves_the_record_byte_identical() -> None:
    *_rest, transition = _choose_support(
        _snapshot(anchor_ordinal=4), _policy()
    )

    assert "center_admitted" not in transition
    assert "incumbent_anchor_bucket" not in transition
