import hashlib
import json

import pytest

from production_weak_forecast_revision_writer import commit_production_weak_forecast_revision
from production_sequential_bayes_transaction_b_host import ProductionSequentialBayesTransactionBHost
from sequential_bayes_checkpoint_reader import load_latest_persisted_checkpoint
from sequential_bayes_shadow_writer import materialize_outbox_execution_contract_row
from tests.test_production_sequential_bayes_public_ssot_writer import _activation, _conn, _rows, NOW, PREV_CHECKPOINT, PREV_RUN, TARGET
from weak_forecast_revision_schema_contract import ensure_weak_forecast_revision_schema
from weak_forecast_revision_transaction_runtime import compile_weak_forecast_revision_candidate


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _snapshot(snapshot_id, likelihood, state="active"):
    return {
        "forecast_evidence_snapshot_id": snapshot_id,
        "target_identity": TARGET,
        "event_date": "2026-07-03",
        "decision_as_of": NOW,
        "weak_likelihood_artifact_sha256": "b" * 64,
        "weak_likelihood_sha256": _sha(likelihood),
        "source_set_key": "taf",
        "source_revision_ids_json": '["tafrev"]',
        "source_revisions_json": '[{"source_id":"taf","source_revision_id":"tafrev"}]',
        "temperature_grid_json": "[29.0,30.0]",
        "likelihood_by_x_json": json.dumps(likelihood, sort_keys=True),
        "forecast_peak_timing_prior_json": "{}",
        "snapshot_json": "{}",
        "snapshot_sha256": _sha({"id": snapshot_id, "likelihood": likelihood}),
        "state": state,
        "created_at": NOW,
    }


def test_weak_revision_atomically_advances_checkpoint_and_public_ssot_without_outbox():
    conn = _conn()
    ensure_weak_forecast_revision_schema(conn)
    joint = [
        {"temperature_c": 29.0, "state": "warming", "probability": 0.25},
        {"temperature_c": 29.0, "state": "terminated", "probability": 0.25},
        {"temperature_c": 30.0, "state": "warming", "probability": 0.25},
        {"temperature_c": 30.0, "state": "terminated", "probability": 0.25},
    ]
    conn.execute(
        """UPDATE posterior_checkpoints SET joint_posterior_json=?,joint_posterior_sha256=?,
           physical_high_distribution_json=?,latent_post_state_json=? WHERE posterior_checkpoint_id=?""",
        (json.dumps(joint), _sha(joint), '{"29":0.5,"30":0.5}', '{"warming":0.5,"terminated":0.5}', PREV_CHECKPOINT),
    )
    old = _snapshot("forecast-prev", {"29": 1.0, "30": 1.0})
    new = _snapshot("forecast-new", {"29": 0.5, "30": 2.0})
    columns = list(old)
    conn.execute(
        f"INSERT INTO weak_forecast_evidence_snapshots({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(old[key] for key in columns),
    )
    fact = {"posterior_ssot_fact": {"posterior_run_id": PREV_RUN, "posterior_checkpoint_id": PREV_CHECKPOINT, "probability_runtime_identity": {"bayes_formula_id": _activation()["bayes_formula_id"]}}}
    conn.execute(
        """INSERT INTO event_fact_history(run_date,slug,event_date,fact_as_of,composer_version,fact_json,created_at,correlation_id,fact_snapshot_id,posterior_run_id)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("2026-07-03", TARGET, "2026-07-03", NOW, "sequential_bayes_v2_production", json.dumps(fact), NOW, "prior", "fact-prior", PREV_RUN),
    )
    fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO event_fact_latest VALUES(?,?,?,?,?,?)", (TARGET, "2026-07-03", fact_id, NOW, "fact-prior", "prior"))
    conn.commit()
    conn.row_factory = __import__("sqlite3").Row
    previous = dict(conn.execute("SELECT * FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (PREV_CHECKPOINT,)).fetchone())
    candidate = compile_weak_forecast_revision_candidate(
        previous_checkpoint_row=previous,
        previous_snapshot_row=old,
        revised_snapshot_row=new,
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id="tafrev-amd",
        revision_kind="amendment",
        public_slug=TARGET,
        decision_as_of="2026-07-03T06:59:59Z",
        replay_generation=0,
    )
    competing_snapshot = _snapshot("forecast-competing", {"29": 2.0, "30": 0.5})
    competing_candidate = compile_weak_forecast_revision_candidate(
        previous_checkpoint_row=previous,
        previous_snapshot_row=old,
        revised_snapshot_row=competing_snapshot,
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id="tafrev-cor-competing",
        revision_kind="correction",
        public_slug=TARGET,
        decision_as_of="2026-07-03T07:00:01Z",
        replay_generation=0,
    )
    result = commit_production_weak_forecast_revision(conn, activation=_activation(), candidate=candidate)
    assert result["ok"] is True and result["places_orders"] is False
    assert load_latest_persisted_checkpoint(
        conn, target_identity=TARGET, event_date="2026-07-03", replay_generation=0,
    )["posterior_checkpoint_id"] == result["posterior_checkpoint_id"]
    assert conn.execute("SELECT COUNT(*) FROM weak_forecast_revision_updates").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    latest = json.loads(conn.execute("SELECT h.fact_json FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id").fetchone()[0])
    assert latest["posterior_ssot_fact"]["forecast_evidence_snapshot_id"] == "forecast-new"
    assert latest["posterior_ssot_fact"]["last_weak_forecast_revision_update_id"].startswith("weakupd_")
    with pytest.raises(RuntimeError, match="predecessor_cas_mismatch"):
        commit_production_weak_forecast_revision(
            conn, activation=_activation(), candidate=competing_candidate,
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id='forecast-competing'"
    ).fetchone()[0] == 0

    # A subsequent METAR/SPECI strong update must consume the AMD successor
    # checkpoint and its active weak snapshot, not fall back to the P0 one.
    predecessor = dict(conn.execute(
        "SELECT * FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
        (result["posterior_checkpoint_id"],),
    ).fetchone())
    update, checkpoint, realization, outbox, _execution = _rows()
    update.update({
        "strong_observation_update_id": "update-after-amd",
        "previous_posterior_run_id": predecessor["posterior_run_id"],
        "previous_posterior_sha256": predecessor["joint_posterior_sha256"],
        "previous_checkpoint_id": predecessor["posterior_checkpoint_id"],
        "canonical_observation_event_id": "event-after-amd",
        "forecast_evidence_snapshot_id": "forecast-new",
    })
    auxiliary = json.loads(update["path_auxiliary_json"])
    auxiliary["termination_risk"]["canonical_observation_event_id"] = "event-after-amd"
    auxiliary["peak_timing_state"].update({
        "canonical_observation_event_id": "event-after-amd",
        "strong_observation_update_id": "update-after-amd",
    })
    update["path_auxiliary_json"] = json.dumps(auxiliary, sort_keys=True)
    checkpoint.update({
        "posterior_checkpoint_id": "pchk-after-amd",
        "posterior_run_id": "post-after-amd",
        "previous_checkpoint_id": predecessor["posterior_checkpoint_id"],
        "forecast_evidence_snapshot_id": "forecast-new",
        "last_strong_update_id": "update-after-amd",
        "last_canonical_event_id": "event-after-amd",
        "state": "production_ready",
    })
    realization.update({
        "realization_state_id": "realization-after-amd",
        "posterior_run_id": "post-after-amd",
        "fact_snapshot_id": "factsnap-after-amd",
    })
    realization_state = json.loads(realization["state_json"])
    realization_state["projection"]["peak_timing_state"].update({
        "canonical_observation_event_id": "event-after-amd",
        "strong_observation_update_id": "update-after-amd",
    })
    realization["state_json"] = json.dumps(realization_state, sort_keys=True)
    outbox.update({
        "outbox_event_id": "outbox-after-amd",
        "posterior_run_id": "post-after-amd",
        "fact_snapshot_id": "factsnap-after-amd",
        "correlation_id": "corr-after-amd",
    })
    execution = materialize_outbox_execution_contract_row(
        outbox_row=outbox,
        checkpoint_row=checkpoint,
        canonical_observation_event={
            "canonical_observation_event_id": "event-after-amd",
            "first_actionable_seen_at": "2026-07-03T07:00:01Z",
        },
        inference_decision_deadline_at="2026-07-03T07:00:04Z",
    )
    strong_result = ProductionSequentialBayesTransactionBHost(conn, activation=_activation()).commit_candidate(
        candidate={
            "state": "candidate_ready",
            "candidate_mode": "production",
            "shadow_only": False,
            "strong_update_row": update,
            "checkpoint_row": checkpoint,
            "realization_state_row": realization,
            "outbox_row": outbox,
            "outbox_execution_contract_row": execution,
        },
        previous_checkpoint={
            "posterior_checkpoint_id": predecessor["posterior_checkpoint_id"],
            "posterior_run_id": predecessor["posterior_run_id"],
        },
        slug=TARGET,
        event_date="2026-07-03",
        fact_as_of="2026-07-03T07:00:02Z",
    )
    assert strong_result["ok"] is True
    assert conn.execute(
        "SELECT forecast_evidence_snapshot_id FROM strong_observation_updates WHERE strong_observation_update_id='update-after-amd'"
    ).fetchone()[0] == "forecast-new"
