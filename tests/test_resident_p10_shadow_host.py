from __future__ import annotations

import copy
import hashlib
import json
import sqlite3

import pytest

from dynamic_three_leg_shadow_decision_writer import initialize_isolated_dynamic_three_leg_shadow_decision_schema
from resident_p10_shadow_host import (
    ResidentP10ShadowHost,
)
from resident_p10_shadow_prepared_cycle import (
    freeze_resident_p10_shadow_cycle,
    initialize_resident_p10_shadow_prepared_cycle_schema,
    persist_resident_p10_shadow_cycle,
    validate_resident_p10_shadow_cycle,
)
from strong_observation_lr_runtime_registry import (
    activate_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)
from tests.strong_observation_artifact_fixtures import strong_activation_attestation
from resident_p10_shadow_bundle_runner import run_resident_p10_shadow_bundle
from resident_p10_shadow_service import ResidentP10ShadowService
from realization_state_projection_runtime import POLICY_SCHEMA_VERSION
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_schema_contract import apply_forward_schema
from source_observation_runtime import build_source_observation
from tests.strong_observation_artifact_fixtures import approved_strong_artifact


TARGET = "highest-temperature-in-chengdu-on-july-3-2026"


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
        "posterior_checkpoint_id": "pchk_host_previous_123456",
        "posterior_run_id": "post_host_previous_123456",
        "target_identity": TARGET,
        "event_date": "2026-07-03",
        "joint_posterior": joint,
        "joint_posterior_sha256": _hash(joint),
        "observed_max_lower_bound_c": 28.5,
        "consumed_event_ids": [],
    }


def _seed_previous_checkpoint(conn: sqlite3.Connection, previous: dict) -> None:
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
            previous["posterior_checkpoint_id"], TARGET, previous["posterior_run_id"], "", "initial",
            "forecast-current", "prior-current", "resolution-v1", "", "", "ancestor-initial",
            "temperature-grid", "state-grid", _json(previous["joint_posterior"]), previous["joint_posterior_sha256"],
            _json({"29": 0.5, "30": 0.5}), _json([]), _json({}), _json({}), _json({}), _json({}),
            28.5, _json([]), "ancestor-initial", "artifact-initial", 0, "", "shadow_ready",
            "2026-07-03T06:00:00Z", "2026-07-03T06:00:00Z",
        ),
    )


def _prepared_cycle(previous: dict) -> dict:
    market = {
        "market_snapshot_id": "market_host_1234567890abcdef",
        "buckets": {
            "29C": {"yes_ask": 0.25, "no_ask": 0.75},
            "30C": {
                "yes_ask": 0.65,
                "no_ask": 0.35,
                "yes_quote_id": "quote_host_1234567890abcdef",
                "yes_token_id": "token_host_1234567890abcdef",
                "yes_tick_size": "0.001",
                "yes_fee_schedule_id": "fee_host_1234567890abcdef",
            },
        },
    }
    return {
        "target_identity": TARGET,
        "event_date": "2026-07-03",
        "public_slug": TARGET,
        "previous_checkpoint": previous,
        "prewarmed_components": {
            "forecast_snapshot": {"ready": True, "forecast_snapshot_id": "forecast-current", "artifact_sha256": "forecast-artifact-host", "valid_until": "2026-07-03T07:30:00Z"},
            "prior_context": {"ready": True, "prior_context_id": "prior-current", "artifact_sha256": "prior-artifact-host", "valid_until": "2026-07-03T07:30:00Z"},
            "strong_lr_artifact": {"ready": True, "approved": True, "strong_lr_artifact_sha256": "strong-artifact-host", "valid_until": "2026-07-03T07:30:00Z"},
            "market_context": {"ready": True, "market_snapshot_id": market["market_snapshot_id"], "fresh_until": "2026-07-03T07:30:00Z"},
        },
        "posterior_context": {
            "forecast_evidence_snapshot_id": "forecast-current",
            "prior_context_id": "prior-current",
            "approved_lr_artifact": approved_strong_artifact(
                model_artifact_sha256="strong-artifact-host",
                raw_log_score_by_temperature_and_pre_state={"29|warming": -0.4, "30|warming": 0.4},
            ),
            "resolution_contract": {
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
            },
            "realization_policy": {
                "schema": POLICY_SCHEMA_VERSION,
                "artifact_sha256": "realization-policy-host",
                "approval_state": "approved",
                "credible_probability_floor": 0.05,
                "strong_support_log_probability_ratio_floor": 0.0,
                "strong_exclusion_probability_ceiling": 0.01,
                "strong_exclusion_log_probability_ratio_ceiling": -1.0,
            },
            "temperature_quantization_c": 1.0,
            "source_reliability_before": {"AMSC": {"state": "unknown"}},
            "source_reliability_after": {"AMSC": {"state": "unknown"}},
            "path_auxiliary": {"shadow_only": True},
        },
        "decision_context": {
            "lease_owner": "resident-p10-shadow-host",
            "market_snapshot": market,
            "capital_plan": {
                "capital_snapshot_id": "capital_host_1234567890abcdef",
                "risk_snapshot_id": "risk_host_1234567890abcdef",
                "capital_epoch_id": "capital_epoch_host_1234567890abcdef",
                "risk_reservation_id": "risk_reservation_host_1234567890abcdef",
                "notional_by_leg": {"top_yes_wave": 100.0, "second_yes_tail": 20.0, "safe_remote_no": 10.0, "trajectory_no_current_report": 10.0, "hard_zero_no": 15.0},
            },
            "strategy_artifact_sha256": "strategy-artifact-host",
            "top_yes_wave_entry_plan": {
                "schema": "top_yes_wave_entry_plan_v1",
                "approval_state": "approved",
                "market_alpha_policy_artifact_sha256": "market-alpha-policy-host",
                "market_alpha_policy_version": "market-alpha-policy-v1",
                "target_formula_input_sha256": "formula-input-host",
                "target_formula_output_sha256": "formula-output-host",
                "frozen_wave_target_price": 0.75,
                "max_holding_deadline": "2026-07-03T20:00:00Z",
                "target_bucket": "30C",
                "entry_market_snapshot_id": market["market_snapshot_id"],
                "strategy_artifact_sha256": "strategy-artifact-host",
            },
        },
    }


def _persisted_cycle_id(
    conn: sqlite3.Connection,
    previous: dict,
    *,
    cycle_input: dict | None = None,
) -> str:
    cycle = cycle_input if cycle_input is not None else _prepared_cycle(previous)
    artifact = cycle["posterior_context"]["approved_lr_artifact"]
    register_strong_lr_runtime_artifact(
        conn,
        artifact=artifact,
        registered_at="2026-07-03T06:00:00Z",
        registered_by="pytest",
        registration_basis="test_runtime_baseline",
        registration_ref="pytest://resident-p10",
    )
    activate_strong_lr_runtime_artifact(
        conn,
        target_identity=cycle["target_identity"],
        artifact_sha256=artifact["model_artifact_sha256"],
        activated_at="2026-07-03T06:00:00Z",
        activated_by="pytest",
        activation_basis="approved_runtime_baseline",
        activation_ref="pytest://resident-p10-approval",
        activation_evidence=strong_activation_attestation(
            target_identity=cycle["target_identity"],
            artifact_sha256=artifact["model_artifact_sha256"],
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://resident-p10-approval",
        ),
    )
    frozen = freeze_resident_p10_shadow_cycle(
        cycle,
        prepared_at="2026-07-03T06:01:00Z",
        prepared_by="resident-p10-shadow-test-preparer",
    )
    assert frozen["ok"] is True, frozen["blockers"]
    persisted = persist_resident_p10_shadow_cycle(conn, prepared_cycle=frozen["prepared_cycle"])
    assert persisted["ok"] is True
    return persisted["prepared_cycle_id"]


def test_p10_prepared_cycle_excludes_l5_signer_readiness() -> None:
    cycle = _prepared_cycle(_previous_checkpoint())

    frozen = freeze_resident_p10_shadow_cycle(
        cycle,
        prepared_at="2026-07-03T06:01:00Z",
        prepared_by="resident-p10-shadow-test-preparer",
    )

    assert frozen["ok"] is True, frozen["blockers"]
    assert frozen["validation"]["prewarm"]["purpose"] == "probability_ssot_inference"
    assert frozen["validation"]["topology"]["require_execution_readiness"] is False


def test_p10_prepared_cycle_rejects_l5_signer_input() -> None:
    cycle = _prepared_cycle(_previous_checkpoint())
    cycle["prewarmed_components"]["resident_signer_connection_pool"] = {
        "ready": True,
        "pool_id": "must-not-enter-p10",
        "valid_until": "2026-07-03T07:30:00Z",
    }

    frozen = freeze_resident_p10_shadow_cycle(
        cycle,
        prepared_at="2026-07-03T06:01:00Z",
        prepared_by="resident-p10-shadow-test-preparer",
    )

    assert frozen["ok"] is False
    assert "resident_p10_execution_component_forbidden" in frozen["blockers"]
    assert any(
        blocker.startswith("resident_p10_execution_or_secret_field_forbidden:prewarmed_components.resident_signer_connection_pool")
        for blocker in frozen["blockers"]
    )


def test_p10_prepared_cycle_rejects_nested_clob_or_secret_context() -> None:
    cycle = _prepared_cycle(_previous_checkpoint())
    cycle["decision_context"]["execution_context"] = {"clob_client": "must-not-enter-p10"}

    frozen = freeze_resident_p10_shadow_cycle(
        cycle,
        prepared_at="2026-07-03T06:01:00Z",
        prepared_by="resident-p10-shadow-test-preparer",
    )

    assert frozen["ok"] is False
    assert "resident_p10_execution_or_secret_field_forbidden:decision_context.execution_context.clob_client" in frozen["blockers"]


def test_physical_only_prepared_cycle_does_not_require_market_or_decision_context() -> None:
    cycle = _prepared_cycle(_previous_checkpoint())
    cycle["prewarmed_components"].pop("market_context")
    cycle["decision_context"] = {}
    resolution = cycle["posterior_context"]["resolution_contract"]
    resolution["bridge_closed"] = False
    resolution["validated"] = False
    resolution.pop("physical_to_settlement_kernel")
    resolution.pop("bucket_contract")

    frozen = freeze_resident_p10_shadow_cycle(
        cycle,
        prepared_at="2026-07-03T06:01:00Z",
        prepared_by="resident-p10-shadow-test-preparer",
    )

    assert frozen["ok"] is True
    validation = frozen["validation"]
    assert validation["market_projection_required"] is False
    assert validation["market_projection_prewarm"]["state"] == "not_applicable"
    assert validation["boundary"]["market_context_cannot_block_physical_posterior"] is True


def _source() -> dict:
    return build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:31:00Z",
        response_completed_at="2026-07-03T06:31:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="fixture://resident-p10-host",
        raw_payload={"source": "amsc"},
        payload={
            "report_type": "METAR",
            "arrival_time_quality": "observed_first_seen",
            "first_seen_at": "2026-07-03T06:31:00Z",
            "as_of_eligible_at": "2026-07-03T06:31:00Z",
        },
    ).as_dict()


def _archived_bundle(source: dict, *, manifest_suffix: str) -> dict:
    source["payload"]["raw_object_ref"] = f"objects/host/{manifest_suffix}.json.gz"
    source["payload"]["transport"] = {
        "raw_archive_manifest_ref": f"manifests/host/{manifest_suffix}.json",
        "raw_archive_manifest_sha256": f"host-manifest-{manifest_suffix}",
    }
    return {
        "schema": "amsc_zuuu_metar_taf_observation_bundle_v1",
        "read_only": True,
        "station_id": "ZUUU",
        "source_observations": [source],
        "raw_history_archive": {
            "manifest_ref": source["payload"]["transport"]["raw_archive_manifest_ref"],
            "manifest_sha256": source["payload"]["transport"]["raw_archive_manifest_sha256"],
            "raw_objects_by_sha256": {
                source["raw_sha256"]: {
                    "raw_sha256": source["raw_sha256"],
                    "raw_object_ref": source["payload"]["raw_object_ref"],
                    "first_seen_at": source["payload"]["first_seen_at"],
                    "arrival_time_quality": "observed_first_seen",
                }
            },
        },
    }


def test_prepared_resident_shadow_host_dispatches_without_network_or_refresh() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    initialize_resident_p10_shadow_prepared_cycle_schema(conn)
    previous = _previous_checkpoint()
    _seed_previous_checkpoint(conn, previous)
    cycle_id = _persisted_cycle_id(conn, previous)
    clock = iter([
        "2026-07-03T06:31:00.001Z", "2026-07-03T06:31:00.010Z", "2026-07-03T06:31:00.050Z",
        "2026-07-03T06:31:00.100Z", "2026-07-03T06:31:00.400Z", "2026-07-03T06:31:00.500Z",
        "2026-07-03T06:31:00.700Z",
    ])
    host = ResidentP10ShadowHost(conn, prepared_cycle_id=cycle_id, timestamp_observer=lambda: next(clock))

    result = host.process_source_observation(source_observation=_source())

    assert result["ok"] is True
    assert result["checkpoint_advanced"] is True
    assert result["current_checkpoint_id"] != previous["posterior_checkpoint_id"]
    assert host.current_checkpoint["consumed_event_ids"]
    assert host.current_checkpoint["source_event_ids"] == host.current_checkpoint["consumed_event_ids"]
    assert conn.execute("SELECT COUNT(*) FROM observation_fast_lane_latency_packets").fetchone()[0] == 1
    assert result["boundary"]["network_acquisition"] is False
    assert result["boundary"]["clob"] is False

    restarted = ResidentP10ShadowHost(
        conn,
        prepared_cycle_id=cycle_id,
        timestamp_observer=lambda: "2026-07-03T06:31:01.000Z",
    )
    assert restarted.current_checkpoint["posterior_checkpoint_id"] == host.current_checkpoint["posterior_checkpoint_id"]
    assert restarted.current_checkpoint["consumed_event_ids"] == host.current_checkpoint["consumed_event_ids"]


def test_controlled_bundle_runner_only_delivers_raw_archived_individual_amsc_arrivals() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    initialize_resident_p10_shadow_prepared_cycle_schema(conn)
    previous = _previous_checkpoint()
    _seed_previous_checkpoint(conn, previous)
    cycle_id = _persisted_cycle_id(conn, previous)
    bundle = _archived_bundle(_source(), manifest_suffix="source")
    clock = iter([
        "2026-07-03T06:31:00.001Z", "2026-07-03T06:31:00.010Z", "2026-07-03T06:31:00.050Z",
        "2026-07-03T06:31:00.100Z", "2026-07-03T06:31:00.400Z", "2026-07-03T06:31:00.500Z",
        "2026-07-03T06:31:00.700Z",
    ])

    result = run_resident_p10_shadow_bundle(
        conn,
        prepared_cycle_id=cycle_id,
        amsc_observation_bundle=bundle,
        timestamp_observer=lambda: next(clock),
    )

    assert result["ok"] is True
    assert result["bundle_dispatch"]["dispatch_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM observation_fast_lane_latency_packets").fetchone()[0] == 1
    assert result["boundary"]["network_acquisition"] is False
    assert result["boundary"]["scheduler_or_watcher"] is False
    assert result["boundary"]["clob"] is False


def test_resident_service_reuses_one_host_across_raw_archived_bundles() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    initialize_resident_p10_shadow_prepared_cycle_schema(conn)
    previous = _previous_checkpoint()
    _seed_previous_checkpoint(conn, previous)
    cycle_id = _persisted_cycle_id(conn, previous)
    clock = iter([
        "2026-07-03T06:31:00.001Z", "2026-07-03T06:31:00.010Z", "2026-07-03T06:31:00.050Z",
        "2026-07-03T06:31:00.100Z", "2026-07-03T06:31:00.400Z", "2026-07-03T06:31:00.500Z",
        "2026-07-03T06:31:00.700Z",
        "2026-07-03T07:01:00.001Z", "2026-07-03T07:01:00.010Z", "2026-07-03T07:01:00.050Z",
        "2026-07-03T07:01:00.100Z", "2026-07-03T07:01:00.400Z", "2026-07-03T07:01:00.500Z",
        "2026-07-03T07:01:00.700Z",
    ])
    service = ResidentP10ShadowService(
        conn,
        prepared_cycle_id=cycle_id,
        timestamp_observer=lambda: next(clock),
    )
    first = service.process_amsc_bundle(
        amsc_observation_bundle=_archived_bundle(_source(), manifest_suffix="first")
    )
    first_checkpoint_id = first["current_checkpoint_id"]
    second_source = build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T07:00:00Z",
        fetched_at="2026-07-03T07:01:00Z",
        response_completed_at="2026-07-03T07:01:00Z",
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref="fixture://resident-p10-service-second",
        raw_payload={"source": "amsc-service-second"},
        payload={
            "report_type": "SPECI",
            "arrival_time_quality": "observed_first_seen",
            "first_seen_at": "2026-07-03T07:01:00Z",
            "as_of_eligible_at": "2026-07-03T07:01:00Z",
        },
    ).as_dict()
    second = service.process_amsc_bundle(
        amsc_observation_bundle=_archived_bundle(second_source, manifest_suffix="second")
    )

    assert first["ok"] is True
    assert second["ok"] is True, second["bundle_dispatch"]["dispatches"][0]
    assert second["current_checkpoint_id"] != first_checkpoint_id
    assert service.readiness()["bundle_dispatch_count"] == 2
    assert service.readiness()["arrival_dispatch_count"] == 2
    assert conn.execute("SELECT COUNT(*) FROM observation_fast_lane_latency_packets").fetchone()[0] == 2
    assert service.readiness()["boundary"]["resident_host_reused_across_bundles"] is True
    assert service.readiness()["boundary"]["network_acquisition"] is False


def test_prepared_cycle_rejects_prewarm_to_posterior_identity_mismatch() -> None:
    cycle = _prepared_cycle(_previous_checkpoint())
    broken = copy.deepcopy(cycle)
    broken["prewarmed_components"]["forecast_snapshot"]["forecast_snapshot_id"] = "different-forecast"
    frozen = freeze_resident_p10_shadow_cycle(
        broken,
        prepared_at="2026-07-03T06:01:00Z",
        prepared_by="resident-p10-shadow-test-preparer",
    )

    validation = validate_resident_p10_shadow_cycle(frozen["prepared_cycle"])

    assert validation["ok"] is False
    assert "resident_p10_forecast_prewarm_posterior_binding_mismatch" in validation["blockers"]


def test_prepared_cycle_rejects_retired_static_strong_lr_artifact_before_hot_path() -> None:
    previous = _previous_checkpoint()
    broken = _prepared_cycle(previous)
    broken["posterior_context"]["approved_lr_artifact"] = {
        "model_artifact_sha256": "strong-artifact-host",
        "raw_log_score_by_temperature_and_pre_state": {"29|warming": -0.4, "30|warming": 0.4},
    }
    frozen = freeze_resident_p10_shadow_cycle(
        broken,
        prepared_at="2026-07-03T06:01:00Z",
        prepared_by="resident-p10-shadow-test-preparer",
    )

    assert frozen["ok"] is False
    assert any(
        blocker.startswith("resident_p10_strong_artifact_contract:strong_lr_artifact_schema_invalid")
        for blocker in frozen["blockers"]
    )


def test_prepared_cycle_rejects_caller_injected_source_reliability_post_state() -> None:
    previous = _previous_checkpoint()
    broken = _prepared_cycle(previous)
    broken["posterior_context"]["source_reliability_after"] = {"AMSC": {"state": "verified"}}
    frozen = freeze_resident_p10_shadow_cycle(
        broken,
        prepared_at="2026-07-03T06:01:00Z",
        prepared_by="resident-p10-shadow-test-preparer",
    )

    assert frozen["ok"] is False
    assert "resident_p10_external_source_reliability_post_state_update_not_allowed" in frozen["blockers"]


def test_host_only_loads_append_only_persisted_prepared_cycle() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    initialize_resident_p10_shadow_prepared_cycle_schema(conn)
    previous = _previous_checkpoint()
    _seed_previous_checkpoint(conn, previous)

    with pytest.raises(ValueError, match="resident_p10_prepared_cycle_missing"):
        ResidentP10ShadowHost(conn, prepared_cycle_id="p10pc_not_persisted")

    raw_cycle = _prepared_cycle(previous)
    cycle_id = _persisted_cycle_id(conn, previous, cycle_input=raw_cycle)
    raw_cycle["posterior_context"]["forecast_evidence_snapshot_id"] = "caller-mutation-must-not-reach-host"

    host = ResidentP10ShadowHost(conn, prepared_cycle_id=cycle_id)

    assert host.readiness()["prepared_cycle_id"] == cycle_id
    with pytest.raises(sqlite3.IntegrityError, match="resident_p10_prepared_cycle_append_only"):
        conn.execute(
            "UPDATE resident_p10_prepared_cycles_shadow SET prepared_by='tamper' WHERE prepared_cycle_id=?",
            (cycle_id,),
        )


def test_host_rejects_latest_checkpoint_from_a_different_frozen_dependency_set() -> None:
    conn = sqlite3.connect(":memory:")
    apply_forward_schema(conn)
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_isolated_dynamic_three_leg_shadow_decision_schema(conn)
    initialize_resident_p10_shadow_prepared_cycle_schema(conn)
    previous = _previous_checkpoint()
    _seed_previous_checkpoint(conn, previous)
    cycle_id = _persisted_cycle_id(conn, previous)
    conn.execute(
        "UPDATE posterior_checkpoints SET forecast_evidence_snapshot_id='different-frozen-forecast' WHERE posterior_checkpoint_id=?",
        (previous["posterior_checkpoint_id"],),
    )

    with pytest.raises(ValueError, match="resident_p10_shadow_latest_checkpoint_cycle_binding_mismatch"):
        ResidentP10ShadowHost(conn, prepared_cycle_id=cycle_id)
