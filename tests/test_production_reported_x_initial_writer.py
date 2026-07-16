from __future__ import annotations

import json
import sqlite3

from production_probability_successor_activation_contract import (
    BAYES_FORMULA_ID,
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    LATENT_TYPE,
    PROJECTION_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    TARGET_TYPE,
)
from production_probability_successor_runtime_manifest import (
    successor_runtime_manifest_sha256,
)
from production_reported_x_initial_checkpoint import (
    compile_reported_x_initial_checkpoint,
)
from production_reported_x_initial_writer import (
    commit_reported_x_initial_posterior,
)
from reported_x_successor_schema_contract import COLUMNS
from tests.test_initial_posterior_checkpoint_shadow import (
    AS_OF,
    TARGET,
    _resolution_contract,
)
from tests.test_production_initial_posterior import _conn, _production_artifacts


def _activation() -> dict:
    prior, weak, strong = _production_artifacts()
    return {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "activation_effective_at": AS_OF,
        "cutover_approval_id": "ADR-0002-reported-x-successor",
        "bayes_formula_id": BAYES_FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "projection_schema": PROJECTION_SCHEMA,
        "latent_type": LATENT_TYPE,
        "target_type": TARGET_TYPE,
        "production_transaction_b_authorized": True,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "no_order_validation_accepted": True,
        "speci_soft_evidence_only": True,
        "regular_metar_only_x_hard_bound": True,
        "bridge_consumes_reported_x_only": True,
        "public_ssot_exposes_reported_x_only": True,
        "prior_model_artifact_sha256": prior["prior_model_artifact_sha256"],
        "weak_likelihood_artifact_sha256": weak["artifact_sha256"],
        "strong_lr_artifact_sha256": strong["model_artifact_sha256"],
        "runtime_manifest_sha256": successor_runtime_manifest_sha256(),
        "legacy_writer_retirement_receipt_sha256": "e" * 64,
        "places_orders": False,
        "authorizes_clob": False,
    }


def _candidate() -> dict:
    prior, weak, strong = _production_artifacts()
    return compile_reported_x_initial_checkpoint(
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


def _successor_conn() -> sqlite3.Connection:
    conn = _conn()
    for column, sql_type in COLUMNS.items():
        conn.execute(
            f"ALTER TABLE posterior_checkpoints ADD COLUMN {column} {sql_type}"
        )
    conn.commit()
    return conn


def test_reported_x_p0_commits_atomically_and_is_idempotent() -> None:
    conn = _successor_conn()
    candidate = _candidate()
    first = commit_reported_x_initial_posterior(
        conn,
        activation=_activation(),
        initial_checkpoint=candidate,
        slug=TARGET,
        event_date="2026-07-03",
        fact_as_of=AS_OF,
        correlation_id="reported-x-p0",
    )
    retry = commit_reported_x_initial_posterior(
        conn,
        activation=_activation(),
        initial_checkpoint=candidate,
        slug=TARGET,
        event_date="2026-07-03",
        fact_as_of=AS_OF,
        correlation_id="reported-x-p0",
    )
    assert first["ok"] is True and first["idempotent"] is False
    assert retry["ok"] is True and retry["idempotent"] is True
    row = conn.execute(
        "SELECT physical_high_distribution_json,reported_physical_x_distribution_json "
        "FROM posterior_checkpoints"
    ).fetchone()
    assert row[0] == "{}"
    assert all(float(key).is_integer() for key in json.loads(row[1]))
    payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history").fetchone()[0]
    )["posterior_ssot_fact"]
    assert payload["schema"] == PUBLIC_SSOT_SCHEMA
    assert "physical_high_distribution" not in payload
    assert payload["latent_probability_public"] is False
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
