import json
import sqlite3

from production_probability_v6_transaction_commit_runtime import (
    commit_v6_candidate_transaction,
)
from tests.test_production_probability_v6_market_transaction_writer import _setup
from tests.test_reported_x_market_realization_runtime import _artifact, _policy, _target


def _case():
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
        "fact_snapshot_id": "fact-commit-1",
        "slug": "physical-zuuu-2025-04-30",
        "event_date": "2025-04-30",
        "fact_as_of": "2025-04-30T07:30:00.100Z",
        "correlation_id": "corr-commit-1",
    }
    return conn, bridge, common


def test_v6_commit_runtime_keeps_physical_x_live_when_market_is_disabled():
    conn, _bridge, common = _case()
    result = commit_v6_candidate_transaction(
        conn,
        **common,
        market_inputs={
            "state": "physical_only",
            "market_projection_na_reason": "market_projection_context_disabled",
        },
    )

    assert result["ok"] is True
    assert result["writer_route"] == "physical_only"
    assert result["market_projection_available"] is False
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2


def test_v6_commit_runtime_writes_market_transaction_b_atomically_when_approved():
    conn, bridge, common = _case()
    artifact = _artifact()
    target = _target()
    result = commit_v6_candidate_transaction(
        conn,
        **common,
        market_inputs={
            "state": "market_inputs_ready",
            "market_context_result": {
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
            "bridge_activation": bridge,
            "bridge_artifact": artifact,
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
        },
    )

    assert result["ok"] is True
    assert result["writer_route"] == "market_transaction_b"
    assert result["market_projection_available"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
