from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from bayes_basket_target.cash_phase import BasketCashPhasePolicyArtifact
from bayes_basket_target.cash_phase_oos import (
    REQUIRED_FAULT_CASES,
    build_cash_phase_oos_evidence,
    build_cash_phase_oos_scorecard,
)
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.promotion_evidence import (
    validate_cash_phase_forward_scorecard,
)
from bayes_basket_target.fleet import expected_station_count


AS_OF = "2026-07-12T00:00:00Z"
START = "2026-07-08T00:00:00Z"
END = "2026-07-11T00:00:00Z"


def _policy(station: str) -> BasketCashPhasePolicyArtifact:
    return BasketCashPhasePolicyArtifact.create(
        policy_version=f"cash-design-{station}-v1",
        station_id=station,
        station_timezone="UTC",
        heavy_to_standard_local_time="19:54",
        standard_to_tail_local_time="09:58",
        cash_by_phase={"HEAVY": 3.0, "STANDARD": 2.0, "TAIL": 1.0},
        valid_from_utc="2026-07-01T00:00:00Z",
        valid_until_utc="2026-08-01T00:00:00Z",
        evidence_status="DESIGN_CANDIDATE",
        promoted=False,
    )


def _fault_cases() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "passed": True,
            "evidence_owner": "bayes_basket_target",
            "evidence_sha256": f"{index:x}" * 64,
        }
        for index, name in enumerate(sorted(REQUIRED_FAULT_CASES), start=1)
    ]


def _row(
    station: str,
    phase: str,
    *,
    event_id: str | None = None,
    decision_at: str | None = None,
    observation_id: str | None = None,
) -> dict[str, object]:
    clocks = {
        "HEAVY": "2026-07-08T12:00:00Z",
        "STANDARD": "2026-07-09T20:00:00Z",
        "TAIL": "2026-07-10T10:00:00Z",
    }
    decision = decision_at or clocks[phase]
    identity = event_id or f"{station}-{phase}"
    policy = _policy(station)
    return {
        "event_id": identity,
        "observation_id": observation_id or f"obs-{identity}-{decision}",
        "station_id": station,
        "station_timezone": "UTC",
        "phase": phase,
        "current_phase": phase,
        "event_date_local": "2026-07-10",
        "decision_at_utc": decision,
        "checkpoint_id": f"checkpoint-{identity}-{decision}",
        "policy_version": policy.policy_version,
        "policy_sha256": policy.content_sha256,
        "evidence_owner": "bayes_basket_target",
        "production_mode": "production_no_live",
        "evaluation_split": "OOS",
        "probabilities": {"winner": 1.0},
        "all_in_yes_ask": 0.20,
        "candidate_authorized": True,
        "fill_status": "NO_EXECUTION",
        "no_execution_policy": {"orders_submitted": 0},
        "execution_cost_usdc": 0.0,
        "realized_pnl_usdc": 0.0,
        "settlement_pnl_usdc": 0.0,
        "failure_cost_usdc": 0.0,
        "labels": [{
            "truth_revision_no": 1,
            "labeled_at_utc": "2026-07-11T12:00:00Z",
            "settlement_closed_at_utc": "2026-07-11T12:00:00Z",
            "winning_bucket": "winner",
            "truth_sha256": "a" * 64,
        }],
    }


def _station_rows(station: str) -> list[dict[str, object]]:
    return [_row(station, phase) for phase in ("HEAVY", "STANDARD", "TAIL")]


def _scorecard(rows: list[dict[str, object]]) -> dict[str, object]:
    policy = _policy("S00")
    return build_cash_phase_oos_scorecard(
        rows,
        as_of_utc=AS_OF,
        policy_version=policy.policy_version,
        policy_promoted=policy.promoted,
        policy_evidence_status=policy.evidence_status,
        expected_station_id=policy.station_id,
        expected_station_timezone=policy.station_timezone,
        evaluation_start_utc=START,
        evaluation_end_utc=END,
        policy_training_cutoff_utc=START,
        minimum_unique_events_per_phase=1,
        fault_cases=_fault_cases(),
        strict_target_owned=True,
    )


def test_design_candidate_can_only_emit_promotion_eligible_evidence() -> None:
    scorecard = _scorecard(_station_rows("S00"))
    assert scorecard["checks"]["promotion_eligible"] is True
    assert scorecard["policy_promoted"] is False
    assert scorecard["policy_evidence_status"] == "DESIGN_CANDIDATE"
    assert scorecard["writes_production_db"] is False
    assert scorecard["calls_order_gateway"] is False
    assert scorecard["orders_submitted"] == 0
    assert validate_cash_phase_forward_scorecard(scorecard)["valid"] is True


def test_half_open_evaluation_window_includes_start_and_excludes_end() -> None:
    rows = _station_rows("S00")
    rows[0] = _row("S00", "HEAVY", decision_at=START)
    rows.append(_row(
        "S00",
        "TAIL",
        event_id="at-exclusive-end",
        decision_at=END,
    ))
    scorecard = _scorecard(rows)
    assert scorecard["scope"]["decision_count"] == 3
    assert scorecard["checks"]["promotion_eligible"] is True


def test_training_overlap_and_timezone_drift_fail_closed() -> None:
    policy = _policy("S00")
    with pytest.raises(
        ContractError,
        match="cash_phase_oos_training_overlaps_evaluation",
    ):
        build_cash_phase_oos_scorecard(
            _station_rows("S00"),
            as_of_utc=AS_OF,
            policy_version=policy.policy_version,
            policy_evidence_status=policy.evidence_status,
            expected_station_id="S00",
            expected_station_timezone="UTC",
            evaluation_start_utc=START,
            evaluation_end_utc=END,
            policy_training_cutoff_utc="2026-07-08T00:00:00.000001Z",
            minimum_unique_events_per_phase=1,
            fault_cases=_fault_cases(),
            strict_target_owned=True,
        )
    wrong = _station_rows("S00")
    wrong[0]["station_timezone"] = "Asia/Seoul"
    scorecard = _scorecard(wrong)
    assert scorecard["checks"]["promotion_eligible"] is False
    assert "station_timezone_mismatch" in scorecard["blockers"]


def test_sample_deficit_and_phase_rewind_are_explicit() -> None:
    scorecard = _scorecard(_station_rows("S00"))
    strict = build_cash_phase_oos_scorecard(
        _station_rows("S00"),
        as_of_utc=AS_OF,
        policy_version="cash-design-S00-v1",
        policy_evidence_status="DESIGN_CANDIDATE",
        expected_station_id="S00",
        expected_station_timezone="UTC",
        evaluation_start_utc=START,
        evaluation_end_utc=END,
        policy_training_cutoff_utc=START,
        minimum_unique_events_per_phase=2,
        fault_cases=_fault_cases(),
        strict_target_owned=True,
    )
    assert scorecard["checks"]["promotion_eligible"] is True
    assert strict["checks"]["promotion_eligible"] is False
    assert "phase_heavy_sample_size_insufficient" in strict["blockers"]

    rewind = _station_rows("S00")
    rewind.extend([
        _row(
            "S00",
            "TAIL",
            event_id="rewind",
            decision_at="2026-07-09T21:00:00Z",
        ),
        _row(
            "S00",
            "STANDARD",
            event_id="rewind",
            decision_at="2026-07-10T08:00:00Z",
        ),
    ])
    rewound = _scorecard(rewind)
    assert "phase_nonmonotonic" in rewound["blockers"]
    assert rewound["checks"]["promotion_eligible"] is False


def test_same_semantic_event_with_different_content_is_conflict() -> None:
    rows = _station_rows("S00")
    duplicate = deepcopy(rows[0])
    duplicate["observation_id"] = "conflicting-copy"
    duplicate["all_in_yes_ask"] = 0.21
    rows.append(duplicate)
    scorecard = _scorecard(rows)
    assert "duplicate_event_content_conflict" in scorecard["blockers"]
    assert scorecard["checks"]["promotion_eligible"] is False


def _fleet(count: int | None = None) -> dict[str, object]:
    # Defaults to the fleet rather than restating its size, so the
    # "44 or 46 is out of scope" cases stay one either side of it.
    if count is None:
        count = expected_station_count()
    station_ids = [f"S{index:02d}" for index in range(count)]
    return {
        "observations": [
            row for station in station_ids for row in _station_rows(station)
        ],
        "policies": [_policy(station) for station in station_ids],
        "registry_rows": [
            {"station_id": station, "settlement_timezone": "UTC"}
            for station in station_ids
        ],
        "policy_training_cutoffs": {
            station: START for station in station_ids
        },
        "fault_cases": _fault_cases(),
        "as_of_utc": AS_OF,
        "evaluation_start_utc": START,
        "evaluation_end_utc": END,
        "expected_station_ids": station_ids,
        "minimum_unique_events_per_phase": 1,
    }


def test_45_station_evidence_is_content_addressed_but_never_mutates() -> None:
    args = _fleet()
    first = build_cash_phase_oos_evidence(**args)
    second = build_cash_phase_oos_evidence(**args)
    assert first == second
    assert first["status"] == "PROMOTION_ELIGIBLE"
    assert first["promotion_eligible_station_count"] == expected_station_count()
    assert len(first["promotion_decision_candidates"]) == expected_station_count()
    assert all(
        row["mutates_policy"] is False and row["writes_authority"] is False
        for row in first["promotion_decision_candidates"]
    )
    assert first["writes_production_db"] is False
    assert first["calls_order_gateway"] is False


@pytest.mark.parametrize(
    "count",
    [expected_station_count() - 1, expected_station_count() + 1],
)
def test_one_either_side_of_the_fleet_is_na(count: int) -> None:
    args = _fleet(count)
    if count > expected_station_count():
        with pytest.raises(
            ContractError,
            match="cash_phase_oos_policy_rows_exceeded",
        ):
            build_cash_phase_oos_evidence(**args)
    else:
        evidence = build_cash_phase_oos_evidence(**args)
        assert evidence["status"] == "NA"
        assert evidence["promotion_decision_candidates"] == []
        assert (
            f"expected_station_count_not_{expected_station_count()}:{count}"
            in evidence["global_blockers"]
        )


def test_low_wrong_low_rewind_restores_identical_content() -> None:
    args = _fleet()
    low_1 = build_cash_phase_oos_evidence(**args)
    wrong_args = deepcopy(args)
    wrong_args["observations"][0]["evidence_owner"] = "source_wallet"
    wrong = build_cash_phase_oos_evidence(**wrong_args)
    low_2 = build_cash_phase_oos_evidence(**args)
    assert low_1 == low_2
    assert low_1["status"] == "PROMOTION_ELIGIBLE"
    assert wrong["status"] == "NA"
    assert wrong["promotion_decision_candidates"] == []


def test_resource_row_limit_fails_before_partial_evidence() -> None:
    args = _fleet()
    args["observations"] = [
        *args["observations"],
        *[
            {
                **_row("S00", "HEAVY"),
                "observation_id": f"overflow-{index}",
            }
            for index in range(4096)
        ],
    ]
    with pytest.raises(
        ContractError,
        match="cash_phase_oos_observation_rows_exceeded",
    ):
        build_cash_phase_oos_evidence(**args)
