from __future__ import annotations

import json
import sqlite3

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.station_registry import coordinate_registry_manifest
from bayes_basket_target.native_ensemble_oos import _selection_identity
from scripts.plan_native_ensemble_activation_repair import (
    HISTORICAL_OOS_TEMPORAL_SCOPE,
    HISTORICAL_REPUBLISHER_RECEIPT,
    _independent_family_audit,
    _candidate_lineage,
    _strict_oos_sample_evidence,
    build_plan,
)
from tests.test_audit_native_ensemble_activation_oos_linkage import (
    _insert_activation,
    _insert_evaluation,
    _schema,
)


AS_OF = "2026-08-02T00:00:00.000Z"
ACTIVATED = "2026-08-01T17:52:54.725Z"
SYSTEM_IDS = ["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"]
ECCC_SYSTEM_IDS = ["ECCC_GEM_GLOBAL_ENS", *SYSTEM_IDS]
CYCLE_BY_SYSTEM = {
    "ECMWF_IFS_ENS_OPEN_PF50": "18Z",
    "NOAA_GEFS_05": "18Z",
}
CYCLE_JSON = json.dumps(CYCLE_BY_SYSTEM, sort_keys=True, separators=(",", ":"))
CYCLE_SHA = content_sha256(CYCLE_BY_SYSTEM)
MASK_SHA = content_sha256(SYSTEM_IDS)
ECCC_MASK_SHA = content_sha256(ECCC_SYSTEM_IDS)
HOLDOUT_DATES = [
    "2026-07-23",
    "2026-07-24",
    "2026-07-25",
    "2026-07-26",
    "2026-07-27",
    "2026-07-28",
    "2026-07-29",
    "2026-07-30",
]


def _strict_system_evaluation(system_id: str, transport: str) -> dict:
    predictions = []
    for day in HOLDOUT_DATES:
        raw_hashes = [
            content_sha256({"system": system_id, "event_date": day, "member": member})
            for member in ("m00", "m01")
        ]
        predictions.append({
            "event_date": day,
            "decision_at_utc": "2026-07-21T18:00:00Z",
            "member_set_sha256": content_sha256(raw_hashes),
            "member_run_content_shas": raw_hashes,
        })
    return {
        "state": "OOS_READY",
        "system_id": system_id,
        "transport_provider_id": transport,
        "train_n": 20,
        "holdout_n": 8,
        "train_end_date": "2026-07-14",
        "holdout_start_date": HOLDOUT_DATES[0],
        "holdout_end_date": HOLDOUT_DATES[-1],
        "train_source_counts": {
            "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE": 20,
            "FORWARD_LOCAL_RECEIPT": 0,
            "RETROSPECTIVE_ARCHIVE_TRAIN_ONLY": 0,
        },
        "holdout_source_class": "OFFLINE_CHRONOLOGICAL_NATIVE_ARCHIVE",
        "predictions": predictions,
    }


def _candidate(station_id: str, *, state: str = "PRODUCTION_NO_LIVE_OOS_PASS") -> dict:
    transports = {
        "ECMWF_IFS_ENS_OPEN_PF50": "ECMWF_OPEN_DATA_S3",
        "NOAA_GEFS_05": "NOAA_GEFS_PDS_S3",
    }
    evaluations = {
        system_id: _strict_system_evaluation(system_id, transports[system_id])
        for system_id in SYSTEM_IDS
    }
    cross_predictions = []
    for index, day in enumerate(HOLDOUT_DATES):
        cross_predictions.append({
            "event_date": day,
            "decision_at_utc": "2026-07-21T18:00:00Z",
            "trained_through_event_date": "2026-07-14",
            "system_member_set_shas": {
                system_id: evaluations[system_id]["predictions"][index]["member_set_sha256"]
                for system_id in SYSTEM_IDS
            },
        })
    return {
        "state": state,
        "station_id": station_id,
        "season_bin": "JJA",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "run_cycle_scope": "SAME_NATIVE_CYCLE",
        "run_cycle_by_system": dict(CYCLE_BY_SYSTEM),
        "run_cycle_by_system_sha256": CYCLE_SHA,
        "settlement_definition": "official_daily_tmax_c",
        "selected_system_ids": list(SYSTEM_IDS),
        "selected_system_count": 2,
        "independent_family_count": 2,
        "available_system_set_hash": MASK_SHA,
        "system_transport_provider_ids": transports,
        "system_weights": {
            "ECMWF_IFS_ENS_OPEN_PF50": 0.45,
            "NOAA_GEFS_05": 0.55,
        },
        "family_weights": {"ECMWF": 0.45, "NOAA_GEFS": 0.55},
        "family_weight_caps": {"ECMWF": 0.45, "NOAA_GEFS": 0.75},
        "supported_exact_masks": [list(SYSTEM_IDS)],
        "threshold_profile": "FORMAL_PROMOTION_20_TRAIN_8_HOLDOUT",
        "min_train_days": 20,
        "min_holdout_days": 8,
        "formal_promotion_min_train_days": 20,
        "formal_promotion_min_holdout_days": 8,
        "rolling_origin_fold_count": 8,
        "effective_sample_n": 8.0,
        "train_start_date": "2026-06-01",
        "train_end_date": "2026-07-14",
        "holdout_start_date": HOLDOUT_DATES[0],
        "holdout_end_date": HOLDOUT_DATES[-1],
        "oos_metrics": {"brier": 0.2, "crps": 0.3},
        "climatology_baseline_metrics": {"brier": 0.4, "crps": 0.5},
        "predictions": cross_predictions,
        "system_evaluations": evaluations,
        "cross_system_residual_correlation": {
            "ECMWF_IFS_ENS_OPEN_PF50": {"NOAA_GEFS_05": 0.4},
            "NOAA_GEFS_05": {"ECMWF_IFS_ENS_OPEN_PF50": 0.4},
        },
        "probability_pathway": "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE",
        "p0_exact_match_ready": True,
        "p0_exact_match_blocker": None,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
    }


def _eccc_candidate(
    station_id: str,
    *,
    state: str = "PRODUCTION_NO_LIVE_OOS_PASS",
) -> dict:
    """Build a duplicate ECCC-containing cohort used only for filter tests."""

    candidate = _candidate(station_id, state=state)
    candidate["selected_system_ids"] = list(ECCC_SYSTEM_IDS)
    candidate["selected_system_count"] = len(ECCC_SYSTEM_IDS)
    candidate["available_system_set_hash"] = ECCC_MASK_SHA
    candidate["run_cycle_by_system"] = {
        system_id: "18Z" for system_id in ECCC_SYSTEM_IDS
    }
    candidate["run_cycle_by_system_sha256"] = content_sha256(
        candidate["run_cycle_by_system"]
    )
    candidate["system_transport_provider_ids"]["ECCC_GEM_GLOBAL_ENS"] = (
        "ECCC_DD_WEATHER"
    )
    candidate["system_evaluations"]["ECCC_GEM_GLOBAL_ENS"] = (
        _strict_system_evaluation("ECCC_GEM_GLOBAL_ENS", "ECCC_DD_WEATHER")
    )
    for index, prediction in enumerate(candidate["predictions"]):
        prediction["system_member_set_shas"]["ECCC_GEM_GLOBAL_ENS"] = (
            candidate["system_evaluations"]["ECCC_GEM_GLOBAL_ENS"]["predictions"]
            [index]["member_set_sha256"]
        )
    return candidate


def _late_republisher_candidate(
    station_id: str = "KATL",
    *,
    include_contract: bool = True,
    native_system: str | None = None,
) -> dict:
    """Build a late historical cohort for timing-boundary tests."""

    candidate = _candidate(station_id)
    candidate["oos_event_temporal_scope"] = HISTORICAL_OOS_TEMPORAL_SCOPE
    candidate["future_event_count"] = 0
    candidate["source_role_by_system"] = {
        system_id: (
            "official_native_archive"
            if system_id == native_system
            else "aggregator_republisher"
        )
        for system_id in SYSTEM_IDS
    }
    for system_id in SYSTEM_IDS:
        provider = (
            "ECMWF_OPEN_DATA_S3"
            if system_id == native_system
            else "OPEN_METEO"
        )
        candidate["system_transport_provider_ids"][system_id] = provider
        evaluation = candidate["system_evaluations"][system_id]
        evaluation["transport_provider_id"] = provider
        if provider == "OPEN_METEO":
            evaluation["holdout_source_class"] = HISTORICAL_REPUBLISHER_RECEIPT
            evaluation["train_source_counts"] = {
                HISTORICAL_REPUBLISHER_RECEIPT: 20,
            }
        for prediction in evaluation["predictions"]:
            prediction["decision_at_utc"] = "2026-07-31T12:00:00Z"
            if include_contract and provider == "OPEN_METEO":
                prediction["historical_oos_contract_ids"] = [
                    f"contract-{system_id}-{prediction['event_date']}"
                ]
            else:
                prediction.pop("historical_oos_contract_ids", None)
    for prediction in candidate["predictions"]:
        prediction["decision_at_utc"] = "2026-07-31T12:00:00Z"
        prediction["historical_oos_contract_ids_by_system"] = {
            system_id: (
                [f"contract-{system_id}-{prediction['event_date']}"]
                if include_contract and system_id != native_system
                else []
            )
            for system_id in SYSTEM_IDS
        }
    return candidate


def _manifest(
    tmp_path,
    *,
    candidate_state: str = "PRODUCTION_NO_LIVE_OOS_PASS",
    include_eccc_duplicate: bool = False,
):
    reports = []
    for station_id in coordinate_registry_manifest()["station_ids"]:
        candidate = _candidate(station_id, state=candidate_state)
        candidates = [candidate]
        if include_eccc_duplicate and station_id == "KATL":
            candidates.append(_eccc_candidate(station_id, state=candidate_state))
        report_identity = {
            "schema": "native_ensemble_strict_forward_oos_v1",
            "station_id": station_id,
            "cross_candidates": candidates,
        }
        reports.append({
            **report_identity,
            "content_sha256": content_sha256(report_identity),
        })
    identity = {
        "schema": "native_ensemble_forward_oos_manifest_v1",
        "as_of_utc": ACTIVATED,
        "min_train_days": 20,
        "min_holdout_days": 8,
        "reports": reports,
        "production_no_live": True,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }
    manifest = {**identity, "content_sha256": content_sha256(identity)}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def _db(tmp_path):
    path = tmp_path / "ssot.db"
    conn = sqlite3.connect(path)
    _schema(conn)
    conn.commit()
    return conn, path


def _insert_eccc_activation(connection: sqlite3.Connection) -> None:
    cycle_by_system = {
        system_id: "18Z" for system_id in ECCC_SYSTEM_IDS
    }
    cycle_json = json.dumps(cycle_by_system, sort_keys=True, separators=(",", ":"))
    cycle_sha = content_sha256(cycle_by_system)
    system_json = json.dumps(ECCC_SYSTEM_IDS, separators=(",", ":"))
    connection.execute(
        "INSERT INTO calibration_cross_native_ensemble_artifact VALUES(?,?,?,?,?"
        ",?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "artifact-eccc", "KATL", "JJA", "d2", "18Z",
            "official_daily_tmax_c", system_json, len(ECCC_SYSTEM_IDS), 3,
            system_json, "e" * 64, "test", "{}",
            "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE",
            "2026-08-01T00:00:00.000Z", "2026-09-01T00:00:00.000Z",
            "f" * 64, cycle_json, cycle_sha,
        ),
    )
    connection.execute(
        "INSERT INTO calibration_native_ensemble_activation VALUES(?,?,?,?,?"
        ",?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "activation-eccc", "CROSS_NATIVE_ENSEMBLE", "artifact-eccc", "KATL",
            "*", "JJA", "d2", "18Z", "official_daily_tmax_c", ECCC_MASK_SHA,
            1, "2026-08-01T00:00:00.000Z", "2026-09-01T00:00:00.000Z",
            "2026-08-01T00:00:00.000Z", "PRODUCTION_NO_LIVE_ONLY",
            "g" * 64, cycle_json, cycle_sha,
        ),
    )


def test_late_open_meteo_closed_historical_contract_is_timing_eligible():
    candidate = _late_republisher_candidate()

    evidence = _strict_oos_sample_evidence(candidate, tuple(SYSTEM_IDS))

    assert evidence["state"] == (
        "VERIFIED_STRICT_20_TRAIN_8_HOLDOUT_NATIVE_HASH_LINEAGE"
    )
    assert evidence["reasons"] == []


def test_late_native_holdout_remains_pre_event_blocked_in_mixed_cohort():
    candidate = _late_republisher_candidate(native_system=SYSTEM_IDS[0])

    evidence = _strict_oos_sample_evidence(candidate, tuple(SYSTEM_IDS))

    assert evidence["state"].startswith("BLOCKED_")
    assert "cross_holdout_decision_not_pre_event" in evidence["reasons"]
    assert (
        f"{SYSTEM_IDS[0]}:system_holdout_arrival_not_pre_event"
        in evidence["reasons"]
    )


def test_late_open_meteo_without_historical_contract_remains_blocked():
    candidate = _late_republisher_candidate(include_contract=False)

    evidence = _strict_oos_sample_evidence(candidate, tuple(SYSTEM_IDS))

    assert evidence["state"].startswith("BLOCKED_")
    assert "cross_holdout_decision_not_pre_event" in evidence["reasons"]
    assert any(
        reason.endswith(":system_holdout_arrival_not_pre_event")
        for reason in evidence["reasons"]
    )
    assert any(
        reason.endswith(":republisher_historical_oos_contract_missing")
        for reason in evidence["reasons"]
    )


def test_late_open_meteo_with_wrong_temporal_scope_remains_blocked():
    candidate = _late_republisher_candidate()
    candidate["oos_event_temporal_scope"] = "OBSERVATIONAL_CANDIDATE_NOT_OOS"

    evidence = _strict_oos_sample_evidence(candidate, tuple(SYSTEM_IDS))

    assert evidence["state"].startswith("BLOCKED_")
    assert "oos_temporal_scope_not_closed_historical" in evidence["reasons"]
    assert "cross_holdout_decision_not_pre_event" in evidence["reasons"]


def test_repair_plan_is_read_only_and_surfaces_missing_evaluations(tmp_path):
    manifest_path, _ = _manifest(tmp_path)
    conn, database = _db(tmp_path)
    before = database.read_bytes()
    conn.close()

    plan = build_plan(database, manifest_path, as_of_utc=AS_OF, activated_at_utc=ACTIVATED)

    assert plan["station_count"] == 45
    assert plan["candidate_count"] == 45
    assert plan["candidate_action_summary"] == {"READY_APPEND_EXACT_EVALUATION": 45}
    assert plan["artifact_pointer_count"] == 0
    assert database.read_bytes() == before


def test_exact_mask_filter_excludes_eccc_candidate_and_activation(tmp_path):
    manifest_path, _ = _manifest(tmp_path, include_eccc_duplicate=True)
    conn, database = _db(tmp_path)
    _insert_activation(conn)
    _insert_eccc_activation(conn)
    conn.commit()
    before = database.read_bytes()
    conn.close()

    plan = build_plan(
        database,
        manifest_path,
        as_of_utc=AS_OF,
        activated_at_utc=ACTIVATED,
        selected_system_ids=(
            "ECMWF_IFS_ENS_OPEN_PF50,NOAA_GEFS_05"
        ),
    )

    assert plan["selected_system_ids_filter"] == SYSTEM_IDS
    assert plan["manifest_candidate_count"] == 46
    assert plan["candidate_count"] == 45
    assert plan["filtered_out_candidate_count"] == 1
    assert plan["activation_count"] == 1
    assert all(
        item["selected_system_ids"] == SYSTEM_IDS
        for item in plan["candidate_plans"]
    )
    assert all(
        json.loads(item["scope"]["ensemble_system_ids_json"]) == SYSTEM_IDS
        for item in plan["activation_plans"]
    )
    assert database.read_bytes() == before


def test_strict_pass_gets_content_addressed_pointer_without_db_write(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    conn, database = _db(tmp_path)
    candidate = _candidate("KATL")
    selection_sha = content_sha256(_selection_identity(candidate))
    _insert_evaluation(conn, evaluation_id="evaluation-katl", selection_sha=selection_sha)
    conn.commit()
    before = database.read_bytes()
    conn.close()

    plan = build_plan(database, manifest_path, as_of_utc=AS_OF, activated_at_utc=ACTIVATED)
    katl = next(item for item in plan["candidate_plans"] if item["scope"]["station_id"] == "KATL")

    assert katl["action"] == "READY_REPAIR_ARTIFACT_AND_ACTIVATION"
    assert katl["artifact_pointer"]["schema"] == "native_ensemble_artifact_pointer_v1"
    pointer = katl["artifact_pointer"]
    assert pointer["source_manifest_sha256"] == manifest["content_sha256"]
    assert pointer["pointer_sha256"] == content_sha256({k: v for k, v in pointer.items() if k != "pointer_sha256"})
    assert database.read_bytes() == before


def test_latest_rejected_cannot_revive_prior_pass(tmp_path):
    manifest_path, _ = _manifest(tmp_path)
    conn, database = _db(tmp_path)
    candidate = _candidate("KATL")
    selection_sha = content_sha256(_selection_identity(candidate))
    _insert_evaluation(
        conn,
        evaluation_id="evaluation-katl-rejected",
        selection_sha=selection_sha,
        state="OOS_REJECTED",
    )
    conn.commit()
    conn.close()

    plan = build_plan(database, manifest_path, as_of_utc=AS_OF, activated_at_utc=ACTIVATED)
    katl = next(item for item in plan["candidate_plans"] if item["scope"]["station_id"] == "KATL")

    assert katl["action"] == "BLOCKED_LATEST_REJECTED"
    assert katl["artifact_pointer"] is None


def test_pointer_repair_requires_actual_strict_sample_and_hash_lineage(tmp_path):
    manifest_path, _ = _manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    report = next(
        item for item in payload["reports"] if item["station_id"] == "KATL"
    )
    candidate = report["cross_candidates"][0]
    candidate["system_evaluations"][SYSTEM_IDS[0]]["train_n"] = 19
    candidate["system_evaluations"][SYSTEM_IDS[1]]["predictions"][0][
        "member_run_content_shas"
    ] = ["not-a-sha256"]
    report_identity = {key: value for key, value in report.items() if key != "content_sha256"}
    report["content_sha256"] = content_sha256(report_identity)
    manifest_identity = {key: value for key, value in payload.items() if key != "content_sha256"}
    payload["content_sha256"] = content_sha256(manifest_identity)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    conn, database = _db(tmp_path)
    selection_sha = content_sha256(_selection_identity(candidate))
    _insert_evaluation(conn, evaluation_id="evaluation-unsafe", selection_sha=selection_sha)
    conn.commit()
    conn.close()

    plan = build_plan(database, manifest_path, as_of_utc=AS_OF, activated_at_utc=ACTIVATED)
    katl = next(item for item in plan["candidate_plans"] if item["scope"]["station_id"] == "KATL")

    assert katl["action"] == "BLOCKED_CANDIDATE_CONTRACT"
    assert katl["artifact_pointer"] is None
    assert katl["validation"]["strict_oos_evidence"]["state"].startswith("BLOCKED_")
    assert any("system_train_sample_below_20" in reason for reason in katl["validation"]["reasons"])
    assert any("system_member_raw_hash_lineage_invalid" in reason for reason in katl["validation"]["reasons"])


def test_independent_family_audit_uses_registered_family_ids(tmp_path):
    conn, _database = _db(tmp_path)
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_system ("
        "ensemble_system_id TEXT PRIMARY KEY, "
        "independence_family_id TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_system VALUES (?,?)",
        [
            (SYSTEM_IDS[0], "ECMWF"),
            (SYSTEM_IDS[1], "NOAA_GEFS"),
        ],
    )
    candidate = _candidate("KATL")
    candidate["system_family_ids"] = {
        SYSTEM_IDS[0]: "ECMWF",
        SYSTEM_IDS[1]: "NOAA_GEFS",
    }
    audit = _independent_family_audit(conn, candidate, tuple(SYSTEM_IDS))
    assert audit["state"] == "VERIFIED_INDEPENDENT_FAMILY_REGISTRY"
    assert audit["independence_family_ids"] == ["ECMWF", "NOAA_GEFS"]
    conn.close()


def test_independent_family_audit_rejects_one_registered_family(tmp_path):
    conn, _database = _db(tmp_path)
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_system ("
        "ensemble_system_id TEXT PRIMARY KEY, "
        "independence_family_id TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO calibration_native_ensemble_system VALUES (?,?)",
        [(system_id, "ONE_FAMILY") for system_id in SYSTEM_IDS],
    )
    audit = _independent_family_audit(
        conn, _candidate("KATL"), tuple(SYSTEM_IDS)
    )
    assert audit["state"] == "BLOCKED_SINGLE_INDEPENDENT_FAMILY"
    conn.close()


def test_dwd_official_transport_is_native_but_not_an_automatic_promotion():
    candidate = _candidate("KATL")
    selected = ("DWD_ICON_GLOBAL_EPS", "NOAA_GEFS_05")
    candidate["system_transport_provider_ids"] = {
        "DWD_ICON_GLOBAL_EPS": "DWD",
        "NOAA_GEFS_05": "NOAA_GEFS_PDS_S3",
    }

    assert _candidate_lineage(candidate, selected) == {
        "state": "OFFICIAL_NATIVE_LINEAGE",
        "providers": {
            "DWD_ICON_GLOBAL_EPS": "DWD",
            "NOAA_GEFS_05": "NOAA_GEFS_PDS_S3",
        },
    }
