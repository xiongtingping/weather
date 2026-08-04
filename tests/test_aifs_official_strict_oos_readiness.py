from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.audit_aifs_official_strict_oos_readiness as readiness
import scripts.audit_aifs_model_ingress as ingress
from bayes_basket_target.aifs_deterministic_authority import (
    build_aifs_deterministic_promotion_receipt,
    issue_aifs_deterministic_provider_authority,
)
from bayes_basket_target.aifs_deterministic_oos import (
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    build_aifs_deterministic_strict_oos_candidate,
    build_aifs_deterministic_training_facts,
)
from bayes_basket_target.model_arrival import validate_model_arrival_capture
from tests.test_aifs_deterministic_oos import _inputs


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_aifs_readiness_and_ingress_default_to_45_station_decode() -> None:
    expected = "aifs_official_45station_native_decode_lifecycle_v2_20260801.json"
    assert readiness.DEFAULT_DECODE_EVIDENCE.name == expected
    assert ingress.DEFAULT_OFFICIAL_DECODE.name == (
        "aifs_official_station_candidates_20260801.json"
    )


def _database(path: Path) -> None:
    import sqlite3

    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE calibration_native_ensemble_historical_member_run(
          ensemble_system_id TEXT, station_id TEXT, target_local_date TEXT,
          native_run_init_utc TEXT, transport_provider_id TEXT,
          offline_oos_eligible INTEGER, qc_status TEXT
        );
        CREATE TABLE calibration_native_ensemble_historical_member_bucket(
          ensemble_system_id TEXT, source_role TEXT
        );
        CREATE TABLE calibration_cross_native_ensemble_oos_evaluation(
          ensemble_system_ids_json TEXT, oos_state TEXT
        );
        CREATE TABLE calibration_native_ensemble_activation(
          artifact_id TEXT, artifact_kind TEXT, authority_scope TEXT
        );
        CREATE TABLE calibration_cross_native_ensemble_artifact(
          cross_ensemble_artifact_id TEXT, ensemble_system_ids_json TEXT
        );
        """)
        conn.execute(
            "INSERT INTO calibration_native_ensemble_historical_member_run VALUES(?,?,?,?,?,?,?)",
            (
                readiness.AIFS_REPUBLISHER_COMPARISON_SYSTEM_ID, "KATL", "2026-07-30", None,
                "OPEN_METEO", 0, "ACCEPTED",
            ),
        )
        conn.execute(
            "INSERT INTO calibration_native_ensemble_historical_member_bucket VALUES(?,?)",
            (readiness.AIFS_REPUBLISHER_COMPARISON_SYSTEM_ID, "aggregator_republisher"),
        )


def _arrival(*, station: str, valid: str, raw_sha: str) -> dict[str, object]:
    return validate_model_arrival_capture({
        "arrival_id": f"aifs-{station}-{valid}",
        "provider_id": readiness.PROVIDER_ID,
        "model_id": "ECMWF",
        "station_id": station,
        "target_local_date": "2026-08-01",
        "target_timezone": "America/New_York",
        "run_initialization_utc": "2026-07-31T18:00:00Z",
        "valid_time_utc": valid,
        "published_at_utc": "2026-07-31T23:28:00Z",
        "received_at_utc": "2026-08-01T05:00:00Z",
        "decision_at_utc": "2026-08-01T05:01:00Z",
        "publication_time_source": "provider_http_last_modified",
        "source_url": (
            "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/"
            "20260731/18z/aifs-single/0p25/oper/test.grib2"
        ),
        "raw_payload_sha256s": [raw_sha],
        "receipt_sha256": _sha("receipt-" + raw_sha),
        "capture_mode": "prospective",
        "historical_arrival_time_observed": True,
        "aggregator_or_republisher": False,
        "open_meteo": False,
    })


def _capture_tree(tmp_path: Path, *, output_count: int = 4) -> tuple[Path, Path]:
    capture_root = tmp_path / "captures"
    run_dir = capture_root / "run"
    station_dir = run_dir / "station_arrivals"
    station_dir.mkdir(parents=True)
    valid_times = [
        "2026-08-01T06:00:00Z",
        "2026-08-01T12:00:00Z",
        "2026-08-01T18:00:00Z",
        "2026-08-02T00:00:00Z",
    ]
    outputs: list[dict[str, str]] = []
    points: list[dict[str, object]] = []
    for ordinal, valid in enumerate(valid_times[:output_count]):
        raw_sha = _sha(f"raw-{ordinal}")
        arrival = _arrival(station="KATL", valid=valid, raw_sha=raw_sha)
        path = station_dir / f"KATL_{ordinal}.json"
        path.write_text(json.dumps({"status": "CAPTURED", "arrival": arrival}))
        outputs.append({"status": "CAPTURED", "path": str(path)})
        points.append({
            "provider_id": readiness.PROVIDER_ID,
            "raw_message_sha256": raw_sha,
        })
    result = {
        "schema": "bayes_basket_target_aifs_forward_capture_scheduler_v1",
        "state": "CAPTURED_NATIVE_FORWARD_CANDIDATE",
        "provider_id": readiness.PROVIDER_ID,
        "product_id": readiness.PRODUCT_ID,
        "independence_family_id": "ECMWF",
        "target_local_date": "2026-08-01",
        "run_initialization_utc": "2026-07-31T18:00:00Z",
        "execution_decision_at_utc": "2026-08-01T05:01:00Z",
        "authority": {
            "source_role": "official_native",
            "native_arrival_index_eligible": True,
            "formal_p0_eligible_without_strict_oos": False,
            "counts_as_independent_family_from_ifs": False,
        },
        "boundary": {
            "writes_target_database": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "promotion_allowed": False,
        },
        "fanout": {
            "expected_arrival_count": 4,
            "captured_count": 4,
            "blocked_count": 0,
            "blockers": [],
            "outputs": outputs,
        },
    }
    run_dir.joinpath("run_result.json").write_text(json.dumps(result))
    decode = {
        "schema": "bayes_basket_target_native_model_arrival_decode_evidence_v1",
        "boundary": {
            "read_only_decode": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
        "curves": {
            "all_complete": True,
            "curves": [{
                "provider_id": readiness.PROVIDER_ID,
                "state": "COMPLETE",
                "event_date": "2026-08-01",
                "model_run_initialization_utc": "2026-07-31T18:00:00Z",
                "station_id": "KATL",
                "coverage": {"point_count": 4},
                "points": points,
            }],
        },
    }
    decode_path = tmp_path / "decode.json"
    decode_path.write_text(json.dumps(decode))
    return capture_root, decode_path


def _deterministic_artifact_paths(tmp_path: Path) -> dict[str, Path]:
    curves, arrivals, truths = _inputs(
        count=MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1
    )
    batch = build_aifs_deterministic_training_facts(
        curves, arrivals, truths, as_of_utc="2026-07-31T00:00:00Z"
    )
    report = build_aifs_deterministic_strict_oos_candidate(batch)
    candidate = report["candidates"][0]
    assert candidate["oos_state"] == "STRICT_OOS_PASS"
    promotion = build_aifs_deterministic_promotion_receipt(
        training_batch=batch,
        strict_oos_report=report,
        candidate_content_sha256=candidate["content_sha256"],
        decided_at_utc="2026-07-31T01:00:00Z",
        decision_actor="readiness-test-gate",
    )
    authority = issue_aifs_deterministic_provider_authority(
        training_batch=batch,
        strict_oos_report=report,
        promotion_receipt=promotion,
        valid_from_utc="2026-07-31T01:00:00Z",
        valid_until_utc="2026-08-30T01:00:00Z",
    )
    paths = {
        "training": tmp_path / "aifs_training.json",
        "oos": tmp_path / "aifs_oos.json",
        "promotion": tmp_path / "aifs_promotion.json",
        "authority": tmp_path / "aifs_authority.json",
    }
    for key, payload in {
        "training": batch,
        "oos": report,
        "promotion": promotion,
        "authority": authority,
    }.items():
        paths[key].write_text(json.dumps(payload), encoding="utf-8")
    return paths


def test_republisher_history_cannot_construct_official_aifs_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "EXPECTED_STATION_COUNT", 1)
    db = tmp_path / "target.db"
    _database(db)
    capture_root, decode = _capture_tree(tmp_path)
    before = db.read_bytes()

    receipt = readiness.audit_readiness(
        db_path=db,
        capture_root=capture_root,
        decode_evidence_path=decode,
    )

    assert db.read_bytes() == before
    assert receipt["state"] == "BLOCKED_STRICT_OOS"
    assert receipt["official_native_forward_capture"]["station_valid_arrival_count"] == 4
    assert receipt["closed_day_strict_oos"]["semantic_track"] == "AIFS_SINGLE_DETERMINISTIC"
    assert receipt["closed_day_strict_oos"]["deterministic_artifacts"] == {
        "state": "MISSING",
        "reason": "aifs_single_deterministic_artifacts_missing",
    }
    assert receipt["closed_day_strict_oos"]["open_meteo_republisher_comparison"] == {
        "open_meteo_aifs_ensemble_republisher_member_run_count": 1,
        "open_meteo_aifs_ensemble_republisher_member_bucket_count": 1,
        "republisher_rows_are_aifs_single_authority_eligible": 0,
    }
    assert receipt["provider_binding"]["constructible"] is False
    assert receipt["provider_binding"]["formal_p0_role_eligible"] is False
    assert receipt["boundary"]["orders_submitted"] == 0


def test_deterministic_aifs_artifacts_make_provider_ready_but_not_p0_role_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "EXPECTED_STATION_COUNT", 1)
    db = tmp_path / "target.db"
    _database(db)
    capture_root, decode = _capture_tree(tmp_path)
    artifacts = _deterministic_artifact_paths(tmp_path)
    before = db.read_bytes()

    receipt = readiness.audit_readiness(
        db_path=db,
        capture_root=capture_root,
        decode_evidence_path=decode,
        deterministic_training_batch_path=artifacts["training"],
        deterministic_strict_oos_report_path=artifacts["oos"],
        deterministic_promotion_receipt_path=artifacts["promotion"],
        deterministic_provider_authority_path=artifacts["authority"],
    )

    assert db.read_bytes() == before
    assert receipt["state"] == "FORMAL_DETERMINISTIC_PROVIDER_READY"
    assert receipt["closed_day_strict_oos"]["deterministic_artifacts"]["training_fact_count"] == 29
    assert receipt["provider_binding"] == {
        "schema_required": "ecmwf_aifs_single_official_deterministic_provider_authority_v1",
        "constructible": True,
        "formal_provider_eligible": True,
        "formal_p0_role_eligible": False,
        "requires_deterministic_p0_role_adapter": True,
        "reason": "provider_ready_but_deterministic_p0_role_adapter_required",
    }
    assert "aifs_single_deterministic_p0_role_adapter_required" in receipt["blockers"]


def test_forward_capture_scope_fails_closed_when_station_valid_fanout_is_short(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness, "EXPECTED_STATION_COUNT", 1)
    db = tmp_path / "target.db"
    _database(db)
    capture_root, decode = _capture_tree(tmp_path, output_count=3)

    with pytest.raises(readiness.AifsReadinessError, match="fanout_incomplete"):
        readiness.audit_readiness(
            db_path=db,
            capture_root=capture_root,
            decode_evidence_path=decode,
        )
