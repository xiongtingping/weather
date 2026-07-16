from __future__ import annotations

from observation_fast_lane_contract import (
    HARD_EXECUTION_DEADLINE_MS,
    SEGMENT_BUDGET_MS,
    TOTAL_SLO_MS,
    build_observation_fast_lane_contract,
    validate_observation_fast_lane_topology,
)


def _topology(**overrides) -> dict:
    values = {
        "release_delay_ms": 0,
        "synchronous_forecast_refresh_on_hot_path": False,
        "generic_task_queue_on_hot_path": False,
        "resident_canonical_and_inference_worker": True,
        "transaction_b_atomic_before_intent": True,
        "posterior_commit_outbox_durable_before_intent": True,
        "decision_reads_committed_ssot": True,
        "signer_connection_pool_resident": True,
    }
    values.update(overrides)
    return values


def test_shadow_fast_lane_requires_atomic_posterior_and_durable_intent_within_three_seconds() -> None:
    contract = build_observation_fast_lane_contract(
        correlation_id="corr_fast_lane_shadow_001",
        timestamps={
            "first_actionable_seen_at": "2026-07-03T06:30:00.000Z",
            "canonicalized_at": "2026-07-03T06:30:00.050Z",
            "transaction_b_committed_at": "2026-07-03T06:30:00.400Z",
            "decision_intent_durable_at": "2026-07-03T06:30:00.700Z",
        },
        topology=_topology(),
        mode="shadow",
    )

    assert contract["ok"] is True
    assert contract["total_elapsed_ms"] == 700.0
    assert contract["total_slo_ms"] == TOTAL_SLO_MS
    assert contract["boundary"]["transaction_b_not_bypassed"] is True


def test_shadow_p8_p9_does_not_require_l5_signer_but_live_does() -> None:
    shadow = build_observation_fast_lane_contract(
        correlation_id="corr_fast_lane_shadow_no_signer_001",
        timestamps={
            "first_actionable_seen_at": "2026-07-03T06:30:00.000Z",
            "canonicalized_at": "2026-07-03T06:30:00.050Z",
            "transaction_b_committed_at": "2026-07-03T06:30:00.400Z",
            "decision_intent_durable_at": "2026-07-03T06:30:00.700Z",
        },
        topology=_topology(signer_connection_pool_resident=False),
        mode="shadow",
    )
    live = build_observation_fast_lane_contract(
        correlation_id="corr_fast_lane_live_no_signer_001",
        timestamps={
            "first_actionable_seen_at": "2026-07-03T06:30:00.000Z",
            "canonicalized_at": "2026-07-03T06:30:00.050Z",
            "transaction_b_committed_at": "2026-07-03T06:30:00.400Z",
            "decision_intent_durable_at": "2026-07-03T06:30:00.700Z",
            "order_submitted_at": "2026-07-03T06:30:01.000Z",
        },
        topology=_topology(signer_connection_pool_resident=False),
        mode="live",
    )

    assert shadow["ok"] is True
    assert shadow["topology"]["require_execution_readiness"] is False
    assert live["ok"] is False
    assert "fast_lane_resident_signer_or_connection_pool_missing" in live["blockers"]


def test_segment_budgets_are_consistent_with_t1_to_t3_and_total_slos() -> None:
    assert HARD_EXECUTION_DEADLINE_MS == TOTAL_SLO_MS == 3000
    assert (
        SEGMENT_BUDGET_MS["canonicalization"]
        + SEGMENT_BUDGET_MS["strong_inference_and_transaction_b"]
        == 500
    )
    assert sum(SEGMENT_BUDGET_MS.values()) == TOTAL_SLO_MS


def test_live_fast_lane_accepts_exact_segment_and_total_budget_boundaries() -> None:
    contract = build_observation_fast_lane_contract(
        correlation_id="corr_fast_lane_live_boundary_001",
        timestamps={
            "first_actionable_seen_at": "2026-07-03T06:30:00.000Z",
            "canonicalized_at": "2026-07-03T06:30:00.100Z",
            "transaction_b_committed_at": "2026-07-03T06:30:00.500Z",
            "decision_intent_durable_at": "2026-07-03T06:30:01.000Z",
            "order_submitted_at": "2026-07-03T06:30:03.000Z",
        },
        topology=_topology(),
        mode="live",
    )

    assert contract["ok"] is True
    assert contract["segment_elapsed_ms"] == {
        "canonicalization": 100.0,
        "strong_inference_and_transaction_b": 400.0,
        "decision_and_durable_intent": 500.0,
        "sign_and_submit": 2000.0,
    }
    assert contract["total_elapsed_ms"] == 3000.0


def test_current_35_second_release_delay_and_generic_queue_are_hard_architecture_blockers() -> None:
    topology = validate_observation_fast_lane_topology(
        _topology(release_delay_ms=35000, generic_task_queue_on_hot_path=True)
    )

    assert topology["ok"] is False
    assert "fast_lane_release_delay_must_be_zero" in topology["blockers"]
    assert "fast_lane_generic_task_queue_forbidden" in topology["blockers"]


def test_live_fast_lane_enforces_order_segment_and_total_slo_without_reordering_events() -> None:
    contract = build_observation_fast_lane_contract(
        correlation_id="corr_fast_lane_live_001",
        timestamps={
            "first_actionable_seen_at": "2026-07-03T06:30:00.000Z",
            "canonicalized_at": "2026-07-03T06:30:00.100Z",
            "transaction_b_committed_at": "2026-07-03T06:30:00.500Z",
            "decision_intent_durable_at": "2026-07-03T06:30:01.000Z",
            "order_submitted_at": "2026-07-03T06:30:03.001Z",
        },
        topology=_topology(),
        mode="live",
    )

    assert contract["ok"] is False
    assert "fast_lane_segment_budget_exceeded:sign_and_submit" in contract["blockers"]
    assert "fast_lane_total_slo_exceeded" in contract["blockers"]


def test_fast_lane_contract_rejects_missing_correlation_id() -> None:
    contract = build_observation_fast_lane_contract(
        correlation_id="",
        timestamps={
            "first_actionable_seen_at": "2026-07-03T06:30:00.000Z",
            "canonicalized_at": "2026-07-03T06:30:00.050Z",
            "transaction_b_committed_at": "2026-07-03T06:30:00.400Z",
            "decision_intent_durable_at": "2026-07-03T06:30:00.700Z",
        },
        topology=_topology(),
        mode="shadow",
    )

    assert contract["ok"] is False
    assert "fast_lane_correlation_id_missing" in contract["blockers"]
