from __future__ import annotations

import hashlib
import json

import pytest

from production_reported_x_initial_writer import commit_reported_x_initial_posterior
from production_reported_x_market_ssot_runtime import (
    materialize_reported_x_market_handoff_rows,
    materialize_reported_x_market_public_fact_row,
)
from production_reported_x_market_transaction_writer import commit_reported_x_market_transaction_b
from production_sequential_bayes_public_ssot_writer import ProductionTransactionIntegrityError
from lineage_identity_runtime import build_realization_state_id
from tests.test_production_reported_x_initial_writer import _activation, _candidate, _successor_conn


NOW = "2026-07-03T07:00:01Z"


def _setup():
    conn = _successor_conn()
    commit_reported_x_initial_posterior(
        conn,
        activation=_activation(),
        initial_checkpoint=_candidate(),
        slug="zuuu-2026-07-03",
        event_date="2026-07-03",
        fact_as_of="2026-07-03T06:00:00Z",
        correlation_id="reported-x-p0",
    )
    conn.row_factory = None
    fields = [row[1] for row in conn.execute("PRAGMA table_info(posterior_checkpoints)")]
    previous = conn.execute(
        f"SELECT {','.join(fields)} FROM posterior_checkpoints ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    previous = dict(zip(fields, previous))
    checkpoint = dict(previous)
    checkpoint.update({
        "posterior_checkpoint_id": "pchk-market-current",
        "posterior_run_id": "post-market-current",
        "previous_checkpoint_id": previous["posterior_checkpoint_id"],
        "checkpoint_kind": "incremental",
        "last_strong_update_id": "stupd-market-current",
        "last_canonical_event_id": "canobs-market-current",
        "consumed_event_ids_json": json.dumps(["canobs-market-current"]),
        "state": "production_ready",
        "created_at": NOW,
        "committed_at": NOW,
    })
    update = {
        "strong_observation_update_id": "stupd-market-current",
        "target_identity": checkpoint["target_identity"],
        "previous_posterior_run_id": previous["posterior_run_id"],
        "previous_posterior_sha256": previous["joint_posterior_sha256"],
        "previous_checkpoint_id": previous["posterior_checkpoint_id"],
        "canonical_observation_event_id": "canobs-market-current",
        "forecast_evidence_snapshot_id": checkpoint["forecast_evidence_snapshot_id"],
        "prior_context_id": checkpoint["prior_context_id"],
        "model_artifact_sha256": checkpoint["model_artifact_sha256"],
        "decision_as_of": NOW,
        "state": "applied",
        "raw_log_score_json": "[]",
        "raw_log_score_sha256": "1" * 64,
        "normalizer_offset": 0.0,
        "posterior_delta_json": "[]",
        "posterior_delta_sha256": "2" * 64,
        "pre_state_transition_json": "{}",
        "post_state_transition_json": "{}",
        "source_reliability_before_json": "{}",
        "source_reliability_after_json": "{}",
        "path_auxiliary_json": json.dumps({
            "remaining_warming_marginal": [],
            "termination_risk": {
                "schema": "termination_risk_marginal_v2_reported_x",
                "state": "available",
                "canonical_observation_event_id": "canobs-market-current",
                "updates_posterior": False,
            },
            "peak_timing_state": {
                "schema": "peak_timing_state_l3_v1",
                "ok": True,
                "state": "na",
                "canonical_observation_event_id": "canobs-market-current",
                "strong_observation_update_id": "stupd-market-current",
            },
            "strong_likelihood_update_identity": {
                "schema": "strong_likelihood_cumulative_replacement_identity_v1",
                "likelihood_update_kind": "cumulative_evidence_replacement_delta_log_lr_v1",
            },
        }),
        "observed_max_lower_bound_c": checkpoint["reported_x_lower_bound_c"],
        "quality_json": "{}",
        "block_reasons_json": "[]",
        "started_at": "2026-07-03T07:00:00Z",
        "completed_at": NOW,
        "created_at": NOW,
    }
    projection = {
        "posterior_run_id": checkpoint["posterior_run_id"],
        "posterior_checkpoint_id": checkpoint["posterior_checkpoint_id"],
        "physical_resolution_contract_id": checkpoint["resolution_contract_id"],
        "resolution_contract_id": "market-resolution",
        "current_report_bucket": "",
        "target_bucket": "30C",
        "market_bucket_probabilities": {"30C": 1.0},
    }
    state_payload = {"schema": "realization_state_l3_v2", "projection": projection}
    state_json = json.dumps(
        state_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    state_sha256 = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
    realization_state_id = build_realization_state_id(
        posterior_run_id=checkpoint["posterior_run_id"],
        fact_snapshot_id="factsnap-market-current",
        state_sha256=state_sha256,
    )
    realization = {
        "realization_state_id": realization_state_id,
        "posterior_run_id": checkpoint["posterior_run_id"],
        "fact_snapshot_id": "factsnap-market-current",
        "resolution_contract_id": "market-resolution",
        "target_bucket": "30C",
        "second_target_bucket": "",
        "credible_bucket_set_json": json.dumps(["30C"]),
        "current_report_bucket": "",
        "current_report_bucket_probability": None,
        "probability_above_current_report_bucket": None,
        "current_report_bucket_hard_zero": 0,
        "settlement_exclusion_certificates_json": "{}",
        "state_json": state_json,
        "state_sha256": state_sha256,
        "created_at": NOW,
        "committed_at": NOW,
    }
    candidate = {
        "ok": True,
        "state": "market_realization_candidate_ready",
        "reported_x_unchanged": True,
        "posterior_checkpoint_id": checkpoint["posterior_checkpoint_id"],
        "posterior_run_id": checkpoint["posterior_run_id"],
        "market_projection": {
            "target_contract_sha256": "4" * 64,
            "bridge_artifact_sha256": "5" * 64,
            "resolution_bridge": {
                "bridge_closed": True,
                "resolution_contract_id": "market-resolution",
                "settlement_scalar_distribution": [{"settlement_value_c": 30.0, "probability": 1.0}],
                "market_bucket_probabilities": {"30C": 1.0},
                "settlement_exclusion_certificates": {},
            },
        },
        "realization_projection": projection,
        "realization_state_row": realization,
    }
    event = {
        "canonical_observation_event_id": "canobs-market-current",
        "first_actionable_seen_at": "2026-07-03T07:00:00Z",
    }
    handoff = materialize_reported_x_market_handoff_rows(
        checkpoint_row=checkpoint,
        market_realization_candidate=candidate,
        canonical_observation_event=event,
        fact_snapshot_id="factsnap-market-current",
        correlation_id="corr-market-current",
        outbox_event_id="outbox-market-current",
        inference_decision_deadline_at="2026-07-03T07:00:03Z",
    )
    public = materialize_reported_x_market_public_fact_row(
        activation=_activation(),
        checkpoint_row=checkpoint,
        strong_update_row=update,
        market_realization_candidate=candidate,
        fact_snapshot_id="factsnap-market-current",
        slug="zuuu-2026-07-03",
        event_date="2026-07-03",
        fact_as_of=NOW,
        correlation_id="corr-market-current",
    )
    bridge_activation = {
        "schema": "resolution_bridge_production_activation_v1",
        "state": "production_enabled",
        "artifact_path": "config/artifacts/resolution_bridge/approved.json",
        "artifact_sha256": "5" * 64,
        "approval_id": "ADR-XSB-TEST",
        "approval_receipt_sha256": "6" * 64,
        "places_orders": False,
        "authorizes_clob": False,
    }
    return conn, previous, update, checkpoint, candidate, handoff, public, bridge_activation


def test_successor_market_transaction_commits_probability_realization_outbox_and_public_atomically() -> None:
    conn, previous, update, checkpoint, candidate, handoff, public, bridge_activation = _setup()
    result = commit_reported_x_market_transaction_b(
        conn,
        probability_activation=_activation(),
        bridge_activation=bridge_activation,
        expected_previous_checkpoint_id=previous["posterior_checkpoint_id"],
        expected_previous_posterior_run_id=previous["posterior_run_id"],
        strong_update_row=update,
        checkpoint_row=checkpoint,
        realization_state_row=handoff["realization_state_row"],
        outbox_row=handoff["outbox_row"],
        outbox_execution_contract_row=handoff["outbox_execution_contract_row"],
        public_fact_row=public,
    )
    assert result["ok"] is True
    assert result["places_orders"] is False
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1
    payload = json.loads(conn.execute(
        "SELECT fact_json FROM event_fact_history WHERE fact_snapshot_id='factsnap-market-current'"
    ).fetchone()[0])["posterior_ssot_fact"]
    assert payload["market_bucket_probabilities"] == {"30C": 1.0}
    assert payload["current_report_bucket"] == ""


def test_successor_market_transaction_rolls_back_everything_when_public_insert_fails() -> None:
    conn, previous, update, checkpoint, candidate, handoff, public, bridge_activation = _setup()
    public["fact_json"] = None
    with pytest.raises(Exception):
        commit_reported_x_market_transaction_b(
            conn,
            probability_activation=_activation(),
            bridge_activation=bridge_activation,
            expected_previous_checkpoint_id=previous["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=previous["posterior_run_id"],
            strong_update_row=update,
            checkpoint_row=checkpoint,
            realization_state_row=handoff["realization_state_row"],
            outbox_row=handoff["outbox_row"],
            outbox_execution_contract_row=handoff["outbox_execution_contract_row"],
            public_fact_row=public,
        )
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1


def test_successor_market_transaction_rejects_direct_report_bucket_before_write() -> None:
    conn, previous, update, checkpoint, candidate, handoff, public, bridge_activation = _setup()
    payload = json.loads(handoff["realization_state_row"]["state_json"])
    payload["projection"]["current_report_bucket"] = "30C"
    handoff["realization_state_row"]["state_json"] = json.dumps(payload)
    with pytest.raises(ProductionTransactionIntegrityError, match="realization_binding"):
        commit_reported_x_market_transaction_b(
            conn,
            probability_activation=_activation(),
            bridge_activation=bridge_activation,
            expected_previous_checkpoint_id=previous["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=previous["posterior_run_id"],
            strong_update_row=update,
            checkpoint_row=checkpoint,
            realization_state_row=handoff["realization_state_row"],
            outbox_row=handoff["outbox_row"],
            outbox_execution_contract_row=handoff["outbox_execution_contract_row"],
            public_fact_row=public,
        )


def test_successor_market_transaction_exact_retry_is_idempotent() -> None:
    conn, previous, update, checkpoint, candidate, handoff, public, bridge_activation = _setup()
    kwargs = {
        "probability_activation": _activation(),
        "bridge_activation": bridge_activation,
        "expected_previous_checkpoint_id": previous["posterior_checkpoint_id"],
        "expected_previous_posterior_run_id": previous["posterior_run_id"],
        "strong_update_row": update,
        "checkpoint_row": checkpoint,
        "realization_state_row": handoff["realization_state_row"],
        "outbox_row": handoff["outbox_row"],
        "outbox_execution_contract_row": handoff["outbox_execution_contract_row"],
        "public_fact_row": public,
    }
    first = commit_reported_x_market_transaction_b(conn, **kwargs)
    second = commit_reported_x_market_transaction_b(conn, **kwargs)
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1


def test_successor_market_transaction_rejects_tampered_public_x_distribution() -> None:
    conn, previous, update, checkpoint, candidate, handoff, public, bridge_activation = _setup()
    payload = json.loads(public["fact_json"])
    payload["posterior_ssot_fact"]["reported_physical_x_distribution"] = {"99": 1.0}
    public["fact_json"] = json.dumps(payload)
    with pytest.raises(ProductionTransactionIntegrityError, match="public_ssot_binding"):
        commit_reported_x_market_transaction_b(
            conn,
            probability_activation=_activation(),
            bridge_activation=bridge_activation,
            expected_previous_checkpoint_id=previous["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=previous["posterior_run_id"],
            strong_update_row=update,
            checkpoint_row=checkpoint,
            realization_state_row=handoff["realization_state_row"],
            outbox_row=handoff["outbox_row"],
            outbox_execution_contract_row=handoff["outbox_execution_contract_row"],
            public_fact_row=public,
        )


def test_successor_market_transaction_rejects_wrong_previous_posterior_sha_and_rolls_back() -> None:
    conn, previous, update, checkpoint, candidate, handoff, public, bridge_activation = _setup()
    update["previous_posterior_sha256"] = "f" * 64
    with pytest.raises(ProductionTransactionIntegrityError, match="previous_posterior_sha256"):
        commit_reported_x_market_transaction_b(
            conn,
            probability_activation=_activation(),
            bridge_activation=bridge_activation,
            expected_previous_checkpoint_id=previous["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=previous["posterior_run_id"],
            strong_update_row=update,
            checkpoint_row=checkpoint,
            realization_state_row=handoff["realization_state_row"],
            outbox_row=handoff["outbox_row"],
            outbox_execution_contract_row=handoff["outbox_execution_contract_row"],
            public_fact_row=public,
        )
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
