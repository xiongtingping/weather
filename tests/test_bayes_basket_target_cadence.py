from __future__ import annotations

import math

import pytest

from bayes_basket_target.cadence import (
    CADENCE_GATE_SECONDS_KEY,
    CadenceDecision,
    CadenceSnapshot,
    CadenceThresholds,
    evaluate_cadence,
)


BASE = {
    "observed_at_utc": "2026-07-27T00:00:00Z",
    "order_state": "FLAT",
    "hazard_score": 0.67,
    "safety_gates_passed": True,
    "candidate_present": True,
    "checkpoint_version": "checkpoint-1",
    "quote_version": "quote-1",
    "fill_version": "fill-1",
    "time_gate_version": "time-1",
}
THRESHOLDS = {CADENCE_GATE_SECONDS_KEY: 300.0, "allow_candidate_hazard_threshold": 0.67}


def _snapshot(**overrides: object) -> CadenceSnapshot:
    raw = {**BASE, **overrides}
    value = CadenceSnapshot.from_mapping(raw)
    assert value is not None
    return value


def test_first_snapshot_requests_reevaluation_and_never_authorizes_order() -> None:
    result = evaluate_cadence(_snapshot(), thresholds=THRESHOLDS)
    assert result == CadenceDecision.REEVALUATE.value
    assert result in {"HOLD", "REEVALUATE", "ALLOW_CANDIDATE"}


def test_hazard_just_below_boundary_only_releases_reevaluation() -> None:
    current = _snapshot(hazard_score=0.669999)
    assert evaluate_cadence(current, thresholds=THRESHOLDS) == "REEVALUATE"


def test_repeated_snapshot_holds_until_inclusive_time_threshold() -> None:
    previous = _snapshot()
    same = _snapshot()
    assert evaluate_cadence(same, previous, THRESHOLDS) == "HOLD"

    at_boundary = _snapshot(observed_at_utc="2026-07-27T00:05:00Z")
    assert evaluate_cadence(at_boundary, previous, THRESHOLDS) == "ALLOW_CANDIDATE"


@pytest.mark.parametrize("field", ["quote_version", "fill_version", "checkpoint_version", "time_gate_version"])
def test_each_explicit_change_releases_cadence_immediately(field: str) -> None:
    previous = _snapshot()
    current = _snapshot(**{field: f"{field}-2"}, hazard_score=0.2)
    assert evaluate_cadence(current, previous, THRESHOLDS) == "REEVALUATE"


def test_forward_time_without_change_still_releases_reevaluation() -> None:
    previous = _snapshot()
    current = _snapshot(observed_at_utc="2026-07-27T00:05:01Z", hazard_score=0.2)
    assert evaluate_cadence(current, previous, THRESHOLDS) == "REEVALUATE"


def test_backward_time_is_fail_closed_even_when_quote_changed() -> None:
    previous = _snapshot()
    current = _snapshot(observed_at_utc="2026-07-26T23:59:59Z", quote_version="quote-2")
    assert evaluate_cadence(current, previous, THRESHOLDS) == "HOLD"


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_state": "UNKNOWN"},
        {"order_state": "RECONCILE_REQUIRED"},
        {"order_state": "BROKEN_STATE"},
        {"safety_gates_passed": False},
        {"hazard_score": math.nan},
        {"hazard_score": math.inf},
        {"quote_version": None},
        {"observed_at_utc": "2026-07-27T00:00:00"},
    ],
)
def test_invalid_or_unsafe_input_fails_closed(overrides: dict[str, object]) -> None:
    raw = {**BASE, **overrides}
    assert evaluate_cadence(raw, thresholds=THRESHOLDS) == "HOLD"


@pytest.mark.parametrize(
    "thresholds",
    [
        {CADENCE_GATE_SECONDS_KEY: 0.0, "allow_candidate_hazard_threshold": 0.5},
        {CADENCE_GATE_SECONDS_KEY: -1.0, "allow_candidate_hazard_threshold": 0.5},
        {CADENCE_GATE_SECONDS_KEY: math.nan, "allow_candidate_hazard_threshold": 0.5},
        {CADENCE_GATE_SECONDS_KEY: 1.0, "allow_candidate_hazard_threshold": -0.01},
        {CADENCE_GATE_SECONDS_KEY: 1.0, "allow_candidate_hazard_threshold": 1.01},
        {CADENCE_GATE_SECONDS_KEY: 1.0, "allow_candidate_hazard_threshold": math.inf},
        {CADENCE_GATE_SECONDS_KEY: 1.0},
        {"allow_candidate_hazard_threshold": 0.5},
    ],
)
def test_invalid_thresholds_fail_closed(thresholds: dict[str, object]) -> None:
    assert evaluate_cadence(_snapshot(), thresholds=thresholds) == "HOLD"


def test_known_open_order_can_be_rechecked_but_cannot_allow_candidate() -> None:
    previous = _snapshot(order_state="OPEN")
    current = _snapshot(order_state="OPEN", observed_at_utc="2026-07-27T00:05:00Z")
    assert evaluate_cadence(current, previous, THRESHOLDS) == "REEVALUATE"


def test_mapping_and_typed_inputs_are_equivalent_and_inputs_are_not_mutated() -> None:
    raw = dict(BASE)
    before = dict(raw)
    typed = _snapshot()
    assert evaluate_cadence(raw, thresholds=THRESHOLDS) == evaluate_cadence(typed, thresholds=CadenceThresholds.from_mapping(THRESHOLDS))
    assert raw == before


def test_station_policy_uses_gate_seconds_intercept_and_does_not_guess_missing_key() -> None:
    thresholds = CadenceThresholds.from_station_policy_theta(
        {CADENCE_GATE_SECONDS_KEY: 600.0}
    )
    assert thresholds is not None
    assert thresholds.gate_seconds_intercept == 600.0
    assert thresholds.reevaluate_after_seconds == 600.0
    assert CadenceThresholds.from_station_policy_theta({}) is None


def test_missing_prior_snapshot_is_explicit_reevaluation_not_allow() -> None:
    assert evaluate_cadence(_snapshot(), previous=None, thresholds=THRESHOLDS) == (
        CadenceDecision.REEVALUATE.value
    )
