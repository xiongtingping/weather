from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.materialize_dwd_icon_global_eps_candidate import build_manifest


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "bayes_basket_target" / "open_meteo_ensemble.py"


def _db(path: Path, *, member_count: int = 40) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE calibration_native_ensemble_historical_member_bucket (
          ensemble_system_id TEXT, station_id TEXT, target_local_date TEXT,
          archive_lead_bucket TEXT, archive_query_at_utc TEXT,
          member_set_sha256 TEXT, member_count INTEGER,
          bucket_contract_sha256 TEXT, historical_member_bucket_id TEXT,
          content_sha256 TEXT, source_role TEXT, authority_scope TEXT,
          holdout_eligible INTEGER
        );
        CREATE TABLE calibration_native_ensemble_member_bucket (
          ensemble_system_id TEXT, station_id TEXT, target_local_date TEXT,
          aggregator_snapshot_at_utc TEXT, member_set_sha256 TEXT,
          member_count INTEGER, bucket_contract_sha256 TEXT,
          member_bucket_id TEXT, content_sha256 TEXT, source_role TEXT,
          authority_scope TEXT
        );
        CREATE TABLE calibration_native_ensemble_member_run (
          ensemble_system_id TEXT, native_timestamps_complete INTEGER,
          run_init_utc TEXT, published_at_utc TEXT, transport_provider_id TEXT
        );
        CREATE TABLE calibration_cross_native_ensemble_oos_evaluation (
          ensemble_system_ids_json TEXT, oos_state TEXT
        );
        CREATE TABLE calibration_native_ensemble_activation (
          artifact_id TEXT, artifact_kind TEXT
        );
        CREATE TABLE calibration_cross_native_ensemble_artifact (
          cross_ensemble_artifact_id TEXT, ensemble_system_ids_json TEXT
        );
        CREATE TABLE calibration_native_ensemble_system_artifact (
          ensemble_system_id TEXT
        );
        """
    )
    values = []
    for index, system_id in enumerate((
        "DWD_ICON_EPS_SEAMLESS",
        "DWD_ICON_GLOBAL_EPS",
    )):
        common = (
            system_id, "TEST", "2026-08-02", "d2",
            "2026-07-31T00:00:00Z", f"{index:x}" * 64, member_count,
            "b" * 64, f"archive-{index}", f"c{index}" * 32,
        )
        values.append((*common, "aggregator_republisher", "CALIBRATION_TRAIN_ONLY", 0))
        conn.execute(
            "INSERT INTO calibration_native_ensemble_member_bucket VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                system_id, "TEST", "2026-08-02", "2026-07-31T01:00:00Z",
                f"{index:x}" * 64, member_count, "b" * 64,
                f"forward-{index}", f"d{index}" * 32,
                "aggregator_republisher", "PRODUCTION_NO_LIVE_CANDIDATE",
            ),
        )
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_historical_member_bucket VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    conn.commit()
    conn.close()


def _config(path: Path) -> None:
    path.write_text(json.dumps({
        "global_aggregator_capture_models": {"ICON": {
            "model_parameter": "icon_global_eps",
            "source_class": "aggregator_republisher",
        }},
        "global_ensemble_aggregator_capture_models": {
            system_id: {
                "system_id": system_id,
                "transport_provider_id": "OPEN_METEO",
                "source_class": "aggregator_republisher",
                "geographic_scope": "global",
                "independence_family_id": "DWD_ICON_EPS",
                "expected_member_count": 40,
                "formal_p0_eligible": False,
                "native_arrival_index_eligible": False,
            }
            for system_id in (
                "DWD_ICON_EPS_SEAMLESS",
                "DWD_ICON_GLOBAL_EPS",
            )
        },
        "station_regional_pool": {"TEST": []},
    }), encoding="utf-8")


def _add_republisher_bucket_tables(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE calibration_republisher_ensemble_historical_member_bucket (
          ensemble_system_id TEXT, station_id TEXT, target_local_date TEXT,
          archive_lead_bucket TEXT, archive_query_at_utc TEXT,
          member_set_sha256 TEXT, member_count INTEGER,
          bucket_contract_sha256 TEXT, historical_member_bucket_id TEXT,
          content_sha256 TEXT, source_role TEXT, authority_scope TEXT,
          holdout_eligible INTEGER
        );
        CREATE TABLE calibration_republisher_ensemble_member_bucket (
          ensemble_system_id TEXT, station_id TEXT, target_local_date TEXT,
          aggregator_snapshot_at_utc TEXT, member_set_sha256 TEXT,
          member_count INTEGER, bucket_contract_sha256 TEXT,
          republisher_member_bucket_id TEXT, content_sha256 TEXT,
          source_role TEXT, authority_scope TEXT
        );
        """
    )
    for index, system_id in enumerate((
        "DWD_ICON_EPS_SEAMLESS",
        "DWD_ICON_GLOBAL_EPS",
    )):
        conn.execute(
            "INSERT INTO calibration_republisher_ensemble_historical_member_bucket "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                system_id, "REPUBLISHER", "2026-08-02", "d0",
                "2026-08-01T00:00:00Z", f"{index:x}" * 64, 40,
                "b" * 64, f"republisher-archive-{index}", f"e{index}" * 32,
                "aggregator_republisher", "CALIBRATION_TRAIN_ONLY", 0,
            ),
        )
        conn.execute(
            "INSERT INTO calibration_republisher_ensemble_member_bucket "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                system_id, "REPUBLISHER", "2026-08-02",
                "2026-08-01T01:00:00Z", f"{index:x}" * 64, 40,
                "b" * 64, f"republisher-forward-{index}", f"f{index}" * 32,
                "aggregator_republisher", "PRODUCTION_NO_LIVE_CANDIDATE",
            ),
        )
    conn.commit()
    conn.close()


def _official_readiness(path: Path) -> None:
    station_cohort = {
        "content_sha256_valid": True,
        "record_shape_valid": True,
        "station_member_pairs_unique": True,
        "per_station_member_set_complete": True,
        "actual_station_count": 45,
        "actual_member_record_count": 1800,
        "native_arrival_index_eligible": False,
        "formal_p0_eligible": False,
    }
    document = {
        "schema": "dwd_icon_global_eps_official_readiness_v1",
        "state": "STATION_MEMBER_COHORT_READY_STRICT_OOS_PENDING",
        "official_eps_products": {
            "DWD_ICON_GLOBAL_EPS": {
                "source_role": "official_native",
                "native_adapter_execution_ready": True,
                "cohorts": [{
                    "ready": True,
                    "station_member_cohort_ready": True,
                    "official_raw_object_count": 1,
                    "unique_member_count": 40,
                    "run_initialization_utc": "2026-08-01T00:00:00Z",
                    "valid_time_utc": "2026-08-01T12:00:00Z",
                    "station_member_cohort": station_cohort,
                }],
            },
        },
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib
    document["content_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(document), encoding="utf-8")


def test_icon_global_eps_manifest_is_global_republisher_candidate_only(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ssot.db"
    config = tmp_path / "pool.json"
    _db(db)
    _config(config)

    manifest = build_manifest(config, CATALOG, db)

    assert manifest["readiness"] == "candidate_only"
    assert manifest["entrypoint_trace"]["network_requests"] == 0
    assert manifest["entrypoint_trace"]["sqlite_writes"] == 0
    assert manifest["model_inventory"]["dwd_icon_global_eps_scope"] == "global"
    assert manifest["model_inventory"]["icon_eu_and_icon_d2_scope"].startswith("regional")
    assert {row["ensemble_system_id"] for row in manifest["models"]} == {
        "DWD_ICON_EPS_SEAMLESS", "DWD_ICON_GLOBAL_EPS",
    }
    for row in manifest["models"]:
        assert row["source_class"] == "aggregator_republisher"
        assert row["formal_p0_eligible"] is False
        assert row["native_arrival_index_eligible"] is False
        assert row["hard_no_eligible"] is False
        assert row["archive_training_candidate"]["summary"]["cohort_count"] == 1
        assert row["forward_candidate"]["summary"]["cohort_count"] == 1


def test_icon_global_eps_manifest_rejects_incomplete_member_set(tmp_path: Path) -> None:
    db = tmp_path / "ssot.db"
    config = tmp_path / "pool.json"
    _db(db, member_count=39)
    _config(config)

    with pytest.raises(RuntimeError, match="candidate_member_completeness_failed"):
        build_manifest(config, CATALOG, db)


def test_icon_global_eps_manifest_reads_new_republisher_bucket_identity(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ssot.db"
    config = tmp_path / "pool.json"
    _db(db)
    _add_republisher_bucket_tables(db)
    _config(config)

    manifest = build_manifest(config, CATALOG, db)

    for model in manifest["models"]:
        assert model["archive_training_candidate"]["cohorts"] == [{
            "station_id": "REPUBLISHER",
            "target_local_date": "2026-08-02",
            "archive_lead_bucket": "d0",
            "archive_query_at_utc": "2026-08-01T00:00:00Z",
            "member_set_sha256": (
                "0" if model["ensemble_system_id"] == "DWD_ICON_EPS_SEAMLESS" else "1"
            ) * 64,
            "member_count": 40,
            "bucket_contract_sha256": "b" * 64,
            "materialized_bucket_id": (
                "republisher-archive-0"
                if model["ensemble_system_id"] == "DWD_ICON_EPS_SEAMLESS"
                else "republisher-archive-1"
            ),
            "content_sha256": (
                "e0" if model["ensemble_system_id"] == "DWD_ICON_EPS_SEAMLESS" else "e1"
            ) * 32,
        }]
        assert model["forward_candidate"]["cohorts"][0][
            "materialized_bucket_id"
        ].startswith("republisher-forward-")


def test_verified_native_sidecar_corrects_status_without_reclassifying_republisher(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ssot.db"
    config = tmp_path / "pool.json"
    readiness = tmp_path / "official-readiness.json"
    _db(db)
    _config(config)
    _official_readiness(readiness)

    manifest = build_manifest(
        config, CATALOG, db, official_readiness_path=readiness
    )
    global_eps = next(
        row for row in manifest["models"]
        if row["ensemble_system_id"] == "DWD_ICON_GLOBAL_EPS"
    )
    seamless = next(
        row for row in manifest["models"]
        if row["ensemble_system_id"] == "DWD_ICON_EPS_SEAMLESS"
    )
    evidence = global_eps["official_native_evidence"]
    assert evidence["official_native_cohort_observed"] is True
    assert evidence["native_arrival_index_eligible"] is False
    assert evidence["formal_p0_eligible"] is False
    assert global_eps["source_class"] == "aggregator_republisher"
    assert global_eps["formal_p0_eligible"] is False
    assert global_eps["native_arrival_index_eligible"] is False
    assert "official_native_arrival_missing" not in global_eps["fail_closed_reasons"]
    assert "own_strict_oos_missing" in global_eps["fail_closed_reasons"]
    assert seamless["official_native_evidence"]["official_native_cohort_observed"] is False


def test_native_sidecar_rejects_a_promotion_or_incomplete_station_cohort(tmp_path: Path) -> None:
    db = tmp_path / "ssot.db"
    config = tmp_path / "pool.json"
    readiness = tmp_path / "official-readiness.json"
    _db(db)
    _config(config)
    _official_readiness(readiness)
    document = json.loads(readiness.read_text(encoding="utf-8"))
    document["official_eps_products"]["DWD_ICON_GLOBAL_EPS"]["cohorts"][0]["station_member_cohort"]["formal_p0_eligible"] = True
    identity = {key: value for key, value in document.items() if key != "content_sha256"}
    import hashlib
    document["content_sha256"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    readiness.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeError, match="candidate_official_readiness_station_cohort_invalid"):
        build_manifest(config, CATALOG, db, official_readiness_path=readiness)


def test_native_sidecar_hash_mismatch_cannot_change_republisher_status(tmp_path: Path) -> None:
    db = tmp_path / "ssot.db"
    config = tmp_path / "pool.json"
    readiness = tmp_path / "official-readiness.json"
    _db(db)
    _config(config)
    _official_readiness(readiness)
    document = json.loads(readiness.read_text(encoding="utf-8"))
    document["content_sha256"] = "0" * 64
    readiness.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeError, match="candidate_official_readiness_invalid"):
        build_manifest(config, CATALOG, db, official_readiness_path=readiness)
