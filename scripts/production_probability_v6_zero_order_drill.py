#!/usr/bin/env python3
"""In-memory production-schema fault drill; never touches live DB or CLOB."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_state_bayes_v6.artifact_contracts import validate_observation_artifact, validate_transition_artifact
from physical_state_bayes_v6.contracts import JointCell
from physical_state_bayes_v6.schema_contract import TABLE_COLUMNS, verify_schema
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from scripts.replay_physical_state_bayes_v6_canonical import (
    _recompile_physical_p0_for_replay,
)
from production_probability_v6_public_ssot_writer import (
    V6TransactionConflict,
    _insert,
    commit_v6_public_physical_transaction,
    materialize_v6_public_physical_fact_row,
)
from production_probability_v6_weak_revision_runtime import compile_v6_weak_revision_candidate
from production_probability_v6_weak_revision_writer import (
    commit_v6_weak_revision_transaction,
    materialize_v6_weak_revision_public_fact_row,
)
from identifiable_physical_state_bayes.core import (
    validate_observation_artifact as validate_physical_observation_artifact,
    validate_state_model_artifact,
)
from identifiable_physical_state_bayes.reporting import validate_reporting_operator_artifact


SCHEMA_VERSION = "production_probability_v6_zero_order_drill_v1"
CLONE_TABLES = (
    "canonical_observation_events",
    "strong_observation_updates",
    "posterior_checkpoints",
    "production_initial_posterior_lineage",
    "weak_forecast_evidence_snapshots",
    "weak_forecast_revision_updates",
    "event_fact_history",
    "event_fact_latest",
    "auto_orders",
    "posterior_commit_outbox",
    "posterior_outbox_execution_contracts",
)
ORDER_TABLES = ("auto_orders", "posterior_commit_outbox", "posterior_outbox_execution_contracts")


def _event(identity: str, valid: str, temperature: int) -> dict[str, Any]:
    return {
        "canonical_observation_event_id": identity,
        "canonical_observation_key": f"ZUUU|METAR|{valid}",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-15",
        "station_id": "ZUUU",
        "report_type": "METAR",
        "valid_time": valid,
        "revision_no": 0,
        "normalized_physical_sha256": identity[-1] * 64,
        "selected_source_observation_id": f"source-{identity}",
        "selected_source_id": "amsc",
        "selection_role": "primary",
        "selection_policy_version": "v6-drill",
        "first_transport_seen_at": valid,
        "first_actionable_seen_at": valid,
        "accepted_at": valid,
        "state": "accepted_primary",
        "supersedes_event_id": None,
        "conflict_group_id": None,
        "parsed_observation_json": json.dumps(
            {
                "temperature_c": float(temperature),
                "dewpoint_c": 18.0,
                "cloud_text": "FEW040",
                "weather_text": "",
            }
        ),
        "quality_flags_json": "[]",
        "created_at": valid,
        "parsed_observation": {
            "temperature_c": float(temperature),
            "dewpoint_c": 18.0,
            "cloud_text": "FEW040",
            "weather_text": "",
        },
    }


def _clone_schema(source_db: Path) -> sqlite3.Connection:
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    target = sqlite3.connect(":memory:")
    try:
        for table in CLONE_TABLES:
            row = source.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None:
                raise ValueError(f"v6_zero_order_source_table_missing:{table}")
            target.execute(row[0])
        for table, columns in TABLE_COLUMNS.items():
            present = {str(row[1]) for row in target.execute(f"PRAGMA table_info({table})")}
            for column, sql_type in columns.items():
                if column not in present:
                    target.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
        target.commit()
    finally:
        source.close()
    if verify_schema(target).get("ok") is not True:
        raise ValueError("v6_zero_order_cloned_schema_invalid")
    return target


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in ORDER_TABLES}


def _physical_artifacts(root: Path, activation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    values = [
        json.loads((root / "config/artifacts/identifiable_physical_state_model_v1.json").read_text()),
        json.loads((root / "config/artifacts/identifiable_physical_observation_v1.json").read_text()),
        json.loads((root / "config/artifacts/amsc_frozen_reporting_operator_v1.json").read_text()),
    ]
    artifacts = (
        validate_state_model_artifact(values[0]),
        validate_physical_observation_artifact(values[1]),
        validate_reporting_operator_artifact(values[2]),
    )
    for artifact, field in zip(
        artifacts,
        (
            "physical_state_model_artifact_sha256",
            "physical_observation_artifact_sha256",
            "physical_reporting_operator_sha256",
        ),
    ):
        if artifact["artifact_sha256"] != activation[field]:
            raise ValueError(f"v6_zero_order_{field}_mismatch")
    return artifacts


def _physical_weak(row: Mapping[str, Any]) -> dict[str, Any]:
    def load(name: str, default: Any) -> Any:
        try:
            return json.loads(str(row.get(name) or ""))
        except (TypeError, ValueError):
            return default

    return {
        "artifact_sha256": str(row.get("weak_likelihood_artifact_sha256") or ""),
        "forecast_evidence_snapshot_id": str(row.get("forecast_evidence_snapshot_id") or ""),
        "forecast_scenario_paths": load("forecast_scenario_paths_json", {}),
        "candidate_scenario_responsibilities": load(
            "candidate_scenario_responsibilities_json", {}
        ),
        "forecast_background_context": load("forecast_background_context_json", {}),
    }


def _audit_identifiable_physical(
    root: Path,
    source_db: Path,
    activation: dict[str, Any],
    transition: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    """Run the zero-order transaction drill on a real physical P0 lineage."""

    state_model, physical_observation, reporting_operator = _physical_artifacts(root, activation)
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source_p0 = source.execute(
            "SELECT p.* FROM posterior_checkpoints p "
            "WHERE p.checkpoint_kind='initial' AND p.physical_composer_schema=? "
            "AND EXISTS (SELECT 1 FROM canonical_observation_events e "
            "WHERE e.target_identity=p.target_identity "
            "AND e.state IN ('accepted_primary','accepted_substitute') "
            "AND e.first_actionable_seen_at>=p.created_at) "
            "ORDER BY p.rowid LIMIT 1",
            (IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,),
        ).fetchone()
        if source_p0 is None:
            raise ValueError("v6_zero_order_physical_p0_missing")
        source_p0 = dict(source_p0)
        lineage = source.execute(
            "SELECT * FROM production_initial_posterior_lineage WHERE posterior_checkpoint_id=?",
            (source_p0["posterior_checkpoint_id"],),
        ).fetchone()
        weak_row = source.execute(
            "SELECT * FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
            (source_p0["forecast_evidence_snapshot_id"],),
        ).fetchone()
        events = [
            dict(row)
            for row in source.execute(
                "SELECT * FROM canonical_observation_events WHERE target_identity=? "
                "AND state IN ('accepted_primary','accepted_substitute') "
                "AND first_actionable_seen_at>=? "
                "ORDER BY valid_time,first_actionable_seen_at,canonical_observation_event_id LIMIT 2",
                (source_p0["target_identity"], source_p0["created_at"]),
            ).fetchall()
        ]
        if lineage is None or weak_row is None or len(events) < 2:
            raise ValueError("v6_zero_order_physical_fixture_incomplete")
        weak_row = dict(weak_row)
        weak = _physical_weak(weak_row)
        p0_candidate = _recompile_physical_p0_for_replay(
            root=root,
            source_p0=source_p0,
            activation=activation,
            weak=weak,
            state_model_artifact=state_model,
            reporting_operator_artifact=reporting_operator,
        )
        p0 = dict(p0_candidate["checkpoint_row"])
        lineage = dict(p0_candidate["initial_lineage_row"])
    finally:
        source.close()

    conn = _clone_schema(source_db)
    try:
        p0["state"] = "committed"
        _insert(conn, "weak_forecast_evidence_snapshots", weak_row)
        _insert(conn, "posterior_checkpoints", p0)
        _insert(conn, "production_initial_posterior_lineage", lineage)
        for event in events:
            _insert(conn, "canonical_observation_events", event)
        conn.commit()
        event1, event2 = events
        event1["parsed_observation"] = json.loads(event1["parsed_observation_json"])
        event2["parsed_observation"] = json.loads(event2["parsed_observation_json"])
        candidate1 = compose_v6_canonical_event(
            activation=activation,
            previous_checkpoint=p0,
            canonical_event=event1,
            pre_event_history=[],
            weak_likelihood=weak,
            transition_artifact=transition,
            observation_artifact=observation,
            physical_state_model_artifact=state_model,
            physical_observation_artifact=physical_observation,
            physical_reporting_operator_artifact=reporting_operator,
            decision_as_of=event1["first_actionable_seen_at"],
            completed_at=event1["first_actionable_seen_at"],
        )
        public1 = materialize_v6_public_physical_fact_row(
            activation=activation,
            strong_update_row=candidate1["strong_update_row"],
            checkpoint_row=candidate1["checkpoint_row"],
            fact_snapshot_id="fact-physical-drill-1",
            slug="physical-drill-" + p0["target_identity"].replace(":", "-"),
            event_date=p0["target_identity"].rsplit(":", 1)[-1],
            fact_as_of=event1["first_actionable_seen_at"],
            correlation_id="correlation-physical-drill-1",
            market_bucket_probabilities_na_reason="zero_order_drill",
        )
        before_orders = _counts(conn)
        committed1 = commit_v6_public_physical_transaction(
            conn,
            activation=activation,
            expected_previous_checkpoint_id=p0["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=p0["posterior_run_id"],
            strong_update_row=candidate1["strong_update_row"],
            checkpoint_row=candidate1["checkpoint_row"],
            public_fact_row=public1,
        )
        idempotent = commit_v6_public_physical_transaction(
            conn,
            activation=activation,
            expected_previous_checkpoint_id=p0["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=p0["posterior_run_id"],
            strong_update_row=candidate1["strong_update_row"],
            checkpoint_row=candidate1["checkpoint_row"],
            public_fact_row=public1,
        )
        candidate2 = compose_v6_canonical_event(
            activation=activation,
            previous_checkpoint=candidate1["checkpoint_row"],
            canonical_event=event2,
            pre_event_history=[event1],
            weak_likelihood=weak,
            transition_artifact=transition,
            observation_artifact=observation,
            physical_state_model_artifact=state_model,
            physical_observation_artifact=physical_observation,
            physical_reporting_operator_artifact=reporting_operator,
            decision_as_of=event2["first_actionable_seen_at"],
            completed_at=event2["first_actionable_seen_at"],
        )
        public2 = materialize_v6_public_physical_fact_row(
            activation=activation,
            strong_update_row=candidate2["strong_update_row"],
            checkpoint_row=candidate2["checkpoint_row"],
            fact_snapshot_id="fact-physical-drill-2",
            slug=public1["slug"],
            event_date=public1["event_date"],
            fact_as_of=event2["first_actionable_seen_at"],
            correlation_id="correlation-physical-drill-2",
            market_bucket_probabilities_na_reason="zero_order_drill",
        )
        conn.execute(
            "CREATE TRIGGER fail_v6_public BEFORE INSERT ON event_fact_history "
            "WHEN NEW.fact_snapshot_id='fact-physical-drill-2' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        rollback_observed = False
        try:
            commit_v6_public_physical_transaction(
                conn,
                activation=activation,
                expected_previous_checkpoint_id=candidate1["checkpoint_row"]["posterior_checkpoint_id"],
                expected_previous_posterior_run_id=candidate1["checkpoint_row"]["posterior_run_id"],
                strong_update_row=candidate2["strong_update_row"],
                checkpoint_row=candidate2["checkpoint_row"],
                public_fact_row=public2,
            )
        except sqlite3.IntegrityError:
            rollback_observed = True
        finally:
            conn.execute("DROP TRIGGER fail_v6_public")
        rolled_back_cleanly = (
            conn.execute(
                "SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
                (candidate2["checkpoint_row"]["posterior_checkpoint_id"],),
            ).fetchone()[0]
            == 0
            and conn.execute(
                "SELECT COUNT(*) FROM strong_observation_updates WHERE strong_observation_update_id=?",
                (candidate2["strong_update_row"]["strong_observation_update_id"],),
            ).fetchone()[0]
            == 0
        )
        cas_conflict = False
        try:
            commit_v6_public_physical_transaction(
                conn,
                activation=activation,
                expected_previous_checkpoint_id="wrong-predecessor",
                expected_previous_posterior_run_id="wrong-run",
                strong_update_row=candidate2["strong_update_row"],
                checkpoint_row=candidate2["checkpoint_row"],
                public_fact_row=public2,
            )
        except Exception as exc:
            cas_conflict = "predecessor" in str(exc) or "mismatch" in str(exc)

        revised_snapshot = dict(weak_row)
        revised_snapshot.update(
            {
                "forecast_evidence_snapshot_id": weak_row["forecast_evidence_snapshot_id"] + "-revised",
                "decision_as_of": event2["first_actionable_seen_at"],
                "created_at": event2["first_actionable_seen_at"],
                "weak_likelihood_sha256": "5" * 64,
                "snapshot_sha256": "4" * 64,
                "forecast_scenario_paths_sha256": "3" * 64,
                "candidate_scenario_responsibilities_sha256": "2" * 64,
            }
        )
        revised_paths = json.loads(revised_snapshot["forecast_scenario_paths_json"])
        revised_paths["sha256"] = "3" * 64
        revised_snapshot["forecast_scenario_paths_json"] = json.dumps(revised_paths)
        revised_weak = {**weak, "forecast_scenario_paths": revised_paths}
        weak_candidate = compile_v6_weak_revision_candidate(
            activation=activation,
            previous_checkpoint=candidate1["checkpoint_row"],
            previous_snapshot=weak_row,
            revised_snapshot=revised_snapshot,
            previous_weak_likelihood=weak,
            revised_weak_likelihood=revised_weak,
            physical_state_model_artifact=state_model,
            physical_reporting_operator_artifact=reporting_operator,
            trigger_source_id="physical-drill-source",
            trigger_source_revision_id="physical-drill-revision",
            revision_kind="amendment",
            decision_as_of=event2["first_actionable_seen_at"],
        )
        previous_public = conn.execute(
            "SELECT h.fact_json FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id "
            "WHERE l.slug=? AND l.event_date=?",
            (public1["slug"], public1["event_date"]),
        ).fetchone()
        weak_public = materialize_v6_weak_revision_public_fact_row(
            activation=activation,
            previous_fact_json=previous_public[0],
            checkpoint_row=weak_candidate["checkpoint_row"],
            update_row=weak_candidate["update_row"],
            fact_snapshot_id="fact-physical-weak-drill",
            slug=public1["slug"],
            event_date=public1["event_date"],
            fact_as_of=event2["first_actionable_seen_at"],
        )
        weak_committed = commit_v6_weak_revision_transaction(
            conn,
            activation=activation,
            candidate=weak_candidate,
            public_fact_row=weak_public,
        )
        after_orders = _counts(conn)
        gates = {
            "first_commit_succeeded": committed1.get("ok") is True,
            "idempotent_retry_succeeded": idempotent.get("idempotent") is True,
            "fault_injection_raised": rollback_observed,
            "fault_injection_rolled_back_all_probability_rows": rolled_back_cleanly,
            "cas_conflict_fail_closed": cas_conflict,
            "taf_amd_forward_only_weak_revision_committed": (
                weak_committed.get("ok") is True
                and weak_candidate.get("past_state_transitions_replayed") is False
                and weak_candidate.get("future_forcing_replaced") is True
            ),
            "no_order_or_outbox_rows_created": before_orders == after_orders == {table: 0 for table in ORDER_TABLES},
        }
        return {
            "schema": SCHEMA_VERSION,
            "ok": all(gates.values()),
            "state": "zero_order_transaction_drill_accepted" if all(gates.values()) else "rejected",
            "gates": gates,
            "activation_contract_sha256": activation["activation_contract_sha256"],
            "runtime_manifest_sha256": activation["runtime_manifest_sha256"],
            "transition_artifact_sha256": "",
            "observation_artifact_sha256": "",
            "physical_state_model_artifact_sha256": activation["physical_state_model_artifact_sha256"],
            "physical_observation_artifact_sha256": activation["physical_observation_artifact_sha256"],
            "physical_reporting_operator_sha256": activation["physical_reporting_operator_sha256"],
            "uses_in_memory_clone_of_production_schema": True,
            "writes_archive_db": False,
            "places_orders": False,
        }
    finally:
        conn.close()


def audit(root: Path, source_db: Path) -> dict[str, Any]:
    activation = validate_v6_activation(
        json.loads((root / "config/production_probability_runtime_activation.v6_candidate.json").read_text())
    )
    transition = validate_transition_artifact(
        json.loads((root / "config/physical_state_bayes_v6_transition_artifact.json").read_text())
    )
    observation = validate_observation_artifact(
        json.loads((root / "config/physical_state_bayes_v6_observation_artifact.json").read_text())
    )
    if activation.get("composer_schema") == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        return _audit_identifiable_physical(root, source_db, activation, transition, observation)
    conn = _clone_schema(source_db)
    try:
        paths = [
            {"scenario_id": "gfs-drill", "hourly_curve": [
                {"valid_time_utc": "2026-07-14T16:00:00Z", "temperature_c": 24.0},
                {"valid_time_utc": "2026-07-14T17:00:00Z", "temperature_c": 26.0},
                {"valid_time_utc": "2026-07-15T08:00:00Z", "temperature_c": 38.0},
            ]}
        ]
        path_sha = "7" * 64
        weak = {
            "artifact_sha256": activation["weak_likelihood_artifact_sha256"],
            "joint_forecast_weak_likelihood_sha256": "8" * 64,
            "forecast_scenario_paths": {"sha256": path_sha, "paths": paths},
            "candidate_scenario_responsibilities": {
                "sha256": "9" * 64,
                "rows": [
                    {"continuous_y_c": y, "scenarios": [{"scenario_id": "gfs-drill", "responsibility": 1.0}]}
                    for y in (34.0, 38.0)
                ],
            },
        }
        p0 = compile_v6_p0_candidate(
            activation=activation,
            target_identity="physical_weather_day_v1:ZUUU:2026-07-15",
            event_date="2026-07-15",
            decision_as_of="2026-07-14T16:00:00Z",
            state_reference_valid_at="2026-07-14T16:00:00Z",
            prior_context_id="prior-drill",
            prior_model_artifact_sha256=activation["prior_model_artifact_sha256"],
            forecast_evidence_snapshot_id="forecast-drill",
            weak_likelihood_artifact_sha256=activation["weak_likelihood_artifact_sha256"],
            resolution_contract_id="resolution-drill",
            forcing_generation_sha256=path_sha,
            initial_joint=(JointCell(34.0, "warming", 0.4), JointCell(38.0, "warming", 0.6)),
            candidate_scenario_responsibilities=weak["candidate_scenario_responsibilities"],
            replay_generation=0,
        )
        checkpoint0 = dict(p0["checkpoint_row"])
        checkpoint0["state"] = "committed"
        old_snapshot = {
            "forecast_evidence_snapshot_id": "forecast-drill",
            "target_identity": checkpoint0["target_identity"],
            "event_date": "2026-07-15",
            "decision_as_of": "2026-07-14T16:00:00Z",
            "weak_likelihood_artifact_sha256": activation["weak_likelihood_artifact_sha256"],
            "weak_likelihood_sha256": "8" * 64,
            "source_set_key": "gfs",
            "source_revision_ids_json": "[]",
            "source_revisions_json": "[]",
            "temperature_grid_json": json.dumps([34.0, 38.0]),
            "likelihood_by_x_json": json.dumps({"34": 1.0, "38": 1.0}),
            "forecast_peak_timing_prior_json": "{}",
            "snapshot_json": "{}",
            "snapshot_sha256": "6" * 64,
            "state": "active",
            "created_at": "2026-07-14T16:00:00Z",
            "forecast_scenario_paths_json": json.dumps(weak["forecast_scenario_paths"]),
            "forecast_scenario_paths_sha256": path_sha,
            "candidate_scenario_responsibilities_json": json.dumps(weak["candidate_scenario_responsibilities"]),
            "candidate_scenario_responsibilities_sha256": "9" * 64,
        }
        _insert(conn, "weak_forecast_evidence_snapshots", old_snapshot)
        _insert(conn, "posterior_checkpoints", checkpoint0)
        _insert(conn, "production_initial_posterior_lineage", p0["initial_lineage_row"])
        event1 = _event("event-drill-1", "2026-07-14T16:30:00Z", 25)
        _insert(conn, "canonical_observation_events", {k: v for k, v in event1.items() if k != "parsed_observation"})
        conn.commit()
        candidate1 = compose_v6_canonical_event(
            activation=activation,
            previous_checkpoint=checkpoint0,
            canonical_event=event1,
            pre_event_history=[],
            weak_likelihood=weak,
            transition_artifact=transition,
            observation_artifact=observation,
            decision_as_of="2026-07-14T16:30:00Z",
            completed_at="2026-07-14T16:30:00.100Z",
        )
        public1 = materialize_v6_public_physical_fact_row(
            activation=activation,
            strong_update_row=candidate1["strong_update_row"],
            checkpoint_row=candidate1["checkpoint_row"],
            fact_snapshot_id="fact-drill-1",
            slug="chengdu-high-2026-07-15",
            event_date="2026-07-15",
            fact_as_of="2026-07-14T16:30:00.100Z",
            correlation_id="correlation-drill-1",
            market_bucket_probabilities_na_reason="zero_order_drill",
        )
        before_orders = _counts(conn)
        committed1 = commit_v6_public_physical_transaction(
            conn,
            activation=activation,
            expected_previous_checkpoint_id=checkpoint0["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=checkpoint0["posterior_run_id"],
            strong_update_row=candidate1["strong_update_row"],
            checkpoint_row=candidate1["checkpoint_row"],
            public_fact_row=public1,
        )
        idempotent = commit_v6_public_physical_transaction(
            conn,
            activation=activation,
            expected_previous_checkpoint_id=checkpoint0["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=checkpoint0["posterior_run_id"],
            strong_update_row=candidate1["strong_update_row"],
            checkpoint_row=candidate1["checkpoint_row"],
            public_fact_row=public1,
        )
        event2 = _event("event-drill-2", "2026-07-14T17:00:00Z", 26)
        _insert(conn, "canonical_observation_events", {k: v for k, v in event2.items() if k != "parsed_observation"})
        conn.commit()
        candidate2 = compose_v6_canonical_event(
            activation=activation,
            previous_checkpoint=candidate1["checkpoint_row"],
            canonical_event=event2,
            pre_event_history=[event1],
            weak_likelihood=weak,
            transition_artifact=transition,
            observation_artifact=observation,
            decision_as_of="2026-07-14T17:00:00Z",
            completed_at="2026-07-14T17:00:00.100Z",
        )
        public2 = materialize_v6_public_physical_fact_row(
            activation=activation,
            strong_update_row=candidate2["strong_update_row"],
            checkpoint_row=candidate2["checkpoint_row"],
            fact_snapshot_id="fact-drill-2",
            slug="chengdu-high-2026-07-15",
            event_date="2026-07-15",
            fact_as_of="2026-07-14T17:00:00.100Z",
            correlation_id="correlation-drill-2",
            market_bucket_probabilities_na_reason="zero_order_drill",
        )
        conn.execute(
            "CREATE TRIGGER fail_v6_public BEFORE INSERT ON event_fact_history "
            "WHEN NEW.fact_snapshot_id='fact-drill-2' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        rollback_observed = False
        try:
            commit_v6_public_physical_transaction(
                conn,
                activation=activation,
                expected_previous_checkpoint_id=candidate1["checkpoint_row"]["posterior_checkpoint_id"],
                expected_previous_posterior_run_id=candidate1["checkpoint_row"]["posterior_run_id"],
                strong_update_row=candidate2["strong_update_row"],
                checkpoint_row=candidate2["checkpoint_row"],
                public_fact_row=public2,
            )
        except sqlite3.IntegrityError:
            rollback_observed = True
        finally:
            conn.execute("DROP TRIGGER fail_v6_public")
        rolled_back_cleanly = (
            conn.execute(
                "SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
                (candidate2["checkpoint_row"]["posterior_checkpoint_id"],),
            ).fetchone()[0]
            == 0
            and conn.execute(
                "SELECT COUNT(*) FROM strong_observation_updates WHERE strong_observation_update_id=?",
                (candidate2["strong_update_row"]["strong_observation_update_id"],),
            ).fetchone()[0]
            == 0
        )
        cas_conflict = False
        try:
            commit_v6_public_physical_transaction(
                conn,
                activation=activation,
                expected_previous_checkpoint_id="wrong-predecessor",
                expected_previous_posterior_run_id="wrong-run",
                strong_update_row=candidate2["strong_update_row"],
                checkpoint_row=candidate2["checkpoint_row"],
                public_fact_row=public2,
            )
        except (V6TransactionConflict, Exception) as exc:
            cas_conflict = "predecessor" in str(exc) or "mismatch" in str(exc)
        revised_snapshot = {
            **old_snapshot,
            "forecast_evidence_snapshot_id": "forecast-drill-revised",
            "decision_as_of": "2026-07-14T17:05:00Z",
            "weak_likelihood_sha256": "5" * 64,
            "likelihood_by_x_json": json.dumps({"34": 0.5, "38": 2.0}),
            "snapshot_sha256": "4" * 64,
            "forecast_scenario_paths_sha256": "3" * 64,
            "candidate_scenario_responsibilities_sha256": "2" * 64,
            "created_at": "2026-07-14T17:05:00Z",
        }
        weak_candidate = compile_v6_weak_revision_candidate(
            activation=activation,
            previous_checkpoint=candidate1["checkpoint_row"],
            previous_snapshot=old_snapshot,
            revised_snapshot=revised_snapshot,
            trigger_source_id="zuuu_taf_forecast_amsc",
            trigger_source_revision_id="tafrev-drill-amd",
            revision_kind="amendment",
            decision_as_of="2026-07-14T17:05:00Z",
        )
        previous_public = conn.execute(
            "SELECT h.fact_json FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id "
            "WHERE l.slug=? AND l.event_date=?",
            ("chengdu-high-2026-07-15", "2026-07-15"),
        ).fetchone()
        weak_public = materialize_v6_weak_revision_public_fact_row(
            activation=activation,
            previous_fact_json=previous_public[0],
            checkpoint_row=weak_candidate["checkpoint_row"],
            update_row=weak_candidate["update_row"],
            fact_snapshot_id="fact-weak-drill",
            slug="chengdu-high-2026-07-15",
            event_date="2026-07-15",
            fact_as_of="2026-07-14T17:05:00Z",
        )
        weak_committed = commit_v6_weak_revision_transaction(
            conn,
            activation=activation,
            candidate=weak_candidate,
            public_fact_row=weak_public,
        )
        after_orders = _counts(conn)
        gates = {
            "first_commit_succeeded": committed1.get("ok") is True,
            "idempotent_retry_succeeded": idempotent.get("idempotent") is True,
            "fault_injection_raised": rollback_observed,
            "fault_injection_rolled_back_all_probability_rows": rolled_back_cleanly,
            "cas_conflict_fail_closed": cas_conflict,
            "taf_amd_forward_only_weak_revision_committed": (
                weak_committed.get("ok") is True
                and weak_candidate.get("past_state_transitions_replayed") is False
                and weak_candidate.get("future_forcing_replaced") is True
            ),
            "no_order_or_outbox_rows_created": before_orders == after_orders == {table: 0 for table in ORDER_TABLES},
        }
        return {
            "schema": SCHEMA_VERSION,
            "ok": all(gates.values()),
            "state": "zero_order_transaction_drill_accepted" if all(gates.values()) else "rejected",
            "gates": gates,
            "activation_contract_sha256": activation["activation_contract_sha256"],
            "runtime_manifest_sha256": activation["runtime_manifest_sha256"],
            "transition_artifact_sha256": activation["transition_artifact_sha256"],
            "observation_artifact_sha256": activation["observation_artifact_sha256"],
            "order_counts_before": before_orders,
            "order_counts_after": after_orders,
            "uses_in_memory_clone_of_production_schema": True,
            "writes_archive_db": False,
            "places_orders": False,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data/archive.db"))
    parser.add_argument("--output", default=str(ROOT / "output/audits/physical_state_bayes_v6_zero_order.json"))
    args = parser.parse_args()
    result = audit(ROOT, Path(args.db).resolve())
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
