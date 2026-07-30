from __future__ import annotations

from scripts.commit_bayes_basket_target_p1_no_live_replay import (
    _stable_decision_identity,
)


def _decision(state: str, *, idempotent: bool) -> dict:
    return {
        "schema": "bayes_basket_target_production_candidate_atomic_v1",
        "state": state,
        "candidate_will_order": False,
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "receipt": {
            "schema": "bayes_basket_target_atomic_ledger_v1",
            "state": state,
            "decision_id": "decision-stable",
            "cycle_id": "cycle-stable",
            "reservation_id": "reservation-stable",
            "idempotent": idempotent,
            "calls_order_gateway": False,
            "places_orders": False,
            "orders_submitted": 0,
        },
    }


def test_first_commit_and_replay_normalize_to_exact_same_decision() -> None:
    first = _stable_decision_identity(
        _decision("committed", idempotent=False)
    )
    replay = _stable_decision_identity(
        _decision("already_committed", idempotent=True)
    )

    assert first == replay
    assert first["state"] == "committed_idempotent"
    assert first["idempotent"] is True
    assert first["calls_order_gateway"] is False
    assert first["orders_submitted"] == 0
