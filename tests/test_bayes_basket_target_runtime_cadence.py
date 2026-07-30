from __future__ import annotations

from bayes_basket_target.cadence import CadenceDecision
from bayes_basket_target.runtime import evaluate_no_live_cadence


BASE = {
    "observed_at_utc": "2026-07-27T00:05:00Z",
    "order_state": "FLAT",
    "hazard_score": 0.67,
    "safety_gates_passed": False,
    "candidate_present": False,
    "checkpoint_version": "checkpoint-2",
    "quote_version": "quote-2",
    "fill_version": "fill-2",
    "time_gate_version": "time-2",
}
PREVIOUS = {
    **BASE,
    "observed_at_utc": "2026-07-27T00:00:00Z",
    "checkpoint_version": "checkpoint-1",
    "quote_version": "quote-1",
    "fill_version": "fill-1",
    "time_gate_version": "time-1",
}


def test_runtime_consumes_station_policy_gate_seconds_after_all_execution_gates() -> None:
    current = dict(BASE)
    before = dict(current)
    result = evaluate_no_live_cadence(
        cadence_theta={"gate_seconds_intercept": 300.0},
        current_snapshot=current,
        previous_snapshot=PREVIOUS,
        b13_b14_passed=True,
        budget_passed=True,
        order_state_known=True,
        candidate_present=True,
    )
    assert result == CadenceDecision.ALLOW_CANDIDATE.value
    assert current == before


def test_runtime_gates_cadence_after_b13_b14_budget_and_order_state() -> None:
    for gate in ("b13_b14_passed", "budget_passed", "order_state_known"):
        kwargs = {
            "b13_b14_passed": True,
            "budget_passed": True,
            "order_state_known": True,
            "candidate_present": True,
        }
        kwargs[gate] = False
        result = evaluate_no_live_cadence(
            cadence_theta={"gate_seconds_intercept": 300.0},
            current_snapshot=BASE,
            previous_snapshot=PREVIOUS,
            **kwargs,
        )
        assert result in {
            CadenceDecision.HOLD.value,
            CadenceDecision.REEVALUATE.value,
        }
        assert result != CadenceDecision.ALLOW_CANDIDATE.value


def test_runtime_missing_cadence_evidence_fails_closed_or_re_evaluates() -> None:
    assert evaluate_no_live_cadence(
        cadence_theta={"gate_seconds_intercept": 300.0},
        current_snapshot=None,
        previous_snapshot=PREVIOUS,
        b13_b14_passed=True,
        budget_passed=True,
        order_state_known=True,
        candidate_present=True,
    ) == CadenceDecision.HOLD.value
    assert evaluate_no_live_cadence(
        cadence_theta={"gate_seconds_intercept": 300.0},
        current_snapshot=BASE,
        previous_snapshot=None,
        b13_b14_passed=True,
        budget_passed=True,
        order_state_known=True,
        candidate_present=True,
    ) == CadenceDecision.REEVALUATE.value


def test_runtime_missing_artifact_key_never_invents_an_interval() -> None:
    assert evaluate_no_live_cadence(
        cadence_theta={},
        current_snapshot=BASE,
        previous_snapshot=PREVIOUS,
        b13_b14_passed=True,
        budget_passed=True,
        order_state_known=True,
        candidate_present=True,
    ) == CadenceDecision.HOLD.value

