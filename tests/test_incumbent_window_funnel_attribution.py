"""The incumbent window books its refusal under the gate that refused it.

The loop's branch has separated "the venue has no offer" from "the offer is
under the leg floor" since 2026-08-20, because a funnel that cannot tell the
two apart hands the reader a cause that did not produce the effect.  The
incumbent branch ran the same arithmetic and had only the one counter, so a
held chain resting on a penny leg was reported as a book with nothing to
sell -- and the incumbent branch is the one EVERY held chain takes, which is
the population a reader consults to ask why a chain stopped adding.

Audit only: the window is refused either way.
"""

from __future__ import annotations

from bayes_basket_target import delta_v2_runtime as P
from bayes_basket_target.basket_kernel.contracts import TargetBasketSnapshot
from bayes_basket_target.delta_v2_runtime import _support_windows

# Non-contiguous, so enumeration (contiguous runs only) never forms it
# and the appended-incumbent branch is the one that judges it -- the
# live shape is ZHHH's {31C, 33C or higher} with 32C alive between.
HELD = {"27": 5.0, "30": 5.0}


def _snapshot(asks: dict[str, float | None], probabilities: dict[str, float]):
    ordered = sorted(asks, key=lambda b: int(b))
    buckets = []
    for b in ordered:
        ask = asks[b]
        # A bucket with no ask still has a bid -- that is the shape the book
        # actually takes when nobody will sell (LFPB/CYYZ 2026-09: bids at
        # 0.99, ask side empty).  A bucket with neither side is a different
        # defect and the contract refuses it outright.
        bid = 0.99 if ask is None else max(0.001, round(ask - 0.001, 3))
        buckets.append({
            "bucket": b, "ordinal": int(b), "probability": probabilities[b],
            "bid_price": bid,
            "bid_levels": [{"price": bid, "shares": 50.0}],
            "ask_levels": [] if ask is None else [{"price": ask, "shares": 100.0}],
            "price_tick_size": 0.001, "maker_queue_ahead_shares": 2.0,
            "maker_queue_known": True,
            "filled_shares": HELD.get(b, 0.0),
            "filled_cost_usdc": HELD.get(b, 0.0) * 0.2,
        })
    return TargetBasketSnapshot.from_mapping({
        "schema": "target_basket_snapshot_v6", "snapshot_id": "incumbent-funnel",
        "sequence": 1, "observed_at": "2026-09-15T06:50:00Z", "market_id": "m",
        "station_id": "LFPB", "event_date": "2026-09-15", "quote_version": "q",
        "quote_observed_at": "2026-09-15T06:50:00Z",
        "quote_source": "OFFICIAL_CLOB_WS", "inventory_version": "i",
        "order_state_version": "o",
        "evidence": {
            "probability_stage": "P1", "posterior_version": "p1",
            "posterior_sha256": "a" * 64, "reported_high_bucket": "27",
            "reported_high_ordinal": 27, "reported_high_observation_id": "obs",
            "hard_bound_id": "hb", "reported_high_advanced": False,
        },
        "buckets": buckets,
        "checkpoint_changed": True, "quote_changed": True, "fill_changed": True,
        "time_gate_changed": True, "evidence_fresh": True,
        "contract_matches": True, "order_state_known": True,
        "risk_inputs_complete": True, "time_left_minutes": 300.0,
    })


def _audit(asks, probabilities, monkeypatch):
    monkeypatch.setattr(P, "CHARTER_87_GATE_EFFECTIVE_EDGE", "off")
    _windows, audit = _support_windows(
        snapshot=_snapshot(asks, probabilities), desired=set(HELD),
        probabilities=probabilities, fee_rate=0.0,
        maximum_support_buckets=4, minimum_support_buckets=2,
        center_bucket="28", probability_stage="P1",
    )
    return audit


def test_a_penny_leg_under_a_held_basket_is_the_floor_not_a_missing_offer(
    monkeypatch,
) -> None:
    """The venue IS selling -- at a price the floor refuses."""

    probabilities = {"27": 0.30, "28": 0.25, "29": 0.25, "30": 0.20}
    asks = {"27": 0.008, "28": 0.30, "29": 0.30, "30": 0.20}
    audit = _audit(asks, probabilities, monkeypatch)
    assert audit["leg_floor_refused_window_count"] >= 1, (
        "a held basket resting on a sub-floor leg is a floor refusal"
    )
    assert audit["unpriced_refused_window_count"] == 0, (
        "every leg has an offer; nothing here is unpriced"
    )
    assert audit["last_refusing_gate"] != "unpriced"


def test_a_leg_with_no_offer_at_all_is_still_unpriced(monkeypatch) -> None:
    """The other half of the separation: no ask means no ask."""

    probabilities = {"27": 0.30, "28": 0.25, "29": 0.25, "30": 0.20}
    asks = {"27": None, "28": 0.30, "29": 0.30, "30": 0.20}
    audit = _audit(asks, probabilities, monkeypatch)
    assert audit["unpriced_refused_window_count"] >= 1


def test_moving_the_count_between_gates_never_loses_it(monkeypatch) -> None:
    """Exactly one gate owns the refusal, and the total does not move.

    Also pins a gap this fix did NOT introduce and does not close: the
    appended incumbent is judged outside the enumeration, so when it is
    refused the gate counters exceed ``enumerated_window_count`` by exactly
    one and ``_window_audit_closes`` -- the invariant the refusal page
    rests on -- returns False.  It is true before and after this change,
    on every held chain whose support enumeration cannot form.  Recorded
    rather than papered over: closing it means deciding whether the
    appended window belongs in the enumeration count or outside the gate
    counters, and that is a reading, not a bug fix.
    """

    probabilities = {"27": 0.30, "28": 0.25, "29": 0.25, "30": 0.20}
    cases = {
        "floor": {"27": 0.008, "28": 0.30, "29": 0.30, "30": 0.20},
        "unpriced": {"27": None, "28": 0.30, "29": 0.30, "30": 0.20},
        "priced": {"27": 0.25, "28": 0.30, "29": 0.30, "30": 0.20},
    }
    totals = {}
    for name, asks in cases.items():
        audit = _audit(asks, probabilities, monkeypatch)
        refused = sum(
            int(audit.get("%s_refused_window_count" % gate) or 0)
            if gate != "gapped"
            else int(audit.get("gapped_window_refused_count") or 0)
            for gate in P.WINDOW_REFUSAL_FUNNEL
        )
        totals[name] = (
            refused + int(audit["shortlisted_window_count"] or 0)
            - int(audit["enumerated_window_count"] or 0)
        )
        # One gate, not two: the floor case must not also be unpriced.
        if name == "floor":
            assert audit["leg_floor_refused_window_count"] == 1
            assert audit["unpriced_refused_window_count"] == 0
        if name == "unpriced":
            assert audit["leg_floor_refused_window_count"] == 0
            assert audit["unpriced_refused_window_count"] == 1
    # The refused incumbent is one window outside the enumeration, whichever
    # gate owns it; an admissible one costs nothing.
    assert totals["floor"] == totals["unpriced"] == 1
    assert totals["priced"] == 0
    assert not P._window_audit_closes(
        _audit(cases["floor"], probabilities, monkeypatch)
    )
    assert P._window_audit_closes(
        _audit(cases["priced"], probabilities, monkeypatch)
    )
