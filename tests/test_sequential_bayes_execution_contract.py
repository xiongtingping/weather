from __future__ import annotations

from sequential_bayes_execution_contract import (
    EXPECTED_ACTIVATION_MODE,
    EXPECTED_COMPOSER_SCHEMA,
    EXPECTED_CONTINUOUS_DISTRIBUTION_SCHEMA,
    EXPECTED_CONTINUOUS_TARGET_VARIABLE,
    EXPECTED_FORMULA,
    build_sequential_bayes_execution_contract,
    build_target_realization_transition_execution_contract,
    validate_sequential_bayes_execution_context,
)


def _payload() -> dict:
    buckets = [
        {"bucket": "36°C", "probability": 0.60},
        {"bucket": "37°C", "probability": 0.30},
        {"bucket": "38°C", "probability": 0.10},
    ]
    return {
        "fact_id": 101,
        "fact_as_of": "2026-07-10T08:00:00+00:00",
        "fact_snapshot_id": "fact_test",
        "posterior_run_id": "post_test",
        "posterior_composer_schema": EXPECTED_COMPOSER_SCHEMA,
        "posterior_activation_mode": EXPECTED_ACTIVATION_MODE,
        "source_to_posterior_timing": {
            "latest_source_first_actionable_seen_at": "2026-07-10T08:00:00+00:00"
        },
        "posterior_continuous_distribution": {
            "schema": EXPECTED_CONTINUOUS_DISTRIBUTION_SCHEMA,
            "source": EXPECTED_COMPOSER_SCHEMA,
            "target_variable": EXPECTED_CONTINUOUS_TARGET_VARIABLE,
            "grid_step_c": 0.1,
            "grid": [
                {"temp_c": 35.8, "mass": 0.60, "cdf": 0.60},
                {"temp_c": 35.9, "mass": 0.30, "cdf": 0.90},
                {"temp_c": 36.0, "mass": 0.10, "cdf": 1.00},
            ],
            "bucket_probabilities": buckets,
        },
        "sequential_state_filter": {"schema": "sequential_metar_state_filter_v1"},
        "remaining_warming_mixture": {"schema": "zero_inflated_remaining_warming_mixture_v1"},
        "observation_information_gate": {
            "schema": "continuous_observation_information_gate_v1"
        },
        "posterior_boundary": {
            "legacy_bucket_product_posterior_not_used": True,
            "live_no_shadow": True,
        },
        "hard_constraints": {
            "posterior": {"hard_constraint_violation_count": 0}
        },
        "posterior_ssot_fact": {
            "state": "available",
            "fact_snapshot_id": "fact_test",
            "posterior_run_id": "post_test",
            "target_bucket": "36°C",
            "target_probability": 0.60,
            "bucket_probabilities": buckets,
            "layer_chain": {"formula": EXPECTED_FORMULA},
            "boundaries": {
                "live_primary_composer": EXPECTED_COMPOSER_SCHEMA,
                "posterior_activation_mode": EXPECTED_ACTIVATION_MODE,
                "legacy_bucket_product_posterior_used": False,
                "shadow_posterior_emitted": False,
            },
        },
    }


def test_live_sequential_ssot_is_trade_eligible_inside_three_seconds() -> None:
    contract = build_sequential_bayes_execution_contract(
        _payload(),
        now="2026-07-10T08:00:02+00:00",
    )

    assert contract["trade_eligible"] is True
    assert contract["blockers"] == []
    assert contract["signal_to_order_slo_met_at_check"] is True
    assert contract["signal_to_order_hard_deadline_met_at_check"] is True
    assert contract["observation_to_order_slo_met_at_check"] is True
    assert contract["observation_trigger_time_source"] == (
        "source_to_posterior_timing.latest_source_first_actionable_seen_at"
    )


def test_three_seconds_is_both_slo_and_hard_deadline() -> None:
    boundary = build_sequential_bayes_execution_contract(
        _payload(),
        now="2026-07-10T08:00:03+00:00",
    )
    expired = build_sequential_bayes_execution_contract(
        _payload(),
        now="2026-07-10T08:00:03.001+00:00",
    )

    assert boundary["trade_eligible"] is True
    assert boundary["signal_to_order_slo_met_at_check"] is True
    assert boundary["signal_to_order_hard_deadline_met_at_check"] is True
    assert boundary["signal_to_order_hard_deadline_ms"] == 3000
    assert expired["trade_eligible"] is False
    assert "sequential_bayes_signal_to_order_hard_deadline_exceeded" in expired["blockers"]


def test_runtime_override_cannot_relax_three_second_hard_deadline() -> None:
    contract = build_sequential_bayes_execution_contract(
        _payload(),
        now="2026-07-10T08:00:03.001+00:00",
        slo_ms=10_000,
        hard_deadline_ms=60_000,
    )

    assert contract["signal_to_order_slo_ms"] == 3000
    assert contract["signal_to_order_hard_deadline_ms"] == 3000
    assert contract["trade_eligible"] is False


def test_stale_observation_cannot_be_relabelled_as_fresh_posterior_signal() -> None:
    payload = _payload()
    payload["source_to_posterior_timing"]["latest_source_first_actionable_seen_at"] = (
        "2026-07-10T07:50:00+00:00"
    )

    contract = build_sequential_bayes_execution_contract(
        payload,
        now="2026-07-10T08:00:02+00:00",
    )

    assert contract["signal_to_order_hard_deadline_met_at_check"] is True
    assert contract["observation_to_order_hard_deadline_met_at_check"] is False
    assert contract["trade_eligible"] is False
    assert "sequential_bayes_observation_to_order_hard_deadline_exceeded" in contract["blockers"]


def test_observation_trigger_timestamp_is_mandatory_for_primary_execution() -> None:
    payload = _payload()
    payload.pop("source_to_posterior_timing")

    contract = build_sequential_bayes_execution_contract(
        payload,
        now="2026-07-10T08:00:02+00:00",
    )

    assert contract["trade_eligible"] is False
    assert "sequential_bayes_observation_trigger_time_missing" in contract["blockers"]


def test_legacy_or_shadow_posterior_cannot_enter_execution() -> None:
    payload = _payload()
    payload["posterior_composer_schema"] = "hierarchical_temperature_posterior_runtime_v1"
    payload["posterior_activation_mode"] = "shadow"
    contract = build_sequential_bayes_execution_contract(
        payload,
        now="2026-07-10T08:00:01+00:00",
    )

    assert contract["trade_eligible"] is False
    assert "sequential_bayes_composer_schema_mismatch" in contract["blockers"]
    assert "sequential_bayes_activation_not_live_no_shadow" in contract["blockers"]


def test_pre_v31_continuous_surface_cannot_enter_execution() -> None:
    payload = _payload()
    payload["posterior_continuous_distribution"] = {
        "schema": "sequential_remaining_warming_continuous_distribution_v2"
    }

    contract = build_sequential_bayes_execution_contract(
        payload,
        now="2026-07-10T08:00:01+00:00",
    )

    assert contract["trade_eligible"] is False
    assert contract["blockers"] == [
        "sequential_bayes_posterior_continuous_distribution_contract_missing"
    ]


def test_top_level_and_ssot_lineage_must_be_identical() -> None:
    payload = _payload()
    payload["posterior_ssot_fact"]["fact_snapshot_id"] = "fact_other"

    contract = build_sequential_bayes_execution_contract(
        payload,
        now="2026-07-10T08:00:01+00:00",
    )

    assert contract["trade_eligible"] is False
    assert contract["blockers"] == ["sequential_bayes_fact_snapshot_lineage_mismatch"]
    assert contract["lineage_identity_consistent"] is False


def test_v31_continuous_surface_requires_normalized_monotone_grid_and_same_bucket_integral() -> None:
    payload = _payload()
    payload["posterior_continuous_distribution"]["grid"][2]["mass"] = 0.20
    payload["posterior_continuous_distribution"]["grid"][2]["cdf"] = 1.10

    contract = build_sequential_bayes_execution_contract(
        payload,
        now="2026-07-10T08:00:01+00:00",
    )

    assert contract["trade_eligible"] is False
    assert contract["blockers"] == [
        "sequential_bayes_posterior_continuous_distribution_invalid"
    ]
    assert contract["continuous_distribution_contract"]["reason"] == "grid_point_invalid"


def test_worker_revalidates_hard_deadline_without_recomputing_probability() -> None:
    contract = build_sequential_bayes_execution_contract(
        _payload(),
        now="2026-07-10T08:00:02+00:00",
    )
    candidate = {"sequential_bayes_execution_contract": contract}

    inside = validate_sequential_bayes_execution_context(
        candidate,
        now="2026-07-10T08:00:02.999+00:00",
    )
    expired = validate_sequential_bayes_execution_context(
        candidate,
        now="2026-07-10T08:00:03.001+00:00",
    )

    assert inside["ok"] is True
    assert expired["ok"] is False
    assert expired["blockers"] == [
        "sequential_bayes_observation_to_order_hard_deadline_exceeded",
        "sequential_bayes_signal_to_order_hard_deadline_exceeded",
    ]
    assert contract["probability_recomputed"] is False


def test_sell_fill_starts_independent_three_second_transition_timer() -> None:
    origin = build_sequential_bayes_execution_contract(
        _payload(),
        now="2026-07-10T08:00:02+00:00",
    )
    transition = build_target_realization_transition_execution_contract(
        origin,
        transition_signal_at="2026-07-10T08:01:00+00:00",
        now="2026-07-10T08:01:02+00:00",
    )

    assert transition["trade_eligible"] is True
    assert transition["transition_timer_reset"] is True
    assert transition["origin_contract_id"] == origin["contract_id"]
    assert transition["signal_to_order_slo_met_at_check"] is True
    assert validate_sequential_bayes_execution_context(
        {"sequential_bayes_execution_contract": transition},
        now="2026-07-10T08:01:02.999+00:00",
    )["ok"] is True


def test_sell_fill_transition_blocks_after_three_seconds() -> None:
    origin = build_sequential_bayes_execution_contract(
        _payload(),
        now="2026-07-10T08:00:02+00:00",
    )
    transition = build_target_realization_transition_execution_contract(
        origin,
        transition_signal_at="2026-07-10T08:01:00+00:00",
        now="2026-07-10T08:01:03.001+00:00",
    )

    assert transition["trade_eligible"] is False
    assert transition["blockers"] == [
        "target_realization_fill_to_buy_hard_deadline_exceeded"
    ]
