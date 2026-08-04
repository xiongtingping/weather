from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.station_policy_forward_outcome_binding import (
    METRIC_CONTRACT_STATE,
    POLICY_FAMILIES,
    UTILITY_CONTRACT_SCHEMA,
    assert_future_station_policy_outcome_materialization,
    assert_station_policy_utility_contract,
    build_future_station_policy_outcome_materialization,
    build_station_policy_utility_contract,
    build_station_policy_utility_contract_pointer,
    validate_historical_station_policy_utility_binding,
    validate_station_policy_utility_binding,
)


def test_utility_contract_is_versioned_and_unbound() -> None:
    contract = build_station_policy_utility_contract()
    assert contract["schema"] == UTILITY_CONTRACT_SCHEMA
    assert set(contract["families"]) == set(POLICY_FAMILIES)
    assert contract["global_requirements"]["direct_fill_binding"] == "UNBOUND"
    assert contract["promotion_permitted"] is False
    for family in POLICY_FAMILIES:
        required = contract["families"][family]["required_bindings"]
        assert "candidate_inventory" in required
        assert "baseline_inventory" in required
        assert "official_clob_ws" in required
        assert "settlement" in required
        assert contract["families"][family]["formula_language"] == "symbolic_no_evaluation"
    assert_station_policy_utility_contract(contract)

    pointer = build_station_policy_utility_contract_pointer(contract)
    assert pointer["artifact_content_sha256"] == contract["content_sha256"]
    assert pointer["state"] == "DEFINED_UNBOUND"
    assert pointer["metric_state"] == METRIC_CONTRACT_STATE

    tampered = dict(contract)
    tampered["families"] = dict(contract["families"])
    tampered["families"]["target"] = dict(contract["families"]["target"])
    tampered["families"]["target"]["metric_state"] = "READY"
    with pytest.raises(ContractError, match="hash_invalid"):
        assert_station_policy_utility_contract(tampered)


def test_future_materialization_carries_only_contract_pointer() -> None:
    materialization = build_future_station_policy_outcome_materialization(
        station_id="RKPK",
        event_date="2026-08-01",
        settlement_timezone="Asia/Seoul",
        stage="P1",
        decision_at_utc="2026-08-01T00:00:00Z",
        strategy_version="target-v2-shadow",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
    )
    assert materialization["state"] == "UNAVAILABLE"
    assert materialization["bindings"] == {}
    assert materialization["utility_contract_pointer"]["state"] == "DEFINED_UNBOUND"
    assert_future_station_policy_outcome_materialization(materialization)


def test_binding_validator_never_emits_utility_and_keeps_direct_fill_unbound() -> None:
    result = validate_station_policy_utility_binding(
        {
            "family": "reserve",
            "utility_contract_pointer": build_station_policy_utility_contract_pointer(),
            "direct_fill_binding": "UNBOUND",
            # Deliberately no candidate/baseline/WS/fill/settlement values.
        },
        family="reserve",
    )
    assert result["state"] == "UNBOUND"
    assert result["utility_evaluated"] is False
    assert result["score_emitted"] is False
    assert result["promotion_permitted"] is False
    assert "candidate_vs_baseline_binding_section_missing" in result["blockers"]

    forbidden = {
        "family": "target",
        "utility_contract_pointer": build_station_policy_utility_contract_pointer(),
        "candidate_score": 1.0,
    }
    result = validate_station_policy_utility_binding(forbidden, family="target")
    assert "derived_candidate_score_forbidden" in result["blockers"]
    assert result["score_emitted"] is False


def test_binding_validator_rejects_nested_derived_scores_and_bad_ws_lineage() -> None:
    pointer = build_station_policy_utility_contract_pointer()
    result = validate_station_policy_utility_binding(
        {
            "family": "target",
            "utility_contract_pointer": pointer,
            # A nested score must be rejected just like a top-level score;
            # otherwise a caller could smuggle a realised utility through an
            # evidence subsection while the raw binding remains UNBOUND.
            "candidate_vs_baseline": {"candidate_score": 1.0},
            "official_clob_ws": {
                "quote_source": "OPEN_METEO",
                "quote_observed_at_utc": "2026-08-01T00:01:00Z",
                "quote_expires_at_utc": "2026-08-01T00:02:00Z",
            },
            "scope": {"decision_at_utc": "2026-08-01T00:00:00Z"},
        },
        family="target",
    )
    assert "derived_candidate_vs_baseline_candidate_score_forbidden" in result["blockers"]
    assert "official_clob_ws_quote_source_invalid" in result["blockers"]
    assert "official_clob_ws_quote_after_decision" in result["blockers"]
    assert result["utility_evaluated"] is False
    assert result["promotion_permitted"] is False


def test_binding_validator_rejects_pointer_hash_or_contract_mismatch() -> None:
    pointer = build_station_policy_utility_contract_pointer()
    tampered_pointer = dict(pointer)
    tampered_pointer["pointer_sha256"] = "0" * 64
    result = validate_station_policy_utility_binding(
        {
            "family": "target",
            "utility_contract_pointer": tampered_pointer,
        },
        family="target",
    )
    assert "station_policy_utility_contract_pointer_hash_invalid" in result["blockers"]
    assert result["binding_valid"] is False
    assert result["state"] == "UNBOUND"


def _sha(character: str) -> str:
    return character * 64


def _complete_raw_binding(*, family: str = "target") -> dict[str, object]:
    """Return receipt-backed evidence only; it deliberately carries no score."""

    candidate_receipts = [
        {
            "receipt_id": "candidate-partial-1",
            "order_intent_id": "candidate-intent-1",
            "bucket": "38°C",
            "receipt_type": "MAKER_PARTIAL_FILL",
            "authoritative": True,
            "simulated": False,
            "matched_size": 1.0,
            "matched_notional_usdc": 0.42,
            "observed_at_utc": "2026-08-01T00:00:10Z",
            "receipt_content_sha256": _sha("a"),
            "official_ws_snapshot_sha256": _sha("c"),
        },
        {
            "receipt_id": "candidate-partial-2",
            "order_intent_id": "candidate-intent-1",
            "bucket": "38°C",
            "receipt_type": "MAKER_PARTIAL_FILL",
            "authoritative": True,
            "simulated": False,
            "matched_size": 0.5,
            "matched_notional_usdc": 0.21,
            "observed_at_utc": "2026-08-01T00:00:20Z",
            "receipt_content_sha256": _sha("b"),
            "official_ws_snapshot_sha256": _sha("c"),
        },
    ]
    baseline_receipts = [
        {
            "receipt_id": "baseline-zero-1",
            "order_intent_id": "baseline-intent-1",
            "bucket": "38°C",
            "receipt_type": "CANCEL_CONFIRMED",
            "authoritative": True,
            "simulated": False,
            "matched_size": 0.0,
            "matched_notional_usdc": 0.0,
            "observed_at_utc": "2026-08-01T00:00:25Z",
            "receipt_content_sha256": _sha("d"),
            "official_ws_snapshot_sha256": _sha("e"),
        }
    ]
    binding: dict[str, object] = {
        "family": family,
        "utility_contract_pointer": build_station_policy_utility_contract_pointer(),
        "scope": {
            "station_id": "RKPK",
            "event_date": "2026-08-01",
            "settlement_timezone": "Asia/Seoul",
            "stage": "P1",
            "decision_at_utc": "2026-08-01T00:00:00Z",
            "strategy_version": "basket-v2",
            "season_bin": "summer",
            "maturity_bin": "d0",
            "liquidity_bin": "normal",
        },
        "candidate_vs_baseline": {
            "candidate_decision_fact_sha256": _sha("1"),
            "baseline_decision_fact_sha256": _sha("2"),
            "candidate_plan_sha256": _sha("3"),
            "baseline_plan_sha256": _sha("4"),
            "candidate_inventory_vector_sha256": _sha("5"),
            "baseline_inventory_vector_sha256": _sha("6"),
            "candidate_support_bucket_ids": ["38°C"],
            "baseline_support_bucket_ids": ["38°C"],
        },
        "candidate_inventory": {
            "plan_sha256": _sha("3"),
            "inventory_vector_sha256": _sha("5"),
            "support_bucket_ids": ["38°C"],
            "target_quantity_by_bucket": {"38°C": 2.0},
        },
        "baseline_inventory": {
            "plan_sha256": _sha("4"),
            "inventory_vector_sha256": _sha("6"),
            "support_bucket_ids": ["38°C"],
            "target_quantity_by_bucket": {"38°C": 2.0},
        },
        "official_clob_ws": {
            "candidate_ws_snapshot_sha256": _sha("c"),
            "baseline_ws_snapshot_sha256": _sha("e"),
            "candidate_quote_sequence": 11,
            "baseline_quote_sequence": 12,
            "candidate_all_in_yes_ask_by_bucket": {"38°C": 0.42},
            "baseline_all_in_yes_ask_by_bucket": {"38°C": 0.43},
            "quote_source": "OFFICIAL_CLOB_WS",
            "quote_observed_at_utc": "2026-07-31T23:59:59Z",
            "quote_expires_at_utc": "2026-08-01T00:01:00Z",
        },
        "execution": {
            "candidate_fill_receipt_bindings": candidate_receipts,
            "baseline_fill_receipt_bindings": baseline_receipts,
            "candidate_fill_receipt_set_sha256": content_sha256(
                sorted([_sha("a"), _sha("b")])
            ),
            "baseline_fill_receipt_set_sha256": content_sha256([_sha("d")]),
            "candidate_confirmed_filled_qty_by_bucket": {"38°C": 1.5},
            "baseline_confirmed_filled_qty_by_bucket": {"38°C": 0.0},
            "candidate_confirmed_filled_notional_usdc_by_bucket": {"38°C": 0.63},
            "baseline_confirmed_filled_notional_usdc_by_bucket": {"38°C": 0.0},
            "candidate_fill_outcome_by_bucket": {"38°C": "CONFIRMED_FILL"},
            "baseline_fill_outcome_by_bucket": {"38°C": "CONFIRMED_ZERO_FILL"},
        },
        "settlement": {
            "settlement_truth_sha256": _sha("7"),
            "settlement_outcome_sha256": _sha("8"),
            "winner_bucket": "38°C",
            "settlement_closed_at_utc": "2026-08-01T12:00:00Z",
            "settlement_authority": "OFFICIAL_CLOSED_SETTLEMENT_TRUTH",
        },
        "timing": {},
    }
    if family == "reserve":
        execution = binding["execution"]
        assert isinstance(execution, dict)
        execution.update(
            {
                "queue_entered_at_utc": "2026-08-01T00:00:01Z",
                "queue_exit_at_utc": "2026-08-01T00:00:10Z",
                "cancel_requested_at_utc": "2026-08-01T00:00:11Z",
                "cancel_confirmed_at_utc": "2026-08-01T00:00:12Z",
                "cancel_receipt_sha256": _sha("9"),
                "adverse_selection_cost_usdc": 0.01,
                "capital_lock_cost_usdc": 0.02,
                "frozen_queue_cost_rate_usdc_per_second": 0.001,
                "cancel_fee_usdc": 0.0,
                "reprice_cost_usdc": 0.0,
            }
        )
    if family == "cadence":
        execution = binding["execution"]
        assert isinstance(execution, dict)
        execution.update(
            {
                "wait_started_at_utc": "2026-08-01T00:00:00Z",
                "wait_ended_at_utc": "2026-08-01T00:01:00Z",
                "wait_or_act_arm": "ACT",
                "capital_lock_cost_usdc": 0.02,
                "adverse_selection_cost_usdc": 0.01,
                "counterfactual_act_receipt_bindings": candidate_receipts,
                "counterfactual_wait_receipt_bindings": baseline_receipts,
            }
        )
        binding["timing"] = {
            "candidate_decision_at_utc": "2026-08-01T00:00:00Z",
            "baseline_decision_at_utc": "2026-08-01T00:00:00Z",
            "next_checkpoint_at_utc": "2026-08-01T00:01:00Z",
            "event_deadline_at_utc": "2026-08-01T12:00:00Z",
        }
    return binding


@pytest.mark.parametrize("family", POLICY_FAMILIES)
def test_complete_receipt_backed_raw_binding_is_auditable_but_never_scored(
    family: str,
) -> None:
    result = validate_station_policy_utility_binding(
        _complete_raw_binding(family=family), family=family
    )
    assert result["state"] == "BINDING_COMPLETE_NOT_SCORED"
    assert result["binding_valid"] is True
    assert result["utility_evaluated"] is False
    assert result["score_emitted"] is False
    assert result["promotion_permitted"] is False


def test_simulated_or_requested_partial_fill_cannot_complete_binding() -> None:
    simulated = _complete_raw_binding()
    execution = simulated["execution"]
    assert isinstance(execution, dict)
    receipts = execution["candidate_fill_receipt_bindings"]
    assert isinstance(receipts, list) and isinstance(receipts[0], dict)
    receipts[0]["simulated"] = True
    result = validate_station_policy_utility_binding(simulated, family="target")
    assert result["state"] == "UNBOUND"
    assert "execution_candidate_fill_receipt_simulated_or_unknown" in result["blockers"]

    requested_size = _complete_raw_binding()
    execution = requested_size["execution"]
    assert isinstance(execution, dict)
    execution["candidate_confirmed_filled_qty_by_bucket"] = {"38°C": 2.0}
    result = validate_station_policy_utility_binding(requested_size, family="target")
    assert result["state"] == "UNBOUND"
    assert "execution_candidate_receipt_amount_mapping_mismatch" in result["blockers"]


def test_historical_binding_requires_closed_awc_label_and_never_emits_score() -> None:
    binding = _complete_raw_binding()
    binding["evidence_role"] = "historical_strict_oos"
    settlement = binding["settlement"]
    assert isinstance(settlement, dict)
    settlement["settlement_closed_at_utc"] = "2026-08-01T15:00:00Z"
    fact = {
        "station_id": "RKPK",
        "event_date": "2026-08-01",
        "settlement_timezone": "Asia/Seoul",
        "stage": "P1",
        "decision_at_utc": "2026-08-01T00:00:00Z",
        "content_sha256": _sha("1"),
        "plan_sha256": _sha("3"),
        "inventory_sha256": _sha("5"),
        "ws_snapshot_content_sha256": _sha("c"),
        "authority_scope": "HISTORICAL_STRICT_OOS",
    }
    label = {
        "truth_sha256": _sha("7"),
        "outcome_sha256": _sha("8"),
        "winning_bucket": "38°C",
        "settlement_closed_at_utc": "2026-08-01T15:00:00Z",
        "labeled_at_utc": "2026-08-01T15:01:00Z",
        "official_awc_truth": True,
        "truth_source_lane": "AWC",
        "truth_qc_status": "ACCEPTED",
    }
    result = validate_historical_station_policy_utility_binding(
        binding,
        family="target",
        fact=fact,
        label=label,
        evaluation_as_of_utc="2026-08-02T00:00:00Z",
    )
    assert result["state"] == "HISTORICAL_BINDING_COMPLETE_NOT_SCORED"
    assert result["historical_strict_oos"] is True
    assert result["score_emitted"] is False

    future = dict(binding)
    future["evidence_role"] = "observational_candidate_not_oos"
    result = validate_historical_station_policy_utility_binding(
        future,
        family="target",
        fact=fact,
        label=label,
        evaluation_as_of_utc="2026-08-02T00:00:00Z",
    )
    assert result["historical_strict_oos"] is False
    assert result["score_emitted"] is False
    assert "historical_binding_evidence_role_not_historical_strict_oos" in result["blockers"]

    forward_fact = dict(fact)
    forward_fact["authority_scope"] = "PRODUCTION_NO_LIVE_FORWARD"
    result = validate_historical_station_policy_utility_binding(
        binding,
        family="target",
        fact=forward_fact,
        label=label,
        evaluation_as_of_utc="2026-08-02T00:00:00Z",
    )
    assert result["historical_strict_oos"] is False
    assert (
        "historical_binding_fact_authority_scope_not_historical_strict_oos"
        in result["blockers"]
    )

    wrong_close = dict(label)
    wrong_close["settlement_closed_at_utc"] = "2026-08-01T11:00:00Z"
    result = validate_historical_station_policy_utility_binding(
        binding,
        family="target",
        fact=fact,
        label=wrong_close,
        evaluation_as_of_utc="2026-08-02T00:00:00Z",
    )
    assert result["score_emitted"] is False
    assert "historical_binding_settlement_close_scope_mismatch" in result["blockers"]
