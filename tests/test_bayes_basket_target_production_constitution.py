from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from bayes_basket_target.delta_v2_artifact import ARTIFACT_SCHEMA
from bayes_basket_target.contracts import MIN_FORMAL_ENSEMBLE_MODELS
from bayes_basket_target.source_policy import (
    AWC_HOUR_REFERENCE_FOR_AMSC,
    source_policy_reason,
)


ROOT = Path(__file__).parents[1]
DOCTRINE = (
    ROOT
    / "data/reports/polymarket_wallet_0x8fbd7cf5_20260721/doctrine"
)


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_frozen_machine_constitution_matches_runtime_invariants() -> None:
    architecture = _json(DOCTRINE / "bayes_basket_target_architecture_v1.json")
    constitution = architecture["production_constitution"]
    activation = _json(
        ROOT
        / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    )

    assert constitution["frozen"] is True
    assert constitution["change_requires_impact_matrix"] is True
    assert constitution["probability_stages"] == ["P0", "P0-M", "P1"]
    assert constitution["settlement_timezone_authority"] == (
        "market_registry.settlement_timezone"
    )
    assert constitution["formal_ensemble_minimum_positive_weight_models"] == (
        MIN_FORMAL_ENSEMBLE_MODELS
    )
    assert MIN_FORMAL_ENSEMBLE_MODELS == 2
    assert constitution["objective_schema"] == ARTIFACT_SCHEMA
    assert constitution["objective_may_rewrite_probability"] is False
    assert constitution["execution_mode"] == activation["execution_mode"] == (
        "production_no_live"
    )
    assert constitution["live_submit_authorized"] is False
    assert constitution["calls_order_gateway"] is False
    assert activation["live_submit_authorized"] is False
    assert activation["calls_order_gateway"] is False


def test_china_awc_cannot_cross_into_formal_truth() -> None:
    reason = source_policy_reason(
        source_lane="awc",
        report_type="METAR",
        valid_at=datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc),
        expected_lane="amsc",
    )
    assert reason == AWC_HOUR_REFERENCE_FOR_AMSC


def test_vertical_slice_and_delivery_state_authorities_are_unique() -> None:
    architecture = _json(DOCTRINE / "bayes_basket_target_architecture_v1.json")
    roadmap = _json(DOCTRINE / "bayes_basket_development_roadmap_v1_6.json")
    slice_contract = architecture["production_constitution"]["vertical_slice"]
    status = roadmap["delivery_status_authority"]

    assert slice_contract["station_id"] == "CANONICAL_45"
    assert slice_contract["source_lane"] == "AMSC_AWC_ROUTED"
    assert slice_contract["batch_expansion_requires"] == (
        "CANONICAL_45_BOUNDED_NO_LIVE_EXPANSION"
    )
    assert (
        slice_contract[
            "forward_oos_blocks_production_no_live_or_batch_expansion"
        ]
        is False
    )
    assert status["is_only_status_authority"] is True
    assert status["vertical_slice_station_id"] == "CANONICAL_45"
    assert status["batch_expansion_gate"] == (
        "CANONICAL_45_BOUNDED_NO_LIVE_EXPANSION"
    )
    assert (
        status[
            "forward_oos_blocks_production_no_live_or_batch_expansion"
        ]
        is False
    )
    assert status["current"]["batch_45_station"] == "RUNNING"
    assert status["tested_or_rehearsed_is_not_done"] is True
    assert set(status["current"].values()).issubset(set(status["states"]))


def test_current_change_has_complete_impact_matrix() -> None:
    roadmap = _json(DOCTRINE / "bayes_basket_development_roadmap_v1_6.json")
    impact = roadmap["current_change_impact_matrix"]

    assert impact["contracts"] == [
        "bayes_basket_development_roadmap_v1_6",
        "M4-26_DeltaBasketScore_v2",
        "M6-12_reservation_and_execution_utility",
        "M8-13_forward_OOS_promotion",
        "M5-06_authority_input_zero_write",
        "M6-02_economic_quote_identity",
        "CANONICAL_45_CANDIDATE_MODEL_PREFETCH_SEPARATED_FROM_FORMAL_P0",
    ]
    assert impact["change_kind"] == (
        "IMPLEMENTATION_RUNNING_SETTLED_LABEL_MATERIALIZED_FORWARD_EXPANSION_READY"
    )
    assert impact["new_schema_or_stage"] is False
    assert impact["current_exact_scope"] == "CANONICAL_45"
    assert impact["forward_rebuild_required"] is False
    assert impact["maintenance_window_required"] is False
    assert impact["rollback"].startswith("PRESERVE_APPEND_ONLY")
    assert impact["execution_mode"] == "production_no_live"
    assert impact["live_submit_authorized"] is False
    assert impact["calls_order_gateway"] is False


def test_generation_21_zero_write_runtime_receipt_is_authoritative() -> None:
    roadmap = _json(DOCTRINE / "bayes_basket_development_roadmap_v1_6.json")
    runtime = roadmap["ws_decision_zero_write_v1"]
    receipt_path = ROOT / runtime["runtime_receipt_path"]
    receipt = _json(receipt_path)

    assert runtime["status"] == "RUNNING"
    assert runtime["runtime_status"] == "GENERATION_21_VERIFIED"
    assert runtime["runtime_generation"] == 21
    assert runtime["runtime_pid"] == 97722
    assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == (
        runtime["runtime_receipt_sha256"]
    )
    assert receipt["status"] == "PASS"
    assert receipt["sample_count"] == 10
    assert receipt["counts"]["basket_official_ws_snapshot"]["delta"] == 6
    assert receipt["counts"]["basket_target_execution_cycles"]["delta"] == 0
    assert receipt["counts"]["auto_trade_decisions"]["delta"] == 0
    assert receipt["counts"]["basket_policy_training_fact"]["delta"] == 0
    assert receipt["counts"]["basket_target_order_intents"]["final"] == 0
    assert receipt["counts"]["basket_target_order_events"]["final"] == 0
    assert receipt["counts"]["basket_target_order_reconciliations"]["final"] == 0
    assert receipt["runtime"]["live_submit_authorized"] is False
    assert receipt["runtime"]["calls_order_gateway"] is False


def test_delta_basket_score_v2_is_one_unified_running_contract() -> None:
    roadmap = _json(DOCTRINE / "bayes_basket_development_roadmap_v1_6.json")
    contract = roadmap["delta_basket_score_v2_development_contract"]

    assert contract["status"] == "RUNNING"
    assert contract["change_kind"] == (
        "UNIFIED_OBJECTIVE_V2_NO_LIVE_RESIDENT_RUNNING"
    )
    assert contract["hold_action"] == "DeltaBasketScore_v2(HOLD)=0"
    assert contract["target_inventory"].startswith("q_star=argmax")
    assert "must not duplicate" in contract["inventory_fit_role"]
    assert "partial" in contract["partial_fill_distribution"]
    assert "CapitalLockCost" in contract["execution_utility"]
    assert "CapitalReserved" in contract["cross_event_capital"]
    assert "LIMIT_PRICE_NOT_ABOVE_RESERVATION" in (
        contract["hard_feasible_set"]
    )
    assert roadmap["delivery_status_authority"]["current"][
        "DeltaBasketScore_v2"
    ] == "RUNNING"
