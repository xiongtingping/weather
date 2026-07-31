from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import bayes_basket_target.production_worker as production_worker_module
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
    METAR_TRAJECTORY_METHOD_VERSION,
    build_metar_trajectory_likelihood_artifact,
)
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
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.stages import select_current_stage
from bayes_basket_target.taf_revision import (
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
)
from scripts.initialize_bayes_basket_target_ssot import initialize


def _compiled_native_p0_source_packet(
    provider: str,
    *,
    packet_content_sha256: str | None = None,
) -> dict[str, object]:
    packet = {
        "station_id": "RKPK",
        "event_date": "2026-08-01",
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_probability_envelope": {
            "lineage": {
                "native_transport_provider": provider,
            },
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
            _compiled_native_p0_source_packet("OPEN_METEO"),
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
                _compiled_native_p0_source_packet("OPEN_METEO"),
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
        "method_version": METAR_TRAJECTORY_METHOD_VERSION,
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
    artifact = _bridged_taf_artifact(tmp_path)
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
        assert max_scopes == 45
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
        assert max_scopes == 45
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

    with pytest.raises(
        ContractError,
        match="latest_stage_stale_fail_closed",
    ):
        select_current_stage(envelopes, at_utc="2026-07-29T13:00:00Z")


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


def test_resident_consumer_capacity_matches_canonical_45station_two_day_scope() -> None:
    assert len(CANONICAL_TARGET_STATIONS) == 45
    assert len(set(CANONICAL_TARGET_STATIONS)) == 45
    assert MAX_RESIDENT_WS_SCOPES == 90


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
        assert len(worker.station_allowlist) == 45
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


def test_open_scope_round_robin_is_deterministic_and_bounded() -> None:
    worker = object.__new__(TargetProductionWorker)
    worker._resident_open_scope_cursor = 0
    scopes = [
        {"scope_key": "RKPK|2026-07-30", "station_id": "RKPK"},
        {"scope_key": "EGLC|2026-07-30", "station_id": "EGLC"},
        {"scope_key": "KATL|2026-07-30", "station_id": "KATL"},
    ]

    selected = [
        worker._select_resident_open_ws_scope(scopes)["station_id"]
        for _ in range(5)
    ]

    assert selected == ["EGLC", "KATL", "RKPK", "EGLC", "KATL"]


def test_open_scope_round_robin_rejects_more_than_45_open_stations() -> None:
    worker = object.__new__(TargetProductionWorker)
    worker._resident_open_scope_cursor = 0
    scopes = [
        {"scope_key": f"S{index:02d}|2026-07-30"}
        for index in range(46)
    ]

    with pytest.raises(
        ContractError,
        match="resident_target_ws_open_scope_station_cap_exceeded",
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


def test_run_p1_blocker_allows_closed_scope_truth_label_but_blocks_market(
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
    assert "resident_p1_blocked_before_current_scope_market" in (
        result["reason"]
    )
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
        "observation_id": "resident-observation",
        "station_id": "RKPK",
        "event_date_local": "2026-07-27",
        "reported_temperature_c": 29.0,
        "valid_at_utc": "2026-07-26T15:30:00Z",
        "canonical_committed_at_utc": "2026-07-26T15:31:00Z",
        "raw_sha256": "c" * 64,
        "source_lane": "awc",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    }]
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
                **_taf_live_compatibility_lineage(),
            },
    })
    materialize_probability_bridge(
        commit_conn,
        p0,
        created_at_utc=p0.decision_at_utc,
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
        "observation_id": "delayed-new-high",
        "station_id": "RKPK",
        "event_date_local": "2026-07-27",
        "reported_temperature_c": 29.0,
        "valid_at_utc": "2026-07-26T15:30:00Z",
        "canonical_committed_at_utc": "2026-07-26T15:31:00Z",
        "raw_sha256": "c" * 64,
        "source_lane": "awc",
        "report_type": "METAR",
        "evidence_role": "FORMAL",
        "qc_status": "ACCEPTED",
        "correction_suspected": False,
    }]
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
