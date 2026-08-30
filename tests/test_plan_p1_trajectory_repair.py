from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.probability_evidence_store import apply_probability_evidence_schema
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts.plan_p1_trajectory_repair import build_plan
from bayes_basket_target.fleet import expected_station_count


AS_OF = "2026-08-02T00:00:00.000Z"
BUCKETS = ("NIGHT", "MORNING", "AFTERNOON", "EVENING")


def _audit(tmp_path, *, blockers: list[str] | None = None, candidate: dict | None = None):
    scopes = []
    for station_id in coordinate_registry_manifest()["station_ids"]:
        for bucket in BUCKETS:
            scope_key = f"awc|JJA|{bucket}|D0|official_daily_tmax_c"
            source_manifest = {
                "schema": "bayes_basket_metar_trajectory_fold_source_v1",
                "station_id": station_id,
                "local_time_bucket": bucket,
                "eligible_fold_count": 0,
                "v3_eligible_fold_count": 0,
                "v3_incomplete_future_curve_fold_count": 0,
            }
            source_manifest["content_sha256"] = content_sha256(source_manifest)
            scopes.append({
                "station_id": station_id,
                "local_time_bucket": bucket,
                "settlement_timezone": "UTC",
                "source_counts": {"truth_row_count": 30, "observation_row_count": 100, "probability_root_row_count": 3},
                "source_manifest": source_manifest,
                "correction_review": {"unreviewed_correction_count": 0, "active_halt_count": 0},
                "training": {"state": "BLOCKED_EVIDENCE", "error": "", "candidate": candidate if station_id == "KATL" and bucket == "NIGHT" else None},
                "artifact": {"scope_key": scope_key, "authority": {"FORMAL_PROMOTED": "MISSING", "PRODUCTION_NO_LIVE_CANDIDATE": "MISSING"}},
                "blockers": list(blockers or []),
                "formal_eligible": not blockers,
                "state": "FORMAL_ELIGIBLE" if not blockers else "BLOCKED_EVIDENCE",
                "boundary": {"writes_target_database": False, "writes_canonical_database": False, "calls_order_gateway": False, "orders_submitted": 0, "live_submit_authorized": False},
            })
    identity = {
        "schema": "bayes_basket_target_p1_trajectory_45station_audit_v1",
        "execution_mode": "production_no_live",
        "as_of_utc": AS_OF,
        "station_scope": {
            "requested_station_ids": coordinate_registry_manifest()["station_ids"],
            "station_count": expected_station_count(),
            "canonical_station_count": expected_station_count(),
            "canonical_registry_sha256": coordinate_registry_manifest()["registry_sha256"],
        },
        "minimums": {"minimum_prior_truths": 3, "minimum_effective_sample_n": 20.0, "method_version": "canonical_conditional_hourly_future_path_v3"},
        "scopes": scopes,
        "scope_count": 180,
        "formal_eligible_scope_count": sum(bool(item["formal_eligible"]) for item in scopes),
        "blocked_scope_count": sum(not bool(item["formal_eligible"]) for item in scopes),
        "blocker_counts": {},
        "boundary": {"writes_target_database": False, "writes_canonical_database": False, "calls_order_gateway": False, "orders_submitted": 0, "live_submit_authorized": False},
    }
    payload = {**identity, "content_sha256": content_sha256(identity)}
    path = tmp_path / "p1-audit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def _db(tmp_path):
    path = tmp_path / "target.db"
    conn = sqlite3.connect(path)
    apply_probability_evidence_schema(conn, commit=True)
    return conn, path


def _hourly_curve_audit(tmp_path, *, station_id: str = "KATL"):
    entry = {
        "station_id": station_id,
        "event_date": "2026-07-31",
        "independence_family_count": 3,
        "minimum_family_curve_point_count": 8,
        "strict_asof_bindable": False,
        "root_curve_bound_count": 0,
        "blockers": ["archive_query_after_station_day_not_strict_asof"],
    }
    identity = {
        "schema": "bayes_basket_target_p1_hourly_curve_receipt_audit_45station_v1",
        "canonical_station_count": expected_station_count(),
        "entries": [entry],
        "summary": {"archive_query_after_station_day_not_strict_asof": 1},
    }
    payload = {**identity, "content_sha256": content_sha256(identity)}
    path = tmp_path / "hourly-curve-audit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_all_blocked_audit_emits_precise_no_write_plan(tmp_path):
    audit_path, _ = _audit(tmp_path, blockers=["sample_insufficient", "arrival_lineage_missing"])
    conn, database = _db(tmp_path)
    before = database.read_bytes()
    conn.close()

    plan = build_plan(audit_path=audit_path, database=database, as_of_utc=AS_OF)

    assert plan["station_count"] == expected_station_count()
    assert plan["scope_count"] == 180
    assert plan["formal_eligible_scope_count"] == 0
    assert plan["candidate_pointer_count"] == 0
    assert plan["action_summary"] == {"BLOCKED_EVIDENCE": 180}
    assert plan["blocker_summary"] == {"arrival_lineage_missing": 180, "sample_insufficient": 180}
    assert plan["safe_write_state"] == "NO_SAFE_FORMAL_WRITE_REQUIRED"
    assert database.read_bytes() == before


def test_existing_candidate_artifact_pointer_is_content_addressed(tmp_path):
    audit_path, audit = _audit(tmp_path, blockers=["sample_insufficient"])
    conn, database = _db(tmp_path)
    scope_key = "awc|JJA|NIGHT|D0|official_daily_tmax_c"
    artifact_id = "metar-trajectory-KATL-night-v1"
    artifact_sha = "a" * 64
    conn.execute(
        "INSERT INTO basket_probability_evidence_artifact VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (artifact_id, "METAR_TRAJECTORY_LIKELIHOOD", "KATL", "UTC", scope_key, AS_OF, AS_OF, "2100-01-01T00:00:00Z", 0, "PRODUCTION_NO_LIVE_CANDIDATE", "{}", artifact_sha),
    )
    activation_id = "metar-trajectory-KATL-night-v1"
    activation_sha = "b" * 64
    conn.execute(
        "INSERT INTO basket_probability_evidence_activation VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (activation_id, "METAR_TRAJECTORY_LIKELIHOOD", artifact_id, "KATL", scope_key, 1, AS_OF, "2100-01-01T00:00:00Z", AS_OF, "PRODUCTION_NO_LIVE_CANDIDATE", activation_sha),
    )
    conn.commit()
    before = database.read_bytes()
    conn.close()

    plan = build_plan(audit_path=audit_path, database=database, as_of_utc=AS_OF)
    scope = next(item for item in plan["scopes"] if item["station_id"] == "KATL" and item["local_time_bucket"] == "NIGHT")
    pointer = scope["artifact_pointer"]

    assert scope["action"] == "NOOP_CANDIDATE_ARTIFACT_ALREADY_PRESENT"
    assert pointer["authority_scope"] == "PRODUCTION_NO_LIVE_CANDIDATE"
    assert pointer["artifact_content_sha256"] == artifact_sha
    assert pointer["pointer_sha256"] == content_sha256({k: v for k, v in pointer.items() if k != "pointer_sha256"})
    assert database.read_bytes() == before


def test_candidate_summary_without_payload_stays_blocked(tmp_path):
    candidate = {"artifact_id": "candidate-id", "promotion_state": "CANDIDATE_ONLY", "sample_n": 20, "effective_sample_n": 20.0, "oos_gate_passed": True, "formal_probability_oos_available": True}
    audit_path, _ = _audit(tmp_path, blockers=[], candidate=candidate)
    _, database = _db(tmp_path)

    plan = build_plan(audit_path=audit_path, database=database, as_of_utc=AS_OF)
    scope = next(item for item in plan["scopes"] if item["station_id"] == "KATL" and item["local_time_bucket"] == "NIGHT")

    assert scope["action"] == "BLOCKED_CANDIDATE_PAYLOAD_INCOMPLETE"
    assert scope["artifact_pointer"] is None


def test_late_archive_curve_requires_forward_arrival_capture_not_rebinding(tmp_path):
    audit_path, _ = _audit(tmp_path, blockers=["arrival_lineage_missing"])
    hourly_curve_audit_path = _hourly_curve_audit(tmp_path)
    _, database = _db(tmp_path)

    plan = build_plan(
        audit_path=audit_path,
        database=database,
        as_of_utc=AS_OF,
        hourly_curve_audit_path=hourly_curve_audit_path,
    )
    scope = next(
        item for item in plan["scopes"]
        if item["station_id"] == "KATL" and item["local_time_bucket"] == "NIGHT"
    )

    assert scope["hourly_curve_receipts"] == {
        "station_day_count": 1,
        "complete_native_curve_station_day_count": 1,
        "strict_asof_bindable_station_day_count": 0,
        "root_curve_bound_station_day_count": 0,
        "archive_query_after_station_day_count": 1,
    }
    assert scope["repair_steps"][0]["action"] == (
        "CAPTURE_FORWARD_NATIVE_HOURLY_CURVE_WITH_ARRIVAL"
    )
    assert "archive-query time is not arrival" in scope["repair_steps"][0]["required_evidence"]
    assert plan["hourly_curve_receipt_audit"]["content_sha256"] == content_sha256(
        {
            "schema": "bayes_basket_target_p1_hourly_curve_receipt_audit_45station_v1",
            "canonical_station_count": expected_station_count(),
            "entries": [{
                "station_id": "KATL",
                "event_date": "2026-07-31",
                "independence_family_count": 3,
                "minimum_family_curve_point_count": 8,
                "strict_asof_bindable": False,
                "root_curve_bound_count": 0,
                "blockers": ["archive_query_after_station_day_not_strict_asof"],
            }],
            "summary": {"archive_query_after_station_day_not_strict_asof": 1},
        }
    )


def test_plan_refuses_to_mix_audit_and_activation_evidence_times(tmp_path):
    audit_path, _ = _audit(tmp_path, blockers=["sample_insufficient"])
    _, database = _db(tmp_path)

    with pytest.raises(ContractError, match="p1_repair_as_of_must_match_audit"):
        build_plan(
            audit_path=audit_path,
            database=database,
            as_of_utc="2026-08-02T00:00:00.001Z",
        )
