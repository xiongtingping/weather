from __future__ import annotations

import json
from pathlib import Path

import pytest

from production_probability_v6_event_runtime import compose_v6_canonical_event
from production_probability_v6_canonical_event_drain_runtime import (
    drain_v6_canonical_events,
    pending_v6_canonical_event_ids,
    physical_v6_correction_event_ids,
)
from production_probability_v6_initial_writer import (
    commit_v6_initial_posterior,
    materialize_v6_initial_public_fact_row,
)
from production_probability_v6_p0_runtime import compile_v6_p0_candidate
from production_probability_v6_public_ssot_writer import (
    V6TransactionConflict,
    _insert,
)
from production_probability_v6_weak_revision_runtime import (
    IDENTIFIABLE_PHYSICAL_UPDATE_SCHEMA,
    compile_v6_weak_revision_candidate,
)
from production_probability_v6_weak_revision_writer import (
    commit_v6_weak_revision_transaction,
    materialize_v6_weak_revision_public_fact_row,
)
from production_probability_v6_transaction_commit_runtime import (
    commit_v6_candidate_transaction,
)
from production_probability_v6_transaction_branch_runtime import (
    build_v6_transaction_branch_bundle,
)
from scripts.production_probability_v6_zero_order_drill import _clone_schema
from tests.test_identifiable_physical_state_bayes import (
    _observation_artifact,
    _physical_activation,
    _reporting_artifact,
    _revised_weak_likelihood,
    _state_artifact,
    _weak_likelihood,
)
from tests.test_reported_x_market_realization_runtime import _artifact, _target
from resolution_bridge_activation_runtime import (
    activation_sha256 as bridge_activation_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-07-19"
TARGET = f"physical_weather_day_v1:ZUUU:{DAY}"


def _physical_transaction_setup():
    activation = _physical_activation()
    p0 = compile_v6_p0_candidate(
        activation=activation,
        target_identity=TARGET,
        event_date=DAY,
        decision_as_of="2026-07-19T04:00:00Z",
        state_reference_valid_at="2026-07-19T04:00:00Z",
        prior_context_id="prior-physical",
        prior_model_artifact_sha256="2" * 64,
        forecast_evidence_snapshot_id="weak-physical",
        weak_likelihood_artifact_sha256="3" * 64,
        resolution_contract_id="physical-resolution",
        forcing_generation_sha256="a" * 64,
        initial_joint=(),
        candidate_scenario_responsibilities={},
        replay_generation=0,
        supersedes_checkpoint_id="",
        prior_context={},
        weak_likelihood=_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    previous = dict(p0["checkpoint_row"])
    previous["state"] = "committed"
    event = {
        "canonical_observation_event_id": "physical-basket-event",
        "canonical_observation_key": "ZUUU|METAR|2026-07-19T05:00:00Z",
        "target_identity": TARGET,
        "station_id": "ZUUU",
        "report_type": "METAR",
        "valid_time": "2026-07-19T05:00:00Z",
        "revision_no": 0,
        "normalized_physical_sha256": "e" * 64,
        "selected_source_observation_id": "source-physical-basket",
        "selected_source_id": "zuuu_metar_raw_amsc",
        "selection_role": "primary",
        "selection_policy_version": "test",
        "first_transport_seen_at": "2026-07-19T05:00:00Z",
        "first_actionable_seen_at": "2026-07-19T05:00:00Z",
        "accepted_at": "2026-07-19T05:00:00Z",
        "state": "accepted_primary",
        "supersedes_event_id": None,
        "conflict_group_id": None,
        "parsed_observation_json": json.dumps(
            {
                "temperature_c": 30.0,
                "dewpoint_c": 24.0,
                "cloud_text": "BKN020",
                "weather_text": "RA",
                "qnh_hpa": 1000.0,
            }
        ),
        "quality_flags_json": "[]",
        "created_at": "2026-07-19T05:00:00Z",
    }
    event["parsed_observation"] = json.loads(event["parsed_observation_json"])
    candidate = compose_v6_canonical_event(
        activation=activation,
        previous_checkpoint=previous,
        canonical_event=event,
        pre_event_history=[],
        weak_likelihood=_weak_likelihood(),
        transition_artifact={},
        observation_artifact={},
        decision_as_of="2026-07-19T05:00:00.100Z",
        completed_at="2026-07-19T05:00:00.200Z",
        physical_state_model_artifact=_state_artifact(),
        physical_observation_artifact=_observation_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    target = {**_target(), "resolution_local_date": DAY}
    physical_contract = {
        "resolution_contract_id": "physical-resolution",
        "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
        "authority_contract_sha256": "a" * 64,
        "resolution_source_id": (
            "wunderground_zuuu_historical_observation_daily_high_final_v3"
        ),
        "resolution_local_date": DAY,
        "settlement_unit": "C",
        "bridge_closed": False,
        "validated": False,
    }
    artifact = _artifact()
    bridge_activation = {
        "schema": "resolution_bridge_production_activation_v1",
        "state": "production_enabled",
        "artifact_path": "config/approved-bridge.json",
        "artifact_sha256": artifact["artifact_sha256"],
        "approval_id": "ADR-bridge-test",
        "approval_receipt_sha256": "c" * 64,
        "places_orders": False,
        "authorizes_clob": False,
    }
    bridge_activation["activation_sha256"] = bridge_activation_sha256(
        bridge_activation
    )
    return (
        activation,
        p0,
        previous,
        event,
        candidate,
        physical_contract,
        target,
        artifact,
        bridge_activation,
    )


def test_physical_market_projection_commits_for_basket_without_three_leg_outbox():
    (
        activation,
        p0,
        previous,
        event,
        candidate,
        physical_contract,
        target,
        artifact,
        bridge_activation,
    ) = _physical_transaction_setup()
    conn = _clone_schema(ROOT / "data/archive.db")
    _insert(conn, "posterior_checkpoints", previous)
    _insert(conn, "production_initial_posterior_lineage", p0["initial_lineage_row"])
    _insert(
        conn,
        "canonical_observation_events",
        {key: value for key, value in event.items() if key != "parsed_observation"},
    )
    conn.commit()
    result = commit_v6_candidate_transaction(
        conn,
        activation=activation,
        previous_checkpoint=previous,
        candidate=candidate,
        canonical_observation_event=event,
        market_inputs={
            "market_context_result": {
                "ok": True,
                "state": "available",
                "market_projection_available": True,
                "market_context": {
                    "market_target_contract": target,
                    "target_contract_sha256": target["target_contract_sha256"],
                    "bridge_artifact_sha256": artifact["artifact_sha256"],
                    "slug": "chengdu-high-2026-07-19",
                },
            },
            "bridge_activation": bridge_activation,
            "bridge_artifact": artifact,
            "physical_resolution_contract": physical_contract,
        },
        fact_snapshot_id="physical-basket-fact",
        slug="chengdu-high-2026-07-19",
        event_date=DAY,
        fact_as_of="2026-07-19T05:00:00.200Z",
        correlation_id="physical-basket-correlation",
    )
    assert result["ok"] is True
    assert result["writer_route"] == "basket_probability_transaction_b"
    assert result["commit"]["creates_outbox"] is False
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='realization_states'"
    ).fetchone() is None
    fact = json.loads(
        conn.execute(
            "SELECT fact_json FROM event_fact_history WHERE fact_snapshot_id=?",
            ("physical-basket-fact",),
        ).fetchone()[0]
    )["posterior_ssot_fact"]
    assert fact["strategy_projection_role"] == "basket_probability_only"
    assert fact["realization_state_id"] == ""
    assert sum(fact["market_bucket_probabilities"].values()) == pytest.approx(1.0)
    assert fact["market_projection_boundary"]["dynamic_three_leg_realization_consumed"] is False


def test_physical_tampered_bridge_fails_open_only_for_x_and_creates_no_outbox():
    (
        activation,
        _p0,
        previous,
        event,
        candidate,
        physical_contract,
        target,
        artifact,
        bridge_activation,
    ) = _physical_transaction_setup()
    result = build_v6_transaction_branch_bundle(
        activation=activation,
        previous_checkpoint=previous,
        candidate=candidate,
        canonical_observation_event=event,
        market_context_result={
            "ok": True,
            "state": "available",
            "market_projection_available": True,
            "market_context": {
                "market_target_contract": target,
                "target_contract_sha256": target["target_contract_sha256"],
                "bridge_artifact_sha256": artifact["artifact_sha256"],
                "slug": "chengdu-high-2026-07-19",
            },
        },
        bridge_activation=bridge_activation,
        bridge_artifact=artifact,
        physical_resolution_contract={
            **physical_contract,
            "authority_contract_sha256": "f" * 64,
        },
        realization_policy=None,
        fact_snapshot_id="physical-tampered-bridge-fact",
        slug="chengdu-high-2026-07-19",
        event_date=DAY,
        fact_as_of="2026-07-19T05:00:00.200Z",
        correlation_id="physical-tampered-bridge-correlation",
        outbox_event_id="forbidden-outbox",
        inference_decision_deadline_at="2026-07-19T05:00:03Z",
    )
    assert result["writer_route"] == "physical_only"
    assert result["blocks_physical_x"] is False
    assert result["creates_outbox"] is False
    assert result["market_projection_available"] is False
    assert result["market_projection_na_reason"].startswith(
        "market_projection_branch_blocked"
    )
    ssot = json.loads(result["public_fact_row"]["fact_json"])[
        "posterior_ssot_fact"
    ]
    assert sum(ssot["reported_physical_x_distribution"].values()) == pytest.approx(
        1.0
    )
    assert ssot["market_bucket_probabilities"] == {}


def test_physical_transaction_rejects_stale_checkpoint_cas() -> None:
    (
        activation,
        p0,
        previous,
        event,
        candidate,
        _physical_contract,
        _target_contract,
        _artifact_value,
        _bridge_activation,
    ) = _physical_transaction_setup()
    conn = _clone_schema(ROOT / "data/archive.db")
    _insert(conn, "posterior_checkpoints", previous)
    _insert(conn, "production_initial_posterior_lineage", p0["initial_lineage_row"])
    _insert(
        conn,
        "canonical_observation_events",
        {key: value for key, value in event.items() if key != "parsed_observation"},
    )
    competitor = {
        **previous,
        "posterior_checkpoint_id": "physical-competing-checkpoint",
        "posterior_run_id": "physical-competing-run",
        "previous_checkpoint_id": previous["posterior_checkpoint_id"],
        "created_at": "2026-07-19T05:00:00.150Z",
        "committed_at": "2026-07-19T05:00:00.150Z",
    }
    _insert(conn, "posterior_checkpoints", competitor)
    conn.commit()
    with pytest.raises(V6TransactionConflict, match="predecessor_is_not_latest"):
        commit_v6_candidate_transaction(
            conn,
            activation=activation,
            previous_checkpoint=previous,
            candidate=candidate,
            canonical_observation_event=event,
            market_inputs=None,
            fact_snapshot_id="physical-stale-cas-fact",
            slug="chengdu-high-2026-07-19",
            event_date=DAY,
            fact_as_of="2026-07-19T05:00:00.200Z",
            correlation_id="physical-stale-cas-correlation",
        )


def test_physical_correction_event_is_not_silently_auto_consumed() -> None:
    (
        activation,
        p0,
        previous,
        event,
        _candidate,
        _physical_contract,
        _target_contract,
        _artifact_value,
        _bridge_activation,
    ) = _physical_transaction_setup()
    conn = _clone_schema(ROOT / "data/archive.db")
    _insert(conn, "posterior_checkpoints", previous)
    _insert(conn, "production_initial_posterior_lineage", p0["initial_lineage_row"])
    correction = {
        **{key: value for key, value in event.items() if key != "parsed_observation"},
        "canonical_observation_event_id": "physical-correction-event",
        "revision_no": 1,
        "state": "accepted_primary",
        "supersedes_event_id": event["canonical_observation_event_id"],
    }
    _insert(conn, "canonical_observation_events", correction)
    conn.commit()
    assert pending_v6_canonical_event_ids(
        conn,
        activation=activation,
        event_date=DAY,
    ) == []
    assert physical_v6_correction_event_ids(conn, event_date=DAY) == [
        "physical-correction-event"
    ]
    blocked = drain_v6_canonical_events(
        conn,
        activation=activation,
        event_date=DAY,
    )
    assert blocked["ok"] is False
    assert blocked["state"] == "blocked_correction_requires_manual_replay"
    assert blocked["automatic_reverse_update_forbidden"] is True
    latest = conn.execute(
        "SELECT consumed_event_ids_json FROM posterior_checkpoints ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    assert latest == "[]"


def test_physical_p0_materializes_atomic_basket_probability_without_outbox():
    (
        activation,
        p0,
        _previous,
        _event,
        _candidate,
        physical_contract,
        target,
        artifact,
        bridge_activation,
    ) = _physical_transaction_setup()
    market_inputs = {
        "market_projection_available": True,
        "market_context_result": {
            "ok": True,
            "state": "available",
            "market_projection_available": True,
            "market_context": {
                "market_target_contract": target,
                "target_contract_sha256": target["target_contract_sha256"],
                "bridge_artifact_sha256": artifact["artifact_sha256"],
            },
        },
        "bridge_activation": bridge_activation,
        "bridge_artifact": artifact,
        "physical_resolution_contract": physical_contract,
    }
    public = materialize_v6_initial_public_fact_row(
        activation=activation,
        checkpoint_row=p0["checkpoint_row"],
        fact_snapshot_id="physical-basket-p0-fact",
        slug="chengdu-high-2026-07-19",
        event_date=DAY,
        fact_as_of="2026-07-19T04:00:00Z",
        correlation_id="physical-basket-p0-correlation",
        market_inputs=market_inputs,
    )
    ssot = json.loads(public["fact_json"])["posterior_ssot_fact"]
    assert ssot["state"] == "available"
    assert ssot["strategy_projection_role"] == "basket_probability_only"
    assert sum(ssot["market_bucket_probabilities"].values()) == pytest.approx(1.0)
    assert ssot["last_canonical_event_id"] == ""
    conn = _clone_schema(ROOT / "data/archive.db")
    paths = _weak_likelihood()["forecast_scenario_paths"]
    weak_snapshot = {
        "forecast_evidence_snapshot_id": "weak-physical",
        "target_identity": TARGET,
        "event_date": DAY,
        "decision_as_of": "2026-07-19T04:00:00Z",
        "weak_likelihood_artifact_sha256": "3" * 64,
        "weak_likelihood_sha256": "4" * 64,
        "source_set_key": "physical-test",
        "source_revision_ids_json": "[]",
        "source_revisions_json": "[]",
        "temperature_grid_json": "[]",
        "likelihood_by_x_json": "{}",
        "forecast_peak_timing_prior_json": "{}",
        "snapshot_json": "{}",
        "snapshot_sha256": "5" * 64,
        "state": "active",
        "created_at": "2026-07-19T04:00:00Z",
        "forecast_scenario_paths_json": json.dumps(paths),
        "forecast_scenario_paths_sha256": paths["sha256"],
        "candidate_scenario_responsibilities_json": "{}",
        "candidate_scenario_responsibilities_sha256": "6" * 64,
    }
    committed = commit_v6_initial_posterior(
        conn,
        activation=activation,
        p0_candidate=p0,
        weak_snapshot_row=weak_snapshot,
        public_fact_row=public,
    )
    assert committed["ok"] is True
    assert committed["creates_outbox"] is False
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0


def test_physical_weak_revision_commits_new_basket_probability_without_three_leg():
    (
        activation,
        p0,
        _previous,
        _event,
        _candidate,
        physical_contract,
        target,
        artifact,
        bridge_activation,
    ) = _physical_transaction_setup()
    market_inputs = {
        "market_projection_available": True,
        "market_context_result": {
            "ok": True,
            "state": "available",
            "market_projection_available": True,
            "market_context": {
                "market_target_contract": target,
                "target_contract_sha256": target["target_contract_sha256"],
                "bridge_artifact_sha256": artifact["artifact_sha256"],
                "slug": "chengdu-high-2026-07-19",
            },
        },
        "bridge_activation": bridge_activation,
        "bridge_artifact": artifact,
        "physical_resolution_contract": physical_contract,
    }
    initial_public = materialize_v6_initial_public_fact_row(
        activation=activation,
        checkpoint_row=p0["checkpoint_row"],
        fact_snapshot_id="physical-weak-p0-fact",
        slug="chengdu-high-2026-07-19",
        event_date=DAY,
        fact_as_of="2026-07-19T04:00:00Z",
        correlation_id="physical-weak-p0-correlation",
        market_inputs=market_inputs,
    )
    paths = _weak_likelihood()["forecast_scenario_paths"]
    old_snapshot = {
        "forecast_evidence_snapshot_id": "weak-physical",
        "target_identity": TARGET,
        "event_date": DAY,
        "decision_as_of": "2026-07-19T04:00:00Z",
        "weak_likelihood_artifact_sha256": "3" * 64,
        "weak_likelihood_sha256": "4" * 64,
        "source_set_key": "physical-test",
        "source_revision_ids_json": json.dumps(["old-revision"]),
        "source_revisions_json": json.dumps(
            [{"source_id": "zuuu_taf_forecast_amsc", "source_revision_id": "old-revision"}]
        ),
        "temperature_grid_json": "[]",
        "likelihood_by_x_json": "{}",
        "forecast_peak_timing_prior_json": "{}",
        "snapshot_json": "{}",
        "snapshot_sha256": "5" * 64,
        "state": "active",
        "created_at": "2026-07-19T04:00:00Z",
        "forecast_scenario_paths_json": json.dumps(paths),
        "forecast_scenario_paths_sha256": paths["sha256"],
        "candidate_scenario_responsibilities_json": "{}",
        "candidate_scenario_responsibilities_sha256": "6" * 64,
    }
    conn = _clone_schema(ROOT / "data/archive.db")
    initial_commit = commit_v6_initial_posterior(
        conn,
        activation=activation,
        p0_candidate=p0,
        weak_snapshot_row=old_snapshot,
        public_fact_row=initial_public,
    )
    assert initial_commit["ok"] is True

    new_paths = _revised_weak_likelihood()["forecast_scenario_paths"]
    new_snapshot = {
        **old_snapshot,
        "forecast_evidence_snapshot_id": "weak-physical-revised",
        "decision_as_of": "2026-07-19T04:30:00Z",
        "weak_likelihood_sha256": "7" * 64,
        "source_revision_ids_json": json.dumps(["new-revision"]),
        "source_revisions_json": json.dumps(
            [{"source_id": "zuuu_taf_forecast_amsc", "source_revision_id": "new-revision"}]
        ),
        "snapshot_sha256": "8" * 64,
        "forecast_scenario_paths_json": json.dumps(new_paths),
        "forecast_scenario_paths_sha256": new_paths["sha256"],
    }
    candidate = compile_v6_weak_revision_candidate(
        activation=activation,
        previous_checkpoint=p0["checkpoint_row"],
        previous_snapshot=old_snapshot,
        revised_snapshot=new_snapshot,
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id="new-revision",
        revision_kind="amendment",
        decision_as_of="2026-07-19T04:30:00Z",
        previous_weak_likelihood=_weak_likelihood(),
        revised_weak_likelihood=_revised_weak_likelihood(),
        physical_state_model_artifact=_state_artifact(),
        physical_reporting_operator_artifact=_reporting_artifact(),
    )
    public = materialize_v6_weak_revision_public_fact_row(
        activation=activation,
        previous_fact_json=initial_public["fact_json"],
        checkpoint_row=candidate["checkpoint_row"],
        update_row=candidate["update_row"],
        fact_snapshot_id="physical-weak-revision-fact",
        slug="chengdu-high-2026-07-19",
        event_date=DAY,
        fact_as_of="2026-07-19T04:30:00Z",
        market_inputs=market_inputs,
    )
    result = commit_v6_weak_revision_transaction(
        conn,
        activation=activation,
        candidate=candidate,
        public_fact_row=public,
    )
    assert result["ok"] is True
    assert result["creates_outbox"] is False
    repeated = commit_v6_weak_revision_transaction(
        conn,
        activation=activation,
        candidate=candidate,
        public_fact_row=public,
    )
    assert repeated["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0
    assert conn.execute(
        "SELECT probability_update_schema FROM weak_forecast_revision_updates"
    ).fetchone()[0] == IDENTIFIABLE_PHYSICAL_UPDATE_SCHEMA
    latest = conn.execute(
        "SELECT physical_state_reference_valid_at,physical_driver_generation_sha256 "
        "FROM posterior_checkpoints ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert latest == ("2026-07-19T04:30:00Z", "b" * 64)
    fact = json.loads(public["fact_json"])["posterior_ssot_fact"]
    assert fact["strategy_projection_role"] == "basket_probability_only"
    assert sum(fact["market_bucket_probabilities"].values()) == pytest.approx(1.0)
    assert fact["market_projection_boundary"]["dynamic_three_leg_realization_consumed"] is False
    assert conn.execute(
        "SELECT state FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id='weak-physical'"
    ).fetchone()[0] == "superseded"
