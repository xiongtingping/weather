from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import bayes_basket_target.production_worker as production_worker_module
from bayes_basket_target.resource_bounds import MAX_JSON_OUTPUT_BYTES
from bayes_basket_target.production_worker import (
    CANONICAL_TARGET_STATIONS,
    DEFAULT_TARGET_MARKET_MANIFEST,
    MAX_RESIDENT_WS_SCOPES,
    MAX_PREPARED_CYCLES_PER_SCOPE,
    TargetProductionWorker,
    _upsert_native_p0_inbox,
    _manifest_station_allowlist,
    _resolve_target_remaining_budget,
    open_target_writer_if_enabled,
)
from bayes_basket_target.database import PROTECTED_RESIDENT_WS_MANIFEST
from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.probability_bridge import (
    materialize_probability_bridge,
)
from bayes_basket_target.metar_trajectory import (
    METAR_TRAJECTORY_AUTHORITY_SCOPE,
    METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION,
    build_metar_trajectory_likelihood_artifact,
)
from bayes_basket_target.future_temperature_curve import (
    build_root_future_temperature_curve,
)
from bayes_basket_target.metar_path_state import PATH_STATE_SCHEMA
from bayes_basket_target.native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
)
from bayes_basket_target.p0_historical_meta import (
    compile_p0_historical_zero_tilt_proof,
)
from bayes_basket_target.probability_evidence_store import (
    insert_probability_evidence_activation,
    insert_probability_evidence_artifact,
    insert_probability_evidence_promotion,
    metar_trajectory_scope_key,
)
from bayes_basket_target.production_io import (
    load_materialized_probability_envelopes,
)
from bayes_basket_target.runtime_config import load_target_no_live_runtime_config
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.stages import select_current_stage
from bayes_basket_target.taf_revision import (
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
)
from bayes_basket_target.taf_artifact_registry import (
    build_taf_artifact_registry,
)
from scripts.initialize_bayes_basket_target_ssot import initialize
from bayes_basket_target.fleet import expected_station_count


def _seed_target_registry_source_lane(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    source_lane: str,
) -> None:
    """Create the smallest immutable target-registry authority fixture."""

    apply_target_market_registry_schema(conn)
    snapshot_sha = content_sha256(
        {
            "fixture": "target-registry-source-lane",
            "station_id": station_id,
            "event_date": event_date,
            "source_lane": source_lane,
        }
    )
    conn.execute(
        "INSERT OR IGNORE INTO basket_target_market_registry_snapshot("
        "snapshot_sha256,source_schema,source_scope,exported_at_utc,"
        "source_database_path,row_count,ingested_at_utc,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            snapshot_sha,
            "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only",
            "2026-07-26T12:00:00Z",
            "/test/shared-live.db",
            1,
            "2026-07-26T12:00:00Z",
            content_sha256({"snapshot_sha": snapshot_sha}),
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO basket_target_market_registry("
        "snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
        "settlement_timezone,settlement_unit,source_lane,source_eligible,"
        "source_block_reason,resolution_url,rules_sha256,yes_token,no_token,"
        "market_active,market_accepting_orders,registry_json,refreshed_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_sha,
            f"fixture-{station_id.lower()}-{event_date}",
            event_date,
            "fixture-outcome",
            "fixture-city",
            station_id,
            settlement_timezone,
            "C",
            source_lane,
            1,
            "",
            "",
            "a" * 64,
            "fixture-yes",
            "fixture-no",
            1,
            1,
            json.dumps({"market_id": "fixture-market"}),
            "2026-07-26T12:00:00Z",
        ),
    )


def _compiled_native_p0_source_packet(
    provider: str,
    *,
    aggregator_fallback_p0_eligible: bool = False,
    packet_content_sha256: str | None = None,
) -> dict[str, object]:
    lineage = {
        "native_transport_provider": provider,
    }
    if provider == "OPEN_METEO":
        lineage["aggregator_fallback_p0_eligible"] = (
            "true" if aggregator_fallback_p0_eligible else "false"
        )
        lineage["republisher_fallback_p0_eligible"] = (
            "true" if aggregator_fallback_p0_eligible else "false"
        )
        lineage["transport_lineage_class"] = "aggregator_republisher"
        lineage["native_arrival_index_eligible"] = "false"
        lineage["native_formal_p0_eligible"] = "false"
    packet = {
        "station_id": "RKPK",
        "event_date": "2026-08-01",
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_probability_envelope": {
            "lineage": lineage,
        },
    }
    return {
        "schema": "calibrated_native_cross_ensemble_p0_packet_v1",
        "source_class": "calibrated_native_cross_ensemble",
        "packet": packet,
        "packet_content_sha256": (
            packet_content_sha256
            if packet_content_sha256 is not None
            else content_sha256(packet)
        ),
    }


def test_current_stage_routes_p1_to_final_atomic_candidate_and_pre_p1_to_stage_zero() -> None:
    worker = object.__new__(TargetProductionWorker)
    calls: list[str] = []

    def stage_zero(*_args, **_kwargs):
        calls.append("stage_zero")
        return {"state": "stage_zero"}

    def final_candidate(*_args, **_kwargs):
        calls.append("final_candidate")
        return {"state": "final_candidate"}

    worker._commit_stage_zero_execution_candidate = stage_zero
    worker._commit_candidate = final_candidate

    p0_result = worker._commit_current_stage_no_execution_candidate(
        {"probability_envelope": {"stage": "P0"}},
        station_id="RKPK",
        event_date="2026-08-01",
        created_at_utc="2026-07-31T18:00:00Z",
    )
    p1_result = worker._commit_current_stage_no_execution_candidate(
        {"probability_envelope": {"stage": "P1"}},
        station_id="RKPK",
        event_date="2026-07-31",
        created_at_utc="2026-07-31T18:00:00Z",
    )

    assert p0_result["state"] == "stage_zero"
    assert p1_result["state"] == "final_candidate"
    assert calls == ["stage_zero", "final_candidate"]


def _selected_native_p0_provider(path: Path) -> str:
    inbox = json.loads(path.read_text(encoding="utf-8"))
    packet = inbox["packets"][0]["packet"]
    return str(
        packet["native_probability_envelope"]["lineage"]
        ["native_transport_provider"]
    )


def test_native_p0_inbox_mixed_official_beats_aggregator_and_keeps_p0_fingerprint_gate(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "native-p0-inbox.json"
    _upsert_native_p0_inbox(
        inbox_path,
        compiled_packets=[
            _compiled_native_p0_source_packet(
                "OPEN_METEO",
                aggregator_fallback_p0_eligible=True,
            ),
        ],
        generated_at_utc="2026-08-01T00:00:00Z",
    )
    mixed = _compiled_native_p0_source_packet(
        "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT",
        packet_content_sha256="bad" * 16,
    )
    _upsert_native_p0_inbox(
        inbox_path,
        compiled_packets=[mixed],
        generated_at_utc="2026-08-01T00:01:00Z",
    )

    assert _selected_native_p0_provider(inbox_path) == (
        "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT"
    )

    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = sqlite3.connect(":memory:")
    worker.p0_inbox_path = inbox_path
    result = worker.materialize_resident_p0(
        as_of_utc="2026-08-01T01:00:00Z",
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any(
        "resident_p0_packet_content_sha_mismatch" in blocker["reason"]
        for blocker in result["blockers"]
    )
    worker.commit_conn.close()


def test_native_p0_inbox_same_and_mixed_are_equal_official_priority_over_aggregator(
    tmp_path: Path,
) -> None:
    for provider in (
        "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
        "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT",
    ):
        inbox_path = tmp_path / f"{provider}.json"
        _upsert_native_p0_inbox(
            inbox_path,
            compiled_packets=[
                _compiled_native_p0_source_packet(
                    "OPEN_METEO",
                    aggregator_fallback_p0_eligible=True,
                ),
            ],
            generated_at_utc="2026-08-01T00:00:00Z",
        )
        _upsert_native_p0_inbox(
            inbox_path,
            compiled_packets=[
                _compiled_native_p0_source_packet(provider),
            ],
            generated_at_utc="2026-08-01T00:01:00Z",
        )

        assert _selected_native_p0_provider(inbox_path) == provider


def test_native_p0_inbox_excludes_unproved_aggregator_and_expired_official_handoff_rows(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "native-p0-inbox.json"
    official = _compiled_native_p0_source_packet(
        "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
    )
    official["packet"]["native_probability_envelope"]["fresh_until_utc"] = (  # type: ignore[index]
        "2026-08-01T02:00:00Z"
    )
    _upsert_native_p0_inbox(
        inbox_path,
        compiled_packets=[official],
        generated_at_utc="2026-08-01T00:00:00Z",
    )
    # A subsequent update must remove both the expired official handoff
    # packet and a republisher packet.  Neither can poison a fresh P0 batch.
    _upsert_native_p0_inbox(
        inbox_path,
        compiled_packets=[
            _compiled_native_p0_source_packet("OPEN_METEO"),
        ],
        generated_at_utc="2026-08-01T03:00:00Z",
    )
    inbox = json.loads(inbox_path.read_text(encoding="utf-8"))

    assert inbox["packets"] == []


def test_native_p0_inbox_keeps_proved_republisher_fallback_until_official(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "native-p0-inbox.json"
    fallback = _compiled_native_p0_source_packet(
        "OPEN_METEO",
        aggregator_fallback_p0_eligible=True,
    )
    fallback["packet"]["native_probability_envelope"]["fresh_until_utc"] = (  # type: ignore[index]
        "2026-08-01T02:00:00Z"
    )
    _upsert_native_p0_inbox(
        inbox_path,
        compiled_packets=[fallback],
        generated_at_utc="2026-08-01T00:00:00Z",
    )

    assert _selected_native_p0_provider(inbox_path) == "OPEN_METEO"

    _upsert_native_p0_inbox(
        inbox_path,
        compiled_packets=[
            _compiled_native_p0_source_packet(
                "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
            ),
        ],
        generated_at_utc="2026-08-01T00:01:00Z",
    )

    assert _selected_native_p0_provider(inbox_path) == (
        "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT"
    )


def test_materialize_resident_p0_rejects_unproved_open_meteo_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox_path = tmp_path / "native-p0-inbox.json"
    row = _compiled_native_p0_source_packet("OPEN_METEO")
    inbox_path.write_text(json.dumps({
        "schema": "bayes_basket_target_p0_inbox_v1",
        "packets": [row],
    }), encoding="utf-8")
    envelope = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0-open-meteo-marker-test",
        "station_id": "RKPK",
        "event_date": "2026-08-01",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-08-01T00:00:00Z",
        "fresh_until_utc": "2026-08-01T02:00:00Z",
        "bucket_ordinals": {"34": 0, "35": 1},
        "probabilities": {"34": 0.4, "35": 0.6},
        "posterior_version": "p0-native-test",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "native_transport_provider": "OPEN_METEO",
        },
    })
    monkeypatch.setattr(
        production_worker_module,
        "build_probability_envelope",
        lambda _packet: envelope,
    )
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = sqlite3.connect(":memory:")
    worker.p0_inbox_path = inbox_path

    result = worker.materialize_resident_p0(
        as_of_utc="2026-08-01T01:00:00Z",
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert any(
        "resident_p0_native_transport_required"
        in blocker["reason"]
        for blocker in result["blockers"]
    )
    worker.commit_conn.close()


def test_native_p0_inbox_old_same_cycle_replay_does_not_replace_selected_mixed(
    tmp_path: Path,
) -> None:
    inbox_path = tmp_path / "native-p0-inbox.json"
    _upsert_native_p0_inbox(
        inbox_path,
        compiled_packets=[
            _compiled_native_p0_source_packet(
                "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT",
            ),
        ],
        generated_at_utc="2026-08-01T00:01:00Z",
    )
    _upsert_native_p0_inbox(
        inbox_path,
        compiled_packets=[
            _compiled_native_p0_source_packet(
                "OFFICIAL_NATIVE_SAME_CYCLE_SNAPSHOT",
            ),
        ],
        generated_at_utc="2026-08-01T00:02:00Z",
    )

    assert _selected_native_p0_provider(inbox_path) == (
        "OFFICIAL_NATIVE_MIXED_CYCLE_SNAPSHOT"
    )


def _activate_formal_trajectory_artifact(
    conn: sqlite3.Connection,
    *,
    local_time_bucket: str,
):
    activation_id = (
        f"formal-rkpk-{local_time_bucket.lower()}-activation"
    )
    artifact = build_metar_trajectory_likelihood_artifact({
        "activation_id": activation_id,
        "station_id": "RKPK",
        "source_lane": "awc",
        "season_bin": "JJA",
        "local_time_bucket": local_time_bucket,
        "maturity_bin": "D0",
        "settlement_definition": "station_local_calendar_day_tmax",
        "settlement_timezone": "Asia/Seoul",
        "feature_definitions": {
            "running_high": "canonical prefix running maximum",
            "remaining_warming": "strict forward OOS distribution",
        },
        "missing_observation_policy": "DEGRADE_STRENGTH",
        "expected_interval_minutes": 60,
        "long_interval_minutes": 90,
        "max_missing_intervals": 2,
        "missing_interval_strength_factor": 0.7,
        "long_interval_strength_factor": 0.8,
        "path_feature_schema": PATH_STATE_SCHEMA,
        "path_model_coefficients": {
            "base_distribution_mean": 0.3,
            "root_expected_remaining_warming": 0.5,
            "momentum_c_per_hour": 0.35,
            "acceleration_c_per_hour2": 0.1,
            "drawdown_from_high_c": -0.5,
            "dewpoint_depression_c": 0.01,
            "cloud_cover_fraction": -0.8,
            "precipitation_present": -1.0,
            "wind_speed_mps": -0.02,
            "remaining_hours": 0.02,
            "predicted_peak_remaining_warming_c": 0.35,
            "minutes_to_predicted_peak": 0.02,
            "next_curve_delta_from_high_c": 0.15,
            "future_curve_slope_c_per_hour": 0.10,
        },
        "path_model_sigma_c": 0.8,
        "remaining_warming_distribution": {
            "0": 0.25,
            "1": 0.55,
            "2": 0.20,
        },
        "hard_exclusion_baseline_distribution": {
            "0": 0.60,
            "1": 0.30,
            "2": 0.10,
        },
        "base_strength": 0.6,
        "maturity_strength": 0.8,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "sample_n": 80,
        "effective_sample_n": 64.0,
        "oos_metrics": {
            "brier_score": 0.11,
            "nll": 0.38,
            "crps": 0.50,
        },
        "tail_calibration_error": 0.04,
        "trained_through_utc": "2026-07-01T00:00:00Z",
        "valid_from_utc": "2026-07-20T00:00:00Z",
        "valid_until_utc": "2026-08-20T00:00:00Z",
        "promotion_state": "FORMAL_PROMOTED",
        "authority_scope": METAR_TRAJECTORY_AUTHORITY_SCOPE,
        "activated": True,
        "method_version": (
            METAR_TRAJECTORY_CONDITIONAL_HOURLY_CURVE_METHOD_VERSION
        ),
    })
    scope_key = metar_trajectory_scope_key(
        source_lane="awc",
        season_bin="JJA",
        local_time_bucket=local_time_bucket,
        maturity_bin="D0",
        settlement_definition="station_local_calendar_day_tmax",
    )
    insert_probability_evidence_artifact(conn, {
        "artifact_id": artifact.artifact_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "scope_key": scope_key,
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_gate_passed": True,
        "authority_scope": "FORMAL_PROMOTED",
        "artifact": artifact.to_dict(),
        "content_sha256": artifact.content_sha256,
    })
    promotion_identity = {
        "promotion_id": f"{activation_id}-promotion",
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "decision_seq": 1,
        "decision": "PROMOTED",
        "gate_version": "strict-forward-oos-v1",
        "metrics_sha256": "6" * 64,
        "decided_at_utc": "2026-07-19T00:00:00Z",
        "receipt_sha256": "7" * 64,
    }
    insert_probability_evidence_promotion(conn, {
        **promotion_identity,
        "content_sha256": content_sha256(promotion_identity),
    })
    activation_identity = {
        "activation_id": activation_id,
        "artifact_kind": "METAR_TRAJECTORY_LIKELIHOOD",
        "artifact_id": artifact.artifact_id,
        "station_id": "RKPK",
        "scope_key": scope_key,
        "generation": 1,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "activated_at_utc": "2026-07-20T00:00:00Z",
        "authority_scope": "FORMAL_PROMOTED",
    }
    insert_probability_evidence_activation(conn, {
        **activation_identity,
        "content_sha256": content_sha256(activation_identity),
    })
    return artifact


def _taf_live_compatibility_lineage() -> dict[str, str]:
    system_ids = ["ECMWF_IFS_ENS_TEST", "NOAA_GEFS_TEST"]
    family_ids = ["ECMWF", "NOAA_GEFS"]
    system_families = {
        "ECMWF_IFS_ENS_TEST": "ECMWF",
        "NOAA_GEFS_TEST": "NOAA_GEFS",
    }
    system_weights = {
        "ECMWF_IFS_ENS_TEST": 0.45,
        "NOAA_GEFS_TEST": 0.55,
    }
    family_weights = {"ECMWF": 0.45, "NOAA_GEFS": 0.55}
    family_caps = {"ECMWF": 0.45, "NOAA_GEFS": 1.0}
    system_artifacts = {
        "ECMWF_IFS_ENS_TEST": "3" * 64,
        "NOAA_GEFS_TEST": "4" * 64,
    }
    system_activations = {
        "ECMWF_IFS_ENS_TEST": "6" * 64,
        "NOAA_GEFS_TEST": "7" * 64,
    }
    identity = {
        "schema": TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "station_id": "RKPK",
        "season_bin": "summer",
        "lead_bucket": "d2_fallback",
        "run_cycle": "18Z",
        "settlement_definition": "official_daily_tmax_c",
        "available_ensemble_system_set_hash": content_sha256(system_ids),
        "ensemble_system_ids": system_ids,
        "independence_family_ids": family_ids,
        "system_family_ids": system_families,
        "resolved_system_weights": system_weights,
        "resolved_family_weights": family_weights,
        "family_weight_caps": family_caps,
        "cross_ensemble_artifact_sha256": "1" * 64,
        "cross_selection_evidence_sha256": "2" * 64,
        "cross_activation_sha256": "8" * 64,
        "system_calibration_artifacts": {
            system_id: {
                "artifact_content_sha256": system_artifacts[system_id],
                "activation_content_sha256": system_activations[system_id],
            }
            for system_id in system_ids
        },
    }
    fingerprint = content_sha256(identity)
    payload = {**identity, "fingerprint_sha256": fingerprint}
    return {
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "p0_probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "native_cross_ensemble_artifact_sha256": "1" * 64,
        "native_selection_evidence_sha256": "2" * 64,
        "native_cross_activation_sha256": "8" * 64,
        "native_probability_evidence_sha256": "9" * 64,
        "ensemble_system_ids_json": json.dumps(system_ids),
        "independence_family_ids_json": json.dumps(family_ids),
        "system_family_ids_json": json.dumps(system_families),
        "system_weights_json": json.dumps(system_weights),
        "family_weight_caps_json": json.dumps(family_caps),
        "system_artifact_shas_json": json.dumps(system_artifacts),
        "system_activation_shas_json": json.dumps(system_activations),
        "taf_nwp_live_compatibility_fingerprint_sha256": fingerprint,
        "taf_nwp_live_compatibility_json": json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _future_curve_lineage(
    *,
    checkpoint_id: str,
    posterior_sha256: str,
) -> dict[str, str]:
    curve = build_root_future_temperature_curve({
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "root_checkpoint_id": checkpoint_id,
        "root_probability_sha256": posterior_sha256,
        "available_at_utc": "2026-07-26T14:00:00Z",
        "source_snapshot_sha256": "c" * 64,
        "independence_family_ids": ["ECMWF_IFS", "NOAA_GEFS"],
        "points": [
            {"valid_time_utc": "2026-07-26T15:00:00Z", "temperature_c": 27.0},
            {"valid_time_utc": "2026-07-26T18:00:00Z", "temperature_c": 29.0},
            {"valid_time_utc": "2026-07-26T21:00:00Z", "temperature_c": 30.0},
            {"valid_time_utc": "2026-07-27T00:00:00Z", "temperature_c": 29.0},
        ],
    })
    return {
        "conditional_future_temperature_curve_json": json.dumps(
            curve.to_dict(), sort_keys=True, separators=(",", ":")
        ),
        "conditional_future_temperature_curve_sha256": curve.content_sha256,
    }


def test_commit_candidate_preserves_delta_v2_authority_blocker() -> None:
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = object()
    candidate = {
        "production_inputs": {
            "delta_v2_resolution_blocker": (
                "delta_v2_activation_missing"
            ),
        },
    }

    with pytest.raises(
        ContractError,
        match="delta_v2_activation_missing",
    ):
        worker._commit_candidate(
            candidate,
            station_id="RKPK",
            event_date="2026-07-28",
            created_at_utc="2026-07-28T02:30:00Z",
        )


def _bridged_taf_artifact(tmp_path: Path) -> Path:
    source = (
        Path(__file__).resolve().parents[1]
        / "config/artifacts/bayes_basket_target_taf_revision_active.json"
    )
    raw = json.loads(source.read_text(encoding="utf-8"))
    fingerprint = _taf_live_compatibility_lineage()[
        "taf_nwp_live_compatibility_fingerprint_sha256"
    ]
    raw.update({
        "settlement_timezone": "Asia/Seoul",
        "season_bin": "summer",
        "lead_bucket": "d2_fallback",
        "run_cycle": "18Z",
        "trained_through_utc": "2026-06-30T00:00:00Z",
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-31T00:00:00Z",
        "nwp_combination_artifact_kind": (
            TAF_NWP_CROSSFIT_ARTIFACT_KIND
        ),
        "nwp_live_compatibility_contract_version": (
            TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ),
        "nwp_live_compatibility_fingerprint_sha256": fingerprint,
        "nwp_live_compatibility_evidence_sha256": "5" * 64,
        "nwp_live_compatibility_validated": True,
    })
    identity = {
        key: value for key, value in raw.items() if key != "content_sha256"
    }
    path = tmp_path / "bridged-taf-artifact.json"
    path.write_text(
        json.dumps(
            {**identity, "content_sha256": content_sha256(identity)}
        ),
        encoding="utf-8",
    )
    return path


def _retired_deterministic_nwp_refresh_wires_registry_capture_and_p0_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,stage TEXT,state TEXT,"
        "decision_at_utc TEXT,fresh_until_utc TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?)",
        (
            "RKPK",
            "2026-07-27",
            "P0",
            "COMMITTED",
            "2026-07-26T01:00:00Z",
            "2026-07-26T03:00:00Z",
        ),
    )
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.canonical_source_db_path = tmp_path / "archive.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ("RKPK",)
    worker.event_date_allowlist = ("2026-07-27",)
    capture_path = tmp_path / "capture.json"
    capture_path.write_text("{}", encoding="utf-8")
    calls = []

    def export(**kwargs):
        calls.append(("export", kwargs))
        assert kwargs["station_id"] == "RKPK"
        assert kwargs["event_date"] == "2026-07-27"
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        return {"state": "EXPORTED", "snapshot_sha256": "a" * 64}

    monkeypatch.setattr(production_worker_module, "export_shared_live_registry", export)
    monkeypatch.setattr(
        production_worker_module,
        "ingest_target_market_registry",
        lambda *args, **kwargs: {"state": "COMMITTED", "wrote_rows": True},
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "a" * 64, "rows": []},
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *args, **kwargs: [{
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["RKPK"],
        }],
    )
    capture_calls = []

    def capture(**kwargs):
        capture_calls.append(kwargs)
        return {
            "state": "CAPTURED",
            "capture_paths": [str(capture_path)],
        }

    monkeypatch.setattr(
        production_worker_module,
        "capture_official_nwp_scope",
        capture,
    )
    monkeypatch.setattr(
        production_worker_module,
        "candidate_run_initializations",
        lambda **kwargs: [
            "2026-07-25T18:00:00Z",
            "2026-07-25T12:00:00Z",
            "2026-07-25T06:00:00Z",
            "2026-07-25T00:00:00Z",
        ],
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_production_artifacts",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        production_worker_module,
        "compatible_run_candidates",
        lambda **kwargs: [
            {
                "run_initialization_utc": value,
                "activation_binding": {"selected_models": ["ECMWF", "GFS"]},
                "ensemble_activation": {"activation_id": value},
                "ensemble_artifact": {"ensemble_artifact_id": value},
            }
            for value in kwargs["run_initializations"]
        ],
    )
    def refresh_p0(**kwargs):
        station = kwargs["output_root"].name
        packet = {
            "station_id": station,
            "event_date": kwargs["event_date"],
            "decision_at_utc": "2026-07-26T01:30:00Z",
            "fresh_until_utc": "2026-07-26T07:30:00Z",
        }
        identity = {
            "schema": "bayes_basket_target_p0_inbox_v1",
            "generated_at_utc": "2026-07-26T01:30:00Z",
            "packets": [{
                "packet": packet,
                "packet_content_sha256": content_sha256(packet),
            }],
        }
        kwargs["inbox_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["inbox_path"].write_text(json.dumps({
            **identity,
            "content_sha256": content_sha256(identity),
        }))
        return {
            "state": "updated",
            "packet_count": 1,
            "calls_order_gateway": False,
        }

    monkeypatch.setattr(
        production_worker_module,
        "refresh_resident_p0",
        refresh_p0,
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-26T02:00:00Z",
    )

    assert result["state"] == "updated"
    assert result["scope"]["station_ids"] == ["RKPK"]
    assert result["writes_target_database"] is True
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0
    assert result["run_fallback_policy"] == (
        "NEWEST_ACTIVATED_ONLY_CURRENT_P0_FRESH"
    )
    assert capture_calls[0]["run_initializations"] == [
        "2026-07-25T18:00:00Z"
    ]
    assert capture_calls[0]["activation_bindings_by_run"] == {
        "2026-07-25T18:00:00Z": {
            "activation_binding": {
                "selected_models": ["ECMWF", "GFS"],
            },
            "ensemble_activation": {
                "activation_id": "2026-07-25T18:00:00Z",
            },
            "ensemble_artifact": {
                "ensemble_artifact_id": "2026-07-25T18:00:00Z",
            },
        }
    }
    assert [name for name, _ in calls] == ["export"]


def test_resident_nwp_prefetch_can_run_with_read_only_target_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "archive.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()

    def export(**kwargs):
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        return {"state": "EXPORTED", "snapshot_sha256": "a" * 64}

    monkeypatch.setattr(
        production_worker_module, "export_shared_live_registry", export
    )
    monkeypatch.setattr(
        production_worker_module,
        "ingest_target_market_registry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("forecast prefetch must not write target registry")
        ),
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "a" * 64, "rows": []},
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *args, **kwargs: [],
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-29T07:00:00Z",
    )

    assert result["state"] == "no_due_registry_scope"
    assert result["registry_ingress"]["state"] == (
        "deferred_to_production_writer"
    )
    assert result["writes_target_database"] is False


def test_resident_nwp_reuses_registry_worker_catalog_without_narrow_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = None
    worker.canonical_source_db_path = tmp_path / "archive.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ("RKPK",)
    worker.event_date_allowlist = ()
    registry_path = worker.nwp_runtime_root / "market_registry_forward.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        production_worker_module,
        "load_market_registry_snapshot",
        lambda _path: {
            "schema": "bayes_basket_target_market_registry_snapshot_v1",
            "snapshot_sha256": "b" * 64,
            "row_count": 495,
            "source_database_path": str(worker.canonical_source_db_path),
            "rows": [],
        },
    )
    monkeypatch.setattr(
        production_worker_module,
        "export_shared_live_registry",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("NWP must not overwrite resident registry export")
        ),
    )
    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        lambda *args, **kwargs: [],
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-29T16:10:00Z",
    )

    assert result["registry_export"]["state"] == (
        "REUSED_RESIDENT_REGISTRY_EXPORT"
    )
    assert result["registry_export"]["row_count"] == 495
    assert result["registry_export"]["decoupled_from_nwp"] is True
    assert result["registry_ingress"]["state"] == (
        "deferred_to_production_writer"
    )


def _retired_deterministic_nwp_skips_incompatible_scope_without_starving_next_station(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT,event_date TEXT,stage TEXT,state TEXT,"
        "decision_at_utc TEXT)"
    )
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.canonical_source_db_path = tmp_path / "archive.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.p0_inbox_path = tmp_path / "p0-inbox.json"
    worker.db_path = tmp_path / "target.db"
    worker.station_allowlist = ()
    worker.event_date_allowlist = ()
    capture_path = tmp_path / "capture.json"
    capture_path.write_text("{}", encoding="utf-8")

    def export(**kwargs):
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output_path"].write_text("{}", encoding="utf-8")
        return {"state": "EXPORTED", "snapshot_sha256": "a" * 64}

    scopes = [
        {
            "event_date": "2026-07-28",
            "settlement_timezone": "Asia/Taipei",
            "station_ids": ["RCSS"],
        },
        {
            "event_date": "2026-07-28",
            "settlement_timezone": "Asia/Seoul",
            "station_ids": ["RKSI"],
        },
    ]
    monkeypatch.setattr(
        production_worker_module, "export_shared_live_registry", export
    )
    monkeypatch.setattr(
        production_worker_module,
        "ingest_target_market_registry",
        lambda *args, **kwargs: {
            "state": "COMMITTED",
            "wrote_rows": True,
        },
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_market_registry_snapshot",
        lambda _path: {"snapshot_sha256": "a" * 64, "rows": []},
    )

    def capture_scopes(*args, **kwargs):
        assert kwargs["max_scopes"] == (
            production_worker_module.MAX_DISCOVERED_SCOPES
        )
        return scopes

    monkeypatch.setattr(
        production_worker_module,
        "registry_capture_scopes",
        capture_scopes,
    )
    monkeypatch.setattr(
        production_worker_module,
        "candidate_run_initializations",
        lambda **kwargs: [
            "2026-07-27T12:00:00Z",
            "2026-07-27T06:00:00Z",
        ],
    )

    def artifacts(*args, **kwargs):
        if kwargs["station_id"] == "RCSS":
            raise ContractError("calibration_artifact_missing")
        return object()

    monkeypatch.setattr(
        production_worker_module,
        "load_production_artifacts",
        artifacts,
    )
    monkeypatch.setattr(
        production_worker_module,
        "compatible_run_candidates",
        lambda **kwargs: [
            {
                "run_initialization_utc": value,
                "activation_binding": {"selected_models": ["GFS", "ICON"]},
                "ensemble_activation": {"activation_id": value},
                "ensemble_artifact": {"ensemble_artifact_id": value},
            }
            for value in kwargs["run_initializations"]
        ],
    )
    captured_scopes = []
    captured_runs = []
    captured_bindings = []

    def capture(**kwargs):
        captured_scopes.append(kwargs["scope"])
        captured_runs.append(kwargs["run_initializations"])
        captured_bindings.append(kwargs["activation_bindings_by_run"])
        return {
            "state": "CAPTURED",
            "capture_paths": [str(capture_path)],
        }

    monkeypatch.setattr(
        production_worker_module,
        "capture_official_nwp_scope",
        capture,
    )
    def refresh_p0(**kwargs):
        station = kwargs["output_root"].name
        packet = {
            "station_id": station,
            "event_date": kwargs["event_date"],
            "decision_at_utc": "2026-07-27T22:30:00Z",
            "fresh_until_utc": "2026-07-28T04:30:00Z",
        }
        identity = {
            "schema": "bayes_basket_target_p0_inbox_v1",
            "generated_at_utc": "2026-07-27T22:30:00Z",
            "packets": [{
                "packet": packet,
                "packet_content_sha256": content_sha256(packet),
            }],
        }
        kwargs["inbox_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["inbox_path"].write_text(json.dumps({
            **identity,
            "content_sha256": content_sha256(identity),
        }))
        return {
            "state": "updated",
            "packet_count": 1,
            "calls_order_gateway": False,
        }

    monkeypatch.setattr(
        production_worker_module,
        "refresh_resident_p0",
        refresh_p0,
    )

    result = worker.refresh_resident_nwp(
        as_of_utc="2026-07-27T22:45:00Z",
    )

    assert result["state"] == "updated"
    assert result["scope"]["station_ids"] == ["RKSI"]
    assert captured_scopes == [scopes[1]]
    assert captured_runs == [[
        "2026-07-27T12:00:00Z",
        "2026-07-27T06:00:00Z",
    ]]
    assert captured_bindings == [{
        "2026-07-27T12:00:00Z": {
            "activation_binding": {
                "selected_models": ["GFS", "ICON"],
            },
            "ensemble_activation": {
                "activation_id": "2026-07-27T12:00:00Z",
            },
            "ensemble_artifact": {
                "ensemble_artifact_id": "2026-07-27T12:00:00Z",
            },
        },
        "2026-07-27T06:00:00Z": {
            "activation_binding": {
                "selected_models": ["GFS", "ICON"],
            },
            "ensemble_activation": {
                "activation_id": "2026-07-27T06:00:00Z",
            },
            "ensemble_artifact": {
                "ensemble_artifact_id": "2026-07-27T06:00:00Z",
            },
        }
    }]
    assert result["run_fallback_policy"] == (
        "BOUNDED_MATURE_FALLBACK"
    )
    assert result["skipped_incompatible_scopes"] == [
        {
            "station_id": "RCSS",
            "event_date": "2026-07-28",
            "reason": (
                "resident_nwp_scope_failed:"
                "ContractError:calibration_artifact_missing"
            ),
        }
    ]
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_resident_taf_inbox_materializes_p0m_before_p1(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    _seed_target_registry_source_lane(
        conn,
        station_id="RKPK",
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
        source_lane="awc",
    )
    root = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0-rkpk-resident-taf",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T10:00:00Z",
        "fresh_until_utc": "2026-07-28T00:00:00Z",
        "bucket_ordinals": {"34": 0, "35": 1, "36": 2},
        "probabilities": {"34": 0.2, "35": 0.6, "36": 0.2},
        "posterior_version": "p0-test",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "settlement_timezone": "Asia/Seoul",
            "season_bin": "summer",
            "lead_bucket": "d2_fallback",
            "run_cycle": "18Z",
            "settlement_definition": "official_daily_tmax_c",
                "bucket_centers_c_json": json.dumps(
                    {"34": 34.0, "35": 35.0, "36": 36.0},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "bucket_intervals_c_json": json.dumps(
                    {
                        "34": [None, 34.5],
                        "35": [34.5, 35.5],
                        "36": [35.5, None],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            **_taf_live_compatibility_lineage(),
        },
    })
    materialize_probability_bridge(
        conn, root, created_at_utc=root.decision_at_utc
    )
    stale_root = ProbabilityEnvelope.from_mapping({
        **root.to_dict(),
        "checkpoint_id": "p0-rkpk-stale-old-scope",
        "event_date": "2026-07-26",
        "decision_at_utc": "2026-07-25T10:00:00Z",
        "fresh_until_utc": "2026-07-26T11:00:00Z",
        "posterior_version": "p0-stale-old-scope",
        "posterior_sha256": "9" * 64,
        "lineage": {
            **root.lineage,
            "lead_bucket": "d1",
            "run_cycle": "06Z",
        },
    })
    insert_probability_envelope(
        conn,
        stale_root,
        created_at_utc=stale_root.decision_at_utc,
    )
    revision = {
        "schema": "bayes_basket_forecast_revision_v1",
        "revision_id": "taf-rkpk-forward-1",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": "2026-07-26T11:00:00Z",
        "received_at_utc": "2026-07-26T11:05:00Z",
        "valid_from_utc": "2026-07-26T11:00:00Z",
        "valid_until_utc": "2026-07-27T18:00:00Z",
        "source_object_sha256": "c" * 64,
        "supersedes_revision_id": None,
    }
    capture_identity = {
        "schema": "bayes_basket_target_forward_taf_capture_v1",
        "source_class": "official_forward_native_receipt",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "forecast_revision": revision,
        "taf_fields": {
            "tx_c": 35.0,
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
        },
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    stale_revision = {
        **revision,
        "revision_id": "taf-rkpk-stale-old-scope",
        "event_date": "2026-07-26",
        "issued_at_utc": "2026-07-25T11:00:00Z",
        "received_at_utc": "2026-07-25T11:05:00Z",
        "valid_from_utc": "2026-07-25T11:00:00Z",
        "valid_until_utc": "2026-07-26T18:00:00Z",
        "source_object_sha256": "8" * 64,
    }
    stale_capture_identity = {
        **capture_identity,
        "event_date": "2026-07-26",
        "forecast_revision": stale_revision,
    }
    inbox = tmp_path / "taf-inbox.json"
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [
            {
                **stale_capture_identity,
                "content_sha256": content_sha256(stale_capture_identity),
            },
            {
                **capture_identity,
                "content_sha256": content_sha256(capture_identity),
            },
        ],
    }), encoding="utf-8")
    db = tmp_path / "target-read.db"
    with sqlite3.connect(db) as source:
        source.execute("CREATE TABLE marker(value TEXT)")
    source_artifact = _bridged_taf_artifact(tmp_path)
    artifact = tmp_path / "taf-artifact-registry.json"
    artifact.write_text(
        json.dumps(build_taf_artifact_registry([
            json.loads(source_artifact.read_text(encoding="utf-8")),
        ])),
        encoding="utf-8",
    )
    worker = TargetProductionWorker(
        db,
        commit_conn=conn,
        taf_artifact_path=artifact,
        taf_inbox_path=inbox,
    )
    result = worker._materialize_resident_p0m(
        as_of_utc="2026-07-26T12:00:00Z"
    )
    assert result["state"] == "COMMITTED"
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_bridge_history "
        "WHERE stage='P0-M'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) "
        "FROM basket_probability_bridge_bucket_probability b "
        "JOIN basket_probability_bridge_history h "
        "ON h.history_id=b.history_id WHERE h.stage='P0-M'"
    ).fetchone()[0] == 3
    assert result["writes_target_ssot"] is True
    assert result["ignored_inactive_scope_count"] == 1
    assert any(
        item["event_date"] == "2026-07-26"
        and item["state"] == "IGNORED_STALE_P0_SCOPE"
        for item in result["receipts"]
    )
    row = conn.execute(
        "SELECT stage,lineage_json FROM basket_probability_envelope "
        "WHERE station_id='RKPK' AND event_date='2026-07-27' "
        "ORDER BY decision_at_utc DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "P0-M"
    first_lineage = json.loads(row[1])
    assert first_lineage["forecast_revision_id"] == "taf-rkpk-forward-1"
    assert first_lineage["p0_root_checkpoint_id"] == root.checkpoint_id

    refreshed_root = ProbabilityEnvelope.from_mapping({
        **root.to_dict(),
        "checkpoint_id": "p0-rkpk-resident-taf-refresh",
        "decision_at_utc": "2026-07-26T11:30:00Z",
        "probabilities": {"34": 0.1, "35": 0.5, "36": 0.4},
        "posterior_version": "p0-test-refresh",
        "posterior_sha256": "d" * 64,
    })
    insert_probability_envelope(
        conn,
        refreshed_root,
        created_at_utc=refreshed_root.decision_at_utc,
    )
    replay = worker._materialize_resident_p0m(
        as_of_utc="2026-07-26T12:00:00Z"
    )
    assert replay["state"] == "COMMITTED"
    assert replay["writes_target_ssot"] is True
    p0m_rows = conn.execute(
        "SELECT checkpoint_id,lineage_json FROM basket_probability_envelope "
        "WHERE station_id='RKPK' AND event_date='2026-07-27' "
        "AND stage='P0-M' ORDER BY decision_at_utc"
    ).fetchall()
    assert len(p0m_rows) == 2
    assert json.loads(p0m_rows[0][1]) == first_lineage
    refreshed_lineage = json.loads(p0m_rows[1][1])
    assert refreshed_lineage["forecast_revision_id"] == "taf-rkpk-forward-1"
    assert (
        refreshed_lineage["p0_root_checkpoint_id"]
        == refreshed_root.checkpoint_id
    )
    assert (
        refreshed_lineage["p0_root_content_sha256"]
        == refreshed_root.content_sha256
    )

    conflicting_capture = {
        **capture_identity,
        "taf_fields": {
            **capture_identity["taf_fields"],
            "tx_c": 36.0,
        },
    }
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [{
            **conflicting_capture,
            "content_sha256": content_sha256(conflicting_capture),
        }],
    }), encoding="utf-8")
    conflicting = worker._materialize_resident_p0m(
        as_of_utc="2026-07-26T12:00:00Z"
    )
    assert conflicting["state"] == "BLOCKED_FAIL_CLOSED"
    assert any(
        reason in conflicting["blockers"][0]["reason"]
        for reason in (
            "resident_taf_revision_content_conflict",
            "probability_stage_decision_time_conflict",
            "forward_taf_capture_content_conflict",
        )
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope WHERE stage='P0-M'"
    ).fetchone()[0] == 2

    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [{
            **capture_identity,
            "content_sha256": content_sha256(capture_identity),
        }],
    }), encoding="utf-8")

    replay_again = worker._materialize_resident_p0m(
        as_of_utc="2026-07-26T12:00:00Z"
    )
    assert replay_again["state"] == "COMMITTED"
    assert replay_again["writes_target_ssot"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope WHERE stage='P0-M'"
    ).fetchone()[0] == 2
    tampered = {
        **capture_identity,
        "taf_fields": {
            **capture_identity["taf_fields"],
            "tx_c": 99.0,
        },
        "content_sha256": content_sha256(capture_identity),
    }
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [tampered],
    }), encoding="utf-8")
    with pytest.raises(ContractError, match="sha_mismatch"):
        worker._materialize_resident_p0m(
            as_of_utc="2026-07-26T12:00:00Z"
        )
    retrospective_identity = {
        **capture_identity,
        "source_class": "official_date_addressed_retrospective_capture",
        "boundary": {
            **capture_identity["boundary"],
            "native_local_receipt": False,
            "retrospective_replay_only": True,
        },
    }
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": [{
            **retrospective_identity,
            "content_sha256": content_sha256(retrospective_identity),
        }],
    }), encoding="utf-8")
    with pytest.raises(ContractError, match="receipt_required"):
        worker._materialize_resident_p0m(
            as_of_utc="2026-07-26T12:00:00Z"
        )
    worker.close()


def test_resident_taf_legacy_crossfit_hash_without_live_bridge_blocks(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    inbox = tmp_path / "taf-inbox.json"
    inbox.write_text(
        json.dumps({
            "schema": "bayes_basket_target_taf_revision_inbox_v1",
            "captures": [],
        }),
        encoding="utf-8",
    )
    db = tmp_path / "target-read.db"
    with sqlite3.connect(db) as source:
        source.execute("CREATE TABLE marker(value TEXT)")
    active_path = (
        Path(__file__).resolve().parents[1]
        / "config/artifacts/"
        "bayes_basket_target_taf_revision_active.json"
    )
    legacy_raw = json.loads(active_path.read_text(encoding="utf-8"))
    for key in (
        "nwp_combination_artifact_kind",
        "nwp_live_compatibility_contract_version",
        "nwp_live_compatibility_fingerprint_sha256",
        "nwp_live_compatibility_evidence_sha256",
        "nwp_live_compatibility_validated",
    ):
        legacy_raw.pop(key, None)
    legacy_identity = {
        key: value
        for key, value in legacy_raw.items()
        if key != "content_sha256"
    }
    legacy_path = tmp_path / "legacy-taf-artifact.json"
    legacy_path.write_text(
        json.dumps({
            **legacy_identity,
            "content_sha256": content_sha256(legacy_identity),
        }),
        encoding="utf-8",
    )
    worker = TargetProductionWorker(
        db,
        commit_conn=conn,
        taf_artifact_path=legacy_path,
        taf_inbox_path=inbox,
    )

    result = worker._materialize_resident_p0m(
        as_of_utc="2026-07-27T17:05:00Z"
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert result["writes_target_ssot"] is False
    assert result["orders_submitted"] == 0
    assert result["blockers"] == [{
        "station_id": "RKPK",
        "event_date": "",
        "reason": (
            "ContractError:"
            "resident_taf_nwp_live_compatibility_bridge_missing"
        ),
    }]
    worker.close()


def test_rkpk_taf_artifact_never_blocks_another_station_p0_lane(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    revision = {
        "schema": "bayes_basket_forecast_revision_v1",
        "revision_id": "taf-katl-forward-1",
        "station_id": "KATL",
        "event_date": "2026-07-30",
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": "2026-07-29T12:00:00Z",
        "received_at_utc": "2026-07-29T12:05:00Z",
        "valid_from_utc": "2026-07-29T12:00:00Z",
        "valid_until_utc": "2026-07-30T18:00:00Z",
        "source_object_sha256": "c" * 64,
        "supersedes_revision_id": None,
    }
    capture_identity = {
        "schema": "bayes_basket_target_forward_taf_capture_v1",
        "source_class": "official_forward_native_receipt",
        "station_id": "KATL",
        "event_date": "2026-07-30",
        "settlement_timezone": "America/New_York",
        "forecast_revision": revision,
        "taf_fields": {
            "tx_c": 35.0,
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
        },
        "boundary": {
            "native_local_receipt": True,
            "retrospective_replay_only": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    inbox = tmp_path / "taf-katl-inbox.json"
    inbox.write_text(
        json.dumps({
            "schema": "bayes_basket_target_taf_revision_inbox_v1",
            "captures": [{
                **capture_identity,
                "content_sha256": content_sha256(capture_identity),
            }],
        }),
        encoding="utf-8",
    )
    db = tmp_path / "target-read.db"
    with sqlite3.connect(db) as source:
        source.execute("CREATE TABLE marker(value TEXT)")
    worker = TargetProductionWorker(
        db,
        commit_conn=conn,
        taf_artifact_path=_bridged_taf_artifact(tmp_path),
        taf_inbox_path=inbox,
    )
    result = worker._materialize_resident_p0m(
        as_of_utc="2026-07-29T13:00:00Z",
    )
    assert result["state"] == "COMMITTED"
    assert result["blockers"] == []
    assert result["ignored_no_artifact_scope_count"] == 1
    assert result["receipts"] == [{
        "station_id": "KATL",
        "event_date": "2026-07-30",
        "state": "IGNORED_NO_EXACT_TAF_ARTIFACT",
        "write_count": 0,
        "writes": [],
    }]
    assert result["writes_target_ssot"] is False
    assert result["calls_order_gateway"] is False
    worker.close()


def test_resident_taf_revision_chain_rolls_back_on_second_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    _seed_target_registry_source_lane(
        conn,
        station_id="RKPK",
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
        source_lane="awc",
    )
    root = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0-rkpk-taf-atomic",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T10:00:00Z",
        "fresh_until_utc": "2026-07-28T00:00:00Z",
        "bucket_ordinals": {"34": 0, "35": 1, "36": 2},
        "probabilities": {"34": 0.2, "35": 0.6, "36": 0.2},
        "posterior_version": "p0-taf-atomic-test",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "settlement_timezone": "Asia/Seoul",
            "season_bin": "summer",
            "lead_bucket": "d2_fallback",
            "run_cycle": "18Z",
            "settlement_definition": "official_daily_tmax_c",
                "bucket_centers_c_json": json.dumps(
                    {"34": 34.0, "35": 35.0, "36": 36.0},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "bucket_intervals_c_json": json.dumps(
                    {
                        "34": [None, 34.5],
                        "35": [34.5, 35.5],
                        "36": [35.5, None],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            **_taf_live_compatibility_lineage(),
        },
    })
    materialize_probability_bridge(
        conn, root, created_at_utc=root.decision_at_utc
    )

    revisions = [
        {
            "schema": "bayes_basket_forecast_revision_v1",
            "revision_id": "taf-rkpk-atomic-1",
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "revision_kind": "TAF",
            "sequence": 1,
            "issued_at_utc": "2026-07-26T11:00:00Z",
            "received_at_utc": "2026-07-26T11:05:00Z",
            "valid_from_utc": "2026-07-26T11:00:00Z",
            "valid_until_utc": "2026-07-27T18:00:00Z",
            "source_object_sha256": "c" * 64,
            "supersedes_revision_id": None,
        },
        {
            "schema": "bayes_basket_forecast_revision_v1",
            "revision_id": "taf-rkpk-atomic-2",
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "revision_kind": "AMD",
            "sequence": 2,
            "issued_at_utc": "2026-07-26T11:30:00Z",
            "received_at_utc": "2026-07-26T11:35:00Z",
            "valid_from_utc": "2026-07-26T11:30:00Z",
            "valid_until_utc": "2026-07-27T18:00:00Z",
            "source_object_sha256": "d" * 64,
            "supersedes_revision_id": "taf-rkpk-atomic-1",
        },
    ]
    captures = []
    for revision, tx_c in zip(revisions, (35.0, 36.0)):
        identity = {
            "schema": "bayes_basket_target_forward_taf_capture_v1",
            "source_class": "official_forward_native_receipt",
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "forecast_revision": revision,
            "taf_fields": {
                "tx_c": tx_c,
                "cloud_cover_fraction": 0.25,
                "precipitation_probability": 0.1,
                "wind_speed_mps": 4.0,
            },
            "boundary": {
                "native_local_receipt": True,
                "retrospective_replay_only": False,
                "calls_order_gateway": False,
                "places_orders": False,
            },
        }
        captures.append({
            **identity,
            "content_sha256": content_sha256(identity),
        })
    inbox = tmp_path / "taf-atomic-inbox.json"
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": captures,
    }), encoding="utf-8")
    artifact = _bridged_taf_artifact(tmp_path)
    worker = _worker(tmp_path, conn)
    worker.taf_artifact_path = artifact
    worker.taf_inbox_path = inbox
    calls = 0

    def fail_after_second_insert(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = insert_probability_envelope(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("injected_second_p0m_write_failure")
        return result

    monkeypatch.setattr(
        production_worker_module,
        "insert_probability_envelope",
        fail_after_second_insert,
    )

    result = worker._materialize_resident_p0m(
        as_of_utc="2026-07-26T12:00:00Z"
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert result["writes_target_ssot"] is False
    assert "injected_second_p0m_write_failure" in result["blockers"][0]["reason"]
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope WHERE stage='P0-M'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_bridge_history "
        "WHERE stage='P0-M'"
    ).fetchone()[0] == 0
    worker.close()


def _worker(tmp_path: Path, commit_conn: sqlite3.Connection) -> TargetProductionWorker:
    db = tmp_path / "target-read.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE marker(value TEXT)")
    return TargetProductionWorker(db, commit_conn=commit_conn)


def _retired_deterministic_p0_commits_historical_proof_and_envelope_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    worker = _worker(tmp_path, conn)
    worker.p0_inbox_path = tmp_path / "p0-proof-inbox.json"
    proof = compile_p0_historical_zero_tilt_proof(
        station_id="RKPK",
        target_local_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
        settlement_definition="station_local_calendar_day_tmax",
        season_bin="JJA",
        lead_bucket="d1",
        run_cycle="00Z",
        as_of_utc="2026-07-26T10:00:00Z",
        model_probabilities={"34": 0.4, "35": 0.6},
        bucket_centers_c={"34": 34.0, "35": 35.0},
        ensemble_artifact_sha256="a" * 64,
        ensemble_selection_evidence_sha256="b" * 64,
        ensemble_activation_sha256="c" * 64,
    )
    packet = {
        "station_id": "RKPK",
        "p0_historical_tilt_proof": proof.to_dict(),
    }
    worker.p0_inbox_path.write_text(json.dumps({
        "schema": "bayes_basket_target_p0_inbox_v1",
        "packets": [{
            "source_class": "official_forward_model_bundle",
            "packet": packet,
            "packet_content_sha256": content_sha256(packet),
        }],
    }), encoding="utf-8")
    envelope = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "p0-rkpk-history-proof",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T10:00:00Z",
        "fresh_until_utc": "2026-07-28T00:00:00Z",
        "bucket_ordinals": {"34": 0, "35": 1},
        "probabilities": {"34": 0.4, "35": 0.6},
        "posterior_version": "p0_nwp_historical_meta_anchor_v4",
        "posterior_sha256": "d" * 64,
        "bridge_sha256": "b" * 64,
        "lineage": {
            "settlement_timezone": "Asia/Seoul",
            "season_bin": "JJA",
            "settlement_definition": (
                "station_local_calendar_day_tmax"
            ),
            "bucket_intervals_c_json": json.dumps(
                {"34": [None, 34.5], "35": [34.5, None]},
                sort_keys=True,
                separators=(",", ":"),
            ),
            "p0_historical_tilt_proof_id": proof.proof_id,
            "p0_historical_tilt_proof_sha256": proof.content_sha256,
        },
    })
    monkeypatch.setattr(
        production_worker_module,
        "build_probability_envelope",
        lambda _packet: envelope,
    )

    first = worker.materialize_resident_p0(
        as_of_utc="2026-07-26T12:00:00Z"
    )
    replay = worker.materialize_resident_p0(
        as_of_utc="2026-07-26T12:00:00Z"
    )

    assert first["state"] == "COMMITTED"
    assert first["writes_target_ssot"] is True
    assert replay["writes_target_ssot"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_bridge_history "
        "WHERE checkpoint_id=?",
        (envelope.checkpoint_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT stage FROM basket_probability_bridge_latest "
        "WHERE station_id='RKPK' AND event_date='2026-07-27'"
    ).fetchone()[0] == "P0"
    stored = conn.execute(
        "SELECT proof_id,content_sha256 FROM "
        "basket_probability_update_proof "
        "WHERE proof_kind='P0_HISTORICAL_TILT'"
    ).fetchone()
    assert tuple(stored) == (proof.proof_id, proof.content_sha256)
    worker.close()


def _retired_deterministic_p0_inbox_rolls_back_every_packet_on_second_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    worker = _worker(tmp_path, conn)
    inbox = tmp_path / "p0-inbox.json"
    worker.p0_inbox_path = inbox
    packets = [
        {"station_id": station_id, "sequence": sequence}
        for sequence, station_id in enumerate(("RKPK", "RKSI"), start=1)
    ]
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_p0_inbox_v1",
        "packets": [
            {
                "source_class": "official_forward_model_bundle",
                "packet": packet,
                "packet_content_sha256": content_sha256(packet),
            }
            for packet in packets
        ],
    }), encoding="utf-8")

    def build(packet):
        station_id = str(packet["station_id"])
        return ProbabilityEnvelope.from_mapping({
            "stage": "P0",
            "checkpoint_id": f"p0-{station_id.lower()}-atomic",
            "station_id": station_id,
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "decision_at_utc": "2026-07-26T10:00:00Z",
            "fresh_until_utc": "2026-07-28T00:00:00Z",
            "bucket_ordinals": {"34": 0, "35": 1},
            "probabilities": {"34": 0.4, "35": 0.6},
            "posterior_version": "p0-atomic-test",
            "posterior_sha256": "a" * 64,
            "bridge_sha256": "b" * 64,
            "lineage": {
                "settlement_timezone": "Asia/Seoul",
                "season_bin": "JJA",
                "settlement_definition": (
                    "station_local_calendar_day_tmax"
                ),
                "bucket_intervals_c_json": json.dumps(
                    {"34": [None, 34.5], "35": [34.5, None]},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        })

    calls = 0

    def fail_after_second_insert(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = insert_probability_envelope(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("injected_second_p0_write_failure")
        return result

    monkeypatch.setattr(
        production_worker_module,
        "build_probability_envelope",
        build,
    )
    monkeypatch.setattr(
        production_worker_module,
        "insert_probability_envelope",
        fail_after_second_insert,
    )

    result = worker.materialize_resident_p0(
        as_of_utc="2026-07-26T12:00:00Z"
    )

    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert result["writes_target_ssot"] is False
    assert "injected_second_p0_write_failure" in result["blockers"][0]["reason"]
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope"
    ).fetchone()[0] == 0
    worker.close()


def _retired_deterministic_p0_same_scope_clock_conflict_fails_closed_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    worker = _worker(tmp_path, conn)
    inbox = tmp_path / "p0-conflict-inbox.json"
    packet_rows = [
        {"station_id": "RKPK", "version": "v1"},
        {"station_id": "RKPK", "version": "v2"},
    ]
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_p0_inbox_v1",
        "packets": [
            {
                "source_class": "official_forward_model_bundle",
                "packet": packet,
                "packet_content_sha256": content_sha256(packet),
            }
            for packet in packet_rows
        ],
    }), encoding="utf-8")
    worker.p0_inbox_path = inbox

    def build(packet):
        return ProbabilityEnvelope.from_mapping({
            "stage": "P0",
            "checkpoint_id": f"p0-{packet['version']}",
            "station_id": packet["station_id"],
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "decision_at_utc": "2026-07-26T10:00:00Z",
            "fresh_until_utc": "2026-07-28T00:00:00Z",
            "bucket_ordinals": {"34": 0, "35": 1},
            "probabilities": {"34": 0.4, "35": 0.6},
            "posterior_version": f"p0-{packet['version']}",
            "posterior_sha256": ("a" if packet["version"] == "v1" else "c") * 64,
            "bridge_sha256": "b" * 64,
            "lineage": {
                "settlement_timezone": "Asia/Seoul",
                "season_bin": "JJA",
                    "settlement_definition": (
                        "station_local_calendar_day_tmax"
                    ),
                    "bucket_intervals_c_json": json.dumps(
                        {"34": [None, 34.5], "35": [34.5, None]},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
            },
        })

    monkeypatch.setattr(
        production_worker_module,
        "build_probability_envelope",
        build,
    )
    result = worker.materialize_resident_p0(
        as_of_utc="2026-07-26T12:00:00Z"
    )
    assert result["state"] == "BLOCKED_FAIL_CLOSED"
    assert "resident_p0_scope_content_conflict" in result["blockers"][0]["reason"]
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope"
    ).fetchone()[0] == 0
    worker.close()


def _retired_deterministic_p0_catalog_bridge_upgrade_is_semantic_zero_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    apply_production_ssot_schema(conn)
    worker = _worker(tmp_path, conn)
    inbox = tmp_path / "p0-bridge-upgrade-inbox.json"
    packet = {"station_id": "RKPK", "version": "scope-bridge-v2"}
    inbox.write_text(json.dumps({
        "schema": "bayes_basket_target_p0_inbox_v1",
        "packets": [{
            "source_class": "official_forward_model_bundle",
            "packet": packet,
            "packet_content_sha256": content_sha256(packet),
        }],
    }), encoding="utf-8")
    worker.p0_inbox_path = inbox
    common = {
        "stage": "P0",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T10:00:00Z",
        "fresh_until_utc": "2026-07-28T00:00:00Z",
        "bucket_ordinals": {"34": 0, "35": 1},
        "probabilities": {"34": 0.4, "35": 0.6},
        "posterior_version": "p0_nwp_probability_stack_v3",
        "lineage": {
            "settlement_timezone": "Asia/Seoul",
            "model_run_ids": "ecmwf,gfs",
            "bucket_intervals_c_json": (
                '{"34":[33.5,34.5],"35":[34.5,35.5]}'
            ),
        },
    }
    existing = ProbabilityEnvelope.from_mapping({
        **common,
        "checkpoint_id": "p0-catalog-root-bridge",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
    })
    candidate = ProbabilityEnvelope.from_mapping({
        **common,
        "checkpoint_id": "p0-station-day-bridge",
        "posterior_sha256": "c" * 64,
        "bridge_sha256": "d" * 64,
    })
    insert_probability_envelope(
        conn,
        existing,
        created_at_utc=existing.decision_at_utc,
    )
    conn.commit()
    monkeypatch.setattr(
        production_worker_module,
        "build_probability_envelope",
        lambda _packet: candidate,
    )

    result = worker.materialize_resident_p0(
        as_of_utc="2026-07-26T12:00:00Z"
    )

    assert result["state"] == "COMMITTED"
    assert result["writes_target_ssot"] is False
    assert result["receipts"][0]["state"] == (
        "ALREADY_COMMITTED_EQUIVALENT_BRIDGE_UPGRADE"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM basket_probability_envelope"
    ).fetchone()[0] == 1
    worker.close()


def test_resident_probability_chain_has_one_explicit_stage_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object.__new__(TargetProductionWorker)
    calls: list[str] = []

    monkeypatch.setattr(
        worker,
        "materialize_resident_p0",
        lambda *, as_of_utc: calls.append("P0") or {"state": "COMMITTED"},
    )
    monkeypatch.setattr(
        worker,
        "_materialize_resident_p0m",
        lambda *, as_of_utc: calls.append("P0-M") or {"state": "NO_NEW_TAF_REVISION"},
    )
    monkeypatch.setattr(
        worker,
        "_materialize_resident_p1",
        lambda *, as_of_utc: calls.append("P1") or {"state": "NO_NEW_REPORTED_HIGH"},
    )

    result = worker._materialize_resident_probability_chain(
        as_of_utc="2026-07-26T12:00:00Z"
    )
    assert calls == ["P0", "P0-M", "P1"]
    assert result["stage_order"] == ["P0", "P0-M", "P1"]
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_station_scoped_ws_manifest_preserves_explicit_allowlist() -> None:
    assert _manifest_station_allowlist({
        "station_scope": {
            "mode": "allowlist",
            "station_ids": ["rkpk", "RKSI", "rkpk"],
        }
    }) == ("RKPK", "RKSI")
    assert _manifest_station_allowlist({
        "station_scope": {"mode": "full_registry", "station_ids": []}
    }) == ()


def test_prepared_cycle_retention_guard_does_not_append_again(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT,plan_id TEXT,state TEXT,station_id TEXT,event_date TEXT,created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?,?)",
        ("cycle-1", "plan-1", "prepared", "ZUUU", "2026-07-24", "2026-07-24T00:00:00Z"),
    )
    worker = _worker(tmp_path, conn)
    try:
        result = worker._scope_cycle_guard(
            station_id="ZUUU",
            event_date="2026-07-24",
            candidate={"plan": {"plan_id": "new-plan"}},
        )
    finally:
        worker.close()
    assert result is not None
    assert result["state"] == "retained_existing_prepared_candidate"
    assert result["writes_target_ledger"] is False
    assert result["max_prepared_cycles_per_scope"] == MAX_PREPARED_CYCLES_PER_SCOPE == 1


def _forward_worker_source_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE auto_trade_decisions("
        "decision_id TEXT,decision_sha256 TEXT,reason_codes_json TEXT,"
        "details_json TEXT,cycle_id TEXT,created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_policy_training_fact("
        "cycle_id TEXT,decision_id TEXT,policy_version_vector_json TEXT,"
        "feature_json TEXT)"
    )
    return conn


def _insert_forward_source_row(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    source_sha: str = "a" * 64,
    fact: bool = True,
) -> int:
    details = '{"forward_observation_status":"ELIGIBLE"}'
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?,?,?,?,?)",
        (
            decision_id,
            source_sha,
            "[\"reason\"]",
            details,
            f"cycle-{decision_id}",
            "2026-07-27T00:00:00Z",
        ),
    )
    rowid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    if fact:
        conn.execute(
            "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?)",
            (
                f"cycle-{decision_id}",
                decision_id,
                '{"policy":"v1"}',
                '{"feature":"bounded"}',
            ),
        )
    conn.commit()
    return rowid


def _forward_flush_worker(
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> TargetProductionWorker:
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.cash_phase_forward_store_root = tmp_path
    worker.cash_phase_forward_store_path = tmp_path / "forward.sqlite3"
    return worker


def test_forward_flush_quarantines_first_poison_after_valid_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _forward_worker_source_db()
    _insert_forward_source_row(conn, decision_id="decision-01")
    _insert_forward_source_row(conn, decision_id="decision-02")
    _insert_forward_source_row(conn, decision_id="decision-03")
    worker = _forward_flush_worker(conn, tmp_path)
    store_state = {"generation": 0, "last_source_rowid": 0, "observation_count": 0}
    commit_calls: list[dict] = []
    quarantine_calls: list[dict] = []

    def read_state(*args, **kwargs):
        return dict(store_state)

    def commit_batch(path, **kwargs):
        commit_calls.append(kwargs)
        records = list(kwargs["records"])
        store_state.update({
            "generation": 1,
            "last_source_rowid": max(item["source_rowid"] for item in records),
            "observation_count": len(records),
        })
        return {
            "state": "committed",
            "generation": 1,
            "last_source_rowid": store_state["last_source_rowid"],
            "inserted": len(records),
            "observation_count": len(records),
        }

    def quarantine(path, **kwargs):
        quarantine_calls.append(kwargs)
        source_rowid = int(kwargs["records"][0]["source_rowid"])
        store_state.update({
            "generation": 2,
            "last_source_rowid": source_rowid,
            "observation_count": 2,
        })
        return {
            "state": "quarantined",
            "generation": 2,
            "last_source_rowid": source_rowid,
            "quarantined": 1,
            "observation_count": 2,
        }

    def probe():
        rowid = int(worker._forward_flush_probe_rowid)
        if rowid == 3:
            raise ContractError(
                "cash_phase_forward_decision_json_invalid: unsafe detail with spaces"
            )
        return {
            "records": [{
                "observation": {
                    "observation_key": f"observation-{rowid}",
                    "decision_id": f"decision-{rowid:02d}",
                },
                "decision_id": f"decision-{rowid:02d}",
                "decision_sha256": "a" * 64,
                "source_rowid": rowid,
            }]
        }

    monkeypatch.setattr(production_worker_module, "read_forward_store_state", read_state)
    monkeypatch.setattr(production_worker_module, "commit_forward_observation_batch", commit_batch)
    monkeypatch.setattr(
        production_worker_module,
        "quarantine_forward_observation_batch",
        quarantine,
    )
    monkeypatch.setattr(worker, "_flush_cash_phase_forward_observations_probe", probe)

    result = worker._flush_cash_phase_forward_observations()

    assert result["state"] == "blocked_fail_closed"
    assert result["promotion_allowed"] is False
    assert result["cursor_advanced"] is True
    assert result["quarantined_count"] == 1
    assert len(commit_calls) == 1
    assert [item["source_rowid"] for item in commit_calls[0]["records"]] == [1, 2]
    assert len(quarantine_calls) == 1
    assert set(quarantine_calls[0]["records"][0]) == {
        "decision_id", "source_decision_sha256", "source_rowid", "error_code"
    }
    assert quarantine_calls[0]["records"][0]["source_rowid"] == 3
    assert (
        quarantine_calls[0]["records"][0]["error_code"]
        == "cash_phase_forward_decision_json_invalid"
    )
    assert result["orders_submitted"] == 0
    assert result["calls_order_gateway"] is False


def test_forward_flush_resumes_after_quarantine_on_next_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _forward_worker_source_db()
    _insert_forward_source_row(conn, decision_id="decision-01")
    _insert_forward_source_row(conn, decision_id="decision-02")
    _insert_forward_source_row(conn, decision_id="decision-03")
    worker = _forward_flush_worker(conn, tmp_path)
    state = {"generation": 2, "last_source_rowid": 2, "observation_count": 0}
    calls: list[int] = []

    def read_state(*args, **kwargs):
        return dict(state)

    def commit_batch(path, **kwargs):
        records = list(kwargs["records"])
        calls.extend(item["source_rowid"] for item in records)
        state.update({
            "generation": state["generation"] + 1,
            "last_source_rowid": max(item["source_rowid"] for item in records),
            "observation_count": 1,
        })
        return {
            "state": "committed",
            "generation": state["generation"],
            "last_source_rowid": state["last_source_rowid"],
            "inserted": 1,
            "observation_count": 1,
        }

    monkeypatch.setattr(production_worker_module, "read_forward_store_state", read_state)
    monkeypatch.setattr(production_worker_module, "commit_forward_observation_batch", commit_batch)

    def probe():
        rowid = int(worker._forward_flush_probe_rowid)
        return {
            "records": [{
                "observation": {"observation_key": "observation-03", "decision_id": "decision-03"},
                "decision_id": "decision-03",
                "decision_sha256": "a" * 64,
                "source_rowid": rowid,
            }]
        }

    monkeypatch.setattr(worker, "_flush_cash_phase_forward_observations_probe", probe)
    result = worker._flush_cash_phase_forward_observations()

    assert result["state"] == "committed"
    assert result["promotion_allowed"] is True
    assert result["cursor_advanced"] is True
    assert calls == [3]
    assert state["last_source_rowid"] == 3


def test_forward_flush_quarantine_failure_reports_prefix_cursor_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _forward_worker_source_db()
    _insert_forward_source_row(conn, decision_id="decision-01")
    _insert_forward_source_row(conn, decision_id="decision-02")
    worker = _forward_flush_worker(conn, tmp_path)
    state = {"generation": 0, "last_source_rowid": 0, "observation_count": 0}

    monkeypatch.setattr(
        production_worker_module,
        "read_forward_store_state",
        lambda *args, **kwargs: dict(state),
    )

    def commit_batch(path, **kwargs):
        state.update(
            generation=1,
            last_source_rowid=1,
            observation_count=1,
        )
        return {
            "state": "committed",
            "generation": 1,
            "last_source_rowid": 1,
            "inserted": 1,
            "observation_count": 1,
        }

    def probe():
        rowid = int(worker._forward_flush_probe_rowid)
        if rowid == 2:
            raise ContractError("cash_phase_forward_decision_json_invalid")
        return {
            "records": [{
                "observation": {
                    "observation_key": "observation-01",
                    "decision_id": "decision-01",
                },
                "decision_id": "decision-01",
                "decision_sha256": "a" * 64,
                "source_rowid": 1,
            }]
        }

    monkeypatch.setattr(
        production_worker_module,
        "commit_forward_observation_batch",
        commit_batch,
    )
    monkeypatch.setattr(
        production_worker_module,
        "quarantine_forward_observation_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ContractError("forward_store_quarantine_generation_cas_mismatch")
        ),
    )
    monkeypatch.setattr(
        worker,
        "_flush_cash_phase_forward_observations_probe",
        probe,
    )
    result = worker._flush_cash_phase_forward_observations()
    assert result["state"] == "blocked_fail_closed"
    assert result["promotion_allowed"] is False
    assert result["cursor_advanced"] is True
    assert result["poison_cursor_advanced"] is False
    assert result["written"] is True
    assert state["last_source_rowid"] == 1


def test_forward_flush_identity_or_resource_failure_does_not_advance_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _forward_worker_source_db()
    _insert_forward_source_row(conn, decision_id="decision-01", source_sha="bad")
    worker = _forward_flush_worker(conn, tmp_path)
    state = {"generation": 0, "last_source_rowid": 0, "observation_count": 0}
    quarantine_calls: list[dict] = []
    monkeypatch.setattr(production_worker_module, "read_forward_store_state", lambda *a, **k: dict(state))
    monkeypatch.setattr(
        production_worker_module,
        "quarantine_forward_observation_batch",
        lambda *a, **k: quarantine_calls.append(k),
    )
    identity_result = worker._flush_cash_phase_forward_observations()
    assert identity_result["state"] == "blocked_fail_closed"
    assert identity_result["cursor_advanced"] is False
    assert quarantine_calls == []

    conn.execute(
        "UPDATE auto_trade_decisions SET decision_sha256=? WHERE decision_id=?",
        ("b" * 64, "decision-01"),
    )
    conn.commit()
    monkeypatch.setattr(
        production_worker_module,
        "assert_process_memory_budget",
        lambda **kwargs: (_ for _ in ()).throw(
            ValueError("resource_probe_unavailable")
        ),
    )
    resource_result = worker._flush_cash_phase_forward_observations()
    assert resource_result["state"] == "blocked_fail_closed"
    assert resource_result["cursor_advanced"] is False
    assert quarantine_calls == []


def test_delta_v2_label_projection_failure_keeps_authority_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE label_marker(value TEXT)")
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.cash_phase_forward_store_path = tmp_path / "forward.sqlite3"
    worker.cash_phase_forward_store_root = tmp_path
    worker._last_delta_v2_label_scan_monotonic = None

    def materialize(
        source_conn,
        *,
        as_of_utc,
        after_fact_rowid,
        cursor_state,
        max_facts,
    ):
        assert after_fact_rowid == 0
        assert max_facts == production_worker_module.MAX_LABEL_BATCH
        source_conn.execute("INSERT INTO label_marker VALUES('uncommitted')")
        return {
            "state": "committed",
            "candidate_count": 1,
            "appended_count": 1,
            "source_cursor_after": 1,
            "has_more": False,
        }

    monkeypatch.setattr(
        production_worker_module,
        "materialize_delta_v2_settled_training_labels",
        materialize,
    )
    monkeypatch.setattr(
        production_worker_module,
        "append_forward_settlement_labels",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ContractError("forward_store_label_write_failed")
        ),
    )
    result = worker._materialize_delta_v2_labels(
        as_of_utc="2026-07-28T00:00:00Z"
    )
    assert result["state"] == "blocked_fail_closed"
    assert result["appended_count"] == 1
    assert result["source_labels_committed"] is True
    assert conn.execute("SELECT COUNT(*) FROM label_marker").fetchone()[0] == 1


def test_delta_v2_label_projection_runs_after_source_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE label_marker(value TEXT)")
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.cash_phase_forward_store_path = tmp_path / "forward.sqlite3"
    worker.cash_phase_forward_store_root = tmp_path
    worker._last_delta_v2_label_scan_monotonic = None
    observed: list[bool] = []

    def materialize(
        source_conn,
        *,
        as_of_utc,
        after_fact_rowid,
        cursor_state,
        max_facts,
    ):
        assert after_fact_rowid == 0
        assert max_facts == production_worker_module.MAX_LABEL_BATCH
        source_conn.execute("INSERT INTO label_marker VALUES('committed')")
        return {
            "state": "committed",
            "candidate_count": 1,
            "appended_count": 1,
            "source_cursor_after": 1,
            "has_more": False,
        }

    monkeypatch.setattr(
        production_worker_module,
        "read_forward_store_state",
        lambda *args, **kwargs: {"generation": 7},
    )

    def append_labels(
        path,
        *,
        source_conn,
        expected_generation,
        as_of_utc,
        allowed_root,
    ):
        observed.append(source_conn.in_transaction)
        assert path == worker.cash_phase_forward_store_path
        assert allowed_root == tmp_path
        assert expected_generation == 7
        return {"state": "committed", "appended_count": 1}

    monkeypatch.setattr(
        production_worker_module,
        "materialize_delta_v2_settled_training_labels",
        materialize,
    )
    monkeypatch.setattr(production_worker_module, "append_forward_settlement_labels", append_labels)
    result = worker._materialize_delta_v2_labels(
        as_of_utc="2026-07-28T00:00:00Z"
    )
    assert result["state"] == "committed"
    assert result["forward_settlement_label_sync"]["state"] == "committed"
    assert observed == [False]
    assert conn.execute("SELECT COUNT(*) FROM label_marker").fetchone()[0] == 1


def test_delta_v2_label_scan_closes_283_facts_in_bounded_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE label_marker(value INTEGER)")
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.cash_phase_forward_store_path = tmp_path / "forward.sqlite3"
    worker.cash_phase_forward_store_root = tmp_path
    worker._last_delta_v2_label_scan_monotonic = None
    worker._delta_v2_label_fact_cursor = 0
    worker._delta_v2_label_cursor_initialized = True
    observed: list[int] = []

    def materialize(
        source_conn,
        *,
        as_of_utc,
        after_fact_rowid,
        cursor_state,
        max_facts,
    ):
        assert as_of_utc == "2026-07-29T15:01:00Z"
        assert max_facts == production_worker_module.MAX_LABEL_BATCH
        observed.append(after_fact_rowid)
        remaining = 283 - after_fact_rowid
        count = min(max_facts, max(0, remaining))
        source_conn.executemany(
            "INSERT INTO label_marker VALUES(?)",
            [(after_fact_rowid + offset + 1,) for offset in range(count)],
        )
        return {
            "state": "committed" if count else "no_new_final_labels",
            "candidate_count": count,
            "appended_count": count,
            "blockers": [],
            "source_cursor_after": after_fact_rowid + count,
            "has_more": remaining > max_facts,
            "skipped_open_count": 0,
        }

    monkeypatch.setattr(
        production_worker_module,
        "materialize_delta_v2_settled_training_labels",
        materialize,
    )
    monkeypatch.setattr(
        production_worker_module,
        "read_forward_store_state",
        lambda *args, **kwargs: {"generation": 7},
    )
    monkeypatch.setattr(
        production_worker_module,
        "append_forward_settlement_labels",
        lambda *args, **kwargs: {
            "state": "already_current",
            "ingested": 0,
            "inserted": 0,
            "skipped_ineligible": 0,
            "more_pending": False,
            "generation": 7,
        },
    )

    result = worker._materialize_delta_v2_labels(
        as_of_utc="2026-07-29T15:01:00Z"
    )

    assert result["state"] == "committed"
    assert result["appended_count"] == 283
    assert result["page_count"] == 5
    assert observed == [0, 64, 128, 192, 256]
    assert worker._delta_v2_label_fact_cursor == 283
    assert conn.execute(
        "SELECT COUNT(*) FROM label_marker"
    ).fetchone()[0] == 283


def test_delta_v2_label_commit_failure_does_not_advance_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE label_marker(value TEXT)")

    class CommitFailingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        @property
        def in_transaction(self) -> bool:
            return self.connection.in_transaction

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def commit(self) -> None:
            raise sqlite3.OperationalError("injected_label_commit_failure")

        def rollback(self) -> None:
            self.connection.rollback()

    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = CommitFailingConnection(raw)
    worker.cash_phase_forward_store_path = tmp_path / "forward.sqlite3"
    worker.cash_phase_forward_store_root = tmp_path
    worker._last_delta_v2_label_scan_monotonic = None
    worker._delta_v2_label_fact_cursor = 9
    worker._delta_v2_label_cursor_initialized = True

    def materialize(
        source_conn,
        *,
        as_of_utc,
        after_fact_rowid,
        cursor_state,
        max_facts,
    ):
        assert after_fact_rowid == 9
        source_conn.execute("INSERT INTO label_marker VALUES('rollback')")
        return {
            "state": "committed",
            "candidate_count": 1,
            "appended_count": 1,
            "source_cursor_after": 10,
            "has_more": False,
        }

    monkeypatch.setattr(
        production_worker_module,
        "materialize_delta_v2_settled_training_labels",
        materialize,
    )
    result = worker._materialize_delta_v2_labels(
        as_of_utc="2026-07-29T15:01:00Z"
    )

    assert result["state"] == "blocked_fail_closed"
    assert "injected_label_commit_failure" in result["reason"]
    assert worker._delta_v2_label_fact_cursor == 9
    assert raw.execute(
        "SELECT COUNT(*) FROM label_marker"
    ).fetchone()[0] == 0


def test_delta_v2_truth_throttle_does_not_block_due_label_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    calls: list[str] = []
    monkeypatch.setattr(
        worker,
        "_materialize_delta_v2_truths",
        lambda **_kwargs: {
            "state": "throttled",
            "reason": "delta_v2_settlement_scan_interval",
            "written_count": 0,
        },
    )

    def labels(*, as_of_utc: str, enforce_interval: bool = True):
        calls.append(as_of_utc)
        assert enforce_interval is True
        return {"state": "committed", "appended_count": 64}

    monkeypatch.setattr(worker, "_materialize_delta_v2_labels", labels)
    truth, label = worker._run_delta_v2_settlement_maintenance(
        as_of_utc="2026-07-29T15:01:00Z"
    )

    assert truth["state"] == "throttled"
    assert label["state"] == "committed"
    assert calls == ["2026-07-29T15:01:00Z"]


def test_delta_v2_truth_blocker_rolls_back_before_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE truth_marker(value TEXT)")
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    worker.canonical_source_error = None
    worker._last_delta_v2_label_scan_monotonic = None
    worker._delta_v2_truth_cursor_initialized = True

    def materialize(
        target_conn,
        canonical_conn,
        *,
        as_of_utc,
        max_scopes,
        after_fact_rowid,
    ):
        assert canonical_conn is worker.canonical_source_conn
        assert as_of_utc == "2026-07-28T15:00:00Z"
        assert max_scopes == expected_station_count()
        assert after_fact_rowid == 0
        target_conn.execute("INSERT INTO truth_marker VALUES('rollback')")
        return {
            "state": "blocked_fail_closed",
            "blockers": [{
                "reason": (
                    "ContractError:"
                    "settlement_truth_correction_requires_manual_review"
                ),
            }],
        }

    monkeypatch.setattr(
        production_worker_module,
        "materialize_settled_training_truths",
        materialize,
    )
    result = worker._materialize_delta_v2_truths(
        as_of_utc="2026-07-28T15:00:00Z"
    )

    assert result["state"] == "blocked_fail_closed"
    assert "settlement_truth_correction_requires_manual_review" in (
        result["reason"]
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM truth_marker"
    ).fetchone()[0] == 0
    repeated = worker._materialize_delta_v2_truths(
        as_of_utc="2026-07-28T15:00:01Z"
    )
    assert repeated["state"] == "blocked_fail_closed"
    assert repeated["state"] != "throttled"


def test_truth_bootstrap_rotates_instead_of_serving_one_alphabetical_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every settled candidate eventually gets a turn, whatever its name.

    The candidate set is routinely larger than one page, and cutting a
    station-sorted list at a fixed offset served the same prefix every cycle.
    Stations late in the alphabet failed no gate -- replaying their scopes
    reaches the write -- they were never asked, which looks exactly like a
    source outage in the receipts.
    """

    conn = sqlite3.connect(":memory:")
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    worker.canonical_source_error = None
    worker._last_delta_v2_label_scan_monotonic = None
    worker._delta_v2_truth_cursor_initialized = True

    # 100 candidates spanning the alphabet; one page is 45.
    candidates = [
        (f"{chr(ord('A') + index // 4)}{index:03d}", "2026-08-05", "Etc/UTC")
        for index in range(100)
    ]
    monkeypatch.setattr(
        production_worker_module,
        "_candidate_ensemble_scopes_without_truth",
        lambda _conn, *, limit: candidates,
    )
    monkeypatch.setattr(
        production_worker_module,
        "settlement_day_window",
        lambda _day, _tz: {"target_day_end_utc": "2026-08-06T00:00:00Z"},
    )
    served: list[list[tuple[str, str]]] = []

    def materialize(target_conn, canonical_conn, **kwargs):
        served.append(list(kwargs.get("additional_scopes") or []))
        return {
            "state": "complete",
            "written_count": 0,
            "blockers": [],
            "source_cursor_after": 0,
        }

    monkeypatch.setattr(
        production_worker_module,
        "materialize_settled_training_truths",
        materialize,
    )
    for _cycle in range(3):
        # The pass has its own scan interval; this test is about which page it
        # serves, not how often it runs.
        worker._last_delta_v2_truth_scan_monotonic = None
        worker._materialize_delta_v2_truths(as_of_utc="2026-08-10T12:00:00Z")

    assert [len(page) for page in served] == [45, 45, 45]
    assert served[0] != served[1]
    # Three pages of 45 over 100 candidates cover every one of them.
    assert len({scope for page in served for scope in page}) == len(candidates)


def test_delta_v2_truth_commits_before_label_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE truth_marker(value TEXT)")
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    worker.canonical_source_error = None
    worker._last_delta_v2_label_scan_monotonic = None
    worker._delta_v2_truth_cursor_initialized = True

    def materialize(
        target_conn,
        canonical_conn,
        *,
        as_of_utc,
        max_scopes,
        after_fact_rowid,
    ):
        assert max_scopes == expected_station_count()
        assert after_fact_rowid == 0
        target_conn.execute("INSERT INTO truth_marker VALUES('commit')")
        return {
            "state": "complete",
            "written_count": 1,
            "blockers": [],
            "source_cursor_after": 7,
        }

    monkeypatch.setattr(
        production_worker_module,
        "materialize_settled_training_truths",
        materialize,
    )
    result = worker._materialize_delta_v2_truths(
        as_of_utc="2026-07-28T15:00:00Z"
    )

    assert result["state"] == "complete"
    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) FROM truth_marker"
    ).fetchone()[0] == 1
    assert worker._delta_v2_truth_fact_cursor == 7


def test_delta_v2_truth_commit_failure_does_not_advance_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE truth_marker(value TEXT)")

    class CommitFailingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        @property
        def in_transaction(self) -> bool:
            return self.connection.in_transaction

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def commit(self) -> None:
            raise sqlite3.OperationalError("injected_commit_failure")

        def rollback(self) -> None:
            self.connection.rollback()

    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = CommitFailingConnection(raw)
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    worker.canonical_source_error = None
    worker._last_delta_v2_label_scan_monotonic = None
    worker._delta_v2_truth_fact_cursor = 3
    worker._delta_v2_truth_cursor_initialized = True

    def materialize(
        target_conn,
        canonical_conn,
        *,
        as_of_utc,
        max_scopes,
        after_fact_rowid,
    ):
        assert after_fact_rowid == 3
        target_conn.execute("INSERT INTO truth_marker VALUES('rollback')")
        return {
            "state": "complete",
            "written_count": 1,
            "blockers": [],
            "source_cursor_after": 7,
        }

    monkeypatch.setattr(
        production_worker_module,
        "materialize_settled_training_truths",
        materialize,
    )
    result = worker._materialize_delta_v2_truths(
        as_of_utc="2026-07-28T15:00:00Z"
    )

    assert result["state"] == "blocked_fail_closed"
    assert "injected_commit_failure" in result["reason"]
    assert worker._delta_v2_truth_fact_cursor == 3
    assert raw.execute(
        "SELECT COUNT(*) FROM truth_marker"
    ).fetchone()[0] == 0


def test_delta_v2_truth_restart_starts_with_newest_bounded_scope_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_policy_training_fact("
        "fact_id TEXT,station_id TEXT,event_date TEXT)"
    )
    for index in range(50):
        conn.execute(
            "INSERT INTO basket_policy_training_fact VALUES(?,?,?)",
            (f"fact-{index:02d}", f"K{index:03d}", "2026-07-29"),
        )
    worker = TargetProductionWorker.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    worker.canonical_source_error = None
    worker._last_delta_v2_label_scan_monotonic = None
    worker._delta_v2_truth_fact_cursor = 0
    worker._delta_v2_truth_cursor_initialized = False
    observed: list[int] = []

    def materialize(
        target_conn,
        canonical_conn,
        *,
        as_of_utc,
        max_scopes,
        after_fact_rowid,
    ):
        observed.append(after_fact_rowid)
        return {
            "state": "complete",
            "written_count": 0,
            "blockers": [],
            "source_cursor_after": 50,
        }

    monkeypatch.setattr(
        production_worker_module,
        "materialize_settled_training_truths",
        materialize,
    )
    result = worker._materialize_delta_v2_truths(
        as_of_utc="2026-07-29T15:00:00Z"
    )

    assert result["state"] == "complete"
    assert observed == [5]
    assert worker._delta_v2_truth_fact_cursor == 50
    assert worker._delta_v2_truth_cursor_initialized is True



def test_blocked_prepared_cycle_does_not_block_future_recompile(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT,plan_id TEXT,state TEXT,station_id TEXT,event_date TEXT,created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE auto_trade_decisions(cycle_id TEXT,decided_state TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?,?)",
        ("blocked-cycle", "blocked-plan", "prepared", "ZUUU", "2026-07-24", "2026-07-24T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?)",
        ("blocked-cycle", "blocked"),
    )
    worker = _worker(tmp_path, conn)
    try:
        result = worker._scope_cycle_guard(
            station_id="ZUUU",
            event_date="2026-07-24",
            candidate={"plan": {"plan_id": "new-plan"}},
        )
    finally:
        worker.close()

    assert result is None


def test_blocked_cycle_zero_writes_until_authority_scope_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT,plan_id TEXT,state TEXT,station_id TEXT,"
        "event_date TEXT,created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE auto_trade_decisions("
        "cycle_id TEXT,decided_state TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_policy_training_fact("
        "fact_id TEXT,cycle_id TEXT,decision_id TEXT,"
        "decision_scope_key TEXT,station_id TEXT,event_date TEXT,"
        "decision_at_utc TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?,?)",
        (
            "blocked-cycle",
            "blocked-plan",
            "prepared",
            "ZUUU",
            "2026-07-24",
            "2026-07-24T00:00:00Z",
        ),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?)",
        ("blocked-cycle", "blocked"),
    )
    conn.execute(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?,?)",
        (
            "fact-1",
            "blocked-cycle",
            "decision-1",
            "authority-scope-1",
            "ZUUU",
            "2026-07-24",
            "2026-07-24T00:00:00Z",
        ),
    )
    candidate = {"production_market_snapshot": {}}
    worker = _worker(tmp_path, conn)
    try:
        monkeypatch.setattr(
            production_worker_module,
            "delta_v2_decision_scope_key",
            lambda **_: "authority-scope-1",
        )
        repeated = worker._scope_cycle_guard(
            station_id="ZUUU",
            event_date="2026-07-24",
            candidate=candidate,
        )
        assert repeated is not None
        assert (
            repeated["state"]
            == "retained_existing_authority_decision"
        )
        assert repeated["writes_target_ledger"] is False
        assert repeated["training_fact_id"] == "fact-1"

        monkeypatch.setattr(
            production_worker_module,
            "delta_v2_decision_scope_key",
            lambda **_: "authority-scope-2",
        )
        changed = worker._scope_cycle_guard(
            station_id="ZUUU",
            event_date="2026-07-24",
            candidate=candidate,
        )
        assert changed is None
    finally:
        worker.close()


def test_non_quiescent_cycle_fails_closed_without_writes(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT,plan_id TEXT,state TEXT,station_id TEXT,event_date TEXT,created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?,?)",
        ("cycle-1", "plan-1", "reconcile_required", "ZUUU", "2026-07-24", "2026-07-24T00:00:00Z"),
    )
    worker = _worker(tmp_path, conn)
    try:
        result = worker._scope_cycle_guard(
            station_id="ZUUU",
            event_date="2026-07-24",
            candidate={"plan": {"plan_id": "new-plan"}},
        )
    finally:
        worker.close()

    assert result is not None
    assert result["state"] == "blocked_fail_closed"
    assert result["reason"] == "target_existing_cycle_not_quiescent"
    assert result["calls_order_gateway"] is False


def test_multiple_unsettled_candidates_are_a_retention_violation(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT,plan_id TEXT,state TEXT,station_id TEXT,event_date TEXT,created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?,?)",
        [
            ("cycle-1", "plan-1", "prepared", "ZUUU", "2026-07-24", "2026-07-24T00:00:00Z"),
            ("cycle-2", "plan-2", "prepared", "ZUUU", "2026-07-24", "2026-07-24T00:01:00Z"),
        ],
    )
    worker = _worker(tmp_path, conn)
    try:
        result = worker._scope_cycle_guard(
            station_id="ZUUU",
            event_date="2026-07-24",
            candidate={"plan": {"plan_id": "new-plan"}},
        )
    finally:
        worker.close()

    assert result is not None
    assert result["state"] == "blocked_fail_closed"
    assert result["reason"] == "target_prepared_cycle_retention_violation"


def test_decision_scopes_admit_pre_event_p0_and_p0m_but_not_future_p1(
    tmp_path: Path,
) -> None:
    target_db = tmp_path / "target-read.db"
    with sqlite3.connect(target_db) as conn:
            conn.execute(
                "CREATE TABLE basket_probability_envelope("
                "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
                "state TEXT,stage TEXT)"
            )
            conn.execute(
                "CREATE TABLE basket_official_ws_evidence_cache("
                "station_id TEXT,event_date TEXT,settlement_timezone TEXT)"
            )
            conn.executemany(
                "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?)",
                [
                    (
                        "RKPK",
                        "2026-07-30",
                        "Asia/Seoul",
                        "COMMITTED",
                        "P0",
                    ),
                    (
                        "RKPK",
                        "2026-07-30",
                        "Asia/Seoul",
                        "COMMITTED",
                        "P0-M",
                    ),
                    (
                        "RKSI",
                        "2026-07-30",
                        "Asia/Seoul",
                        "COMMITTED",
                        "P1",
                    ),
                ],
            )

    commit_conn = sqlite3.connect(":memory:")
    worker = TargetProductionWorker(target_db, commit_conn=commit_conn)
    try:
        scopes = worker._decision_scopes(as_of_utc="2026-07-29T13:00:00Z")
        executable_scopes = worker._decision_scopes(
            as_of_utc="2026-07-29T13:00:00Z",
            include_not_started=False,
        )
    finally:
        worker.close()
        commit_conn.close()

    assert scopes == [("RKPK", "2026-07-30")]
    assert executable_scopes == []


def test_future_truth_is_not_read_and_future_p1_is_not_materialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    commit_conn = sqlite3.connect(target_db)
    commit_conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(commit_conn)
    monkeypatch.setattr(
        production_worker_module,
        "load_started_registry_rows",
        lambda *_args, **_kwargs: [],
    )

    def unexpected_future_truth_read(*_args, **_kwargs):
        raise AssertionError("future truth must remain outside P1 ingress")

    monkeypatch.setattr(
        production_worker_module,
        "load_resident_formal_observations",
        unexpected_future_truth_read,
    )
    worker = TargetProductionWorker(target_db, commit_conn=commit_conn)
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    try:
        result = worker._materialize_resident_p1(
            as_of_utc="2026-07-29T13:00:00Z",
        )
        assert result["p1_update_count"] == 0
        assert result["writes_target_ssot"] is False
        assert commit_conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope "
            "WHERE stage='P1'"
        ).fetchone()[0] == 0
    finally:
        worker.close()


def test_stale_or_invalid_p1_never_falls_back_to_p0() -> None:
    envelopes = [
        ProbabilityEnvelope.from_mapping({
            "stage": "P0",
            "checkpoint_id": "p0-fresh",
            "station_id": "RKPK",
            "event_date": "2026-07-30",
            "settlement_timezone": "Asia/Seoul",
            "decision_at_utc": "2026-07-29T12:00:00Z",
            "fresh_until_utc": "2026-07-29T15:00:00Z",
            "bucket_ordinals": {"36°C": 0, "37°C": 1},
            "probabilities": {"36°C": 0.4, "37°C": 0.6},
            "posterior_version": "p0-v1",
            "posterior_sha256": "a" * 64,
            "bridge_sha256": "b" * 64,
            "lineage": {"settlement_timezone": "Asia/Seoul"},
        }),
        ProbabilityEnvelope.from_mapping({
            "stage": "P1",
            "checkpoint_id": "p1-stale",
            "station_id": "RKPK",
            "event_date": "2026-07-30",
            "settlement_timezone": "Asia/Seoul",
            "decision_at_utc": "2026-07-29T12:30:00Z",
            "fresh_until_utc": "2026-07-29T12:45:00Z",
            "bucket_ordinals": {"36°C": 0, "37°C": 1},
            "probabilities": {"36°C": 0.2, "37°C": 0.8},
            "posterior_version": "p1-v1",
            "posterior_sha256": "c" * 64,
            "bridge_sha256": "d" * 64,
            "lineage": {"p0_root_checkpoint_id": "p0-fresh"},
        }),
    ]

    # Fifteen minutes past its cadence the P1 is still what we know, and the
    # P0 beneath it is fresh until 15:00 -- so this is the case where crossing
    # down would be tempting and is still wrong.  It serves the P1 instead.
    selected = select_current_stage(envelopes, at_utc="2026-07-29T13:00:00Z")
    assert selected.stage == "P1"
    assert selected.checkpoint_id == "p1-stale"

    # Three hours past, the P1 no longer describes the day.  The fresh P0 is
    # still sitting right there and still does not get the authority.
    with pytest.raises(
        ContractError,
        match="latest_stage_stale_fail_closed",
    ):
        select_current_stage(envelopes, at_utc="2026-07-29T15:45:01Z")


def test_resident_decision_scopes_exclude_closed_registry_days(tmp_path: Path) -> None:
    target_db = tmp_path / "target-read.db"
    with sqlite3.connect(target_db) as conn:
            conn.execute(
                "CREATE TABLE basket_probability_envelope("
                "station_id TEXT,event_date TEXT,settlement_timezone TEXT,state TEXT)"
            )
            conn.executemany(
                "INSERT INTO basket_probability_envelope VALUES(?,?,?,?)",
                [
                    ("ZAAA", "2026-07-23", "America/New_York", "COMMITTED"),
                    ("ZBBB", "2026-07-24", "America/New_York", "COMMITTED"),
                ],
            )

    commit_conn = sqlite3.connect(":memory:")
    worker = TargetProductionWorker(target_db, commit_conn=commit_conn)
    try:
        scopes = worker._decision_scopes(as_of_utc="2026-07-24T15:00:00Z")
    finally:
        worker.close()
        commit_conn.close()

    assert scopes == [("ZBBB", "2026-07-24")]


def test_resident_decision_scopes_obey_vertical_slice_allowlists(
    tmp_path: Path,
) -> None:
    target_db = tmp_path / "target-read.db"
    with sqlite3.connect(target_db) as conn:
        conn.execute(
            "CREATE TABLE basket_probability_envelope("
            "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
            "state TEXT)"
        )
        conn.executemany(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?)",
            [
                ("RKPK", "2026-07-28", "Asia/Seoul", "COMMITTED"),
                ("RKSI", "2026-07-28", "Asia/Seoul", "COMMITTED"),
                ("RKPK", "2026-07-29", "Asia/Seoul", "COMMITTED"),
            ],
        )

    commit_conn = sqlite3.connect(":memory:")
    worker = TargetProductionWorker(
        target_db,
        commit_conn=commit_conn,
        station_allowlist=("RKPK",),
        event_date_allowlist=("2026-07-28",),
    )
    try:
        scopes = worker._decision_scopes(
            as_of_utc="2026-07-28T02:00:00Z"
        )
    finally:
        worker.close()
        commit_conn.close()

    assert scopes == [("RKPK", "2026-07-28")]


def test_current_p1_supersedes_stale_optional_p0m_replay_blocker(
    monkeypatch,
) -> None:
    worker = object.__new__(TargetProductionWorker)
    worker.conn = sqlite3.connect(":memory:")
    envelopes = [
        SimpleNamespace(
            stage="P0",
            checkpoint_id="p0-current",
            decision_at_utc="2026-07-28T02:00:00Z",
            lineage={},
        ),
        SimpleNamespace(
            stage="P0-M",
            checkpoint_id="p0m-stale",
            decision_at_utc="2026-07-28T01:00:00Z",
            lineage={"p0_root_checkpoint_id": "p0-old"},
        ),
        SimpleNamespace(
            stage="P1",
            checkpoint_id="p1-current",
            decision_at_utc="2026-07-28T02:05:00Z",
            lineage={"p0_root_checkpoint_id": "p0-current"},
        ),
    ]
    monkeypatch.setattr(
        production_worker_module,
        "load_materialized_probability_envelopes",
        lambda *args, **kwargs: envelopes,
    )
    monkeypatch.setattr(
        production_worker_module,
        "target_registry_source_lane",
        lambda *_args, **_kwargs: "awc",
    )
    try:
        assert worker._replay_root_mismatch_reason(
            station_id="RKPK",
            event_date="2026-07-28",
            as_of_utc="2026-07-28T02:10:00Z",
        ) is None
        envelopes[-1].lineage = {
            "p0_root_checkpoint_id": "p0-old"
        }
        assert worker._replay_root_mismatch_reason(
            station_id="RKPK",
            event_date="2026-07-28",
            as_of_utc="2026-07-28T02:10:00Z",
        ) == "resident_p1_replay_required"
    finally:
        worker.conn.close()


def test_writer_flag_cannot_open_in_live_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_TARGET_PRODUCTION_WRITER", "1")
    monkeypatch.setenv("PM_TARGET_EXECUTION_MODE", "production_live")
    activation = Path(__file__).parents[1] / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"

    with pytest.raises(RuntimeError, match="execution_mode_not_production_no_live"):
        open_target_writer_if_enabled(tmp_path / "target.db", activation_path=activation)


def test_default_target_budget_is_target_owned_and_ignores_resident_bankroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PM_TARGET_REMAINING_BUDGET_USDC", raising=False)

    assert _resolve_target_remaining_budget() == 20.0
    assert load_target_no_live_runtime_config()["target_remaining_budget_usdc"] == 20.0


def test_explicit_target_budget_override_is_target_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_TARGET_REMAINING_BUDGET_USDC", "12.5")

    assert _resolve_target_remaining_budget() == 12.5


def test_invalid_target_budget_override_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_TARGET_REMAINING_BUDGET_USDC", "NaN")

    with pytest.raises(RuntimeError, match="target_remaining_budget_usdc_invalid"):
        _resolve_target_remaining_budget()


def test_default_market_manifest_is_target_owned() -> None:
    assert DEFAULT_TARGET_MARKET_MANIFEST.name == "bayes_basket_target_official_ws_manifest.json"
    assert DEFAULT_TARGET_MARKET_MANIFEST.resolve() != PROTECTED_RESIDENT_WS_MANIFEST


def test_resident_consumer_capacity_matches_canonical_45station_three_day_scope() -> None:
    # Listing-at-entry (charter 48) makes three concurrent event days the
    # steady state: the listing day, today and tomorrow.  The reader's cap
    # must match the publisher's (MAX_TARGET_SCOPES = 45 x 3); a stricter
    # reader turns a bigger catalog into a global WS-path outage.
    assert len(CANONICAL_TARGET_STATIONS) == expected_station_count()
    assert len(set(CANONICAL_TARGET_STATIONS)) == expected_station_count()
    assert MAX_RESIDENT_WS_SCOPES == expected_station_count() * 3


def test_production_default_station_scope_is_canonical_45(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    sqlite3.connect(target_db).close()
    monkeypatch.setattr(
        production_worker_module,
        "DEFAULT_TARGET_DB",
        target_db,
    )
    monkeypatch.delenv("PM_TARGET_STATION_ALLOWLIST", raising=False)
    worker = TargetProductionWorker(
        target_db,
        nwp_runtime_root=tmp_path / "nwp-runtime",
    )
    try:
        assert worker.station_allowlist == CANONICAL_TARGET_STATIONS
        assert "RKPK" in worker.station_allowlist
        assert len(worker.station_allowlist) == expected_station_count()
    finally:
        if worker.conn is not None:
            worker.conn.close()


def test_corrupt_nwp_rotation_state_fails_only_nwp_lane_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    with sqlite3.connect(target_db) as conn:
        conn.execute("CREATE TABLE hard_no_probe(id INTEGER PRIMARY KEY)")
    runtime = tmp_path / "nwp-runtime"
    runtime.mkdir()
    (runtime / "rotation_state.json").write_text(
        '{"schema":"tampered"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        production_worker_module,
        "DEFAULT_TARGET_DB",
        target_db,
    )
    monkeypatch.delenv("PM_TARGET_STATION_ALLOWLIST", raising=False)
    worker = TargetProductionWorker(
        target_db,
        nwp_runtime_root=runtime,
    )
    try:
        result = worker.refresh_resident_nwp(
            as_of_utc="2026-07-30T06:00:00Z",
        )
        assert result["state"] == "blocked_fail_closed"
        assert result["reason"].startswith(
            "resident_nwp_rotation_state_invalid:"
        )
        assert result["calls_order_gateway"] is False
        assert result["orders_submitted"] == 0
        # The target read handle remains usable; a corrupt optional NWP
        # progress receipt must not take down the independent hard-NO lane.
        assert worker.conn is not None
        assert worker.conn.execute(
            "SELECT COUNT(*) FROM hard_no_probe"
        ).fetchone()[0] == 0
    finally:
        if worker.conn is not None:
            worker.conn.close()


def test_open_scope_selection_is_deterministic_and_starves_nobody() -> None:
    """Same catalog, same instant, same choice -- and everyone gets served.

    The selection used to be alphabetical round-robin, which was
    deterministic but ordered by ICAO code.  What has to survive the change
    is determinism and the absence of starvation, not the alphabet: with the
    ordering now rebuilt from current time and current evidence every cycle,
    a scope that is never picked would be a scope never compiled at all.
    """

    from datetime import datetime, timedelta, timezone

    scopes = [
        _rot_scope("RKPK", "Asia/Seoul"),        # 15:20 local at the start
        _rot_scope("EGLC", "Europe/London"),     # 07:20
        _rot_scope("KATL", "America/New_York"),  # 02:20
    ]
    base = datetime(2026, 8, 12, 6, 20, tzinfo=timezone.utc)

    def _run(worker, cycles=14):
        picked = []
        for step in range(cycles):
            at = (base + timedelta(minutes=20 * step)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            picked.append(
                worker._select_resident_open_ws_scope(
                    scopes, as_of_utc=at
                )["station_id"]
            )
        return picked

    selected = _run(object.__new__(TargetProductionWorker))

    # The warm station leads, whatever the alphabet says.
    assert selected[0] == "RKPK"
    # Every station is reached: ageing lifts a quiet one until it wins, so a
    # position held somewhere dark is still revisited.
    assert set(selected) == {"RKPK", "EGLC", "KATL"}
    # And the rotation follows the sun -- over these four hours London walks
    # into its warm window while Seoul walks out of it.
    assert selected[-1] == "EGLC"

    # Same inputs, same run: a fresh worker repeats the sequence exactly.
    assert selected == _run(object.__new__(TargetProductionWorker))


def test_republisher_inbox_packet_cap_fits_inside_the_json_write_limit() -> None:
    # These two bounds have to agree, and they did not: the count cap allowed
    # 256 packets while the byte cap allowed roughly 176 of them, so the inbox
    # filled to 99.7% of the write limit and then every capture failed with
    # target_json_output_bytes_exceeded.  A count cap that a single write
    # cannot satisfy is not a bound, it is a delayed failure.
    #
    # 95KiB is the largest packet observed in the live inbox; each carries
    # every registered ensemble system, so the figure moves with
    # ENSEMBLE_SYSTEMS rather than being arbitrary.
    largest_observed_packet_bytes = 95 * 1024
    projected = (
        production_worker_module.MAX_REPUBLISHER_INBOX_PACKETS
        * largest_observed_packet_bytes
    )

    assert projected < MAX_JSON_OUTPUT_BYTES
    # And not so far under that the cap is doing no work at all.
    assert projected > MAX_JSON_OUTPUT_BYTES // 2


def test_pending_official_capture_states_cover_every_async_admission() -> None:
    """Official first: a scope whose own packet is coming must not be bought.

    Official capture is asynchronous -- the cycle that starts one returns
    SUBMITTED and the packet lands minutes later, taking precedence over
    anything the aggregator produced meanwhile.  The aggregator's ensemble
    endpoint bills per member, so one such capture costs ~409 units against a
    10,000/day allowance while the catalogue is 270 scopes; paying for evidence
    that is about to be superseded is what exhausted the quota.

    Listed explicitly rather than inferred from "not CAPTURED", because a new
    admission state must be classified deliberately: treating an unknown state
    as terminal silently restores the spending this prevents.
    """

    pending = production_worker_module.OFFICIAL_NATIVE_CAPTURE_PENDING_STATES

    # Every state the admission path can return without a packet in hand.
    assert pending == {
        "SUBMITTED",
        "IN_FLIGHT",
        "DEFERRED_CAPTURE_SLOTS_BUSY",
        "THROTTLED",
    }
    # CAPTURED is not pending -- it means the packet is already available.
    assert "CAPTURED" not in pending
    # Terminal failures are not pending: those are the scopes the aggregator
    # fallback exists for.
    assert "FAILED" not in pending
    assert "DEADLINE_EXCEEDED" not in pending


def _trajectory_worker(conn) -> TargetProductionWorker:
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = conn
    return worker


def _trajectory_schema(conn) -> None:
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth("
        "station_id TEXT, local_date TEXT, settlement_timezone TEXT,"
        "qc_status TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_probability_evidence_artifact("
        "station_id TEXT, artifact_kind TEXT, trained_through_utc TEXT)"
    )


def test_trajectory_training_is_due_when_a_new_day_settles() -> None:
    """The pass is fold-driven: it owes a run exactly when coverage lags truth.

    A station gains at most one fold per settled day, so an artifact trained
    through the latest settled day's close cannot gain evidence until the next
    day settles -- rerunning the gate before then is a no-op by construction,
    and skipping it after would leave the promotion waiting on a human again,
    which is the failure this pass exists to remove.
    """

    import sqlite3

    conn = sqlite3.connect(":memory:")
    _trajectory_schema(conn)
    worker = _trajectory_worker(conn)
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES"
        "('ZUUU','2026-08-08','Asia/Shanghai','ACCEPTED')"
    )

    # No artifact at all: due.
    due, timezone_name, latest = worker._metar_trajectory_training_due(
        station_id="ZUUU"
    )
    assert (due, timezone_name, latest) == (True, "Asia/Shanghai", "2026-08-08")

    # Covered through the latest day's close (16:00Z for UTC+8): not due.
    conn.execute(
        "INSERT INTO basket_probability_evidence_artifact VALUES"
        "('ZUUU','METAR_TRAJECTORY_LIKELIHOOD','2026-08-08T16:00:00.000Z')"
    )
    assert worker._metar_trajectory_training_due(station_id="ZUUU")[0] is False

    # A newer settled day reopens the debt.
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES"
        "('ZUUU','2026-08-09','Asia/Shanghai','ACCEPTED')"
    )
    assert worker._metar_trajectory_training_due(station_id="ZUUU")[0] is True


def test_trajectory_training_not_due_without_settled_truth() -> None:
    # No truth means no folds and nothing to gate; running would only burn a
    # subprocess pair against an empty packet.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    _trajectory_schema(conn)
    worker = _trajectory_worker(conn)

    assert worker._metar_trajectory_training_due(station_id="ZUUU") == (
        False,
        "",
        "",
    )


def _taf_revision_schema(conn) -> None:
    conn.execute(
        "CREATE TABLE basket_probability_envelope("
        "station_id TEXT, stage TEXT, state TEXT, settlement_timezone TEXT,"
        "decision_at_utc TEXT, lineage_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth("
        "station_id TEXT, local_date TEXT, settlement_timezone TEXT,"
        "qc_status TEXT)"
    )


def _taf_revision_worker(conn, registry_path: Path) -> TargetProductionWorker:
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = conn
    worker.taf_artifact_path = registry_path
    return worker


def _p0_root(
    conn,
    *,
    station: str = "ZUUU",
    season: str = "JJA",
    lead: str = "d2",
    training_lead: str | None = None,
    cycle: str = "18Z",
    decision: str = "2026-08-10T00:00:00Z",
) -> None:
    lineage = {
        "season_bin": season,
        "lead_bucket": lead,
        "run_cycle": cycle,
        "settlement_definition": "official_daily_tmax_c",
    }
    if training_lead is not None:
        lineage["training_lead_bucket"] = training_lead
    conn.execute(
        "INSERT INTO basket_probability_envelope VALUES(?,?,?,?,?,?)",
        (
            station,
            "P0",
            "COMMITTED",
            "Asia/Shanghai",
            decision,
            json.dumps(lineage),
        ),
    )


def _historical_taf_worker(conn, tmp_path: Path) -> TargetProductionWorker:
    worker = object.__new__(TargetProductionWorker)
    worker.conn = conn
    worker.commit_conn = conn
    worker.db_path = tmp_path / "target.db"
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.station_allowlist = ("KMIA", "RKPK", "ZUUU")
    return worker


def test_historical_taf_capture_skips_stations_whose_taf_has_no_tx(
    tmp_path: Path,
) -> None:
    """TX is optional in a TAF and much of the network never publishes it.

    Capturing those stations cannot yield a calibration sample, and the AWC
    response budget is shared, so the request would be taken from a station
    that can.  The set is read off what the archive has yielded rather than
    hardcoded, so a station that starts publishing TX joins on its own.
    """

    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_historical_taf_capture("
        "station_id TEXT, tx_c REAL)"
    )
    worker = _historical_taf_worker(conn, tmp_path)

    # Nothing captured yet: the question cannot be answered, so try them all.
    assert worker._historical_taf_capture_stations() == ["KMIA", "RKPK", "ZUUU"]

    conn.executemany(
        "INSERT INTO calibration_historical_taf_capture VALUES(?,?)",
        [("RKPK", 31.0), ("ZUUU", 30.0), ("KMIA", None)],
    )
    assert worker._historical_taf_capture_stations() == ["RKPK", "ZUUU"]


def test_historical_taf_capture_asks_only_for_closed_days_and_runs_daily(
    tmp_path: Path,
) -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_historical_taf_capture("
        "station_id TEXT, tx_c REAL)"
    )
    conn.execute(
        "INSERT INTO calibration_historical_taf_capture VALUES('RKPK',31.0)"
    )
    worker = _historical_taf_worker(conn, tmp_path)
    submitted: list[list[str]] = []

    class _StubPool:
        def submit(self, function):
            submitted.append(function)
            return SimpleNamespace(done=lambda: True)

    worker._historical_taf_capture_pool = _StubPool()

    first = worker._capture_historical_taf_daily(
        as_of_utc="2026-08-10T12:00:00Z"
    )

    assert first["state"] == "SUBMITTED"
    # Two days of lag so the settlement day is closed in the station's own
    # timezone, then a week of overlap so a short outage still heals.
    assert first["end_date"] == "2026-08-08"
    assert first["start_date"] == "2026-08-02"
    assert first["station_count"] == 1
    assert first["calls_order_gateway"] is False

    # Daily, not every cycle: the same window would otherwise be re-fetched
    # every ten minutes against a shared response budget.
    second = worker._capture_historical_taf_daily(
        as_of_utc="2026-08-10T12:10:00Z"
    )
    assert second["state"] == "THROTTLED"
    assert len(submitted) == 1


def test_taf_scope_lead_reads_the_grouping_the_weights_were_fitted_in() -> None:
    """The calendar label identifies a decision; the hour bucket makes two
    predictions comparable, and that is what a TAF artifact is scoped by.

    d1 and d2 both contain runs issued 18-31h ahead, so the calendar label
    cannot say whether two P0 predictions belong to the same group.  Roots
    compiled before the trainer moved to hour buckets carry only the calendar
    label, and for those it stays the answer -- otherwise an artifact fitted
    under the old vocabulary would stop matching the roots it always matched.
    """

    from bayes_basket_target.taf_revision import taf_scope_lead_bucket

    assert taf_scope_lead_bucket({
        "lead_bucket": "d2",
        "training_lead_bucket": "h24_48",
    }) == "h24_48"
    assert taf_scope_lead_bucket({"lead_bucket": "d2"}) == "d2"
    assert taf_scope_lead_bucket({
        "lead_bucket": "d2",
        "training_lead_bucket": "",
    }) == "d2"
    assert taf_scope_lead_bucket({}) == ""


def test_taf_training_scope_follows_the_hour_bucket_when_the_root_has_one(
    tmp_path: Path,
) -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    _taf_revision_schema(conn)
    worker = _taf_revision_worker(conn, tmp_path / "active.json")
    _p0_root(conn, training_lead="h24_48")

    _due, scope, _reason = worker._taf_revision_training_due(
        station_id="ZUUU",
        as_of_utc="2026-08-10T12:00:00Z",
    )

    # The lane looks for a rolling series under the training grouping, while
    # the calendar label stays on the receipt for whoever reads it.
    assert scope["lead_bucket"] == "h24_48"
    assert scope["calendar_lead_bucket"] == "d2"


def test_taf_training_is_due_for_the_scope_the_live_root_actually_stamps(
    tmp_path: Path,
) -> None:
    """P0-M selects on exact lineage equality, so near-misses are not coverage.

    The one artifact ever activated was fit under the "summer" vocabulary and a
    00Z cycle; the live roots now stamp JJA and 18Z.  A lane that treated any
    artifact for the station as coverage would keep the stage silent forever,
    which is the state this pass exists to end.
    """

    import sqlite3

    conn = sqlite3.connect(":memory:")
    _taf_revision_schema(conn)
    registry = tmp_path / "active.json"
    worker = _taf_revision_worker(conn, registry)
    _p0_root(conn)

    due, scope, reason = worker._taf_revision_training_due(
        station_id="ZUUU",
        as_of_utc="2026-08-10T12:00:00Z",
    )
    assert due is True
    assert reason == "no_artifact_for_live_p0_scope"
    assert (scope["season_bin"], scope["lead_bucket"], scope["run_cycle"]) == (
        "JJA", "d2", "18Z",
    )

    # An artifact for the superseded scope is not coverage for the live one.
    registry.write_text(
        json.dumps(_taf_artifact_registry(season="summer", cycle="00Z")),
        encoding="utf-8",
    )
    assert worker._taf_revision_training_due(
        station_id="ZUUU",
        as_of_utc="2026-08-10T12:00:00Z",
    )[0] is True

    registry.write_text(
        json.dumps(_taf_artifact_registry()),
        encoding="utf-8",
    )
    due, _scope, reason = worker._taf_revision_training_due(
        station_id="ZUUU",
        as_of_utc="2026-08-10T12:00:00Z",
    )
    assert (due, reason) == (False, "active_artifact_covers_live_scope")

    # And it reopens once that artifact's own validity window closes.
    due, _scope, reason = worker._taf_revision_training_due(
        station_id="ZUUU",
        as_of_utc="2026-09-30T12:00:00Z",
    )
    assert (due, reason) == (True, "artifact_validity_window_lapsed")


def test_taf_training_not_due_without_a_committed_p0_root(
    tmp_path: Path,
) -> None:
    # There is no scope to fit against, and guessing one would produce an
    # artifact no root can ever join to -- the exact stranding of 2026-07-29.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    _taf_revision_schema(conn)
    worker = _taf_revision_worker(conn, tmp_path / "active.json")

    due, scope, reason = worker._taf_revision_training_due(
        station_id="ZUUU",
        as_of_utc="2026-08-10T12:00:00Z",
    )
    assert (due, scope, reason) == (False, {}, "no_committed_p0_root")


def test_taf_training_rotates_past_stations_it_cannot_serve(
    tmp_path: Path,
) -> None:
    """One due station per cycle, and a throttled one does not stall the rest."""

    import sqlite3

    conn = sqlite3.connect(":memory:")
    _taf_revision_schema(conn)
    worker = _taf_revision_worker(conn, tmp_path / "active.json")
    worker.station_allowlist = ("KMIA", "RKPK", "ZUUU")
    worker.db_path = tmp_path / "target.db"
    worker.p0_inbox_path = tmp_path / "p0_inbox.json"
    worker.p0_inbox_path.write_text("{}", encoding="utf-8")
    worker.nwp_runtime_root = tmp_path / "runtime"
    worker.taf_bound_artifact_root = tmp_path / "bound"
    _p0_root(conn, station="RKPK")
    _p0_root(conn, station="ZUUU")
    submitted: list[object] = []

    class _StubPool:
        def submit(self, function):
            submitted.append(function)
            return SimpleNamespace(done=lambda: True)

    worker._taf_revision_pool = _StubPool()

    first = worker._run_taf_revision_training(as_of_utc="2026-08-10T12:00:00Z")
    second = worker._run_taf_revision_training(as_of_utc="2026-08-10T12:00:00Z")
    third = worker._run_taf_revision_training(as_of_utc="2026-08-10T12:00:00Z")

    assert len(submitted) == 2
    # KMIA has no P0 root and is skipped without consuming the cycle.
    assert first["state"] == "SUBMITTED" and first["station_id"] == "RKPK"
    assert second["state"] == "SUBMITTED" and second["station_id"] == "ZUUU"
    # Both due stations are now inside their retry throttle.
    assert third["state"] == "NOT_DUE"
    assert all(value["calls_order_gateway"] is False for value in (first, second, third))
    assert all(value["orders_submitted"] == 0 for value in (first, second, third))


def _taf_artifact_registry(
    *,
    season: str = "JJA",
    cycle: str = "18Z",
) -> dict[str, object]:
    identity = {
        "schema": "bayes_basket_taf_revision_artifact_v2",
        "artifact_id": f"taf-zuuu-{season}-d2-{cycle}-test",
        "activation_id": f"taf-zuuu-{season}-d2-{cycle}-test-activation",
        "station_id": "ZUUU",
        "season_bin": season,
        "lead_bucket": "d2",
        "run_cycle": cycle,
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": "Asia/Shanghai",
        "method_version": "oos_taf_confirmation_bounded_correction_v2",
        "trained_through_utc": "2026-07-20T16:00:00Z",
        "sample_n": 26,
        "effective_sample_n": 20.0,
        "oos_metrics": {"brier_score": 0.2, "nll": 0.5, "crps": 0.4},
        "tx_bias_c": 0.1,
        "taf_nwp_delta_coefficient": 0.5,
        "tx_residual_scale_c": 1.0,
        "physical_feature_effect_c": {
            "cloud_cover_fraction": 0.2,
            "precipitation_probability": -0.1,
            "wind_speed_normalized": -0.05,
        },
        "wind_speed_cap_mps": 10.0,
        "confirmation_tolerance_c": 0.75,
        "max_confirmation_strength": 0.16,
        "max_correction_strength": 0.11,
        "max_probability_mass_transfer": 0.05,
        "max_center_displacement_buckets": 1,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "nwp_combination_artifact_sha256": "9" * 64,
        "nwp_combination_artifact_kind": TAF_NWP_CROSSFIT_ARTIFACT_KIND,
        "nwp_live_compatibility_contract_version": (
            TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ),
        "nwp_live_compatibility_fingerprint_sha256": "a" * 64,
        "nwp_live_compatibility_evidence_sha256": "b" * 64,
        "nwp_live_compatibility_validated": True,
        "oos_taf_nwp_residual_correlation": 0.3,
        "correlation_discount_method": (
            "holdout_taf_vs_nwp_combination_residual_pearson_v1"
        ),
        "correlation_discount": 0.7,
        "joint_physical_oos_validated": True,
        "valid_from_utc": "2026-08-01T00:00:00Z",
        "valid_until_utc": "2026-08-31T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "activated": True,
    }
    return build_taf_artifact_registry([
        {**identity, "content_sha256": content_sha256(identity)}
    ])


def _rotation_worker(tmp_path: Path) -> TargetProductionWorker:
    worker = object.__new__(TargetProductionWorker)
    worker.nwp_runtime_root = tmp_path
    worker.nwp_rotation_state_path = tmp_path / "rotation_state.json"
    worker._native_ensemble_scope_cursor = 3
    worker._nwp_rotation_state = None
    return worker


def _rotation_scope(station: str, day: str) -> dict[str, object]:
    return {
        "event_date": day,
        "settlement_timezone": "America/New_York",
        "station_ids": [station],
        "outcome_count": 1,
        "event_day_state": "open",
        "lead_days": 2,
    }


def test_rotation_state_is_persisted_on_a_reused_scope_not_only_a_capture(
    tmp_path: Path,
) -> None:
    # The cycle used to write this file on exactly one of its seven exits.
    # Once packets existed it returned "official_native_reused" instead, so the
    # last write stayed put while capture carried on for another fourteen
    # hours -- leaving the cursor and the scope catalogue describing a sweep
    # two days old.
    worker = _rotation_worker(tmp_path)
    scopes = [
        _rotation_scope("KMIA", "2026-08-09"),
        _rotation_scope("KLGA", "2026-08-09"),
    ]

    receipt = worker._persist_nwp_rotation(
        station_ids=("KMIA", "KLGA"),
        scopes=scopes,
        selected_scopes=[scopes[0]],
        outcomes=[{
            "scope_key": "KMIA|2026-08-09|America/New_York|d2",
            "state": "OFFICIAL_NATIVE_REUSED",
            "candidate_only": False,
            "reason": "official_native_precedence",
        }],
        as_of_utc="2026-08-09T00:00:00Z",
    )

    assert receipt["rotation_state_wrote"] is True
    assert receipt["next_scope_cursor"] == 3
    written = json.loads(
        (tmp_path / "rotation_state.json").read_text(encoding="utf-8")
    )
    assert written["catalog_scope_count"] == 2
    # The catalogue on disk has to be the one just discovered, because the
    # in-flight status receipt republishes it verbatim and a restart resumes
    # the cursor against it.
    assert all("2026-08-09" in key for key in written["catalog_scope_keys"])


def test_rotation_state_write_failure_does_not_fail_the_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rotation state is scheduler telemetry and a restart hint, not an
    # authorization boundary: a failed write must not turn a completed capture
    # into a fail-closed cycle.
    worker = _rotation_worker(tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(production_worker_module, "persist_rotation_state", _boom)

    receipt = worker._persist_nwp_rotation(
        station_ids=("KMIA",),
        scopes=[_rotation_scope("KMIA", "2026-08-09")],
        selected_scopes=[_rotation_scope("KMIA", "2026-08-09")],
        outcomes=[],
        as_of_utc="2026-08-09T00:00:00Z",
    )

    assert receipt["rotation_state_wrote"] is False
    assert "OSError" in receipt["rotation_state_error"]
    # Still advanced in memory, so the current process does not replay the
    # scope it just finished.
    assert worker._nwp_rotation_state is not None


def test_open_scope_round_robin_accepts_every_station_on_several_days() -> None:
    # Open scopes are station-days.  Bounding them at the station count made
    # "more than one event day is open" an error, which is what forced
    # something upstream to pin the lane to a single date.
    worker = object.__new__(TargetProductionWorker)
    worker._resident_open_scope_cursor = 0
    scopes = [
        {"scope_key": f"S{station:02d}|2026-07-{30 + day:02d}"}
        for day in range(3)
        for station in range(45)
    ]

    assert len(scopes) > production_worker_module.MAX_FORWARD_P0_STATIONS
    assert worker._select_resident_open_ws_scope(scopes) is not None


def test_open_scope_round_robin_still_bounds_the_station_day_catalogue() -> None:
    worker = object.__new__(TargetProductionWorker)
    worker._resident_open_scope_cursor = 0
    scopes = [
        {"scope_key": f"S{index:04d}|2026-07-30"}
        for index in range(
            production_worker_module.MAX_FORWARD_P0_STATION_DAYS + 1
        )
    ]

    with pytest.raises(
        ContractError,
        match="resident_target_ws_open_scope_station_day_cap_exceeded",
    ):
        worker._select_resident_open_ws_scope(scopes)


def test_worker_rejects_protected_resident_market_manifest(tmp_path: Path) -> None:
    db = tmp_path / "target-read.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE marker(value TEXT)")

    with pytest.raises(ValueError, match="must_not_be_protected_resident_manifest"):
        TargetProductionWorker(
            db,
            market_manifest_path=PROTECTED_RESIDENT_WS_MANIFEST,
        )


def test_run_keeps_truth_and_labels_outside_official_ws_quote_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    activation = (
        Path(__file__).parents[1]
        / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    )
    initialize(db=target_db, activation_path=activation)
    commit_conn = sqlite3.connect(target_db)
    commit_conn.row_factory = sqlite3.Row
    worker = TargetProductionWorker(
        target_db,
        commit_conn=commit_conn,
        activation_path=activation,
    )
    worker.nwp_runtime_root = tmp_path / "empty-native-runtime"
    calls: list[str] = []

    def resident_first(*, as_of_utc: str):
        assert as_of_utc
        calls.append("resident_p1")
        return {
            "schema": "bayes_basket_target_resident_probability_chain_v1",
            "state": "COMMITTED",
            "p0": {"state": "COMMITTED"},
            "p0m": {"state": "COMMITTED"},
            "p1": {"state": "COMMITTED", "p1_update_count": 1},
        }

    def truth_second(*, as_of_utc: str):
        assert calls == ["resident_p1", "official_ws"]
        calls.append("settlement_truth")
        return {"state": "complete", "written_count": 0}

    def labels_third(*, as_of_utc: str, enforce_interval: bool = True):
        assert calls == [
            "resident_p1",
            "official_ws",
            "settlement_truth",
        ]
        assert enforce_interval is True
        calls.append("settlement_labels")
        return {"state": "complete", "appended_count": 0}

    def market_second(_activation):
        assert calls == ["resident_p1"]
        calls.append("official_ws")
        raise RuntimeError("stop_after_order_check")

    monkeypatch.setattr(
        worker,
        "_materialize_resident_probability_chain",
        resident_first,
    )
    monkeypatch.setattr(worker, "_materialize_delta_v2_truths", truth_second)
    monkeypatch.setattr(worker, "_materialize_delta_v2_labels", labels_third)
    monkeypatch.setattr(worker, "_materialize_market_evidence", market_second)
    monkeypatch.setattr(worker, "_resident_ws_scope_catalog", lambda: [])
    monkeypatch.setattr(worker, "_decision_scopes", lambda **_kwargs: [])
    try:
        result = worker.run()
    finally:
        worker.close()

    assert calls == [
        "resident_p1",
        "official_ws",
        "settlement_truth",
        "settlement_labels",
    ]
    assert result["state"] == "blocked_fail_closed"
    assert result["calls_order_gateway"] is False
    assert result["live_submit_authorized"] is False
    assert result["orders_submitted"] == 0


def test_run_scoped_p1_blocker_allows_truth_label_and_does_not_global_block_market(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    activation = (
        Path(__file__).parents[1]
        / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    )
    initialize(db=target_db, activation_path=activation)
    commit_conn = sqlite3.connect(target_db)
    commit_conn.row_factory = sqlite3.Row
    worker = TargetProductionWorker(
        target_db,
        commit_conn=commit_conn,
        activation_path=activation,
    )
    worker.nwp_runtime_root = tmp_path / "empty-native-runtime"
    downstream_calls: list[str] = []
    blocked_p1 = {
        "state": "BLOCKED_FAIL_CLOSED",
        "blockers": [{
            "station_id": "RKPK",
            "event_date": "2026-07-28",
            "reason": (
                "ContractError:"
                "resident_p1_correction_requires_manual_review"
            ),
        }],
    }
    monkeypatch.setattr(
        worker,
        "_materialize_resident_probability_chain",
        lambda **_kwargs: {
            "schema": "bayes_basket_target_resident_probability_chain_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "p0": {"state": "COMMITTED"},
            "p0m": {"state": "COMMITTED"},
            "p1": blocked_p1,
        },
    )
    def settle_truth(**_kwargs):
        downstream_calls.append("truth")
        return {"state": "complete", "written_count": 1}

    def settle_labels(**_kwargs):
        downstream_calls.append("label")
        return {"state": "committed", "appended_count": 1}

    monkeypatch.setattr(worker, "_materialize_delta_v2_truths", settle_truth)
    monkeypatch.setattr(worker, "_materialize_delta_v2_labels", settle_labels)
    monkeypatch.setattr(
        worker,
        "_materialize_market_evidence",
        lambda *_args, **_kwargs: downstream_calls.append("market"),
    )
    monkeypatch.setattr(worker, "_decision_scopes", lambda **_kwargs: [])
    try:
        result = worker.run()
    finally:
        worker.close()

    assert downstream_calls == ["truth", "label"]
    assert result["state"] == "blocked_fail_closed"
    # The worker now reaches the market boundary; this fixture has no
    # registry snapshot, so that independent evidence blocker is expected.
    assert result["reason"] == "ContractError:target_market_registry_snapshot_empty"
    assert result["delta_v2_truth_ingress"]["written_count"] == 1
    assert result["delta_v2_label_ingress"]["appended_count"] == 1
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_run_truth_blocker_still_prevents_labels_and_market(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    activation = (
        Path(__file__).parents[1]
        / "config/trading/bayes_basket_target_production_no_live_activation_v1.json"
    )
    initialize(db=target_db, activation_path=activation)
    commit_conn = sqlite3.connect(target_db)
    commit_conn.row_factory = sqlite3.Row
    worker = TargetProductionWorker(
        target_db,
        commit_conn=commit_conn,
        activation_path=activation,
    )
    worker.nwp_runtime_root = tmp_path / "empty-native-runtime"
    downstream_calls: list[str] = []
    monkeypatch.setattr(
        worker,
        "_materialize_resident_probability_chain",
        lambda **_kwargs: {
            "schema": "bayes_basket_target_resident_probability_chain_v1",
            "state": "BLOCKED_FAIL_CLOSED",
            "p0": {"state": "COMMITTED"},
            "p0m": {"state": "COMMITTED"},
            "p1": {"state": "BLOCKED_FAIL_CLOSED"},
        },
    )
    monkeypatch.setattr(
        worker,
        "_materialize_delta_v2_truths",
        lambda **_kwargs: {
            "state": "blocked_fail_closed",
            "reason": (
                "ContractError:"
                "settlement_truth_correction_requires_manual_review"
            ),
            "written_count": 0,
        },
    )
    monkeypatch.setattr(
        worker,
        "_materialize_delta_v2_labels",
        lambda **_kwargs: downstream_calls.append("label"),
    )
    monkeypatch.setattr(
        worker,
        "_materialize_market_evidence",
        lambda *_args, **_kwargs: downstream_calls.append("market"),
    )
    monkeypatch.setattr(worker, "_decision_scopes", lambda **_kwargs: [])
    try:
        result = worker.run()
    finally:
        worker.close()

    assert downstream_calls == []
    assert result["state"] == "blocked_fail_closed"
    assert result["reason"] == (
        "ContractError:resident_p1_blocked_before_current_scope_market"
    )
    assert "settlement_truth_correction_requires_manual_review" in (
        result["delta_v2_truth_ingress"]["reason"]
    )
    assert result["delta_v2_truth_ingress"]["written_count"] == 0
    assert result["delta_v2_label_ingress"]["appended_count"] == 0
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_market_failure_preserves_verified_scope_and_real_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = sqlite3.connect(":memory:")
    worker.market_manifest_path = tmp_path / "target-ws.json"
    worker.activation_path = tmp_path / "activation.json"
    manifest = {
        "target_local_date": "2026-07-28",
        "station_scope": {
            "mode": "allowlist",
            "station_ids": ["RKPK"],
        },
    }
    monkeypatch.setattr(
        production_worker_module,
        "_resolve_target_remaining_budget",
        lambda: 0.0,
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_official_ws_quotes",
        lambda _path: (manifest, {}),
    )
    monkeypatch.setattr(
        production_worker_module,
        "materialize_official_ws_evidence_cache",
        lambda *_args, **_kwargs: {"state": "COMMITTED"},
    )

    def blocked_cash(*_args, **_kwargs):
        raise ContractError("resident_cash_phase_authority_manifest_not_unique")

    monkeypatch.setattr(
        production_worker_module,
        "materialize_resident_no_live_market_evidence",
        blocked_cash,
    )
    try:
        result = worker._materialize_market_evidence(
            {"policy_authority_scope": "PRODUCTION_NO_LIVE_ONLY"}
        )
    finally:
        worker.commit_conn.close()

    assert result["state"] == "blocked_fail_closed"
    assert result["station_allowlist"] == ["RKPK"]
    assert result["event_date"] == "2026-07-28"
    assert (
        result["reason"]
        == "ContractError:resident_cash_phase_authority_manifest_not_unique"
    )
    assert result["calls_order_gateway"] is False
    assert result["orders_submitted"] == 0


def test_probability_market_materialization_rewinds_stale_fresh_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = object.__new__(TargetProductionWorker)
    worker.commit_conn = sqlite3.connect(":memory:")
    worker.activation_path = tmp_path / "activation.json"
    manifest_path = tmp_path / "target-ws.json"
    manifest = {
        "target_local_date": "2026-07-29",
        "station_scope": {
            "mode": "allowlist",
            "station_ids": ["RKPK"],
        },
    }
    cache_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        production_worker_module,
        "_resolve_target_remaining_budget",
        lambda: 20.0,
    )
    monkeypatch.setattr(
        production_worker_module,
        "load_official_ws_quotes",
        lambda _path: (manifest, {}),
    )

    def materialize_cache(*_args, **kwargs):
        cache_calls.append(dict(kwargs))
        return {"state": "COMMITTED", "rows_written": 1}

    monkeypatch.setattr(
        production_worker_module,
        "materialize_official_ws_evidence_cache",
        materialize_cache,
    )

    full_materialization_calls: list[dict[str, object]] = []

    def materialize_full(*_args, **kwargs):
        full_materialization_calls.append(dict(kwargs))
        return {"state": "COMMITTED", "rows_written": 3}

    monkeypatch.setattr(
        production_worker_module,
        "materialize_resident_no_live_market_evidence",
        materialize_full,
    )
    try:
        stale_first = worker._materialize_market_evidence_from_manifest(
            {"policy_authority_scope": "PRODUCTION_NO_LIVE_ONLY"},
            manifest_path=manifest_path,
            full_materialization=False,
        )
        fresh = worker._materialize_market_evidence_from_manifest(
            {"policy_authority_scope": "PRODUCTION_NO_LIVE_ONLY"},
            manifest_path=manifest_path,
            full_materialization=True,
        )
        stale_again = worker._materialize_market_evidence_from_manifest(
            {"policy_authority_scope": "PRODUCTION_NO_LIVE_ONLY"},
            manifest_path=manifest_path,
            full_materialization=False,
        )
    finally:
        worker.commit_conn.close()

    assert stale_first["state"] == "evidence_cache_only"
    assert stale_first["full_materialization"] is False
    assert stale_first["station_allowlist"] == ["RKPK"]
    assert stale_first["event_date"] == "2026-07-29"
    assert fresh["state"] == "committed"
    assert stale_again["state"] == "evidence_cache_only"
    assert len(cache_calls) == 3
    assert len(full_materialization_calls) == 1
    assert cache_calls[0]["station_id"] == "RKPK"
    assert cache_calls[0]["event_date"] == "2026-07-29"
    assert full_materialization_calls[0]["materialize_ws_cache"] is False
    assert stale_again["calls_order_gateway"] is False
    assert stale_again["live_submit_authorized"] is False
    assert stale_again["orders_submitted"] == 0


def test_resident_p1_ingress_is_append_only_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    commit_conn = sqlite3.connect(target_db)
    commit_conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(commit_conn)
    _seed_target_registry_source_lane(
        commit_conn,
        station_id="RKPK",
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
        source_lane="awc",
    )
    p0 = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "resident-p0",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T14:00:00Z",
        "fresh_until_utc": "2026-07-26T15:00:00Z",
        "bucket_ordinals": {
            "28°C or below": 0,
            "29°C": 1,
            "30°C or higher": 2,
        },
        "probabilities": {
            "28°C or below": 0.2,
            "29°C": 0.5,
            "30°C or higher": 0.3,
        },
        "posterior_version": "resident-p0-v1",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
            "lineage": {
                "settlement_timezone": "Asia/Seoul",
                "season_bin": "JJA",
                "settlement_definition": "station_local_calendar_day_tmax",
                "bucket_intervals_c_json": json.dumps(
                    {
                        "28°C or below": [None, 28.5],
                        "29°C": [28.5, 29.5],
                        "30°C or higher": [29.5, None],
                    },
                    sort_keys=True,
                        separators=(",", ":"),
                    ),
                    **_future_curve_lineage(
                        checkpoint_id="resident-p0",
                        posterior_sha256="a" * 64,
                    ),
                    **_taf_live_compatibility_lineage(),
            },
    })
    materialize_probability_bridge(
        commit_conn,
        p0,
        created_at_utc=p0.decision_at_utc,
    )
    formal_artifact = _activate_formal_trajectory_artifact(
        commit_conn,
        local_time_bucket="NIGHT",
    )
    commit_conn.commit()
    registry_rows = [
        {
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "settlement_unit": "C",
            "source_lane": "awc",
            "outcome": outcome,
            "registry_json": (
                '{"outcome":"%s","upper":%s}'
                % (
                    outcome,
                    "null" if upper is None else upper,
                )
            ),
        }
        for outcome, upper in (
            ("28°C or below", 28),
            ("29°C", 29),
            ("30°C or higher", None),
        )
    ]
    observations = [{
        "observation_id": f"resident-observation-{index}",
        "station_id": "RKPK",
        "event_date_local": "2026-07-27",
        "reported_temperature_c": temperature,
        "valid_at_utc": f"2026-07-26T15:{minute}:00Z",
        "canonical_committed_at_utc": f"2026-07-26T15:{int(minute) + 1:02d}:00Z",
        "raw_sha256": f"{index:x}" * 64,
        "raw_text": (
            f"{report_type} RKPK 2615{minute}Z 18005KT CAVOK "
            f"{int(temperature):02d}/20 Q1008"
        ),
        "source_lane": "awc",
        "report_type": report_type,
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    } for index, (minute, temperature, report_type) in enumerate(
        (("00", 27.0, "METAR"), ("15", 28.0, "SPECI"), ("30", 29.0, "SPECI")),
        start=1,
    )]
    monkeypatch.setattr(
        "bayes_basket_target.production_worker.load_started_registry_rows",
        lambda *_args, **_kwargs: registry_rows,
    )
    monkeypatch.setattr(
        "bayes_basket_target.production_worker.load_resident_formal_observations",
        lambda *_args, **_kwargs: observations,
    )
    worker = TargetProductionWorker(
        target_db,
        commit_conn=commit_conn,
    )
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    try:
        first = worker._materialize_resident_p1(
            as_of_utc="2026-07-26T16:00:00Z",
        )
        replay = worker._materialize_resident_p1(
            as_of_utc="2026-07-26T16:00:00Z",
        )
        assert first["p1_update_count"] == 1
        assert first["writes_target_ssot"] is True
        assert replay["p1_update_count"] == 0
        assert replay["writes_target_ssot"] is False
        assert commit_conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope "
            "WHERE stage='P1'"
        ).fetchone()[0] == 1
        assert commit_conn.execute(
            "SELECT COUNT(*) FROM basket_probability_bridge_history "
            "WHERE stage='P1'"
        ).fetchone()[0] == 1
        assert commit_conn.execute(
            "SELECT stage FROM basket_probability_bridge_latest "
            "WHERE station_id='RKPK' AND event_date='2026-07-27'"
        ).fetchone()[0] == "P1"
        proof_row = commit_conn.execute(
            "SELECT artifact_id_or_none,activation_id_or_none,proof_json "
            "FROM basket_probability_update_proof "
            "WHERE proof_kind='METAR_TRAJECTORY_UPDATE'"
        ).fetchone()
        assert proof_row is not None
        assert proof_row[0] == formal_artifact.artifact_id
        assert proof_row[1] == formal_artifact.activation_id
        assert json.loads(str(proof_row[2]))["applied_strength"] > 0.0
        assert commit_conn.execute(
            "SELECT COUNT(*) FROM basket_probability_update_proof "
            "WHERE proof_kind='METAR_TRAJECTORY_UPDATE'"
        ).fetchone()[0] == 1
    finally:
        worker.close()


def test_resident_p1_ingress_commits_stale_replay_cursor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    commit_conn = sqlite3.connect(target_db)
    commit_conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(commit_conn)
    _seed_target_registry_source_lane(
        commit_conn,
        station_id="RKPK",
        event_date="2026-07-27",
        settlement_timezone="Asia/Seoul",
        source_lane="awc",
    )
    p0 = ProbabilityEnvelope.from_mapping({
        "stage": "P0",
        "checkpoint_id": "resident-p0-stale-replay",
        "station_id": "RKPK",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Seoul",
        "decision_at_utc": "2026-07-26T14:00:00Z",
        "fresh_until_utc": "2026-07-26T15:00:00Z",
        "bucket_ordinals": {
            "28°C or below": 0,
            "29°C": 1,
            "30°C or higher": 2,
        },
        "probabilities": {
            "28°C or below": 0.2,
            "29°C": 0.5,
            "30°C or higher": 0.3,
        },
        "posterior_version": "resident-p0-v1",
        "posterior_sha256": "a" * 64,
        "bridge_sha256": "b" * 64,
            "lineage": {
                "settlement_timezone": "Asia/Seoul",
                "season_bin": "JJA",
                "settlement_definition": "station_local_calendar_day_tmax",
                    "bucket_intervals_c_json": json.dumps(
                    {
                        "28°C or below": [None, 28.5],
                        "29°C": [28.5, 29.5],
                        "30°C or higher": [29.5, None],
                    },
                    sort_keys=True,
                        separators=(",", ":"),
                    ),
                    **_future_curve_lineage(
                        checkpoint_id="resident-p0-stale-replay",
                        posterior_sha256="a" * 64,
                    ),
                    **_taf_live_compatibility_lineage(),
            },
    })
    materialize_probability_bridge(
        commit_conn,
        p0,
        created_at_utc=p0.decision_at_utc,
    )
    _activate_formal_trajectory_artifact(
        commit_conn,
        local_time_bucket="NIGHT",
    )
    commit_conn.commit()
    registry_rows = [
        {
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "settlement_unit": "C",
            "source_lane": "awc",
            "outcome": outcome,
            "registry_json": (
                '{"outcome":"%s","upper":%s}'
                % (outcome, "null" if upper is None else upper)
            ),
        }
        for outcome, upper in (
            ("28°C or below", 28),
            ("29°C", 29),
            ("30°C or higher", None),
        )
    ]
    observations = [{
        "observation_id": f"delayed-new-high-{index}",
        "station_id": "RKPK",
        "event_date_local": "2026-07-27",
        "reported_temperature_c": temperature,
        "valid_at_utc": f"2026-07-26T15:{minute}:00Z",
        "canonical_committed_at_utc": f"2026-07-26T15:{int(minute) + 1:02d}:00Z",
        "raw_sha256": f"{index + 3:x}" * 64,
        "raw_text": (
            f"{report_type} RKPK 2615{minute}Z 18005KT CAVOK "
            f"{int(temperature):02d}/20 Q1008"
        ),
        "source_lane": "awc",
        "report_type": report_type,
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    } for index, (minute, temperature, report_type) in enumerate(
        (("00", 27.0, "METAR"), ("15", 28.0, "SPECI"), ("30", 29.0, "SPECI")),
        start=1,
    )]
    monkeypatch.setattr(
        "bayes_basket_target.production_worker.load_started_registry_rows",
        lambda *_args, **_kwargs: registry_rows,
    )
    monkeypatch.setattr(
        "bayes_basket_target.production_worker."
        "load_resident_formal_observations",
        lambda *_args, **_kwargs: observations,
    )
    worker = TargetProductionWorker(
        target_db,
        commit_conn=commit_conn,
    )
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    try:
        first = worker._materialize_resident_p1(
            as_of_utc="2026-07-26T18:00:00Z",
        )
        replay = worker._materialize_resident_p1(
            as_of_utc="2026-07-26T18:00:00Z",
        )
        assert first["state"] == "COMMITTED"
        assert first["p1_update_count"] == 1
        assert first["receipts"][0]["current_consumable"] is False
        assert first["receipts"][0]["replay_cursor_only"] is True
        assert replay["p1_update_count"] == 0
        assert commit_conn.execute(
            "SELECT COUNT(*) FROM basket_probability_envelope "
            "WHERE stage='P1'"
        ).fetchone()[0] == 1
        with pytest.raises(
            ContractError,
            match="latest_stage_stale_fail_closed",
        ):
            select_current_stage(
                load_materialized_probability_envelopes(
                    commit_conn,
                    station_id="RKPK",
                    event_date="2026-07-27",
                    evaluation_at_utc="2026-07-26T18:00:00Z",
                ),
                at_utc="2026-07-26T18:00:00Z",
            )
    finally:
        worker.close()


def test_resident_p1_ingress_ignores_registry_scope_without_probability_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    commit_conn = sqlite3.connect(target_db)
    commit_conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(commit_conn)
    monkeypatch.setattr(
        "bayes_basket_target.production_worker.load_started_registry_rows",
        lambda *_args, **_kwargs: [{
            "station_id": "RKPK",
            "event_date": "2026-07-27",
            "settlement_timezone": "Asia/Seoul",
            "source_lane": "awc",
        }],
    )

    def unexpected_source_read(*_args, **_kwargs):
        raise AssertionError("canonical source must not be read without P0")

    monkeypatch.setattr(
        "bayes_basket_target.production_worker."
        "load_resident_formal_observations",
        unexpected_source_read,
    )
    worker = TargetProductionWorker(
        target_db,
        commit_conn=commit_conn,
    )
    worker.canonical_source_conn = sqlite3.connect(":memory:")
    try:
        result = worker._materialize_resident_p1(
            as_of_utc="2026-07-26T16:00:00Z",
        )
        assert result["scope_count"] == 0
        assert result["probability_scope_count"] == 0
        assert result["p1_update_count"] == 0
        assert result["writes_target_ssot"] is False
    finally:
        worker.close()


def test_target_worker_reopens_poisoned_target_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_db = tmp_path / "target.db"
    target_db.touch()

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    old_read = FakeConnection()
    old_write = FakeConnection()
    new_read = FakeConnection()
    new_write = FakeConnection()
    monkeypatch.setattr(
        production_worker_module,
        "open_target_read_only",
        lambda _path: new_read,
    )
    monkeypatch.setattr(
        production_worker_module,
        "open_target_writer_if_enabled",
        lambda _path, *, activation_path: new_write,
    )
    worker = object.__new__(TargetProductionWorker)
    worker.db_path = target_db.resolve()
    worker.activation_path = tmp_path / "activation.json"
    worker.conn = old_read
    worker.commit_conn = old_write
    worker.open_error = "OperationalError:disk I/O error"
    worker.commit_error = "OperationalError:disk I/O error"

    worker.reopen_target_connections()

    assert old_read.closed is True
    assert old_write.closed is True
    assert worker.conn is new_read
    assert worker.commit_conn is new_write
    assert worker.open_error is None
    assert worker.commit_error is None


def _rolling(tmp_path: Path, entries: list[tuple[str, list[str], int]]) -> Path:
    path = tmp_path / "rolling.json"
    path.write_text(
        json.dumps({
            "schema": "rolling_native_taf_oos_v1",
            "accumulating": [
                {
                    "season_bin": "JJA",
                    "lead_bucket": lead,
                    "selected_system_ids": systems,
                    "plan": {
                        "common_case_count": common,
                        "required_common_case_count": 44,
                        "missing_common_case_count": 44 - common,
                    },
                }
                for lead, systems, common in entries
            ],
        }),
        encoding="utf-8",
    )
    return path


LIVE_PAIR = "ECMWF_IFS_ENS_OPEN_PF50,NOAA_GEFS_05"


def test_taf_accumulation_reports_the_serving_pair_over_the_faster_one(
    tmp_path: Path,
) -> None:
    path = _rolling(tmp_path, [
        ("d2", ["GOOGLE_WEATHERNEXT2_ENS", "NOAA_AIGEFS_025"], 30),
        ("d2", ["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"], 4),
    ])

    result = TargetProductionWorker._taf_rolling_accumulation(
        path,
        season_bin="JJA",
        lead_bucket="d2",
        live_system_ids=LIVE_PAIR,
    )

    # The AI pair is 26 days ahead, and its artifact could never serve: the
    # TAF live-compatibility fingerprint binds to the root's model set.
    assert result["is_live_serving_pair"] is True
    assert result["selected_system_ids"] == [
        "ECMWF_IFS_ENS_OPEN_PF50",
        "NOAA_GEFS_05",
    ]
    assert result["common_case_count"] == 4


def test_taf_accumulation_names_a_serving_pair_stranded_by_lead_vocabulary(
    tmp_path: Path,
) -> None:
    # The live RKPK shape: the serving pair has a series, but only under the
    # hour-bucket vocabulary, while the P0 root still stamps a calendar lead.
    # Another pair holding d2 would otherwise present this as a day countdown
    # that no number of settled days can finish.
    path = _rolling(tmp_path, [
        ("d2", ["GOOGLE_WEATHERNEXT2_ENS", "NOAA_AIGEFS_025"], 1),
        ("h00_12", ["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"], 20),
    ])

    result = TargetProductionWorker._taf_rolling_accumulation(
        path,
        season_bin="JJA",
        lead_bucket="d2",
        live_system_ids=LIVE_PAIR,
    )

    assert result["is_live_serving_pair"] is False
    assert result["serving_pair_reason"] == (
        "serving_pair_only_under_other_lead_buckets"
    )
    assert result["serving_pair_lead_buckets"] == ["h00_12"]


def test_taf_accumulation_without_a_live_pair_keeps_the_best_covered(
    tmp_path: Path,
) -> None:
    path = _rolling(tmp_path, [
        ("d2", ["GOOGLE_WEATHERNEXT2_ENS", "NOAA_AIGEFS_025"], 30),
        ("d2", ["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"], 4),
    ])

    result = TargetProductionWorker._taf_rolling_accumulation(
        path,
        season_bin="JJA",
        lead_bucket="d2",
    )

    assert result["is_live_serving_pair"] is False
    assert "serving_pair_reason" not in result
    assert result["common_case_count"] == 30


def _scope(station: str, day_state: str, stage: str, fresh: bool, blocker: str = "") -> dict:
    return {
        "scope_key": f"{station}|2026-08-11",
        "station_id": station,
        "event_date": "2026-08-11",
        "event_day_state": day_state,
        "probability_stage": stage,
        "probability_evidence_fresh": fresh,
        "scope_blocker": blocker,
    }


def test_ws_scope_summary_names_a_stale_open_p1_that_would_vanish() -> None:
    # A stale open P1 raises no preliminary decision (it is not a not-started
    # scope) and is dropped by the actionable filter, so before this summary
    # it left no trace anywhere -- indistinguishable from never scanned.
    summary = TargetProductionWorker._ws_scope_catalog_summary([
        _scope("EHAM", "open", "P1", True),
        _scope(
            "RKPK", "open", "P1", False,
            blocker="resident_target_ws_open_scope_p1_probability_stale",
        ),
    ])

    assert summary["scope_count"] == 2
    assert summary["actionable_scope_count"] == 1
    assert summary["blocked_scope_count"] == 1
    blocked = summary["blocked_scopes"][0]
    assert blocked["station_id"] == "RKPK"
    assert blocked["probability_stage"] == "P1"
    assert blocked["reason"] == (
        "resident_target_ws_open_scope_p1_probability_stale"
    )


def test_ws_scope_summary_reports_a_stale_scope_even_without_a_blocker() -> None:
    # Freshness alone is enough to drop a scope, so a stale one must be
    # reported whether or not a blocker string was attached upstream.
    summary = TargetProductionWorker._ws_scope_catalog_summary([
        _scope("KSEA", "open", "P1", False),
    ])

    assert summary["blocked_scope_count"] == 1
    assert summary["blocked_scopes"][0]["reason"] == (
        "resident_target_ws_probability_stale"
    )


def test_ws_scope_summary_counts_a_fresh_catalog_as_fully_actionable() -> None:
    summary = TargetProductionWorker._ws_scope_catalog_summary([
        _scope("EHAM", "open", "P1", True),
        _scope("KORD", "not_started", "P0", True),
    ])

    assert summary["actionable_scope_count"] == 2
    assert summary["blocked_scopes"] == []


def test_no_snapshot_is_loaded_unless_one_is_asked_for(monkeypatch, tmp_path) -> None:
    # The default snapshot froze 5 of 45 stations on 2026-08-03.  Loading it
    # by default rejected the 40 it never covered and mis-bound the 5 it did,
    # which stopped P0 for the whole fleet.
    monkeypatch.setenv("PM_TARGET_EXECUTION_MODE", "production_no_live")
    monkeypatch.delenv("PM_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST", raising=False)

    worker = TargetProductionWorker(db_path=tmp_path / "target.db")

    assert worker.native_oos_snapshot is None
    assert worker.native_oos_snapshot_error is None


def test_an_explicitly_requested_snapshot_still_fails_closed(
    monkeypatch, tmp_path
) -> None:
    # Pinning a snapshot on purpose keeps the old contract: an unreadable one
    # must leave a non-None sentinel so every compile fails closed rather than
    # falling back to the newest append-only activation.
    monkeypatch.setenv("PM_TARGET_EXECUTION_MODE", "production_no_live")
    missing = tmp_path / "absent_snapshot.json"
    monkeypatch.setenv(
        "PM_TARGET_NATIVE_OOS_SNAPSHOT_MANIFEST", str(missing)
    )

    worker = TargetProductionWorker(db_path=tmp_path / "target.db")

    assert worker.native_oos_snapshot is not None
    assert worker.native_oos_snapshot_error is not None


def _rot_scope(
    station, timezone_name, *, day_state="open",
    envelope_id="env-1", observed_at="2026-08-12T06:00:00Z",
):
    return {
        "scope_key": f"{station}|2026-08-12",
        "station_id": station,
        "event_day_state": day_state,
        "manifest": {
            "target_timezone": timezone_name,
            "probability_authority": {"envelope_id": envelope_id},
            "quotes_by_token": {
                "t1": {"book_observed_at": observed_at},
            },
        },
    }


def test_rotation_orders_by_what_recompiling_is_worth() -> None:
    """Serve where a recompute would change something, not by ICAO code.

    The old ordering was the scope key, which is alphabetical: on a live
    catalog every station then inside its warm window sorted to positions
    13-27, because Asia's codes begin with R, W and Z, while stations in the
    small hours took positions 0-8.
    """

    from bayes_basket_target.production_worker import (
        TargetProductionWorker as Worker,
    )

    as_of = "2026-08-12T06:20:00Z"   # mid-afternoon Shanghai, 01:20 Chicago
    key = Worker._scope_rotation_key
    served = {
        "served_at_utc": "2026-08-12T06:00:00Z",
        "envelope_id": "env-1",
        "book_observed_at": "2026-08-12T06:00:00Z",
    }

    warm = _rot_scope("ZUUU", "Asia/Shanghai")
    overnight = _rot_scope("KORD", "America/Chicago")
    # Alphabetically the overnight station leads; that was the bug.
    assert overnight["scope_key"] < warm["scope_key"]
    assert key(warm, as_of_utc=as_of, last_served=served, quota_usdc=4.0) < (
        key(overnight, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    )

    # A market never compiled counts as maximal new evidence, not as a tier
    # of its own: at a cold start every scope is unseen, and a tier would
    # flatten them all and hand the ordering back to the alphabet.  So it
    # outranks the same station already served, but does not outrank a warm
    # station somewhere else -- a station still in the small hours has
    # nothing to say yet, first look or not.
    unseen_warm = _rot_scope("ZUUU", "Asia/Shanghai")
    assert key(
        unseen_warm, as_of_utc=as_of, last_served=None, quota_usdc=4.0
    ) < key(warm, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    unseen_overnight = _rot_scope("KORD", "America/Chicago")
    assert key(
        unseen_overnight, as_of_utc=as_of, last_served=None, quota_usdc=4.0
    ) > key(warm, as_of_utc=as_of, last_served=served, quota_usdc=4.0)

    # Tier 1 before tier 2: the day being decided beats the day being planned.
    future = _rot_scope("ZUUU", "Asia/Shanghai", day_state="not_started")
    assert key(overnight, as_of_utc=as_of, last_served=served, quota_usdc=4.0) < (
        key(future, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    )

    # New evidence outranks none: same station, same hour, only the envelope
    # differs.  This is the term that lets the rotation skip a scope whose
    # recompute would reproduce the plan it already has.
    rolled = _rot_scope("ZUUU", "Asia/Shanghai", envelope_id="env-2")
    assert key(rolled, as_of_utc=as_of, last_served=served, quota_usdc=4.0) < (
        key(warm, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    )
    # A moved book counts, but for less than a new probability.
    moved = _rot_scope(
        "ZUUU", "Asia/Shanghai", observed_at="2026-08-12T06:19:00Z"
    )
    warm_key = key(warm, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    moved_key = key(moved, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    rolled_key = key(rolled, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    assert rolled_key < moved_key < warm_key

    # Capital it cannot deploy is capital it cannot act on: an unfunded
    # station ranks below a funded one with identical evidence.
    assert key(warm, as_of_utc=as_of, last_served=served, quota_usdc=4.0) < (
        key(warm, as_of_utc=as_of, last_served=served, quota_usdc=0.0)
    )

    # Among equals the longest unserved wins, which is the anti-starvation
    # guarantee.
    stale = {**served, "served_at_utc": "2026-08-12T01:00:00Z"}
    assert key(warm, as_of_utc=as_of, last_served=stale, quota_usdc=4.0) < (
        key(warm, as_of_utc=as_of, last_served=served, quota_usdc=4.0)
    )

    # An unreadable timezone is ordered low, never raised: refusing to
    # classify one scope would hold up every scope behind it.
    broken = _rot_scope("XXXX", "Not/AZone")
    assert Worker._warm_window_weight(broken, as_of_utc=as_of) == 0.05


def test_warm_window_matches_where_highs_are_actually_set() -> None:
    """10:00-17:00 local, which is where 80% of measured highs landed."""

    from bayes_basket_target.production_worker import (
        TargetProductionWorker as Worker,
    )

    def _weight(utc):
        return Worker._warm_window_weight(
            _rot_scope("ZUUU", "Asia/Shanghai"), as_of_utc=utc
        )

    # The window is not a plateau: a plateau tied thirty-one stations at
    # 1.0 and handed the ordering back to the alphabet, putting 10:09 ahead
    # of 15:09.  Weight follows the measured share of highs per hour.
    assert _weight("2026-08-12T07:00:00Z") == pytest.approx(1.0)  # 15:00
    assert _weight("2026-08-12T06:00:00Z") > _weight(
        "2026-08-12T02:00:00Z"
    )                                                 # 14:00 beats 10:00
    assert _weight("2026-08-12T02:00:00Z") < 0.3      # 10:00 is thin
    # The afternoon tail is fatter than the morning approach, as measured.
    assert _weight("2026-08-12T09:00:00Z") > _weight(
        "2026-08-12T03:00:00Z"
    )                                                 # 17:00 beats 11:00
    # Outside decays, and midnight is measured to the nearer edge rather
    # than across the whole clock.
    assert _weight("2026-08-12T11:00:00Z") < 0.2     # 19:00, thin tail
    assert _weight("2026-08-11T18:00:00Z") <= 0.05   # 02:00, the floor
    # Local midnight outranks 02:00 because it is measured that way: 3.4% of
    # observed highs landed in the 00:00 hour against 0.6% at 02:00, which
    # is stations whose warm hours straddle their own settlement boundary.
    # A decay curve drawn from the window edges would have said the
    # opposite, which is the reason to take the shape from the data.
    assert _weight("2026-08-11T16:00:00Z") > _weight("2026-08-11T18:00:00Z")


def test_standing_blocker_is_written_once_and_on_every_change() -> None:
    """A blocker that has not changed is not a new fact.

    Every swept scope used to append a cycle, a decision and a version
    vector whatever it concluded: 29,566 of 29,910 cycles were empty shells
    saying nothing had happened again, and that amplification -- not the
    quote snapshots -- is what drove the store into its size ceiling.
    """

    from bayes_basket_target.production_worker import (
        TargetProductionWorker as Worker,
    )

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE auto_trade_decisions("
        "station_id TEXT, event_date TEXT, decided_state TEXT,"
        "reason_codes_json TEXT, created_at TEXT)"
    )
    worker = object.__new__(Worker)
    worker.commit_conn = conn

    def _standing(codes):
        return worker._standing_blocker_matches(
            station_id="ZUUU", event_date="2026-08-12", reason_codes=codes,
        )

    def _record(state, codes, at):
        conn.execute(
            "INSERT INTO auto_trade_decisions VALUES(?,?,?,?,?)",
            ("ZUUU", "2026-08-12", state, json.dumps(codes), at),
        )

    # Nothing recorded yet: the first occurrence always writes.
    assert _standing(["quote_expired"]) is None

    _record("blocked", ["quote_expired"], "2026-08-12T06:00:00Z")
    # The same blocker standing is suppressed, and says since when.
    assert _standing(["quote_expired"]) == "2026-08-12T06:00:00Z"
    # A different blocker is a transition and must be written.
    assert _standing(["db_locked"]) is None
    # So is the same set in a different order -- reason codes are ordered,
    # and a reordering means a different compile path reached it.
    _record("blocked", ["a", "b"], "2026-08-12T06:01:00Z")
    assert _standing(["a", "b"]) == "2026-08-12T06:01:00Z"
    assert _standing(["b", "a"]) is None

    # A blocker that lifted and returned is a transition both ways: the
    # latest decision is what is compared against, not any earlier one.
    _record("prepared", [], "2026-08-12T06:02:00Z")
    assert _standing(["a", "b"]) is None

    # An unreadable ledger writes rather than suppresses: losing a
    # transition costs the audit trail, an extra row costs a row.
    broken = object.__new__(Worker)
    broken.commit_conn = sqlite3.connect(":memory:")
    assert broken._standing_blocker_matches(
        station_id="ZUUU", event_date="2026-08-12",
        reason_codes=["quote_expired"],
    ) is None


def test_sweep_merges_one_event_day_and_refuses_to_mix_days() -> None:
    """One cycle ingests every open scope of the day, not one of them.

    Serving a single scope per cycle meant a full pass over 33 stations took
    five to eight minutes against a sixty-second quote lease, so a book had
    expired several times over before its turn came round -- which is where
    the recurring ``production_quote_expired`` came from.
    """

    from bayes_basket_target.production_worker import (
        _manifest_station_allowlist,
        _merge_scope_manifests,
    )

    def _scope_manifest(station, tokens, event_date="2026-08-12"):
        return {
            "scope_key": f"{station}|{event_date}",
            "event_date": event_date,
            "event_day_state": "open",
            "target_timezone": "Asia/Shanghai",
            "station_scope": {
                "mode": "allowlist", "station_ids": [station],
            },
            "quotes_by_token": {
                token: {"best_ask": 0.1} for token in tokens
            },
            "quote_side_state_by_token": {
                token: "two_sided" for token in tokens
            },
            "market_registry_scope": {
                "event_date": event_date,
                "markets_by_token": {
                    token: {"outcome": token} for token in tokens
                },
            },
        }

    merged = _merge_scope_manifests([
        _scope_manifest("ZUUU", ["t1", "t2"]),
        _scope_manifest("ZUCK", ["t3"]),
    ])
    # Union of stations, books and registries; nothing recomputed.
    assert _manifest_station_allowlist(merged) == ("ZUCK", "ZUUU")
    assert set(merged["quotes_by_token"]) == {"t1", "t2", "t3"}
    assert set(
        merged["market_registry_scope"]["markets_by_token"]
    ) == {"t1", "t2", "t3"}
    assert merged["subscribed_token_count"] == 3
    # The merged document is its own thing, and names what went into it
    # rather than wearing one member's identity.
    assert merged["scope_key"] == "SWEEP|2026-08-12"
    assert merged["swept_scope_keys"] == [
        "ZUCK|2026-08-12", "ZUUU|2026-08-12",
    ]
    # A single-station sweep is passed through untouched, so the common
    # case keeps its original identity and hash.
    single = _scope_manifest("ZUUU", ["t1"])
    assert _merge_scope_manifests([single]) == single

    # Downstream reads one event_date off the merged document, so a mixed
    # set would silently attribute one day's books to another.
    with pytest.raises(
        ContractError, match="resident_target_ws_sweep_scope_not_one_day"
    ):
        _merge_scope_manifests([
            _scope_manifest("ZUUU", ["t1"]),
            _scope_manifest("ZUCK", ["t2"], event_date="2026-08-13"),
        ])
    with pytest.raises(
        ContractError, match="resident_target_ws_sweep_manifest_empty"
    ):
        _merge_scope_manifests([])


def _forward_scope(station: str, observed_at: str) -> dict:
    return {
        "scope_key": f"{station}|2026-08-17",
        "station_id": station,
        "event_date": "2026-08-17",
        "event_day_state": "not_started",
        "probability_stage": "P0",
        "probability_evidence_fresh": True,
        "scope_blocker": "",
        "manifest": {"connection_observed_at": observed_at},
    }


def test_forward_scope_age_is_the_quantity_the_compile_rejects_on() -> None:
    """Measure the book's age the way ``compile_p0_preselection`` does.

    That compile raises ``p0_preselection_manifest_stale`` when the scope
    manifest's ``connection_observed_at`` is further from the evaluation
    instant than ``max_ws_snapshot_age_sec``.  The sweep re-reads on the same
    quantity, so it refreshes before the compile can refuse rather than after.
    """

    now = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

    fresh = TargetProductionWorker._ws_scope_manifest_age_sec(
        _forward_scope("ZUUU", "2026-08-16T11:59:55.000Z"), now=now
    )
    aged = TargetProductionWorker._ws_scope_manifest_age_sec(
        _forward_scope("ZUUU", "2026-08-16T11:57:00.000Z"), now=now
    )

    assert fresh == 5.0
    assert aged == 180.0


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"connection_observed_at": ""},
        {"connection_observed_at": "not-a-timestamp"},
        # No zone is not "assume UTC": guessing here would silently pass a
        # book that is hours old.
        {"connection_observed_at": "2026-08-16T11:59:00"},
    ],
)
def test_forward_scope_without_a_readable_clock_is_maximally_stale(
    manifest,
) -> None:
    scope = {"manifest": manifest}

    age = TargetProductionWorker._ws_scope_manifest_age_sec(
        scope, now=datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    )

    # Infinity forces a re-read rather than a compile against a book whose
    # age cannot be established.
    assert age == float("inf")


def test_forward_sweep_order_covers_every_scope_exactly_once() -> None:
    keys = [f"S{index:02d}" for index in range(43)]

    rotated = TargetProductionWorker._forward_preselection_order(
        keys, resume_at="S20"
    )

    assert rotated[0] == "S20"
    assert rotated[-1] == "S19"
    assert sorted(rotated) == sorted(keys)
    assert len(set(rotated)) == len(keys)


def test_forward_sweep_order_ignores_a_cursor_that_no_longer_exists() -> None:
    # A scope that closed, or a cursor carried across a manifest change, must
    # not drop the pass or reorder it into something unrecognisable.
    keys = ["CYYZ|d", "KORD|d", "ZUUU|d"]

    assert TargetProductionWorker._forward_preselection_order(
        keys, resume_at="RETIRED|d"
    ) == keys
    assert TargetProductionWorker._forward_preselection_order(
        keys, resume_at=""
    ) == keys


def test_a_systematically_truncated_sweep_still_reaches_every_scope() -> None:
    """The property the rotation exists for.

    Measured 2026-08-16: the pass reached scope 20 of 43 before every
    remaining scope failed, and it restarted at the same head of the
    alphabet every cycle -- so KORD, KSEA and ZUUU held no forward book at
    all, and a forward quote that is missed cannot be fetched again.  With
    the cursor, the same truncation serves everyone within a few cycles.
    """

    keys = [f"S{index:02d}" for index in range(43)]
    cursor = ""
    served: set[str] = set()
    cycles = 0

    while len(served) < len(keys):
        cycles += 1
        assert cycles <= 10, "rotation failed to converge"
        order = TargetProductionWorker._forward_preselection_order(
            keys, resume_at=cursor
        )
        for position, key in enumerate(order):
            cursor = order[(position + 1) % len(order)]
            served.add(key)
            if position + 1 >= 20:  # the measured truncation point
                break

    assert cycles == 3
    assert served == set(keys)


def test_forward_scope_skip_matches_the_lane_it_guards() -> None:
    fresh = _forward_scope("ZUUU", "2026-08-16T12:00:00.000Z")

    assert TargetProductionWorker._forward_preselection_scope_skipped(
        fresh
    ) is False
    # Open days are the resident decision path's, not this lane's.
    assert TargetProductionWorker._forward_preselection_scope_skipped(
        {**fresh, "event_day_state": "open"}
    ) is True
    assert TargetProductionWorker._forward_preselection_scope_skipped(
        {**fresh, "probability_evidence_fresh": False}
    ) is True
    assert TargetProductionWorker._forward_preselection_scope_skipped(
        {**fresh, "scope_blocker": "resident_target_ws_future_scope_rootless"}
    ) is True
    # A scope that vanished from a refreshed catalog resolves to None, and
    # must be skipped rather than dereferenced.
    assert TargetProductionWorker._forward_preselection_scope_skipped(
        None
    ) is True


def _disarming_guard_conn() -> sqlite3.Connection:
    """A prepared candidate whose station-day later refused a scope.

    Blocked decisions write training facts too, so the day's most recent fact
    is almost never the prepared cycle's own.
    """

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT,plan_id TEXT,state TEXT,station_id TEXT,"
        "event_date TEXT,created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE auto_trade_decisions(cycle_id TEXT,decided_state TEXT)"
    )
    conn.execute(
        "CREATE TABLE basket_policy_training_fact("
        "fact_id TEXT,cycle_id TEXT,decision_id TEXT,"
        "decision_scope_key TEXT,station_id TEXT,event_date TEXT,"
        "decision_at_utc TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?,?)",
        [
            (
                "live-cycle", "live-plan", "prepared", "ZUUU",
                "2026-07-24", "2026-07-24T00:00:00Z",
            ),
            (
                "blocked-cycle", "blocked-plan", "prepared", "ZUUU",
                "2026-07-24", "2026-07-24T01:00:00Z",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?)",
        ("blocked-cycle", "blocked"),
    )
    conn.executemany(
        "INSERT INTO basket_policy_training_fact VALUES(?,?,?,?,?,?,?)",
        [
            (
                "fact-live", "live-cycle", "decision-live", "scope-A",
                "ZUUU", "2026-07-24", "2026-07-24T00:00:00Z",
            ),
            (
                "fact-blocked", "blocked-cycle", "decision-blocked", "scope-B",
                "ZUUU", "2026-07-24", "2026-07-24T01:00:00Z",
            ),
        ],
    )
    return conn


def test_later_blocked_fact_does_not_disarm_supersession(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arm reads the retiring cycle's fact, not the station-day's latest."""

    worker = _worker(tmp_path, _disarming_guard_conn())
    try:
        monkeypatch.setattr(
            production_worker_module,
            "delta_v2_decision_scope_key",
            lambda **_: "scope-C",
        )
        result = worker._scope_cycle_guard(
            station_id="ZUUU",
            event_date="2026-07-24",
            candidate={"production_market_snapshot": {}},
        )
    finally:
        worker.close()

    assert result is not None
    assert result["state"] == "supersede_existing_prepared_candidate"
    assert result["writes_target_ledger"] is True
    assert result["cycle_id"] == "live-cycle"
    assert result["decision_id"] == "decision-live"
    assert result["training_fact_id"] == "fact-live"
    assert result["old_decision_scope_key"] == "scope-A"
    assert result["new_decision_scope_key"] == "scope-C"


def test_unchanged_retiring_scope_retains_instead_of_superseding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to supersede: asking the ledger would fail the whole commit."""

    worker = _worker(tmp_path, _disarming_guard_conn())
    try:
        monkeypatch.setattr(
            production_worker_module,
            "delta_v2_decision_scope_key",
            lambda **_: "scope-A",
        )
        result = worker._scope_cycle_guard(
            station_id="ZUUU",
            event_date="2026-07-24",
            candidate={"production_market_snapshot": {}},
        )
    finally:
        worker.close()

    assert result is not None
    assert result["state"] == "retained_existing_prepared_candidate"
    assert result["writes_target_ledger"] is False
