from __future__ import annotations

import json

from production_reported_x_market_ssot_runtime import materialize_reported_x_market_handoff_rows, materialize_reported_x_market_public_fact_row
from tests.test_production_reported_x_public_ssot_writer import _activation, _checkpoint, _update


def _candidate() -> dict:
    checkpoint = _checkpoint()
    realization = {
        "current_report_bucket": "",
        "current_report_bucket_na_reason": "physical_x_must_not_be_directly_mapped_to_market_bucket_b",
    }
    return {
        "ok": True,
        "state": "market_realization_candidate_ready",
        "reported_x_unchanged": True,
        "posterior_checkpoint_id": checkpoint["posterior_checkpoint_id"],
        "posterior_run_id": checkpoint["posterior_run_id"],
        "market_projection": {
            "target_contract_sha256": "1" * 64,
            "bridge_artifact_sha256": "2" * 64,
            "resolution_bridge": {
                "bridge_closed": True,
                "resolution_contract_id": "market-resolution",
                "settlement_scalar_distribution": [{"settlement_value_c": 31, "probability": 0.7}, {"settlement_value_c": 32, "probability": 0.3}],
                "market_bucket_probabilities": {"31C": 0.7, "32C": 0.3},
                "settlement_exclusion_certificates": {"30C or below": {"state": "valid"}},
            },
        },
        "realization_projection": realization,
        "realization_state_row": {
            "realization_state_id": "rstate-1",
            "posterior_run_id": checkpoint["posterior_run_id"],
            "fact_snapshot_id": "factsnap-market",
            "resolution_contract_id": "market-resolution",
        },
    }


def test_market_ssot_keeps_x_and_publishes_b_with_separate_contract_identity() -> None:
    public = materialize_reported_x_market_public_fact_row(
        activation=_activation(),
        checkpoint_row=_checkpoint(),
        strong_update_row=_update(),
        market_realization_candidate=_candidate(),
        fact_snapshot_id="factsnap-market",
        slug="chengdu-high-2026-07-15",
        event_date="2026-07-15",
        fact_as_of="2026-07-15T01:00:01Z",
        correlation_id="corr-market",
    )
    ssot = json.loads(public["fact_json"])["posterior_ssot_fact"]
    assert ssot["reported_physical_x_distribution"] == {"31": 0.7, "32": 0.3}
    assert ssot["market_bucket_probabilities"] == {"31C": 0.7, "32C": 0.3}
    assert ssot["physical_resolution_contract_id"] == "resolution-1"
    assert ssot["resolution_contract_id"] == "market-resolution"
    assert ssot["current_report_bucket"] == ""


def test_market_handoff_rows_bind_same_fact_and_three_second_deadline() -> None:
    checkpoint = _checkpoint()
    rows = materialize_reported_x_market_handoff_rows(
        checkpoint_row=checkpoint,
        market_realization_candidate=_candidate(),
        canonical_observation_event={
            "canonical_observation_event_id": "event-1",
            "first_actionable_seen_at": "2026-07-15T00:59:59Z",
        },
        fact_snapshot_id="factsnap-market",
        correlation_id="corr-market",
        outbox_event_id="outbox-market",
        inference_decision_deadline_at="2026-07-15T01:00:02Z",
    )
    assert rows["ok"] is True
    assert rows["outbox_row"]["fact_snapshot_id"] == "factsnap-market"
    assert rows["outbox_execution_contract_row"]["observation_to_order_hard_deadline_at"] == "2026-07-15T01:00:02.000Z"
    assert rows["places_orders"] is False
