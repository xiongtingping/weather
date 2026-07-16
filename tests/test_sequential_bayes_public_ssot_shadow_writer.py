from __future__ import annotations

import json
import sqlite3

import pytest

from sequential_bayes_public_ssot_shadow_writer import (
    commit_public_physical_ssot_shadow_transaction,
    commit_public_ssot_shadow_transaction_b,
    initialize_isolated_public_ssot_shadow_schema,
    materialize_public_physical_posterior_fact_row,
    materialize_public_posterior_fact_row,
)
from sequential_bayes_schema_contract import apply_forward_schema
from sequential_bayes_shadow_writer import (
    ShadowTransactionIntegrityError,
    materialize_outbox_execution_contract_row,
)


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"
PREVIOUS_CHECKPOINT = "pchk_public_previous_123456"
PREVIOUS_RUN = "post_public_previous_123456"
NOW = "2026-07-03T07:00:00Z"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _seed_previous(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO posterior_checkpoints(
            posterior_checkpoint_id,target_identity,posterior_run_id,previous_checkpoint_id,
            checkpoint_kind,forecast_evidence_snapshot_id,prior_context_id,resolution_contract_id,
            last_strong_update_id,last_canonical_event_id,canonical_sequence_sha256,
            temperature_grid_id,state_grid_id,joint_posterior_json,joint_posterior_sha256,
            physical_high_distribution_json,settlement_scalar_distribution_json,
            market_bucket_distribution_json,latent_post_state_json,trajectory_feature_state_json,
            source_reliability_state_json,observed_max_lower_bound_c,consumed_event_ids_json,
            consumed_ancestor_sha256,model_artifact_sha256,replay_generation,supersedes_checkpoint_id,
            state,created_at,committed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            PREVIOUS_CHECKPOINT, TARGET, PREVIOUS_RUN, "", "initial", "forecast-prev", "prior-prev", "resolution-v1",
            "", "", "sequence-prev", "temp-grid", "state-grid", "[]", "previous-hash", "{}", "[]", "{}", "{}", "{}",
            "{}", None, "[]", "ancestor-prev", "artifact-prev", 0, "", "shadow_ready", "2026-07-03T06:00:00Z", "2026-07-03T06:00:00Z",
        ),
    )


def _rows() -> tuple[dict, dict, dict, dict, dict]:
    update = {
        "strong_observation_update_id": "stupd_public_1234567890",
        "target_identity": TARGET,
        "previous_posterior_run_id": PREVIOUS_RUN,
        "previous_posterior_sha256": "previous-hash",
        "previous_checkpoint_id": PREVIOUS_CHECKPOINT,
        "canonical_observation_event_id": "canobs_public_1234567890",
        "forecast_evidence_snapshot_id": "forecast-current",
        "prior_context_id": "prior-current",
        "model_artifact_sha256": "artifact-current",
        "decision_as_of": NOW,
        "state": "applied",
        "raw_log_score_json": "[]",
        "raw_log_score_sha256": "raw-score-hash",
        "normalizer_offset": 0.0,
        "posterior_delta_json": "[]",
        "posterior_delta_sha256": "delta-hash",
        "pre_state_transition_json": "{}",
        "post_state_transition_json": "{}",
        "source_reliability_before_json": "{}",
        "source_reliability_after_json": "{}",
        "path_auxiliary_json": "{}",
        "observed_max_lower_bound_c": 29.5,
        "quality_json": "{}",
        "block_reasons_json": "[]",
        "started_at": "2026-07-03T06:59:59Z",
        "completed_at": NOW,
        "created_at": NOW,
    }
    checkpoint = {
        "posterior_checkpoint_id": "pchk_public_current_123456",
        "target_identity": TARGET,
        "posterior_run_id": "post_public_current_123456",
        "previous_checkpoint_id": PREVIOUS_CHECKPOINT,
        "checkpoint_kind": "incremental",
        "forecast_evidence_snapshot_id": "forecast-current",
        "prior_context_id": "prior-current",
        "resolution_contract_id": "resolution-v1",
        "last_strong_update_id": update["strong_observation_update_id"],
        "last_canonical_event_id": update["canonical_observation_event_id"],
        "canonical_sequence_sha256": "sequence-current",
        "temperature_grid_id": "temp-grid",
        "state_grid_id": "state-grid",
        "joint_posterior_json": "[]",
        "joint_posterior_sha256": "joint-current-hash",
        "physical_high_distribution_json": _json({"30": 1.0}),
        "settlement_scalar_distribution_json": _json([{"settlement_value_c": 30.0, "probability": 1.0}]),
        "market_bucket_distribution_json": _json({"30C": 1.0}),
        "latent_post_state_json": _json({"warming": 1.0}),
        "trajectory_feature_state_json": "{}",
        "source_reliability_state_json": "{}",
        "observed_max_lower_bound_c": 29.5,
        "consumed_event_ids_json": _json([update["canonical_observation_event_id"]]),
        "consumed_ancestor_sha256": "ancestor-current",
        "model_artifact_sha256": "artifact-current",
        "replay_generation": 0,
        "supersedes_checkpoint_id": "",
        "state": "shadow_ready",
        "created_at": NOW,
        "committed_at": NOW,
    }
    realization = {
        "realization_state_id": "rstate_public_current_123456",
        "posterior_run_id": checkpoint["posterior_run_id"],
        "fact_snapshot_id": "factsnap_public_current_123456",
        "resolution_contract_id": "resolution-v1",
        "target_bucket": "30C",
        "second_target_bucket": "",
        "credible_bucket_set_json": _json(["30C"]),
        "current_report_bucket": "",
        "current_report_bucket_probability": None,
        "probability_above_current_report_bucket": None,
        "current_report_bucket_hard_zero": 0,
        "settlement_exclusion_certificates_json": "{}",
        "state_json": "{}",
        "state_sha256": "state-current-hash",
        "created_at": NOW,
        "committed_at": NOW,
    }
    outbox = {
        "outbox_event_id": "outbox_public_current_123456",
        "posterior_run_id": checkpoint["posterior_run_id"],
        "fact_snapshot_id": realization["fact_snapshot_id"],
        "correlation_id": "corr_public_current_123456",
        "state": "pending",
        "created_at": NOW,
        "attempt_count": 0,
        "claim_generation": 0,
    }
    execution_contract = materialize_outbox_execution_contract_row(
        outbox_row=outbox,
        checkpoint_row=checkpoint,
        canonical_observation_event={
            "canonical_observation_event_id": update["canonical_observation_event_id"],
            "first_actionable_seen_at": "2026-07-03T06:59:58Z",
        },
        inference_decision_deadline_at="2026-07-03T07:00:03Z",
    )
    return update, checkpoint, realization, outbox, execution_contract


def _conn(*, isolated: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    if isolated:
        initialize_isolated_public_ssot_shadow_schema(conn)
    _seed_previous(conn)
    return conn


def _public(checkpoint: dict, realization: dict, outbox: dict) -> dict:
    return materialize_public_posterior_fact_row(
        checkpoint_row=checkpoint,
        realization_state_row=realization,
        slug=TARGET,
        event_date="2026-07-03",
        fact_as_of=NOW,
        correlation_id=outbox["correlation_id"],
    )


def _commit(conn: sqlite3.Connection, rows: tuple[dict, dict, dict, dict, dict], **overrides) -> dict:
    update, checkpoint, realization, outbox, execution_contract = rows
    return commit_public_ssot_shadow_transaction_b(
        conn,
        expected_previous_checkpoint_id=PREVIOUS_CHECKPOINT,
        expected_previous_posterior_run_id=PREVIOUS_RUN,
        strong_update_row=update,
        checkpoint_row=checkpoint,
        realization_state_row=realization,
        outbox_row=outbox,
        outbox_execution_contract_row=execution_contract,
        public_fact_row=_public(checkpoint, realization, outbox),
        **overrides,
    )


def test_public_ssot_transaction_b_commits_p1_history_latest_and_outbox_as_one_shadow_unit() -> None:
    conn = _conn()
    rows = _rows()
    result = _commit(conn, rows)

    assert result["ok"] is True
    assert result["idempotent"] is False
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_outbox_execution_contracts").fetchone()[0] == 1
    assert tuple(conn.execute(
        "SELECT observation_to_order_hard_deadline_at, execution_eligible_at_commit FROM posterior_outbox_execution_contracts"
    ).fetchone()) == ("2026-07-03T07:00:01.000Z", 1)
    fact = conn.execute("SELECT id, fact_json FROM event_fact_history").fetchone()
    latest = conn.execute("SELECT fact_id, fact_snapshot_id FROM event_fact_latest").fetchone()
    assert tuple(latest) == (fact[0], rows[2]["fact_snapshot_id"])
    assert json.loads(fact[1])["posterior_ssot_fact"]["posterior_run_id"] == rows[1]["posterior_run_id"]

    retry = _commit(conn, rows)
    assert retry["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1


def test_public_ssot_fault_after_history_rolls_back_p1_history_latest_and_outbox() -> None:
    conn = _conn()
    rows = _rows()

    with pytest.raises(RuntimeError, match="public_ssot_fault_after_history"):
        _commit(conn, rows, fault_after="history")

    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_outbox_execution_contracts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_fact_latest").fetchone()[0] == 0


def test_physical_only_public_ssot_fault_rolls_back_update_checkpoint_history_and_latest() -> None:
    conn = _conn()
    update, checkpoint, _realization, _outbox, _execution_contract = _rows()
    checkpoint = dict(checkpoint)
    checkpoint["settlement_scalar_distribution_json"] = "[]"
    checkpoint["market_bucket_distribution_json"] = "{}"
    public = materialize_public_physical_posterior_fact_row(
        checkpoint_row=checkpoint,
        peak_timing_state={
            "schema": "peak_timing_state_l3_v1",
            "ok": True,
            "state": "na",
            "na_reason": "forecast_peak_timing_prior_missing",
            "canonical_observation_event_id": checkpoint["last_canonical_event_id"],
            "strong_observation_update_id": checkpoint["last_strong_update_id"],
            "entry_peak_at": "",
            "does_not_update_physical_x": True,
        },
        fact_snapshot_id="factsnap_public_physical_123456",
        slug=TARGET,
        event_date="2026-07-03",
        fact_as_of=NOW,
        correlation_id="corr_public_physical_123456",
        market_bucket_probabilities_na_reason="x_to_s_to_b_bridge_unavailable",
    )
    assert json.loads(public["fact_json"])["posterior_ssot_fact"]["peak_timing_state"]["state"] == "na"

    with pytest.raises(RuntimeError, match="public_physical_ssot_fault_after_history"):
        commit_public_physical_ssot_shadow_transaction(
            conn,
            expected_previous_checkpoint_id=PREVIOUS_CHECKPOINT,
            expected_previous_posterior_run_id=PREVIOUS_RUN,
            strong_update_row=update,
            checkpoint_row=checkpoint,
            public_fact_row=public,
            fault_after="history",
        )

    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_fact_latest").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0


def test_public_ssot_writer_refuses_nonisolated_database_before_any_write() -> None:
    conn = _conn(isolated=False)
    rows = _rows()

    with pytest.raises(ShadowTransactionIntegrityError, match="public_ssot_shadow_manifest_missing"):
        _commit(conn, rows)

    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
