from __future__ import annotations

import json
import sqlite3

import pytest

from initial_posterior_checkpoint_shadow import build_initial_posterior_checkpoint
from production_initial_posterior_checkpoint import (
    compile_production_initial_posterior_checkpoint,
    rebase_production_initial_posterior_successor,
)
from production_initial_posterior_writer import (
    LINEAGE_TABLE,
    ProductionInitialPosteriorIntegrityError,
    commit_production_initial_posterior,
    initialize_production_initial_posterior_schema,
)
from production_probability_activation_contract import SCHEMA_VERSION as ACTIVATION_SCHEMA
from sequential_bayes_schema_contract import apply_forward_schema
from physical_state_bayes_v6.schema_contract import TABLE_COLUMNS as V6_TABLE_COLUMNS
from weak_forecast_revision_schema_contract import ensure_weak_forecast_revision_schema
from tests.test_initial_posterior_checkpoint_shadow import (
    AS_OF,
    TARGET,
    _prior,
    _resolution_contract,
    _strong_artifact,
    _weak,
)


def _activation() -> dict:
    prior, weak, strong = _production_artifacts()
    return {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "production_transaction_b_authorized": True,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "no_order_validation_accepted": True,
        "cutover_approval_id": "pytest-p0",
        "activation_effective_at": "2026-07-03T00:00:00Z",
        "prior_model_artifact_sha256": prior["prior_model_artifact_sha256"],
        "weak_likelihood_artifact_sha256": weak["artifact_sha256"],
        "strong_lr_artifact_sha256": strong["model_artifact_sha256"],
        "runtime_manifest_sha256": "d" * 64,
        "legacy_writer_retirement_receipt_sha256": "e" * 64,
        "bayes_formula_id": "p0_prior_x_weak_once_then_canonical_event_raw_log_lr_then_weak_revision_replacement_log_lr_v1",
        "composer_schema": "sequential_bayes_filter_v3",
        "checkpoint_schema": "posterior_checkpoint_v2",
        "strong_update_schema": "strong_observation_update_v3",
        "public_ssot_schema": "sequential_temperature_posterior_ssot_v2",
        "places_orders": False,
        "authorizes_clob": False,
}


def _production_artifacts() -> tuple[dict, dict, dict]:
    prior, weak, strong = _prior(), _weak(), _strong_artifact()
    prior["artifact_sha256"] = "a" * 64
    prior["prior_model_artifact_sha256"] = "a" * 64
    prior["production_runtime_eligible"] = True
    prior["shadow_only"] = False
    weak["artifact_sha256"] = "b" * 64
    weak["production_runtime_eligible"] = True
    weak["shadow_only"] = False
    strong["artifact_sha256"] = strong["model_artifact_sha256"] = "c" * 64
    strong["production_runtime_eligible"] = True
    strong["shadow_only"] = False
    return prior, weak, strong


def _candidate() -> dict:
    prior, weak, strong = _production_artifacts()
    return compile_production_initial_posterior_checkpoint(
        activation=_activation(),
        target_identity=TARGET,
        event_date="2026-07-03",
        station_id="ZUUU",
        decision_as_of=AS_OF,
        prior_context=prior,
        joint_weak_likelihood=weak,
        active_strong_lr_artifact=strong,
        resolution_contract=_resolution_contract(closed=False),
        source_reliability_initial_state={"AMSC": {"state": "unknown"}},
    )


def _candidate_at(*, activation: dict, decision_as_of: str) -> dict:
    prior, weak, strong = _production_artifacts()
    weak["forecast_evidence_snapshot_id"] = "forecast-" + decision_as_of.replace(":", "").replace("-", "")
    return compile_production_initial_posterior_checkpoint(
        activation=activation,
        target_identity=TARGET,
        event_date="2026-07-03",
        station_id="ZUUU",
        decision_as_of=decision_as_of,
        prior_context=prior,
        joint_weak_likelihood=weak,
        active_strong_lr_artifact=strong,
        resolution_contract=_resolution_contract(closed=False),
        source_reliability_initial_state={"AMSC": {"state": "unknown"}},
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    ensure_weak_forecast_revision_schema(conn)
    conn.executescript(
        """
        CREATE TABLE event_fact_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT NOT NULL, slug TEXT NOT NULL,
          event_date TEXT NOT NULL, fact_as_of TEXT NOT NULL, composer_version TEXT NOT NULL,
          fact_json TEXT NOT NULL, created_at TEXT NOT NULL, correlation_id TEXT,
          fact_snapshot_id TEXT UNIQUE, posterior_run_id TEXT
        );
        CREATE TABLE event_fact_latest (
          slug TEXT NOT NULL,event_date TEXT NOT NULL,fact_id INTEGER NOT NULL,updated_at TEXT NOT NULL,
          fact_snapshot_id TEXT,correlation_id TEXT,PRIMARY KEY(slug,event_date)
        );
        """
    )
    initialize_production_initial_posterior_schema(conn)
    for table, columns in V6_TABLE_COLUMNS.items():
        actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, sql_type in columns.items():
            if column not in actual:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
    conn.commit()
    return conn


def test_production_p0_has_distinct_identity_and_no_shadow_authority() -> None:
    production = _candidate()
    prior, weak, strong = _production_artifacts()
    shadow = build_initial_posterior_checkpoint(
        target_identity=TARGET,
        event_date="2026-07-03",
        station_id="ZUUU",
        decision_as_of=AS_OF,
        prior_context=prior,
        joint_weak_likelihood=weak,
        active_strong_lr_artifact=strong,
        resolution_contract=_resolution_contract(closed=False),
        source_reliability_initial_state={"AMSC": {"state": "unknown"}},
    )

    assert production["ok"] is True
    assert production["initial_mode"] == "production"
    assert production["shadow_only"] is False
    assert production["checkpoint"]["posterior_run_id"] != shadow["checkpoint"]["posterior_run_id"]
    assert production["checkpoint"]["posterior_checkpoint_id"] != shadow["checkpoint"]["posterior_checkpoint_id"]


def test_production_p0_atomically_commits_checkpoint_lineage_and_public_ssot_without_outbox() -> None:
    conn = _conn()
    candidate = _candidate()

    first = commit_production_initial_posterior(
        conn,
        activation=_activation(),
        initial_checkpoint=candidate,
        slug=TARGET,
        event_date="2026-07-03",
        fact_as_of=AS_OF,
        correlation_id="pytest-p0-correlation",
    )
    retry = commit_production_initial_posterior(
        conn,
        activation=_activation(),
        initial_checkpoint=candidate,
        slug=TARGET,
        event_date="2026-07-03",
        fact_as_of=AS_OF,
        correlation_id="pytest-p0-correlation",
    )

    assert first["ok"] is True and first["idempotent"] is False
    assert retry["ok"] is True and retry["idempotent"] is True
    assert conn.execute("SELECT state FROM posterior_checkpoints").fetchone()[0] == "committed"
    assert conn.execute(f"SELECT COUNT(*) FROM {LINEAGE_TABLE}").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_latest").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    payload = json.loads(conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0])
    assert payload["posterior_ssot_fact"]["writer_mode"] == "production"
    assert payload["posterior_ssot_fact"]["checkpoint_kind"] == "initial"
    assert payload["posterior_ssot_fact"]["market_bucket_probabilities"] == {}

    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(f"UPDATE {LINEAGE_TABLE} SET created_at='drift'")


def test_failed_cutover_retry_appends_new_activation_generation_without_deleting_old_lineage() -> None:
    conn = _conn()
    first_activation = _activation()
    first = _candidate_at(activation=first_activation, decision_as_of="2026-07-03T05:00:00Z")
    first_commit = commit_production_initial_posterior(
        conn, activation=first_activation, initial_checkpoint=first, slug=TARGET,
        event_date="2026-07-03", fact_as_of="2026-07-03T05:00:00Z",
        correlation_id="pytest-first-cutover",
    )
    second_activation = _activation()
    second_activation["activation_effective_at"] = "2026-07-03T06:00:00Z"
    second = _candidate_at(activation=second_activation, decision_as_of="2026-07-03T06:00:00Z")
    second = rebase_production_initial_posterior_successor(
        second,
        replay_generation=1,
        supersedes_checkpoint_id=first_commit["posterior_checkpoint_id"],
    )
    second_commit = commit_production_initial_posterior(
        conn, activation=second_activation, initial_checkpoint=second, slug=TARGET,
        event_date="2026-07-03", fact_as_of="2026-07-03T06:00:00Z",
        correlation_id="pytest-second-cutover",
    )

    assert second_commit["ok"] is True
    assert conn.execute(f"SELECT COUNT(*) FROM {LINEAGE_TABLE}").fetchone()[0] == 2
    rows = conn.execute(
        "SELECT replay_generation,supersedes_checkpoint_id FROM posterior_checkpoints ORDER BY committed_at"
    ).fetchall()
    assert rows == [(0, ""), (1, first_commit["posterior_checkpoint_id"])]


def test_production_p0_rejects_activation_drift_and_shadow_database_before_mutation() -> None:
    broken_activation = _activation()
    broken_activation["strong_lr_artifact_sha256"] = "f" * 64
    prior, weak, strong = _production_artifacts()
    with pytest.raises(ValueError, match="activation_artifact_mismatch"):
        compile_production_initial_posterior_checkpoint(
            activation=broken_activation,
            target_identity=TARGET,
            event_date="2026-07-03",
            station_id="ZUUU",
            decision_as_of=AS_OF,
            prior_context=prior,
            joint_weak_likelihood=weak,
            active_strong_lr_artifact=strong,
            resolution_contract=_resolution_contract(closed=False),
            source_reliability_initial_state={"AMSC": {"state": "unknown"}},
        )

    conn = _conn()
    conn.execute("CREATE TABLE v2_shadow_runtime_manifest(singleton INTEGER)")
    conn.commit()
    with pytest.raises(ProductionInitialPosteriorIntegrityError, match="refuses_shadow_database"):
        commit_production_initial_posterior(
            conn,
            activation=_activation(),
            initial_checkpoint=_candidate(),
            slug=TARGET,
            event_date="2026-07-03",
            fact_as_of=AS_OF,
            correlation_id="pytest-p0-correlation",
        )
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 0


def test_production_p0_binds_prior_and_weak_artifact_identities() -> None:
    prior, weak, strong = _production_artifacts()
    wrong_prior = _activation(); wrong_prior["prior_model_artifact_sha256"] = "9" * 64
    with pytest.raises(ValueError, match="activation_prior_artifact_mismatch"):
        compile_production_initial_posterior_checkpoint(
            activation=wrong_prior, target_identity=TARGET, event_date="2026-07-03",
            station_id="ZUUU", decision_as_of=AS_OF, prior_context=prior,
            joint_weak_likelihood=weak, active_strong_lr_artifact=strong,
            resolution_contract=_resolution_contract(closed=False),
            source_reliability_initial_state={"AMSC": {"state": "unknown"}},
        )
    wrong_weak = _activation(); wrong_weak["weak_likelihood_artifact_sha256"] = "8" * 64
    with pytest.raises(ValueError, match="activation_weak_artifact_mismatch"):
        compile_production_initial_posterior_checkpoint(
            activation=wrong_weak, target_identity=TARGET, event_date="2026-07-03",
            station_id="ZUUU", decision_as_of=AS_OF, prior_context=prior,
            joint_weak_likelihood=weak, active_strong_lr_artifact=strong,
            resolution_contract=_resolution_contract(closed=False),
            source_reliability_initial_state={"AMSC": {"state": "unknown"}},
        )


def test_production_p0_rejects_shadow_bound_prior_or_weak_inputs() -> None:
    prior, weak, strong = _production_artifacts()
    prior["shadow_only"] = True
    prior["production_runtime_eligible"] = False
    with pytest.raises(ValueError, match="prior_not_production_eligible"):
        compile_production_initial_posterior_checkpoint(
            activation=_activation(), target_identity=TARGET, event_date="2026-07-03",
            station_id="ZUUU", decision_as_of=AS_OF, prior_context=prior,
            joint_weak_likelihood=weak, active_strong_lr_artifact=strong,
            resolution_contract=_resolution_contract(closed=False),
            source_reliability_initial_state={"AMSC": {"state": "unknown"}},
        )
    prior, weak, strong = _production_artifacts()
    weak["shadow_only"] = True
    weak["production_runtime_eligible"] = False
    with pytest.raises(ValueError, match="weak_not_production_eligible"):
        compile_production_initial_posterior_checkpoint(
            activation=_activation(), target_identity=TARGET, event_date="2026-07-03",
            station_id="ZUUU", decision_as_of=AS_OF, prior_context=prior,
            joint_weak_likelihood=weak, active_strong_lr_artifact=strong,
            resolution_contract=_resolution_contract(closed=False),
            source_reliability_initial_state={"AMSC": {"state": "unknown"}},
        )
