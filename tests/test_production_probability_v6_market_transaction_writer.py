import json
from pathlib import Path
import sqlite3
import time

import pytest

from physical_state_bayes_v6.artifact_contracts import (
    validate_observation_artifact,
    validate_transition_artifact,
)
from physical_state_bayes_v6.contracts import JointCell
from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_market_bundle_runtime import (
    build_v6_market_transaction_bundle,
)
from production_probability_v6_market_transaction_writer import (
    commit_v6_market_transaction_b,
)
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from production_probability_v6_path_auxiliary_runtime import (
    enrich_v6_candidate_with_post_event_auxiliary,
)
from production_probability_v6_public_ssot_writer import (
    V6TransactionIntegrityError,
    _insert,
)
from scripts.production_probability_v6_zero_order_drill import _clone_schema
from tests.test_reported_x_market_realization_runtime import _artifact, _policy, _target


ROOT = Path(__file__).resolve().parents[1]


def _copy_table_schema(conn: sqlite3.Connection, table: str) -> None:
    with sqlite3.connect(f"file:{ROOT / 'data/archive.db'}?mode=ro", uri=True) as source:
        row = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    assert row is not None
    conn.execute(row[0])


def _setup():
    activation = json.loads(
        (ROOT / "config/production_probability_runtime_activation.json").read_text()
    )
    transition = validate_transition_artifact(
        json.loads(
            (ROOT / "config/physical_state_bayes_v6_transition_artifact.json").read_text()
        )
    )
    observation = validate_observation_artifact(
        json.loads(
            (ROOT / "config/physical_state_bayes_v6_observation_artifact.json").read_text()
        )
    )
    conn = _clone_schema(ROOT / "data/archive.db")
    _copy_table_schema(conn, "realization_states")
    target = "physical_weather_day_v1:ZUUU:2025-04-30"
    paths = {
        "sha256": "7" * 64,
        "paths": [
            {
                "scenario_id": "gfs-test",
                "hourly_curve": [
                    {"valid_time_utc": "2025-04-30T07:00:00Z", "temperature_c": 29.0},
                    {"valid_time_utc": "2025-04-30T09:00:00Z", "temperature_c": 31.0},
                ],
            }
        ],
    }
    responsibilities = {
        "sha256": "8" * 64,
        "rows": [
            {
                "continuous_y_c": y,
                "scenarios": [{"scenario_id": "gfs-test", "responsibility": 1.0}],
            }
            for y in (30.0, 31.0)
        ],
    }
    weak = {
        "artifact_sha256": activation["weak_likelihood_artifact_sha256"],
        "joint_forecast_weak_likelihood_sha256": "9" * 64,
        "forecast_scenario_paths": paths,
        "candidate_scenario_responsibilities": responsibilities,
    }
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity=target,
        event_date="2025-04-30",
        decision_as_of="2025-04-30T07:00:00Z",
        state_reference_valid_at="2025-04-30T07:00:00Z",
        prior_context_id="prior-1",
        prior_model_artifact_sha256=activation["prior_model_artifact_sha256"],
        forecast_evidence_snapshot_id="forecast-1",
        weak_likelihood_artifact_sha256=activation["weak_likelihood_artifact_sha256"],
        resolution_contract_id="physical-resolution",
        forcing_generation_sha256=paths["sha256"],
        initial_joint=(
            JointCell(30.0, "warming", 0.5),
            JointCell(31.0, "warming", 0.5),
        ),
        replay_generation=0,
    )
    checkpoint0 = dict(p0["checkpoint_row"])
    checkpoint0["state"] = "committed"
    snapshot = {
        "forecast_evidence_snapshot_id": "forecast-1",
        "target_identity": target,
        "event_date": "2025-04-30",
        "decision_as_of": "2025-04-30T07:00:00Z",
        "weak_likelihood_artifact_sha256": activation["weak_likelihood_artifact_sha256"],
        "weak_likelihood_sha256": "9" * 64,
        "source_set_key": "gfs",
        "source_revision_ids_json": "[]",
        "source_revisions_json": "[]",
        "temperature_grid_json": "[30,31]",
        "likelihood_by_x_json": '{"30":0.5,"31":0.5}',
        "forecast_peak_timing_prior_json": json.dumps(
            {
                "forecast_evidence_snapshot_id": "forecast-1",
                "distribution": [
                    {"peak_at": "2025-04-30T09:00:00Z", "probability": 1.0}
                ],
            }
        ),
        "snapshot_json": "{}",
        "snapshot_sha256": "6" * 64,
        "state": "active",
        "created_at": "2025-04-30T07:00:00Z",
        "forecast_scenario_paths_json": json.dumps(paths),
        "forecast_scenario_paths_sha256": paths["sha256"],
        "candidate_scenario_responsibilities_json": json.dumps(responsibilities),
        "candidate_scenario_responsibilities_sha256": responsibilities["sha256"],
    }
    _insert(conn, "weak_forecast_evidence_snapshots", snapshot)
    _insert(conn, "posterior_checkpoints", checkpoint0)
    _insert(conn, "production_initial_posterior_lineage", p0["initial_lineage_row"])
    event = {
        "canonical_observation_event_id": "event-1",
        "canonical_observation_key": "ZUUU|METAR|2025-04-30T07:30:00Z",
        "target_identity": target,
        "station_id": "ZUUU",
        "report_type": "METAR",
        "valid_time": "2025-04-30T07:30:00Z",
        "revision_no": 0,
        "normalized_physical_sha256": "e" * 64,
        "selected_source_observation_id": "source-1",
        "selected_source_id": "zuuu_metar_raw_amsc",
        "selection_role": "primary",
        "selection_policy_version": "test",
        "first_transport_seen_at": "2025-04-30T07:30:00Z",
        "first_actionable_seen_at": "2025-04-30T07:30:00Z",
        "accepted_at": "2025-04-30T07:30:00Z",
        "state": "accepted_primary",
        "supersedes_event_id": None,
        "conflict_group_id": None,
        "parsed_observation_json": json.dumps(
            {
                "temperature_c": 30.0,
                "dewpoint_c": 18.0,
                "cloud_text": "FEW040",
                "weather_text": "",
            }
        ),
        "quality_flags_json": "[]",
        "created_at": "2025-04-30T07:30:00Z",
    }
    _insert(conn, "canonical_observation_events", event)
    conn.commit()
    event["parsed_observation"] = json.loads(event["parsed_observation_json"])
    candidate = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=checkpoint0,
        canonical_event=event,
        pre_event_history=[],
        weak_likelihood=weak,
        transition_artifact=transition,
        observation_artifact=observation,
        decision_as_of="2025-04-30T07:30:00Z",
        completed_at="2025-04-30T07:30:00.100Z",
    )
    candidate = enrich_v6_candidate_with_post_event_auxiliary(
        candidate=candidate,
        canonical_events_through_current=[event],
        forecast_peak_timing_prior=json.loads(
            snapshot["forecast_peak_timing_prior_json"]
        ),
    )
    bundle = build_v6_market_transaction_bundle(
        activation=activation,
        previous_checkpoint=checkpoint0,
        candidate=candidate,
        physical_resolution_contract={
            "resolution_contract_id": "physical-resolution",
            "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
            "authority_contract_sha256": "a" * 64,
            "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
            "resolution_local_date": "2025-04-30",
            "settlement_unit": "C",
            "bridge_closed": False,
            "validated": False,
        },
        market_target_contract=_target(),
        bridge_artifact=_artifact(),
        canonical_observation_event=event,
        realization_policy=_policy(),
        fact_snapshot_id="fact-1",
        slug="chengdu-high-2025-04-30",
        event_date="2025-04-30",
        fact_as_of="2025-04-30T07:30:00.100Z",
        correlation_id="corr-1",
        outbox_event_id="outbox-1",
        inference_decision_deadline_at="2025-04-30T07:30:03Z",
    )
    bridge_activation = {
        "schema": "resolution_bridge_production_activation_v1",
        "state": "production_enabled",
        "artifact_path": "config/approved-bridge.json",
        "artifact_sha256": _artifact()["artifact_sha256"],
        "approval_id": "ADR-bridge-test",
        "approval_receipt_sha256": "c" * 64,
        "places_orders": False,
        "authorizes_clob": False,
    }
    return conn, activation, checkpoint0, bundle, bridge_activation


def test_v6_market_transaction_is_atomic_and_idempotent():
    conn, activation, previous, bundle, bridge = _setup()
    kwargs = {
        "activation": activation,
        "bridge_activation": bridge,
        "expected_previous_checkpoint_id": previous["posterior_checkpoint_id"],
        "expected_previous_posterior_run_id": previous["posterior_run_id"],
        "strong_update_row": bundle["strong_update_row"],
        "checkpoint_row": bundle["checkpoint_row"],
        "realization_state_row": bundle["realization_state_row"],
        "outbox_row": bundle["outbox_row"],
        "outbox_execution_contract_row": bundle["outbox_execution_contract_row"],
        "public_fact_row": bundle["public_fact_row"],
    }
    first = commit_v6_market_transaction_b(conn, **kwargs)
    second = commit_v6_market_transaction_b(conn, **kwargs)

    assert first["ok"] is True and first["idempotent"] is False
    assert second["ok"] is True and second["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 1


def test_v6_market_transaction_b_commits_well_inside_three_second_budget_on_production_schema_clone():
    conn, activation, previous, bundle, bridge = _setup()
    started = time.monotonic()
    result = commit_v6_market_transaction_b(
        conn,
        activation=activation,
        bridge_activation=bridge,
        expected_previous_checkpoint_id=previous["posterior_checkpoint_id"],
        expected_previous_posterior_run_id=previous["posterior_run_id"],
        strong_update_row=bundle["strong_update_row"],
        checkpoint_row=bundle["checkpoint_row"],
        realization_state_row=bundle["realization_state_row"],
        outbox_row=bundle["outbox_row"],
        outbox_execution_contract_row=bundle["outbox_execution_contract_row"],
        public_fact_row=bundle["public_fact_row"],
    )
    elapsed_ms = (time.monotonic() - started) * 1000.0

    assert result["ok"] is True
    assert elapsed_ms < 3000.0
    execution = conn.execute(
        "SELECT deadline_policy,inference_decision_deadline_at,"
        "observation_to_order_hard_deadline_at FROM posterior_outbox_execution_contracts"
    ).fetchone()
    assert execution[0] == "canonical_first_actionable_plus_3000ms"
    assert execution[1] == execution[2]


def test_v6_market_transaction_rolls_back_all_rows_on_public_failure():
    conn, activation, previous, bundle, bridge = _setup()
    bundle["public_fact_row"]["fact_json"] = None
    with pytest.raises((V6TransactionIntegrityError, TypeError, ValueError)):
        commit_v6_market_transaction_b(
            conn,
            activation=activation,
            bridge_activation=bridge,
            expected_previous_checkpoint_id=previous["posterior_checkpoint_id"],
            expected_previous_posterior_run_id=previous["posterior_run_id"],
            strong_update_row=bundle["strong_update_row"],
            checkpoint_row=bundle["checkpoint_row"],
            realization_state_row=bundle["realization_state_row"],
            outbox_row=bundle["outbox_row"],
            outbox_execution_contract_row=bundle["outbox_execution_contract_row"],
            public_fact_row=bundle["public_fact_row"],
        )
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
