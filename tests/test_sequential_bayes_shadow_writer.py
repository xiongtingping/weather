from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from resolution_bridge_runtime import build_resolution_bridge
from realization_state_projection_runtime import (
    POLICY_SCHEMA_VERSION,
    build_realization_state_projection,
    materialize_projected_realization_state_row,
)
from sequential_bayes_filter_shadow_runtime import build_next_checkpoint, build_strong_observation_update
from sequential_bayes_schema_contract import apply_forward_schema
from sequential_bayes_shadow_writer import (
    ShadowTransactionConflict,
    ShadowTransactionIntegrityError,
    commit_shadow_transaction_b,
    materialize_checkpoint_row,
    materialize_outbox_execution_contract_row,
    materialize_outbox_row,
    materialize_strong_update_row,
)
from strong_observation_lr_artifact_contract import build_strong_observation_lr_input
from temperature_trajectory_feature_runtime import build_empty_pre_event_temperature_trajectory_feature_state
from tests.strong_observation_artifact_fixtures import approved_strong_artifact


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"
PREVIOUS_CHECKPOINT_ID = "pchk_previous_1234567890"
PREVIOUS_RUN_ID = "post_previous_1234567890"
NOW = "2026-07-03T07:00:00Z"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _previous_checkpoint() -> dict:
    joint = [
        {"temperature_c": 29.0, "state": "warming", "probability": 0.5},
        {"temperature_c": 30.0, "state": "warming", "probability": 0.5},
    ]
    return {
        "posterior_checkpoint_id": PREVIOUS_CHECKPOINT_ID,
        "posterior_run_id": PREVIOUS_RUN_ID,
        "target_identity": TARGET,
        "event_date": "2026-07-03",
        "joint_posterior": joint,
        "joint_posterior_sha256": _hash(joint),
        "observed_max_lower_bound_c": 28.5,
        "consumed_event_ids": [],
        "source_event_ids": ["srcevt_previous_1234567890"],
    }


def _seed_previous_checkpoint(conn: sqlite3.Connection, previous: dict) -> None:
    conn.execute(
        """
        INSERT INTO posterior_checkpoints(
            posterior_checkpoint_id, target_identity, posterior_run_id,
            previous_checkpoint_id, checkpoint_kind, forecast_evidence_snapshot_id,
            prior_context_id, resolution_contract_id, last_strong_update_id,
            last_canonical_event_id, canonical_sequence_sha256, temperature_grid_id,
            state_grid_id, joint_posterior_json, joint_posterior_sha256,
            physical_high_distribution_json, settlement_scalar_distribution_json,
            market_bucket_distribution_json, latent_post_state_json,
            trajectory_feature_state_json, source_reliability_state_json,
            observed_max_lower_bound_c, consumed_event_ids_json,
            consumed_ancestor_sha256, model_artifact_sha256, replay_generation,
            supersedes_checkpoint_id, state, created_at, committed_at
        ) VALUES (?, ?, ?, '', 'initial', 'forecast-initial', 'prior-initial',
                  'resolution-v1', '', '', 'ancestor-initial', 'temperature-grid',
                  'state-grid', ?, ?, ?, '{}', '{}', '{}', '{}', ?, ?, '[]',
                  'ancestor-initial', 'artifact-initial', 0, '', 'shadow_ready',
                  '2026-07-03T06:00:00Z', '2026-07-03T06:00:00Z')
        """,
        (
            previous["posterior_checkpoint_id"],
            previous["target_identity"],
            previous["posterior_run_id"],
            _json(previous["joint_posterior"]),
            previous["joint_posterior_sha256"],
            _json({"29": 0.5, "30": 0.5}),
            _json({"warming": 1.0}),
            _json({}),
        ),
    )


def _rows() -> tuple[dict, dict, dict, dict, dict]:
    previous = _previous_checkpoint()
    event = {
        "canonical_observation_event_id": "canobs_current_1234567890",
        "canonical_observation_key": "canokey_current_1234567890",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "selected_source_id": "AMSC",
        "state": "accepted_primary",
        "selection_role": "primary",
        "valid_time": "2026-07-03T06:30:00Z",
        "first_transport_seen_at": "2026-07-03T06:30:30Z",
        "first_actionable_seen_at": "2026-07-03T06:31:00Z",
        "parsed_observation": {"temperature_c": 30.0, "dewpoint_c": 22.0, "cloud_text": "FEW020", "weather_text": ""},
        "quality_flags": [],
    }
    trajectory = build_empty_pre_event_temperature_trajectory_feature_state(
        target_identity=TARGET,
        station_id="ZUUU",
        decision_as_of=NOW,
    )
    assert trajectory["ok"] is True
    strong_input = build_strong_observation_lr_input(
        canonical_observation_event=event,
        trajectory_feature_state=trajectory,
        forecast_evidence_snapshot_id="forecast-current",
        prior_context_id="prior-current",
        source_reliability_pre_state={"AMSC": {"state": "unknown"}},
    )
    assert strong_input["ok"] is True
    update = build_strong_observation_update(
        previous_checkpoint=previous,
        canonical_observation_event=event,
        forecast_evidence_snapshot_id="forecast-current",
        prior_context_id="prior-current",
        approved_lr_artifact=approved_strong_artifact(
            model_artifact_sha256="strong-artifact-current",
            raw_log_score_by_temperature_and_pre_state={
                "29|warming": -0.4,
                "30|warming": 0.4,
            },
        ),
        strong_observation_input=strong_input,
        decision_as_of=NOW,
        deadline_at="2026-07-03T07:00:03Z",
    )
    assert update["ok"] is True and update["state"] == "applied"
    checkpoint = build_next_checkpoint(
        previous_checkpoint=previous,
        strong_update=update,
        fact_snapshot_id="factsnap_current_1234567890",
        resolution_contract_id="resolution-v1",
        committed_at=NOW,
    )
    assert checkpoint["ok"] is True
    resolution_contract = {
        "resolution_contract_id": "resolution-v1",
        "resolution_source_id": "official-source",
        "resolution_local_date": "2026-07-03",
        "settlement_unit": "C",
        "bridge_closed": True,
        "validated": True,
        "physical_to_settlement_kernel": {
            "29": [{"settlement_value_c": 29.0, "probability": 1.0}],
            "30": [{"settlement_value_c": 30.0, "probability": 1.0}],
        },
        "bucket_contract": {
            "29C": {"lower_inclusive": 28.5, "upper_exclusive": 29.5},
            "30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5},
        },
    }
    bridge = build_resolution_bridge(
        physical_high_distribution=checkpoint["physical_high_distribution"],
        resolution_contract=resolution_contract,
    )
    assert bridge["ok"] is True
    update_row = materialize_strong_update_row(
        update,
        source_reliability_before={"AMSC": {"state": "unknown"}},
        source_reliability_after={"AMSC": {"state": "unknown"}},
        path_auxiliary={"shadow_only": True},
        started_at="2026-07-03T06:59:59Z",
        completed_at=NOW,
    )
    checkpoint_row = materialize_checkpoint_row(
        checkpoint,
        resolution_bridge=bridge,
        trajectory_feature_state={"trend": "warming"},
        source_reliability_state={"AMSC": {"state": "unknown"}},
    )
    projection = build_realization_state_projection(
        checkpoint=checkpoint,
        resolution_bridge=bridge,
        resolution_contract=resolution_contract,
        latest_canonical_observation_event=event,
        strong_observation_update=update,
        strong_physical_evidence={
            "schema": "strong_physical_evidence_projection_v1",
            "ok": True,
            "state": "available",
            "canonical_observation_event_id": event["canonical_observation_event_id"],
            "strong_observation_update_id": update["strong_observation_update_id"],
            "model_artifact_sha256": update["model_artifact_sha256"],
            "realization_policy_artifact_sha256": "realization-policy-writer",
            "top_bucket": "30C",
            "top_bucket_joint_strong_support": True,
            "bucket_evidence": {},
            "bucket_exclusions": [],
            "thresholds": {
                "strong_support_log_probability_ratio_floor": 0.0,
                "strong_exclusion_probability_ceiling": 0.01,
                "strong_exclusion_log_probability_ratio_ceiling": -1.0,
            },
        },
        realization_policy={
            "schema": POLICY_SCHEMA_VERSION,
            "artifact_sha256": "realization-policy-writer",
            "approval_state": "approved",
            "credible_probability_floor": 0.05,
            "strong_support_log_probability_ratio_floor": 0.0,
            "strong_exclusion_probability_ceiling": 0.01,
            "strong_exclusion_log_probability_ratio_ceiling": -1.0,
        },
        temperature_quantization_c=1.0,
    )
    assert projection["ok"] is True
    realization_row = materialize_projected_realization_state_row(
        checkpoint_row=checkpoint_row,
        realization_projection=projection,
        fact_snapshot_id="factsnap_current_1234567890",
        created_at=checkpoint_row["created_at"],
        committed_at=checkpoint_row["committed_at"],
        candidate_mode="shadow",
    )
    outbox_row = materialize_outbox_row(
        checkpoint_row,
        fact_snapshot_id="factsnap_current_1234567890",
        correlation_id="corr_current_1234567890",
        outbox_event_id="outbox_current_1234567890",
    )
    execution_contract_row = materialize_outbox_execution_contract_row(
        outbox_row=outbox_row,
        checkpoint_row=checkpoint_row,
        canonical_observation_event=event,
        inference_decision_deadline_at="2026-07-03T07:00:03Z",
    )
    return update_row, checkpoint_row, realization_row, outbox_row, execution_contract_row


def _commit(conn: sqlite3.Connection, rows: tuple[dict, dict, dict, dict, dict], **overrides) -> dict:
    update, checkpoint, realization, outbox, execution_contract = rows
    return commit_shadow_transaction_b(
        conn,
        expected_previous_checkpoint_id=overrides.pop("expected_previous_checkpoint_id", PREVIOUS_CHECKPOINT_ID),
        expected_previous_posterior_run_id=overrides.pop("expected_previous_posterior_run_id", PREVIOUS_RUN_ID),
        strong_update_row=overrides.pop("strong_update_row", update),
        checkpoint_row=overrides.pop("checkpoint_row", checkpoint),
        realization_state_row=overrides.pop("realization_state_row", realization),
        outbox_row=overrides.pop("outbox_row", outbox),
        outbox_execution_contract_row=overrides.pop("outbox_execution_contract_row", execution_contract),
        **overrides,
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    _seed_previous_checkpoint(conn, _previous_checkpoint())
    return conn


def _count(conn: sqlite3.Connection, table: str, key: str, value: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {key}=?", (value,)).fetchone()[0])


def test_transaction_b_commits_all_shadow_rows_atomically_and_is_idempotent() -> None:
    conn = _conn()
    rows = _rows()
    first = _commit(conn, rows)

    assert first["ok"] is True
    assert first["idempotent"] is False
    assert first["shadow_only"] is True
    assert _count(conn, "strong_observation_updates", "strong_observation_update_id", rows[0]["strong_observation_update_id"]) == 1
    assert _count(conn, "posterior_checkpoints", "posterior_checkpoint_id", rows[1]["posterior_checkpoint_id"]) == 1
    assert _count(conn, "realization_states", "realization_state_id", rows[2]["realization_state_id"]) == 1
    assert _count(conn, "posterior_commit_outbox", "outbox_event_id", rows[3]["outbox_event_id"]) == 1
    assert _count(conn, "posterior_outbox_execution_contracts", "outbox_event_id", rows[4]["outbox_event_id"]) == 1
    persisted_contract = conn.execute(
        "SELECT first_actionable_seen_at, observation_to_order_hard_deadline_at, execution_eligible_at_commit, execution_na_reason "
        "FROM posterior_outbox_execution_contracts WHERE outbox_event_id=?",
        (rows[4]["outbox_event_id"],),
    ).fetchone()
    assert tuple(persisted_contract) == (
        "2026-07-03T06:31:00.000Z",
        "2026-07-03T06:31:03.000Z",
        0,
        "transaction_b_committed_after_observation_to_order_hard_deadline",
    )
    assert not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('event_fact_history', 'event_fact_latest')"
    ).fetchall()

    retried = _commit(conn, rows)
    assert retried["ok"] is True
    assert retried["idempotent"] is True


def test_transaction_b_rejects_stale_predecessor_before_any_write() -> None:
    conn = _conn()
    rows = _rows()
    conn.execute(
        """
        INSERT INTO posterior_checkpoints(
            posterior_checkpoint_id, target_identity, posterior_run_id,
            previous_checkpoint_id, checkpoint_kind, forecast_evidence_snapshot_id,
            prior_context_id, resolution_contract_id, last_strong_update_id,
            last_canonical_event_id, canonical_sequence_sha256, temperature_grid_id,
            state_grid_id, joint_posterior_json, joint_posterior_sha256,
            physical_high_distribution_json, settlement_scalar_distribution_json,
            market_bucket_distribution_json, latent_post_state_json,
            trajectory_feature_state_json, source_reliability_state_json,
            observed_max_lower_bound_c, consumed_event_ids_json,
            consumed_ancestor_sha256, model_artifact_sha256, replay_generation,
            supersedes_checkpoint_id, state, created_at, committed_at
        )
        SELECT 'pchk_concurrent_1234567890', target_identity,
               'post_concurrent_1234567890', posterior_checkpoint_id,
               'incremental', forecast_evidence_snapshot_id, prior_context_id,
               resolution_contract_id, 'stupd_concurrent_1234567890',
               'canobs_concurrent_1234567890', canonical_sequence_sha256,
               temperature_grid_id, state_grid_id, joint_posterior_json,
               joint_posterior_sha256, physical_high_distribution_json,
               settlement_scalar_distribution_json, market_bucket_distribution_json,
               latent_post_state_json, trajectory_feature_state_json,
               source_reliability_state_json, observed_max_lower_bound_c,
               consumed_event_ids_json, consumed_ancestor_sha256,
               model_artifact_sha256, 0, '', 'shadow_ready',
               '2026-07-03T06:32:00Z', '2026-07-03T06:32:00Z'
        FROM posterior_checkpoints
        WHERE posterior_checkpoint_id=?
        """,
        (PREVIOUS_CHECKPOINT_ID,),
    )

    with pytest.raises(ShadowTransactionConflict, match="previous_checkpoint_cas_mismatch"):
        _commit(conn, rows)

    assert _count(conn, "strong_observation_updates", "strong_observation_update_id", rows[0]["strong_observation_update_id"]) == 0
    assert _count(conn, "posterior_checkpoints", "posterior_checkpoint_id", rows[1]["posterior_checkpoint_id"]) == 0
    assert _count(conn, "realization_states", "realization_state_id", rows[2]["realization_state_id"]) == 0
    assert _count(conn, "posterior_commit_outbox", "outbox_event_id", rows[3]["outbox_event_id"]) == 0
    assert _count(conn, "posterior_outbox_execution_contracts", "outbox_event_id", rows[4]["outbox_event_id"]) == 0


def test_transaction_b_rejects_changed_previous_posterior_hash_before_any_write() -> None:
    conn = _conn()
    rows = _rows()
    conn.execute(
        "UPDATE posterior_checkpoints SET joint_posterior_sha256='changed-after-inference' WHERE posterior_checkpoint_id=?",
        (PREVIOUS_CHECKPOINT_ID,),
    )

    with pytest.raises(ShadowTransactionConflict, match="previous_posterior_sha256_cas_mismatch"):
        _commit(conn, rows)

    assert _count(conn, "strong_observation_updates", "strong_observation_update_id", rows[0]["strong_observation_update_id"]) == 0
    assert _count(conn, "posterior_checkpoints", "posterior_checkpoint_id", rows[1]["posterior_checkpoint_id"]) == 0


def test_transaction_b_rolls_back_all_prior_shadow_inserts_when_outbox_conflicts() -> None:
    conn = _conn()
    rows = _rows()
    conn.execute(
        """
        INSERT INTO posterior_commit_outbox(
            outbox_event_id, posterior_run_id, fact_snapshot_id, correlation_id,
            state, created_at, attempt_count, claim_generation
        ) VALUES ('outbox_conflicting_existing', ?, 'other-fact', 'other-correlation',
                  'pending', ?, 0, 0)
        """,
        (rows[1]["posterior_run_id"], NOW),
    )

    with pytest.raises(sqlite3.IntegrityError):
        _commit(conn, rows)

    assert _count(conn, "strong_observation_updates", "strong_observation_update_id", rows[0]["strong_observation_update_id"]) == 0
    assert _count(conn, "posterior_checkpoints", "posterior_checkpoint_id", rows[1]["posterior_checkpoint_id"]) == 0
    assert _count(conn, "realization_states", "realization_state_id", rows[2]["realization_state_id"]) == 0
    assert _count(conn, "posterior_commit_outbox", "outbox_event_id", rows[3]["outbox_event_id"]) == 0
    assert _count(conn, "posterior_outbox_execution_contracts", "outbox_event_id", rows[4]["outbox_event_id"]) == 0


def test_transaction_b_rejects_cross_row_binding_mismatch_before_any_write() -> None:
    conn = _conn()
    rows = _rows()
    broken_outbox = {**rows[3], "fact_snapshot_id": "factsnap_wrong"}

    with pytest.raises(ShadowTransactionIntegrityError, match="outbox_fact_binding_mismatch"):
        _commit(conn, rows, outbox_row=broken_outbox)

    assert _count(conn, "strong_observation_updates", "strong_observation_update_id", rows[0]["strong_observation_update_id"]) == 0
