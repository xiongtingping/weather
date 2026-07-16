from __future__ import annotations

import json
import sqlite3

import pytest

from production_sequential_bayes_public_ssot_writer import (
    ACTIVATION_SCHEMA,
    ProductionTransactionIntegrityError,
    commit_production_public_physical_ssot_transaction,
    commit_production_public_ssot_transaction_b,
    materialize_production_public_physical_posterior_fact_row,
    materialize_production_public_posterior_fact_row,
)
from production_sequential_bayes_transaction_b_host import ProductionSequentialBayesTransactionBHost
from production_initial_posterior_writer import initialize_production_initial_posterior_schema
from production_probability_activation_contract import validate_production_probability_activation
from sequential_bayes_schema_contract import apply_forward_schema
from sequential_bayes_shadow_writer import materialize_outbox_execution_contract_row


TARGET, PREV_CHECKPOINT, PREV_RUN, NOW = "zuuu-2026-07-03", "pchk-prev", "post-prev", "2026-07-03T07:00:00Z"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _activation() -> dict:
    return {"schema": ACTIVATION_SCHEMA, "state": "production_enabled", "production_transaction_b_authorized": True, "unique_probability_mainline": True, "legacy_probability_writer_disabled": True, "no_order_validation_accepted": True, "cutover_approval_id": "adr-approved", "activation_effective_at": "2026-07-03T00:00:00Z", "prior_model_artifact_sha256": "a" * 64, "weak_likelihood_artifact_sha256": "b" * 64, "strong_lr_artifact_sha256": "c" * 64, "runtime_manifest_sha256": "d" * 64, "legacy_writer_retirement_receipt_sha256": "e" * 64, "bayes_formula_id": "p0_prior_x_weak_once_then_canonical_event_raw_log_lr_then_weak_revision_replacement_log_lr_v1", "composer_schema": "sequential_bayes_filter_v3", "checkpoint_schema": "posterior_checkpoint_v2", "strong_update_schema": "strong_observation_update_v3", "public_ssot_schema": "sequential_temperature_posterior_ssot_v2", "places_orders": False, "authorizes_clob": False}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_production_initial_posterior_schema(conn)
    conn.executescript("""
        CREATE TABLE event_fact_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT NOT NULL, slug TEXT NOT NULL,
          event_date TEXT NOT NULL, fact_as_of TEXT NOT NULL, composer_version TEXT NOT NULL,
          fact_json TEXT NOT NULL, created_at TEXT NOT NULL, correlation_id TEXT,
          fact_snapshot_id TEXT, posterior_run_id TEXT
        );
        CREATE UNIQUE INDEX event_fact_history_snapshot ON event_fact_history(fact_snapshot_id)
          WHERE fact_snapshot_id IS NOT NULL AND fact_snapshot_id<>'';
        CREATE TABLE event_fact_latest (
          slug TEXT NOT NULL,event_date TEXT NOT NULL,fact_id INTEGER NOT NULL,updated_at TEXT NOT NULL,
          fact_snapshot_id TEXT,correlation_id TEXT,PRIMARY KEY(slug,event_date)
        );
    """)
    conn.execute("""INSERT INTO posterior_checkpoints(
      posterior_checkpoint_id,target_identity,posterior_run_id,previous_checkpoint_id,checkpoint_kind,
      forecast_evidence_snapshot_id,prior_context_id,resolution_contract_id,last_strong_update_id,last_canonical_event_id,
      canonical_sequence_sha256,temperature_grid_id,state_grid_id,joint_posterior_json,joint_posterior_sha256,
      physical_high_distribution_json,settlement_scalar_distribution_json,market_bucket_distribution_json,
      latent_post_state_json,trajectory_feature_state_json,source_reliability_state_json,observed_max_lower_bound_c,
      consumed_event_ids_json,consumed_ancestor_sha256,model_artifact_sha256,replay_generation,supersedes_checkpoint_id,state,created_at,committed_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
      PREV_CHECKPOINT,TARGET,PREV_RUN,"","initial","forecast-prev","prior-prev","resolution-v1","","","sequence-prev","temp","state","[]","previous-hash","{}","[]","{}","{}","{}","{}",None,"[]","ancestor-prev","c" * 64,0,"","committed",NOW,NOW,
    ))
    authorized = validate_production_probability_activation(_activation())
    conn.execute(
        """INSERT INTO production_initial_posterior_lineage(
          posterior_checkpoint_id,target_identity,posterior_run_id,prior_context_id,
          prior_artifact_sha256,prior_model_artifact_sha256,weak_likelihood_artifact_sha256,weak_likelihood_sha256,
          forecast_evidence_snapshot_id,strong_lr_artifact_sha256,
          source_reliability_initial_state_sha256,resolution_contract_id,
          initial_evidence_sha256,activation_contract_sha256,lineage_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (PREV_CHECKPOINT,TARGET,PREV_RUN,"prior-prev","0" * 64,"a" * 64,"b" * 64,"f" * 64,
         "forecast-prev","c" * 64,"1" * 64,"resolution-v1","2" * 64,
         authorized["activation_contract_sha256"],"{}",NOW),
    )
    conn.commit()
    return conn


def test_fact_snapshot_identity_lookup_uses_the_partial_index_contract() -> None:
    conn = _conn()
    plan = conn.execute(
        """EXPLAIN QUERY PLAN
           SELECT id FROM event_fact_history
           WHERE fact_snapshot_id=? AND fact_snapshot_id<>'' LIMIT 1""",
        ("factsnap-current",),
    ).fetchall()

    assert any("event_fact_history_snapshot" in str(row[3]) for row in plan)


def _rows() -> tuple[dict, dict, dict, dict, dict]:
    update = {"strong_observation_update_id":"update-current","target_identity":TARGET,"previous_posterior_run_id":PREV_RUN,"previous_posterior_sha256":"previous-hash","previous_checkpoint_id":PREV_CHECKPOINT,"canonical_observation_event_id":"event-current","forecast_evidence_snapshot_id":"forecast-prev","prior_context_id":"prior-prev","model_artifact_sha256":"c" * 64,"decision_as_of":NOW,"state":"applied","raw_log_score_json":"[]","raw_log_score_sha256":"raw","normalizer_offset":0.0,"posterior_delta_json":"[]","posterior_delta_sha256":"delta","pre_state_transition_json":"{}","post_state_transition_json":"{}","source_reliability_before_json":"{}","source_reliability_after_json":"{}","path_auxiliary_json":_json({"remaining_warming_marginal":[{"remaining_warming_c":0.0,"probability":1.0}],"termination_risk":{"schema":"termination_risk_marginal_v1","state":"available","canonical_observation_event_id":"event-current","probability_no_material_new_daily_high":1.0,"updates_posterior":False},"peak_timing_state":{"schema":"peak_timing_state_l3_v1","ok":True,"state":"na","na_reason":"forecast_peak_timing_prior_missing","canonical_observation_event_id":"event-current","strong_observation_update_id":"update-current","entry_peak_at":"","does_not_update_physical_x":True}}),"observed_max_lower_bound_c":29.5,"quality_json":"{}","block_reasons_json":"[]","started_at":"2026-07-03T06:59:59Z","completed_at":NOW,"created_at":NOW}
    checkpoint = {"posterior_checkpoint_id":"pchk-current","target_identity":TARGET,"posterior_run_id":"post-current","previous_checkpoint_id":PREV_CHECKPOINT,"checkpoint_kind":"incremental","forecast_evidence_snapshot_id":"forecast-prev","prior_context_id":"prior-prev","resolution_contract_id":"resolution-v1","last_strong_update_id":"update-current","last_canonical_event_id":"event-current","canonical_sequence_sha256":"sequence-current","temperature_grid_id":"temp","state_grid_id":"state","joint_posterior_json":"[]","joint_posterior_sha256":"joint-current","physical_high_distribution_json":_json({"30":1.0}),"settlement_scalar_distribution_json":_json([{"settlement_value_c":30.0,"probability":1.0}]),"market_bucket_distribution_json":_json({"30C":1.0}),"latent_post_state_json":"{}","trajectory_feature_state_json":"{}","source_reliability_state_json":"{}","observed_max_lower_bound_c":29.5,"consumed_event_ids_json":_json(["event-current"]),"consumed_ancestor_sha256":"ancestor-current","model_artifact_sha256":"c" * 64,"replay_generation":0,"supersedes_checkpoint_id":"","state":"committed","created_at":NOW,"committed_at":NOW}
    realization = {"realization_state_id":"realization-current","posterior_run_id":"post-current","fact_snapshot_id":"factsnap-current","resolution_contract_id":"resolution-v1","target_bucket":"30C","second_target_bucket":"","credible_bucket_set_json":_json(["30C"]),"current_report_bucket":"","current_report_bucket_probability":None,"probability_above_current_report_bucket":None,"current_report_bucket_hard_zero":0,"settlement_exclusion_certificates_json":"{}","state_json":_json({"schema":"realization_state_l3_v2","projection":{"peak_timing_state":{"schema":"peak_timing_state_l3_v1","ok":True,"state":"na","na_reason":"forecast_peak_timing_prior_missing","canonical_observation_event_id":"event-current","strong_observation_update_id":"update-current","entry_peak_at":"","does_not_update_physical_x":True}}}),"state_sha256":"state-current","created_at":NOW,"committed_at":NOW}
    outbox = {"outbox_event_id":"outbox-current","posterior_run_id":"post-current","fact_snapshot_id":"factsnap-current","correlation_id":"corr-current","state":"pending","created_at":NOW,"attempt_count":0,"claim_generation":0}
    execution = materialize_outbox_execution_contract_row(outbox_row=outbox, checkpoint_row=checkpoint, canonical_observation_event={"canonical_observation_event_id":"event-current","first_actionable_seen_at":"2026-07-03T06:59:58Z"}, inference_decision_deadline_at="2026-07-03T07:00:03Z")
    return update, checkpoint, realization, outbox, execution


def test_production_transaction_b_is_atomic_public_and_outbox_boundary() -> None:
    conn = _conn(); update, checkpoint, realization, outbox, execution = _rows()
    public = materialize_production_public_posterior_fact_row(activation=_activation(), strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization, slug=TARGET, event_date="2026-07-03", fact_as_of=NOW, correlation_id="corr-current")
    result = commit_production_public_ssot_transaction_b(conn, activation=_activation(), expected_previous_checkpoint_id=PREV_CHECKPOINT, expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox, outbox_execution_contract_row=execution, public_fact_row=public)
    assert result["ok"] is True and result["production"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1
    payload = json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])
    assert payload["posterior_ssot_fact"]["writer_mode"] == "production"
    assert payload["posterior_ssot_fact"]["termination_risk"]["schema"] == "termination_risk_marginal_v1"
    assert payload["posterior_ssot_fact"]["termination_risk"]["updates_posterior"] is False
    assert payload["posterior_ssot_fact"]["peak_timing_state"]["state"] == "na"
    assert payload["posterior_ssot_fact"]["probability_runtime_identity"]["bayes_formula_id"] == "p0_prior_x_weak_once_then_canonical_event_raw_log_lr_then_weak_revision_replacement_log_lr_v1"
    assert "shadow_only" not in payload["posterior_ssot_fact"]
    retry = commit_production_public_ssot_transaction_b(conn, activation=_activation(), expected_previous_checkpoint_id=PREV_CHECKPOINT, expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox, outbox_execution_contract_row=execution, public_fact_row=public)
    assert retry["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1


def test_resident_transaction_b_host_only_consumes_frozen_candidate_and_never_orders() -> None:
    conn = _conn(); update, checkpoint, realization, outbox, execution = _rows()
    checkpoint["state"] = "production_ready"
    host = ProductionSequentialBayesTransactionBHost(conn, activation=_activation())
    candidate = {
        "state": "candidate_ready", "candidate_mode": "production", "shadow_only": False,
        "strong_update_row": update, "checkpoint_row": checkpoint,
        "realization_state_row": realization, "outbox_row": outbox,
        "outbox_execution_contract_row": execution,
        "resident_fast_lane_prewarm_dependency_identity": "prewarm-current",
    }
    result = host.commit_candidate(candidate=candidate, previous_checkpoint={"posterior_checkpoint_id": PREV_CHECKPOINT, "posterior_run_id": PREV_RUN}, slug=TARGET, event_date="2026-07-03", fact_as_of=NOW)
    assert result["ok"] is True and result["places_orders"] is False and result["clob"] is False
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1


def test_resident_transaction_b_host_refuses_shadow_candidate_before_mutation() -> None:
    conn = _conn(); update, checkpoint, realization, outbox, execution = _rows()
    host = ProductionSequentialBayesTransactionBHost(conn, activation=_activation())
    result = host.commit_candidate(candidate={"state": "candidate_ready", "candidate_mode": "shadow", "shadow_only": True, "strong_update_row": update, "checkpoint_row": checkpoint, "realization_state_row": realization, "outbox_row": outbox, "outbox_execution_contract_row": execution}, previous_checkpoint={"posterior_checkpoint_id": PREV_CHECKPOINT, "posterior_run_id": PREV_RUN}, slug=TARGET, event_date="2026-07-03", fact_as_of=NOW)
    assert result["ok"] is False
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1


def test_production_writer_requires_explicit_cutover_and_refuses_shadow_database() -> None:
    conn = _conn(); update, checkpoint, realization, outbox, execution = _rows()
    public = materialize_production_public_posterior_fact_row(activation=_activation(), strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization, slug=TARGET, event_date="2026-07-03", fact_as_of=NOW, correlation_id="corr-current")
    with pytest.raises(ProductionTransactionIntegrityError, match="activation_not_authorized"):
        commit_production_public_ssot_transaction_b(conn, activation={}, expected_previous_checkpoint_id=PREV_CHECKPOINT, expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox, outbox_execution_contract_row=execution, public_fact_row=public)
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
    conn.execute("CREATE TABLE v2_shadow_runtime_manifest(singleton INTEGER)")
    with pytest.raises(ProductionTransactionIntegrityError, match="refuses_shadow"):
        commit_production_public_ssot_transaction_b(conn, activation=_activation(), expected_previous_checkpoint_id=PREV_CHECKPOINT, expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox, outbox_execution_contract_row=execution, public_fact_row=public)


def test_public_write_failure_rolls_back_posterior_and_outbox_together() -> None:
    conn = _conn(); update, checkpoint, realization, outbox, execution = _rows()
    public = materialize_production_public_posterior_fact_row(activation=_activation(), strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization, slug=TARGET, event_date="2026-07-03", fact_as_of=NOW, correlation_id="corr-current")
    public["fact_json"] = None
    with pytest.raises(sqlite3.IntegrityError):
        commit_production_public_ssot_transaction_b(conn, activation=_activation(), expected_previous_checkpoint_id=PREV_CHECKPOINT, expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox, outbox_execution_contract_row=execution, public_fact_row=public)
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0


def test_physical_only_never_creates_trade_outbox() -> None:
    conn = _conn(); update, checkpoint, _realization, _outbox, _execution = _rows()
    checkpoint = dict(checkpoint); checkpoint["settlement_scalar_distribution_json"] = "[]"; checkpoint["market_bucket_distribution_json"] = "{}"
    public = materialize_production_public_physical_posterior_fact_row(activation=_activation(), strong_update_row=update, checkpoint_row=checkpoint, fact_snapshot_id="factsnap-physical", slug=TARGET, event_date="2026-07-03", fact_as_of=NOW, correlation_id="corr-physical", market_bucket_probabilities_na_reason="x_to_s_to_b_bridge_unavailable")
    result = commit_production_public_physical_ssot_transaction(conn, activation=_activation(), expected_previous_checkpoint_id=PREV_CHECKPOINT, expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update, checkpoint_row=checkpoint, public_fact_row=public)
    assert result["physical_only"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])["posterior_ssot_fact"]["state"] == "physical_only_market_projection_na"
    assert json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])["posterior_ssot_fact"]["peak_timing_state"]["state"] == "na"


def test_transaction_b_rejects_frozen_p0_context_drift() -> None:
    conn = _conn(); update, checkpoint, realization, outbox, execution = _rows()
    checkpoint = dict(checkpoint); update = dict(update)
    checkpoint["forecast_evidence_snapshot_id"] = "forecast-illegal-refresh"
    update["forecast_evidence_snapshot_id"] = "forecast-illegal-refresh"
    public = materialize_production_public_posterior_fact_row(
        activation=_activation(), strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization,
        slug=TARGET, event_date="2026-07-03", fact_as_of=NOW, correlation_id="corr-current",
    )
    with pytest.raises(ProductionTransactionIntegrityError, match="forecast_snapshot_predecessor_drift"):
        commit_production_public_ssot_transaction_b(
            conn, activation=_activation(), expected_previous_checkpoint_id=PREV_CHECKPOINT,
            expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update,
            checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox,
            outbox_execution_contract_row=execution, public_fact_row=public,
        )
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0


def test_idempotent_retry_cannot_rewind_latest_pointer_after_newer_checkpoint() -> None:
    conn = _conn(); update, checkpoint, realization, outbox, execution = _rows()
    public = materialize_production_public_posterior_fact_row(
        activation=_activation(), strong_update_row=update, checkpoint_row=checkpoint, realization_state_row=realization,
        slug=TARGET, event_date="2026-07-03", fact_as_of=NOW, correlation_id="corr-current",
    )
    commit_production_public_ssot_transaction_b(
        conn, activation=_activation(), expected_previous_checkpoint_id=PREV_CHECKPOINT,
        expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update,
        checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox,
        outbox_execution_contract_row=execution, public_fact_row=public,
    )
    newer = dict(checkpoint)
    newer.update({
        "posterior_checkpoint_id": "pchk-newer", "posterior_run_id": "post-newer",
        "previous_checkpoint_id": "pchk-current", "joint_posterior_sha256": "joint-newer",
        "last_strong_update_id": "update-newer", "last_canonical_event_id": "event-newer",
        "committed_at": "2026-07-03T07:00:01Z", "created_at": "2026-07-03T07:00:01Z",
    })
    fields = list(newer)
    conn.execute(
        f"INSERT INTO posterior_checkpoints({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(newer[field] for field in fields),
    )
    conn.execute(
        "INSERT INTO event_fact_history(run_date,slug,event_date,fact_as_of,composer_version,fact_json,created_at,correlation_id,fact_snapshot_id,posterior_run_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("2026-07-03",TARGET,"2026-07-03","2026-07-03T07:00:01Z","sequential_bayes_v2_production","{}","2026-07-03T07:00:01Z","corr-newer","factsnap-newer","post-newer"),
    )
    newer_fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "UPDATE event_fact_latest SET fact_id=?,updated_at=?,fact_snapshot_id=?,correlation_id=? WHERE slug=? AND event_date=?",
        (newer_fact_id,"2026-07-03T07:00:01Z","factsnap-newer","corr-newer",TARGET,"2026-07-03"),
    )
    conn.commit()

    retry = commit_production_public_ssot_transaction_b(
        conn, activation=_activation(), expected_previous_checkpoint_id=PREV_CHECKPOINT,
        expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update,
        checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox,
        outbox_execution_contract_row=execution, public_fact_row=public,
    )
    latest = conn.execute(
        "SELECT fact_snapshot_id FROM event_fact_latest WHERE slug=? AND event_date=?",
        (TARGET,"2026-07-03"),
    ).fetchone()[0]
    assert retry["idempotent"] is True
    assert retry["latest_repointed"] is False
    assert latest == "factsnap-newer"


def test_activation_rollback_reselects_its_own_checkpoint_generation() -> None:
    conn = _conn(); update, checkpoint, realization, outbox, execution = _rows()
    first_activation = _activation()
    public = materialize_production_public_posterior_fact_row(
        activation=first_activation, strong_update_row=update, checkpoint_row=checkpoint,
        realization_state_row=realization, slug=TARGET, event_date="2026-07-03",
        fact_as_of=NOW, correlation_id="corr-current",
    )
    commit_production_public_ssot_transaction_b(
        conn, activation=first_activation, expected_previous_checkpoint_id=PREV_CHECKPOINT,
        expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update,
        checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox,
        outbox_execution_contract_row=execution, public_fact_row=public,
    )

    second_activation = {**_activation(), "activation_effective_at": "2026-07-03T07:01:00Z"}
    second_authorized = validate_production_probability_activation(second_activation)
    second_p0 = dict(checkpoint)
    second_p0.update({
        "posterior_checkpoint_id": "pchk-second-p0",
        "posterior_run_id": "post-second-p0",
        "previous_checkpoint_id": "",
        "checkpoint_kind": "initial",
        "last_strong_update_id": "",
        "last_canonical_event_id": "",
        "consumed_event_ids_json": "[]",
        "replay_generation": 1,
        "supersedes_checkpoint_id": "pchk-current",
        "created_at": "2026-07-03T07:01:00Z",
        "committed_at": "2026-07-03T07:01:00Z",
    })
    fields = list(second_p0)
    conn.execute(
        f"INSERT INTO posterior_checkpoints({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(second_p0[field] for field in fields),
    )
    conn.execute(
        """INSERT INTO production_initial_posterior_lineage(
          posterior_checkpoint_id,target_identity,posterior_run_id,prior_context_id,
          prior_artifact_sha256,prior_model_artifact_sha256,weak_likelihood_artifact_sha256,
          weak_likelihood_sha256,forecast_evidence_snapshot_id,strong_lr_artifact_sha256,
          source_reliability_initial_state_sha256,resolution_contract_id,initial_evidence_sha256,
          activation_contract_sha256,lineage_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "pchk-second-p0", TARGET, "post-second-p0", "prior-prev", "3" * 64,
            "a" * 64, "b" * 64, "4" * 64, "forecast-prev", "c" * 64,
            "5" * 64, "resolution-v1", "6" * 64,
            second_authorized["activation_contract_sha256"], "{}", "2026-07-03T07:01:00Z",
        ),
    )
    conn.execute(
        """INSERT INTO event_fact_history(
          run_date,slug,event_date,fact_as_of,composer_version,fact_json,created_at,
          correlation_id,fact_snapshot_id,posterior_run_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "2026-07-03", TARGET, "2026-07-03", "2026-07-03T07:01:00Z",
            "sequential_bayes_v2_production", "{}", "2026-07-03T07:01:00Z",
            "corr-second", "factsnap-second", "post-second-p0",
        ),
    )
    second_fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """UPDATE event_fact_latest SET fact_id=?,updated_at=?,fact_snapshot_id=?,correlation_id=?
           WHERE slug=? AND event_date=?""",
        (
            second_fact_id, "2026-07-03T07:01:00Z", "factsnap-second", "corr-second",
            TARGET, "2026-07-03",
        ),
    )
    conn.commit()

    retry = commit_production_public_ssot_transaction_b(
        conn, activation=first_activation, expected_previous_checkpoint_id=PREV_CHECKPOINT,
        expected_previous_posterior_run_id=PREV_RUN, strong_update_row=update,
        checkpoint_row=checkpoint, realization_state_row=realization, outbox_row=outbox,
        outbox_execution_contract_row=execution, public_fact_row=public,
    )
    latest = conn.execute(
        "SELECT fact_snapshot_id FROM event_fact_latest WHERE slug=? AND event_date=?",
        (TARGET, "2026-07-03"),
    ).fetchone()[0]

    assert retry["idempotent"] is True
    assert retry["latest_repointed"] is True
    assert latest == "factsnap-current"
