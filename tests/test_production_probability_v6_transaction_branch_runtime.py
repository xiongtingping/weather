import json
import sqlite3

from production_probability_v6_transaction_branch_runtime import (
    build_v6_transaction_branch_bundle,
)
from tests.test_production_probability_v6_market_transaction_writer import _setup
from tests.test_reported_x_market_realization_runtime import _artifact, _policy, _target


def _inputs():
    conn, activation, previous, built, bridge = _setup()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM canonical_observation_events WHERE canonical_observation_event_id='event-1'"
    ).fetchone()
    event = dict(row)
    event["parsed_observation"] = json.loads(event["parsed_observation_json"])
    candidate = {
        "ok": True,
        "candidate_mode": "production_successor",
        "shadow_only": False,
        "strong_update_row": built["strong_update_row"],
        "checkpoint_row": built["checkpoint_row"],
    }
    common = {
        "activation": activation,
        "previous_checkpoint": previous,
        "candidate": candidate,
        "canonical_observation_event": event,
        "physical_resolution_contract": {
            "resolution_contract_id": "physical-resolution",
            "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
            "authority_contract_sha256": "a" * 64,
            "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
            "resolution_local_date": "2025-04-30",
            "settlement_unit": "C",
            "bridge_closed": False,
            "validated": False,
        },
        "realization_policy": _policy(),
        "fact_snapshot_id": "fact-branch-1",
        "slug": "physical-zuuu-2025-04-30",
        "event_date": "2025-04-30",
        "fact_as_of": "2025-04-30T07:30:00.100Z",
        "correlation_id": "corr-branch-1",
        "outbox_event_id": "outbox-branch-1",
        "inference_decision_deadline_at": "2025-04-30T07:30:03Z",
    }
    return conn, bridge, common


def test_disabled_market_context_builds_physical_bundle_with_explicit_na():
    _conn, bridge, common = _inputs()
    result = build_v6_transaction_branch_bundle(
        **common,
        market_context_result={
            "ok": True,
            "state": "disabled",
            "market_projection_available": False,
            "na_reason": "market_projection_context_disabled",
            "blocks_physical_x": False,
        },
        bridge_activation=bridge,
        bridge_artifact=_artifact(),
    )

    assert result["writer_route"] == "physical_only"
    assert result["creates_outbox"] is False
    assert result["blocks_physical_x"] is False
    assert result["market_projection_na_reason"] == "market_projection_context_disabled"


def test_invalid_market_binding_falls_back_only_to_physical_bundle():
    _conn, bridge, common = _inputs()
    context = {
        "market_target_contract": _target(),
        "target_contract_sha256": _target()["target_contract_sha256"],
        "bridge_artifact_sha256": "f" * 64,
        "slug": "chengdu-high-2025-04-30",
    }
    result = build_v6_transaction_branch_bundle(
        **common,
        market_context_result={
            "ok": True,
            "state": "available",
            "market_projection_available": True,
            "market_context": context,
        },
        bridge_activation=bridge,
        bridge_artifact=_artifact(),
    )

    assert result["writer_route"] == "physical_only"
    assert result["creates_outbox"] is False
    assert result["market_block_reasons"] == ["v6_market_branch_content_binding_mismatch"]


def test_approved_exact_market_binding_builds_market_transaction_bundle():
    _conn, bridge, common = _inputs()
    artifact = _artifact()
    target = _target()
    context = {
        "market_target_contract": target,
        "target_contract_sha256": target["target_contract_sha256"],
        "bridge_artifact_sha256": artifact["artifact_sha256"],
        "slug": "chengdu-high-2025-04-30",
    }
    result = build_v6_transaction_branch_bundle(
        **common,
        market_context_result={
            "ok": True,
            "state": "available",
            "market_projection_available": True,
            "market_context": context,
        },
        bridge_activation=bridge,
        bridge_artifact=artifact,
    )

    assert result["writer_route"] == "market_transaction_b"
    assert result["creates_outbox"] is True
    assert result["reported_x_unchanged"] is True
    assert result["places_orders"] is False


def test_tampered_physical_resolution_binding_disables_only_market_projection():
    _conn, bridge, common = _inputs()
    artifact = _artifact()
    target = _target()
    common["physical_resolution_contract"] = {
        **common["physical_resolution_contract"],
        "authority_contract_sha256": "f" * 64,
    }
    result = build_v6_transaction_branch_bundle(
        **common,
        market_context_result={
            "ok": True,
            "state": "available",
            "market_projection_available": True,
            "market_context": {
                "market_target_contract": target,
                "target_contract_sha256": target["target_contract_sha256"],
                "bridge_artifact_sha256": artifact["artifact_sha256"],
                "slug": "chengdu-high-2025-04-30",
            },
        },
        bridge_activation=bridge,
        bridge_artifact=artifact,
    )

    assert result["writer_route"] == "physical_only"
    assert result["blocks_physical_x"] is False
    assert result["creates_outbox"] is False
    assert result["market_projection_na_reason"].startswith(
        "market_projection_branch_blocked"
    )
