from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from bayes_basket_target.contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.event_contracts import ForecastRevision
from bayes_basket_target.calibration import (
    bind_taf_artifact_to_live_nwp_compatibility,
)
from bayes_basket_target.materialization import materialize_p0m
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.native_ensemble_oos import (
    DEFAULT_MIN_HOLDOUT_DAYS,
    DEFAULT_MIN_TRAIN_DAYS,
    NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
    NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
    NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE,
    _historical_transport_scope_allowed,
    build_native_ensemble_oos,
    persist_native_ensemble_oos,
)
from bayes_basket_target.native_ensemble_history import (
    ingest_historical_archive,
)
from bayes_basket_target.native_ensemble_p0 import (
    compile_native_ensemble_p0,
    persist_native_ensemble_forward_prediction,
)
from bayes_basket_target.open_meteo_ensemble import ENSEMBLE_SYSTEMS
from bayes_basket_target.probability_packet import (
    build_probability_envelope,
)
from bayes_basket_target.p1_compiler import (
    compile_station_p1_packets,
    materialization_packet,
)
from bayes_basket_target.production_io import (
    assert_formal_probability_model_authority,
    target_atomic_facts_from_cycle,
)
from bayes_basket_target.regime_materialization import (
    build_regime_artifact,
)
from bayes_basket_target.ssot import (
    apply_production_ssot_schema,
    apply_target_ssot_schema,
)
from bayes_basket_target.taf_revision import (
    TAF_ARTIFACT_SCHEMA,
    TAF_CORRELATION_METHOD,
    TAF_METHOD_VERSION,
    TAF_NWP_CROSSFIT_ARTIFACT_KIND,
    TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION,
    TafRevisionArtifact,
    compile_taf_revision_proof,
)
from bayes_basket_target.taf_native_ensemble_oos import (
    EXACT_JOIN_SCHEMA,
    evaluate_taf_native_ensemble_oos,
)
from bayes_basket_target.native_ensemble_contract import (
    P0_PROBABILITY_PATHWAY,
    TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
)
from bayes_basket_target.native_ensemble_training_facts import (
    build_taf_native_exact_join,
    ingest_forward_taf_inbox,
    ingest_historical_taf_calibration,
)


SYSTEMS = {
    "ECMWF_IFS_TEST": ("ECMWF", 3),
    "ECMWF_AIFS_TEST": ("ECMWF", 3),
    "NOAA_GEFS_TEST": ("NOAA_GEFS", 3),
}


def test_official_oos_transport_cycle_lead_contract_is_exact() -> None:
    assert _historical_transport_scope_allowed(
        system_id="NOAA_GEFS_05",
        transport_provider_id="NOAA_GEFS_PDS_S3",
        lead_bucket="d2",
        run_cycle="18Z",
    )
    assert not _historical_transport_scope_allowed(
        system_id="NOAA_GEFS_05",
        transport_provider_id="OPEN_METEO",
        lead_bucket="d2",
        run_cycle="18Z",
    )
    assert _historical_transport_scope_allowed(
        system_id="ECMWF_IFS_ENS_OPEN_PF50",
        transport_provider_id="ECMWF_OPEN_DATA_S3",
        lead_bucket="d2",
        run_cycle="18Z",
    )
    assert not _historical_transport_scope_allowed(
        system_id="ECMWF_IFS_ENS_OPEN_PF50",
        transport_provider_id="ECMWF_OPEN_DATA_S3",
        lead_bucket="d2",
        run_cycle="00Z",
    )
    assert _historical_transport_scope_allowed(
        system_id="ECCC_GEM_GLOBAL_ENS",
        transport_provider_id="ECCC_DD_WEATHER",
        lead_bucket="d2",
        run_cycle="00Z",
    )
    assert not _historical_transport_scope_allowed(
        system_id="ECCC_GEM_GLOBAL_ENS",
        transport_provider_id="ECCC_DD_WEATHER",
        lead_bucket="d2",
        run_cycle="18Z",
    )
    assert _historical_transport_scope_allowed(
        system_id="ECMWF_AIFS_025",
        transport_provider_id="OPEN_METEO",
        lead_bucket="d0",
        run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
    )


def _native_p0_forward_packet(
    *,
    event_date: str,
    captured_at_utc: str,
) -> dict[str, object]:
    intervals = {
        "19°C or below": [None, 19.5],
        "20°C": [19.5, 20.5],
        "21°C": [20.5, 21.5],
        "22°C or higher": [21.5, None],
    }
    systems = []
    for system_id, (family_id, member_count) in SYSTEMS.items():
        systems.append({
            "ensemble_system_id": system_id,
            "independence_family_id": family_id,
            "member_count": member_count,
            "members": [
                {
                    "ensemble_system_id": system_id,
                    "member_id": (
                        "control"
                        if index == 0
                        else f"perturbed_{index:02d}"
                    ),
                    "member_role": (
                        "CONTROL" if index == 0 else "PERTURBED"
                    ),
                    "raw_tmax_c": (
                        20.0 + (-0.2, 0.0, 0.2)[index]
                    ),
                }
                for index in range(member_count)
            ],
        })
    stable = {
        "schema": "open_meteo_native_ensemble_forward_packet_v1",
        "station_id": "TEST",
        "event_date": event_date,
        "settlement_timezone": "Etc/UTC",
        "aggregator_snapshot_at_utc": captured_at_utc,
        "bucket_intervals": intervals,
        "systems": systems,
    }
    return {
        **stable,
        "snapshot_content_sha256": content_sha256(stable),
    }


def _historical_archive(*, revised: bool) -> dict[str, object]:
    system_id = "ECMWF_IFS_ENS_GLOBAL"
    config = ENSEMBLE_SYSTEMS[system_id]
    members = [
        {
            "member_id": (
                "control"
                if index == 0
                else f"perturbed_{index:02d}"
            ),
            "member_role": (
                "CONTROL" if index == 0 else "PERTURBED"
            ),
            "raw_tmax_c": (
                20.5
                if revised and index == 0
                else 20.0 + index / 10.0
            ),
        }
        for index in range(int(config["expected_member_count"]))
    ]
    member_identity = [
        {
            "member_id": row["member_id"],
            "member_role": row["member_role"],
            "raw_tmax_c": row["raw_tmax_c"],
        }
        for row in members
    ]
    intervals = {"20°C or higher": [19.5, None]}
    bucket_sha = content_sha256(intervals)
    system = {
        "ensemble_system_id": system_id,
        "independence_family_id": "ECMWF",
        "member_count": len(members),
        "members": members,
        "member_set_sha256": content_sha256(member_identity),
        "raw_bucket_probabilities": {"20°C or higher": 1.0},
    }
    capture_identity = {
        "station_id": "TEST",
        "event_date": "2026-06-01",
        "settlement_timezone": "Etc/UTC",
        "archive_valid_day_bucket": "d0",
        "systems": [system],
        "unavailable_products": [],
    }
    capture = {
        **capture_identity,
        "capture_content_sha256": content_sha256(capture_identity),
    }
    query_at = (
        "2026-07-02T00:00:00Z"
        if revised
        else "2026-07-01T00:00:00Z"
    )
    response_sha = ("b" if revised else "a") * 64
    identity = {
        "schema": "open_meteo_native_ensemble_historical_archive_v1",
        "station_id": "TEST",
        "event_dates": ["2026-06-01"],
        "event_date_count": 1,
        "successful_event_date_count": 1,
        "unavailable_event_date_count": 0,
        "settlement_timezone": "Etc/UTC",
        "latitude": 0.0,
        "longitude": 0.0,
        "archive_query_at_utc": query_at,
        "request_url": "https://ensemble-api.open-meteo.test",
        "response_sha256": response_sha,
        "bucket_contract_sha256": bucket_sha,
        "bucket_intervals": intervals,
        "captures": [capture],
        "archive_contract": {
            "documentation_url": "https://open-meteo.com/",
            "provider_describes_past_days_as_archived_forecasts": True,
            "native_run_init_known": False,
            "native_publication_at_known": False,
            "native_arrival_index_eligible": False,
            "training_only": True,
            "holdout_eligible": False,
            "formal_p0_eligible": False,
        },
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _insert_fixture(conn: sqlite3.Connection) -> None:
    apply_target_ssot_schema(conn)
    apply_production_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    station_identity = {
        "station_id": "TEST",
        "icao_code": "TEST",
        "city_label": "Test",
        "timezone_name": "America/New_York",
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "registry_version": "test-v1",
        "source_role": "TEST",
    }
    conn.execute(
        "INSERT INTO calibration_station_registry VALUES(?,?,?,?,?,?,?,?,?)",
        (
            *station_identity.values(),
            content_sha256(station_identity),
        ),
    )
    snapshot_sha = "1" * 64
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            snapshot_sha,
            "test",
            "shared_live_registry_export_only",
            "2026-05-01T00:00:00Z",
            "/test",
            1,
            "2026-05-01T00:00:01Z",
            "2" * 64,
        ),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            snapshot_sha,
            "test-weather",
            "2026-07-02",
            "20°C",
            "test",
            "TEST",
            "Etc/UTC",
            "C",
            "awc",
            1,
            "",
            "https://example.test",
            "3" * 64,
            "yes",
            "no",
            1,
            1,
            "{}",
            "2026-07-01T00:00:00Z",
        ),
    )
    for system_id, (family_id, member_count) in SYSTEMS.items():
        system_identity = {
            "ensemble_system_id": system_id,
            "canonical_name": system_id,
            "independence_family_id": family_id,
            "provider_id": family_id,
            "source_class": "NATIVE_ENSEMBLE_PROVIDER",
            "aggregator_or_republisher": 0,
            "expected_member_count": member_count,
            "member_identity_contract_version": "test",
            "publication_contract_version": "test",
            "valid_from_utc": "2026-01-01T00:00:00Z",
            "valid_until_utc": "9999-12-31T23:59:59Z",
        }
        conn.execute(
            "INSERT INTO calibration_native_ensemble_system "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *system_identity.values(),
                content_sha256(system_identity),
            ),
        )
    start = date(2026, 6, 1)
    for index in range(28):
        day = start + timedelta(days=index)
        day_text = day.isoformat()
        actual = float(20 + 2 * (index % 4))
        committed = datetime(
            day.year,
            day.month,
            day.day,
            tzinfo=timezone.utc,
        ) + timedelta(days=1, hours=3)
        truth_identity = {
            "station_id": "TEST",
            "local_date": day_text,
            "official_tmax_c": actual,
            "source_lane": "AWC",
            "settlement_definition": "official_daily_tmax_c",
            "revision_no": 0,
            "published_at_utc": committed.isoformat().replace(
                "+00:00", "Z"
            ),
            "received_at_utc": committed.isoformat().replace(
                "+00:00", "Z"
            ),
            "canonical_committed_at_utc": committed.isoformat().replace(
                "+00:00", "Z"
            ),
            "timestamps_complete": 1,
            "qc_status": "ACCEPTED",
            "source_role": "TEST",
            "source_payload_sha256": content_sha256(
                {"day": day_text}
            ),
            "observation_ids_json": "[]",
            "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
        }
        truth_sha = content_sha256(truth_identity)
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "truth-" + truth_sha[:24],
                *truth_identity.values(),
                truth_sha,
            ),
            )
        snapshot = datetime(
            day.year,
            day.month,
            day.day,
            tzinfo=timezone.utc,
        ) - timedelta(hours=12)
        snapshot_text = snapshot.isoformat().replace("+00:00", "Z")
        for system_index, system_id in enumerate(SYSTEMS):
            for member_index, offset in enumerate((-0.2, 0.0, 0.2)):
                member_identity = {
                    "ensemble_system_id": system_id,
                    "station_id": "TEST",
                    "target_local_date": day_text,
                    "run_init_utc": None,
                    "member_id": (
                        "control"
                        if member_index == 0
                        else f"perturbed_{member_index:02d}"
                    ),
                    "member_role": (
                        "CONTROL" if member_index == 0 else "PERTURBED"
                    ),
                    "published_at_utc": None,
                    "received_at_utc": snapshot_text,
                    "lead_bucket": "d1",
                    "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                    "raw_tmax_c": actual + offset + 0.05 * system_index,
                    "settlement_definition": "official_daily_tmax_c",
                    "extraction_version": "test",
                    "provider_id": system_id,
                    "transport_provider_id": "OPEN_METEO",
                    "native_timestamps_complete": 0,
                    "aggregator_snapshot_at_utc": snapshot_text,
                    "source_url": "https://example.test",
                    "raw_object_ref": "test",
                    "source_object_sha256": content_sha256({
                        "system": system_id,
                        "day": day_text,
                    }),
                    "qc_status": "ACCEPTED",
                    "authority_scope": "PRODUCTION_NO_LIVE_CANDIDATE",
                }
                member_sha = content_sha256(member_identity)
                conn.execute(
                    "INSERT INTO calibration_native_ensemble_member_run "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "member-" + member_sha[:24],
                        *member_identity.values(),
                        member_sha,
                    ),
                )
    conn.commit()


def test_historical_republisher_revision_freezes_first_snapshot() -> None:
    conn = sqlite3.connect(":memory:")
    apply_target_ssot_schema(conn)
    station_identity = {
        "station_id": "TEST",
        "icao_code": "TEST",
        "city_label": "Test",
        "timezone_name": "Etc/UTC",
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "registry_version": "test-v1",
        "source_role": "TEST",
    }
    conn.execute(
        "INSERT INTO calibration_station_registry "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (*station_identity.values(), content_sha256(station_identity)),
    )

    first = ingest_historical_archive(
        conn,
        _historical_archive(revised=False),
    )
    frozen_value = conn.execute(
        "SELECT raw_tmax_c FROM "
        "calibration_native_ensemble_historical_member_run "
        "WHERE station_id='TEST' AND member_id='control'"
    ).fetchone()[0]
    revised = ingest_historical_archive(
        conn,
        _historical_archive(revised=True),
    )

    assert first["counts"]["member_runs"] == 51
    assert first["revision_observation_count"] == 0
    assert revised["counts"] == {
        "systems": 0,
        "member_runs": 0,
        "member_buckets": 0,
    }
    assert revised["revision_observation_count"] == 1
    assert revised["first_observed_training_snapshot_frozen"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM "
        "calibration_native_ensemble_historical_member_run "
        "WHERE station_id='TEST'"
    ).fetchone()[0] == 51
    assert conn.execute(
        "SELECT raw_tmax_c FROM "
        "calibration_native_ensemble_historical_member_run "
        "WHERE station_id='TEST' AND member_id='control'"
    ).fetchone()[0] == frozen_value


def test_official_historical_taf_ingress_preserves_asof_authority() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    sample = {
        "station_id": "TEST",
        "event_date": "2026-06-01",
        "settlement_timezone": "Etc/UTC",
        "decision_anchor_utc": "2026-06-01T01:00:00Z",
        "taf": {
            "issue_time_utc": "2026-05-31T23:00:00Z",
            "available_time_utc": "2026-05-31T23:05:00Z",
            "tx_c": 20.0,
            "tx_valid_at_utc": "2026-06-01T12:00:00Z",
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
            "raw_taf_sha256": "a" * 64,
        },
        "receipts": {
            "taf_response_sha256": "b" * 64,
        },
    }
    identity = {
        "schema": "bayes_basket_awc_taf_calibration_dataset_v1",
        "provider": "NOAA Aviation Weather Center",
        "source_class": "official_date_addressed_historical_replay",
        "station_id": "TEST",
        "settlement_timezone": "Etc/UTC",
        "start_date": "2026-06-01",
        "end_date": "2026-06-01",
        "sample_count": 1,
        "samples": [sample],
        "excluded_sample_count": 0,
        "excluded_samples": [],
        "blockers": [],
        "resource_bounds": {},
        "boundary": {
            "native_local_receipt": False,
            "official_awc_database_availability_preserved": True,
            "historical_replay_only": True,
            "truth_days_closed": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    artifact = {
        **identity,
        "content_sha256": content_sha256(identity),
    }

    first = ingest_historical_taf_calibration(conn, artifact)
    replay = ingest_historical_taf_calibration(conn, artifact)

    assert first["wrote_count"] == 1
    assert replay["wrote_count"] == 0
    assert conn.execute(
        "SELECT source_class,authority_scope "
        "FROM calibration_historical_taf_capture"
    ).fetchone() == (
        "official_date_addressed_historical_replay",
        "CALIBRATION_TRAIN_ONLY",
    )


def test_strict_forward_oos_activates_and_compiles_native_p0() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    assert len(report["cross_candidates"]) == 1
    assert report["cross_candidates"][0]["state"] == (
        "PRODUCTION_NO_LIVE_OOS_PASS"
    )
    assert report["formal_p0_eligible"] is False
    assert report["formal_promotion_min_train_days"] == (
        DEFAULT_MIN_TRAIN_DAYS
    )
    assert report["formal_promotion_min_holdout_days"] == (
        DEFAULT_MIN_HOLDOUT_DAYS
    )
    persisted = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    assert persisted["counts"] == {
        "system_artifacts": 3,
        "selection_evidence": 1,
        "cross_artifacts": 1,
        "oos_predictions": 8,
        "activations": 4,
    }
    captures = []
    for index in range(20, 28):
        day = date(2026, 6, 1) + timedelta(days=index)
        decision = datetime(
            day.year,
            day.month,
            day.day,
            tzinfo=timezone.utc,
        ) - timedelta(hours=12)
        available = decision + timedelta(hours=6)
        revision = {
            "schema": "bayes_basket_forecast_revision_v1",
            "revision_id": f"taf-test-{day.isoformat()}",
            "station_id": "TEST",
            "event_date": day.isoformat(),
            "revision_kind": "TAF",
            "sequence": 1,
            "issued_at_utc": (
                available - timedelta(minutes=5)
            ).isoformat().replace("+00:00", "Z"),
            "received_at_utc": available.isoformat().replace(
                "+00:00", "Z"
            ),
            "valid_from_utc": available.isoformat().replace(
                "+00:00", "Z"
            ),
            "valid_until_utc": (
                available + timedelta(days=1)
            ).isoformat().replace("+00:00", "Z"),
            "source_object_sha256": content_sha256({
                "taf": day.isoformat()
            }),
            "supersedes_revision_id": None,
        }
        capture_identity = {
            "schema": "bayes_basket_target_forward_taf_capture_v1",
            "source_class": "official_forward_native_receipt",
            "station_id": "TEST",
            "event_date": day.isoformat(),
            "settlement_timezone": "Etc/UTC",
            "provider_available_at_utc": available.isoformat().replace(
                "+00:00", "Z"
            ),
            "forecast_revision": revision,
            "taf_fields": {
                "tx_c": float(20 + 2 * (index % 4)),
                "cloud_cover_fraction": 0.25,
                "precipitation_probability": 0.1,
                "wind_speed_mps": 4.0,
            },
        }
        captures.append({
            **capture_identity,
            "content_sha256": content_sha256(capture_identity),
        })
    taf_receipt = ingest_forward_taf_inbox(conn, {
        "schema": "bayes_basket_target_taf_revision_inbox_v1",
        "captures": captures,
    })
    assert taf_receipt["wrote_count"] == 8
    candidate = report["cross_candidates"][0]
    exact_join = build_taf_native_exact_join(
        conn,
        station_id="TEST",
        season_bin="JJA",
        lead_bucket="d1",
        run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        settlement_definition="official_daily_tmax_c",
        settlement_timezone="Etc/UTC",
        ensemble_system_ids=candidate["selected_system_ids"],
        independence_family_ids=candidate["independence_family_ids"],
        family_weight_caps=candidate["family_weight_caps"],
        bucket_intervals={
            "19°C or below": [None, 19.5],
            "20°C": [19.5, 20.5],
            "21°C": [20.5, 21.5],
            "22°C or higher": [21.5, None],
        },
    )
    assert exact_join["sample_count"] == 8
    assert all(
        row["native_p0"]["decision_at_utc"]
        < row["taf"]["available_at_utc"]
        < row["settlement_window"]["end_utc"]
        for row in exact_join["samples"]
    )
    intervals = {
        "19°C or below": [None, 19.5],
        "20°C": [19.5, 20.5],
        "21°C": [20.5, 21.5],
        "22°C or higher": [21.5, None],
    }
    systems = []
    for system_id, (family_id, member_count) in SYSTEMS.items():
        members = [
            {
                "ensemble_system_id": system_id,
                "member_id": (
                    "control"
                    if index == 0
                    else f"perturbed_{index:02d}"
                ),
                "member_role": (
                    "CONTROL" if index == 0 else "PERTURBED"
                ),
                "raw_tmax_c": 20.0 + (-0.2, 0.0, 0.2)[index],
            }
            for index in range(member_count)
        ]
        systems.append({
            "ensemble_system_id": system_id,
            "independence_family_id": family_id,
            "member_count": member_count,
            "members": members,
        })
    stable = {
        "schema": "open_meteo_native_ensemble_forward_packet_v1",
        "station_id": "TEST",
        "event_date": "2026-07-02",
        "settlement_timezone": "Etc/UTC",
        "aggregator_snapshot_at_utc": "2026-07-01T12:00:00Z",
        "bucket_intervals": intervals,
        "systems": systems,
    }
    forward = {
        **stable,
        "snapshot_content_sha256": content_sha256(stable),
    }
    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=forward,
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    forward_persisted = persist_native_ensemble_forward_prediction(
        conn,
        compiled,
    )
    assert forward_persisted["state"] == "COMMITTED"
    assert forward_persisted["wrote_count"] == 1
    assert persist_native_ensemble_forward_prediction(
        conn,
        compiled,
    )["state"] == "EXISTING_FIRST_FORWARD_PREDICTION"
    envelope = build_probability_envelope(compiled["packet"])
    assert envelope.stage == "P0"
    assert envelope.lineage["probability_pathway"] == (
        "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
    )
    assert envelope.lineage["native_authority_scope"] == (
        "PRODUCTION_NO_LIVE_ONLY"
    )
    assert len(json.loads(
        envelope.lineage["independence_family_ids_json"]
    )) == 2
    assert sum(envelope.probabilities.values()) == 1.0
    fingerprint_sha = envelope.lineage[
        "taf_nwp_live_compatibility_fingerprint_sha256"
    ]
    artifact_identity = {
        "schema": TAF_ARTIFACT_SCHEMA,
        "artifact_id": "taf-test-native-v1",
        "activation_id": "taf-test-native-activation-v1",
        "station_id": "TEST",
        "season_bin": "JJA",
        "lead_bucket": "d1",
        "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": "Etc/UTC",
        "method_version": TAF_METHOD_VERSION,
        "trained_through_utc": "2026-06-30T00:00:00Z",
        "sample_n": 40,
        "effective_sample_n": 30.0,
        "oos_metrics": {
            "brier_score": 0.12,
            "nll": 0.4,
            "crps": 0.6,
        },
        "tx_bias_c": 0.0,
        "taf_nwp_delta_coefficient": 0.6,
        "tx_residual_scale_c": 0.8,
        "physical_feature_effect_c": {
            "cloud_cover_fraction": -0.5,
            "precipitation_probability": -0.5,
            "wind_speed_normalized": 0.0,
        },
        "wind_speed_cap_mps": 20.0,
        "confirmation_tolerance_c": 0.75,
        "max_confirmation_strength": 0.25,
        "max_correction_strength": 0.20,
        "max_probability_mass_transfer": 0.10,
        "max_center_displacement_buckets": 1,
        "likelihood_ratio_floor": 0.25,
        "likelihood_ratio_ceiling": 4.0,
        "nwp_combination_artifact_sha256": "9" * 64,
        "nwp_combination_artifact_kind": (
            TAF_NWP_CROSSFIT_ARTIFACT_KIND
        ),
        "nwp_live_compatibility_contract_version": (
            TAF_NWP_LIVE_COMPATIBILITY_CONTRACT_VERSION
        ),
        "nwp_live_compatibility_fingerprint_sha256": fingerprint_sha,
        "nwp_live_compatibility_evidence_sha256": "8" * 64,
        "nwp_live_compatibility_validated": True,
        "oos_taf_nwp_residual_correlation": 0.25,
        "correlation_discount_method": TAF_CORRELATION_METHOD,
        "correlation_discount": 0.75,
        "joint_physical_oos_validated": True,
        "valid_from_utc": "2026-07-01T00:00:00Z",
        "valid_until_utc": "2026-08-01T00:00:00Z",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "activated": True,
    }
    taf_artifact = TafRevisionArtifact.from_mapping({
        **artifact_identity,
        "content_sha256": content_sha256(artifact_identity),
    })
    revision = ForecastRevision.from_mapping({
        "revision_id": "taf-test-1",
        "station_id": "TEST",
        "event_date": "2026-07-02",
        "revision_kind": "TAF",
        "sequence": 1,
        "issued_at_utc": "2026-07-01T12:10:00Z",
        "received_at_utc": "2026-07-01T12:20:00Z",
        "valid_from_utc": "2026-07-01T12:00:00Z",
        "valid_until_utc": "2026-07-02T18:00:00Z",
        "source_object_sha256": "7" * 64,
        "supersedes_revision_id": None,
    })
    proof = compile_taf_revision_proof(
        envelope,
        p0_root=envelope,
        forecast_revision=revision,
        artifact=taf_artifact,
        bucket_centers_c={
            "19°C or below": 19.0,
            "20°C": 20.0,
            "21°C": 21.0,
            "22°C or higher": 22.0,
        },
        taf_fields={
            "tx_c": 20.0,
            "cloud_cover_fraction": 0.2,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
        },
        decision_at_utc="2026-07-01T13:00:01Z",
    )
    p0m = materialize_p0m(
        envelope,
        p0_root=envelope,
        taf_revision_artifact=taf_artifact,
        taf_revision_proof=proof,
        decision_at_utc="2026-07-01T13:00:01Z",
        fresh_until_utc="2026-07-01T19:00:00Z",
        bridge_sha256=envelope.bridge_sha256,
    )
    assert p0m.stage == "P0-M"
    assert p0m.lineage["p0_root_checkpoint_id"] == envelope.checkpoint_id
    assert_formal_probability_model_authority(
        [envelope, p0m],
        probability=p0m,
    )
    registry_rows = [
        {
            "station_id": "TEST",
            "event_date": "2026-07-02",
            "settlement_timezone": "Etc/UTC",
            "settlement_unit": "C",
            "outcome": bucket,
            "registry_json": json.dumps({"upper": upper}),
        }
        for bucket, upper in (
            ("19°C or below", 19),
            ("20°C", 20),
            ("21°C", 21),
            ("22°C or higher", None),
        )
    ]
    p1_packets = compile_station_p1_packets(
        predecessor=p0m,
        prediction_root=p0m,
        observations=[{
            "observation_id": "metar-test-1",
            "station_id": "TEST",
            "event_date_local": "2026-07-02",
            "reported_temperature_c": 20.0,
            "valid_at_utc": "2026-07-02T00:10:00Z",
            "canonical_committed_at_utc": "2026-07-02T00:10:01Z",
            "raw_sha256": "6" * 64,
            "source_lane": "AWC",
            "report_type": "METAR",
            "evidence_role": "FORMAL",
            "qc_status": "ACCEPTED",
            "correction_suspected": False,
        }],
        registry_rows=registry_rows,
        compilation_at_utc="2026-07-02T00:10:02Z",
    )
    assert len(p1_packets) == 1
    p1 = build_probability_envelope(
        materialization_packet(p1_packets[0])
    )
    assert p1.stage == "P1"
    assert p1.lineage["p0_root_checkpoint_id"] == envelope.checkpoint_id
    assert p1.lineage["prediction_root_checkpoint_id"] == (
        p0m.checkpoint_id
    )
    assert_formal_probability_model_authority(
        [envelope, p0m, p1],
        probability=p1,
    )
    regime = build_regime_artifact(p1)
    assert regime.station_id == "TEST"
    facts = target_atomic_facts_from_cycle({
        "probability_envelope": p1.to_dict(),
    })
    assert facts["observation"]["observation_id"] == "metar-test-1"
    assert facts["bound"]["bound_id"] == p1.lineage["hard_bound_id"]

    older_packet = json.loads(json.dumps(compiled["packet"]))
    older_packet["decision_at_utc"] = "2026-07-01T12:59:59Z"
    older_packet["native_probability_envelope"][
        "decision_at_utc"
    ] = "2026-07-01T12:59:59Z"
    older_native = older_packet["native_probability_envelope"]
    older_native["posterior_sha256"] = content_sha256({
        "station_id": older_native["station_id"],
        "event_date": older_native["event_date"],
        "decision_at_utc": older_native["decision_at_utc"],
        "probabilities": older_native["probabilities"],
        "native_probability_evidence_sha256": older_native[
            "lineage"
        ]["native_probability_evidence_sha256"],
    })
    inbox_identity = {
        "schema": "bayes_basket_target_p0_inbox_v1",
        "generated_at_utc": "2026-07-01T13:00:00Z",
        "packets": [
            {
                "packet": older_packet,
                "packet_content_sha256": content_sha256(older_packet),
            },
            {
                "packet": compiled["packet"],
                "packet_content_sha256": compiled[
                    "packet_content_sha256"
                ],
            },
        ],
    }
    live_inbox = {
        **inbox_identity,
        "content_sha256": content_sha256(inbox_identity),
    }
    fingerprint = json.loads(
        envelope.lineage["taf_nwp_live_compatibility_json"]
    )
    samples = []
    for index in range(24):
        sample_day = date(2026, 7, 3) + timedelta(days=index)
        event_date = sample_day.isoformat()
        decision = datetime(
            sample_day.year,
            sample_day.month,
            sample_day.day,
            tzinfo=timezone.utc,
        )
        prediction_identity = {
            "station_id": "TEST",
            "event_date": event_date,
            "strict_oos": True,
            "sequence": index,
        }
        samples.append({
            "station_id": "TEST",
            "event_date": event_date,
            "settlement_timezone": "Etc/UTC",
            "calibration_scope": {
                "season_bin": "JJA",
                "lead_bucket": "d1",
                "run_cycle": "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                "settlement_definition": "official_daily_tmax_c",
            },
            "native_p0": {
                "probability_pathway": (
                    "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE"
                ),
                "prediction_oos": True,
                "trained_through_event_date": "2026-06-29",
                "decision_at_utc": decision.isoformat().replace(
                    "+00:00", "Z"
                ),
                "ensemble_system_ids": fingerprint[
                    "ensemble_system_ids"
                ],
                "independence_family_ids": fingerprint[
                    "independence_family_ids"
                ],
                "family_weight_caps": fingerprint[
                    "family_weight_caps"
                ],
                "probabilities": {
                    "19°C or below": 0.05,
                    "20°C": 0.10,
                    "21°C": 0.80,
                    "22°C or higher": 0.05,
                },
                "bucket_centers_c": {
                    "19°C or below": 19.0,
                    "20°C": 20.0,
                    "21°C": 21.0,
                    "22°C or higher": 22.0,
                },
                "prediction_content_sha256": content_sha256(
                    prediction_identity
                ),
            },
            "taf": {
                "available_at_utc": (
                    decision + timedelta(hours=1)
                ).isoformat().replace("+00:00", "Z"),
                "tx_c": 20.0,
                "cloud_cover_fraction": 0.0,
                "precipitation_probability": 0.0,
                "wind_speed_mps": 2.0,
            },
            "truth": {"tmax_c": 20.0},
            "settlement_window": {
                "end_utc": (
                    decision + timedelta(days=1)
                ).isoformat().replace("+00:00", "Z"),
            },
        })
    exact_identity = {
        "schema": EXACT_JOIN_SCHEMA,
        "source_class": "strict_oos_native_p0_taf_truth_exact_join",
        "nwp_crossfit_prediction_artifact_sha256": "9" * 64,
        "sample_count": len(samples),
        "samples": samples,
    }
    native_taf_report = evaluate_taf_native_ensemble_oos(
        exact_join={
            **exact_identity,
            "content_sha256": content_sha256(exact_identity),
        },
        live_p0_inbox=live_inbox,
        taf_artifact_raw=taf_artifact.to_dict(),
        station_id="TEST",
        season_bin="JJA",
        lead_bucket="d1",
        run_cycle="AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
    )
    assert native_taf_report["state"] == "PASS"
    assert native_taf_report["validated"] is True
    bound_taf = bind_taf_artifact_to_live_nwp_compatibility(
        taf_artifact=taf_artifact,
        live_fingerprint=native_taf_report["live_fingerprint"],
        transport_oos_evidence=native_taf_report["evidence"],
    )
    assert bound_taf.nwp_live_compatibility_fingerprint_sha256 == (
        fingerprint["fingerprint_sha256"]
    )


def test_no_live_bootstrap_threshold_is_explicitly_non_formal() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)

    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
        min_train_days=NO_LIVE_BOOTSTRAP_MIN_TRAIN_DAYS,
        min_holdout_days=NO_LIVE_BOOTSTRAP_MIN_HOLDOUT_DAYS,
    )

    assert report["threshold_profile"] == (
        NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
    )
    assert report["min_train_days"] == 2
    assert report["min_holdout_days"] == 1
    assert report["formal_promotion_min_train_days"] == 20
    assert report["formal_promotion_min_holdout_days"] == 8
    assert report["formal_p0_eligible"] is False
    assert all(
        candidate["authority_scope"] == "PRODUCTION_NO_LIVE_ONLY"
        and candidate["formal_p0_eligible"] is False
        and candidate["threshold_profile"]
        == NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
        for candidate in report["cross_candidates"]
    )
    persisted = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    assert persisted["counts"]["cross_artifacts"] == 1
    method_version = conn.execute(
        "SELECT method_version FROM "
        "calibration_cross_native_ensemble_selection_evidence"
    ).fetchone()[0]
    reliability = json.loads(conn.execute(
        "SELECT reliability_parameters_json FROM "
        "calibration_native_ensemble_system_artifact LIMIT 1"
    ).fetchone()[0])
    assert method_version.endswith(
        NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
    )
    assert reliability["oos_threshold_profile"] == (
        NO_LIVE_BOOTSTRAP_THRESHOLD_PROFILE
    )
    assert reliability["formal_promotion_min_train_days"] == 20
    assert reliability["formal_promotion_min_holdout_days"] == 8
    assert reliability["formal_p0_eligible"] is False


def test_native_p0_persistence_is_idempotent_and_compiles() -> None:
    conn = sqlite3.connect(":memory:")
    _insert_fixture(conn)
    report = build_native_ensemble_oos(
        conn,
        station_id="TEST",
        as_of_utc="2026-07-01T00:00:00Z",
    )
    first = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:00:00Z",
    )
    activation_count = conn.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation"
    ).fetchone()[0]
    second = persist_native_ensemble_oos(
        conn,
        report,
        activated_at_utc="2026-07-01T00:01:00Z",
    )
    assert first["counts"]["activations"] == 4
    assert second["counts"]["activations"] == 0
    assert second["receipts"][0]["reused_existing"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM calibration_native_ensemble_activation"
    ).fetchone()[0] == activation_count
    day = date(2026, 7, 2)
    forward_packet = _native_p0_forward_packet(
        event_date=day.isoformat(),
        captured_at_utc="2026-07-01T12:00:00Z",
    )

    compiled = compile_native_ensemble_p0(
        conn,
        forward_packet=forward_packet,
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    replayed = compile_native_ensemble_p0(
        conn,
        forward_packet=forward_packet,
        decision_at_utc="2026-07-01T14:00:00Z",
    )

    assert compiled["schema"] == (
        "calibrated_native_cross_ensemble_p0_packet_v1"
    )
    assert replayed["packet_content_sha256"] == (
        compiled["packet_content_sha256"]
    )

    with_extra = json.loads(json.dumps(forward_packet))
    with_extra["systems"].append({
        "ensemble_system_id": "EXTRA_UNCALIBRATED_ENS",
        "independence_family_id": "EXTRA_FAMILY",
        "member_count": 2,
        "members": [
            {
                "ensemble_system_id": "EXTRA_UNCALIBRATED_ENS",
                "member_id": "control",
                "member_role": "CONTROL",
                "raw_tmax_c": 20.0,
            },
            {
                "ensemble_system_id": "EXTRA_UNCALIBRATED_ENS",
                "member_id": "perturbed_01",
                "member_role": "PERTURBED",
                "raw_tmax_c": 21.0,
            },
        ],
    })
    extra_identity = {
        key: value
        for key, value in with_extra.items()
        if key != "snapshot_content_sha256"
    }
    with_extra["snapshot_content_sha256"] = content_sha256(
        extra_identity
    )
    compiled_with_extra = compile_native_ensemble_p0(
        conn,
        forward_packet=with_extra,
        decision_at_utc="2026-07-01T13:00:00Z",
    )
    extra_lineage = compiled_with_extra["packet"][
        "native_probability_envelope"
    ]["lineage"]
    assert json.loads(
        extra_lineage["excluded_observed_system_ids_json"]
    ) == ["EXTRA_UNCALIBRATED_ENS"]
    assert extra_lineage["excluded_observed_system_reason"] == (
        "NO_EXACT_OOS_ACTIVATION"
    )

    with pytest.raises(
        ContractError,
        match="settlement_day_already_closed",
    ):
        compile_native_ensemble_p0(
            conn,
            forward_packet=forward_packet,
            decision_at_utc="2026-07-03T00:00:00Z",
        )


def test_native_authority_p1_reaches_delta_decision_and_training_fact(
    tmp_path,
    monkeypatch,
) -> None:
    from tests import test_bayes_basket_target_production_chain_e2e as e2e

    original = e2e._inputs()
    envelopes, priors, artifacts, activations, market, cash_policy = original
    system_ids = ["ECMWF_IFS_TEST", "NOAA_GEFS_TEST"]
    family_ids = ["ECMWF", "NOAA_GEFS"]
    system_families = {
        "ECMWF_IFS_TEST": "ECMWF",
        "NOAA_GEFS_TEST": "NOAA_GEFS",
    }
    system_weights = {
        "ECMWF_IFS_TEST": 0.45,
        "NOAA_GEFS_TEST": 0.55,
    }
    family_caps = {"ECMWF": 0.45, "NOAA_GEFS": 1.0}
    system_artifacts = {
        "ECMWF_IFS_TEST": "1" * 64,
        "NOAA_GEFS_TEST": "2" * 64,
    }
    system_activations = {
        "ECMWF_IFS_TEST": "3" * 64,
        "NOAA_GEFS_TEST": "4" * 64,
    }
    fingerprint_identity = {
        "schema": TAF_NATIVE_ENSEMBLE_LIVE_FINGERPRINT_SCHEMA,
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "station_id": "ZUUU",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "NATIVE_TEST",
        "settlement_definition": "official_daily_tmax_c",
        "available_ensemble_system_set_hash": content_sha256(system_ids),
        "ensemble_system_ids": system_ids,
        "independence_family_ids": family_ids,
        "system_family_ids": system_families,
        "resolved_system_weights": system_weights,
        "resolved_family_weights": {
            "ECMWF": 0.45,
            "NOAA_GEFS": 0.55,
        },
        "family_weight_caps": family_caps,
        "cross_ensemble_artifact_sha256": "5" * 64,
        "cross_selection_evidence_sha256": "6" * 64,
        "cross_activation_sha256": "7" * 64,
        "system_calibration_artifacts": {
            system_id: {
                "artifact_content_sha256": system_artifacts[system_id],
                "activation_content_sha256": system_activations[system_id],
            }
            for system_id in system_ids
        },
    }
    fingerprint = {
        **fingerprint_identity,
        "fingerprint_sha256": content_sha256(fingerprint_identity),
    }
    p0_raw = envelopes[0].to_dict()
    p0_raw["lineage"] = {
        **p0_raw["lineage"],
        "probability_pathway": P0_PROBABILITY_PATHWAY,
        "p0_probability_pathway": P0_PROBABILITY_PATHWAY,
        "native_authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_definition": "official_daily_tmax_c",
        "season_bin": "summer",
        "lead_bucket": "d1",
        "run_cycle": "NATIVE_TEST",
        "ensemble_system_ids_json": json.dumps(
            system_ids, separators=(",", ":")
        ),
        "independence_family_ids_json": json.dumps(
            family_ids, separators=(",", ":")
        ),
        "system_family_ids_json": json.dumps(
            system_families, sort_keys=True, separators=(",", ":")
        ),
        "system_weights_json": json.dumps(
            system_weights, sort_keys=True, separators=(",", ":")
        ),
        "family_weight_caps_json": json.dumps(
            family_caps, sort_keys=True, separators=(",", ":")
        ),
        "system_artifact_shas_json": json.dumps(
            system_artifacts, sort_keys=True, separators=(",", ":")
        ),
        "system_activation_shas_json": json.dumps(
            system_activations, sort_keys=True, separators=(",", ":")
        ),
        "native_cross_ensemble_artifact_sha256": "5" * 64,
        "native_selection_evidence_sha256": "6" * 64,
        "native_cross_activation_sha256": "7" * 64,
        "native_probability_evidence_sha256": "8" * 64,
        "taf_nwp_live_compatibility_fingerprint_sha256": (
            fingerprint["fingerprint_sha256"]
        ),
        "taf_nwp_live_compatibility_json": json.dumps(
            fingerprint, sort_keys=True, separators=(",", ":")
        ),
    }
    native_p0 = ProbabilityEnvelope.from_mapping(p0_raw)
    p0m_raw = envelopes[1].to_dict()
    p0m_raw["lineage"] = {
        **p0m_raw["lineage"],
        "p0_root_checkpoint_id": native_p0.checkpoint_id,
        "p0_root_content_sha256": native_p0.content_sha256,
    }
    native_p0m = ProbabilityEnvelope.from_mapping(p0m_raw)
    p1_raw = envelopes[2].to_dict()
    p1_raw["lineage"] = {
        **p1_raw["lineage"],
        "p0_root_checkpoint_id": native_p0.checkpoint_id,
        "p0_root_content_sha256": native_p0.content_sha256,
        "prediction_root_checkpoint_id": native_p0m.checkpoint_id,
        "prediction_root_content_sha256": native_p0m.content_sha256,
    }
    native_p1 = ProbabilityEnvelope.from_mapping(p1_raw)
    assert_formal_probability_model_authority(
        [native_p0, native_p0m, native_p1],
        probability=native_p1,
    )
    monkeypatch.setattr(
        e2e,
        "_inputs",
        lambda: (
            [native_p0, native_p0m, native_p1],
            priors,
            artifacts,
            activations,
            market,
            cash_policy,
        ),
    )
    e2e.test_p1_to_plan_to_multileg_atomic_ledger_is_deterministic_and_no_live(
        tmp_path
    )
