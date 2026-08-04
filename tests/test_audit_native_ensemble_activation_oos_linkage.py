from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.contracts import content_sha256
from scripts.audit_native_ensemble_activation_oos_linkage import (
    _repair_plan_pointer_context,
    _repair_plan_pointer_station_ids,
    build_report,
)


AS_OF = "2026-08-02T02:00:00.000Z"
SYSTEM_IDS = ["ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05"]
CYCLE_BY_SYSTEM = {
    "ECMWF_IFS_ENS_OPEN_PF50": "18Z",
    "NOAA_GEFS_05": "18Z",
}
SYSTEM_IDS_JSON = json.dumps(SYSTEM_IDS, separators=(",", ":"))
CYCLE_JSON = json.dumps(
    CYCLE_BY_SYSTEM, sort_keys=True, separators=(",", ":")
)
MASK_SHA = content_sha256(SYSTEM_IDS)
CYCLE_SHA = content_sha256(CYCLE_BY_SYSTEM)
SELECTION_SHA = "a" * 64


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE calibration_native_ensemble_activation (
          activation_id TEXT PRIMARY KEY,
          artifact_kind TEXT NOT NULL,
          artifact_id TEXT NOT NULL,
          station_id TEXT NOT NULL,
          ensemble_system_id_or_star TEXT NOT NULL,
          season_bin TEXT NOT NULL,
          lead_bucket TEXT NOT NULL,
          run_cycle TEXT NOT NULL,
          settlement_definition TEXT NOT NULL,
          available_system_set_hash_or_star TEXT NOT NULL,
          generation INTEGER NOT NULL,
          valid_from_utc TEXT NOT NULL,
          valid_until_utc TEXT NOT NULL,
          activated_at_utc TEXT NOT NULL,
          authority_scope TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          run_cycle_by_system_json TEXT,
          run_cycle_by_system_sha256 TEXT
        );
        CREATE TABLE calibration_cross_native_ensemble_artifact (
          cross_ensemble_artifact_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          season_bin TEXT NOT NULL,
          lead_bucket TEXT NOT NULL,
          run_cycle TEXT NOT NULL,
          settlement_definition TEXT NOT NULL,
          ensemble_system_ids_json TEXT NOT NULL,
          ensemble_system_count INTEGER NOT NULL,
          independent_family_count INTEGER NOT NULL,
          supported_exact_masks_json TEXT NOT NULL,
          selection_evidence_sha256 TEXT NOT NULL,
          combination_method TEXT NOT NULL,
          family_weight_caps_json TEXT NOT NULL,
          probability_pathway TEXT NOT NULL,
          valid_from_utc TEXT NOT NULL,
          valid_until_utc TEXT NOT NULL,
          content_sha256 TEXT NOT NULL,
          run_cycle_by_system_json TEXT,
          run_cycle_by_system_sha256 TEXT
        );
        CREATE TABLE calibration_cross_native_ensemble_oos_evaluation (
          evaluation_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          season_bin TEXT NOT NULL,
          lead_bucket TEXT NOT NULL,
          run_cycle TEXT NOT NULL,
          run_cycle_by_system_json TEXT NOT NULL,
          run_cycle_by_system_sha256 TEXT NOT NULL,
          settlement_definition TEXT NOT NULL,
          ensemble_system_ids_json TEXT NOT NULL,
          available_system_set_hash TEXT NOT NULL,
          selection_evidence_sha256 TEXT NOT NULL,
          oos_state TEXT NOT NULL,
          threshold_profile TEXT NOT NULL,
          min_train_days INTEGER NOT NULL,
          min_holdout_days INTEGER NOT NULL,
          train_end_date TEXT NOT NULL,
          holdout_start_date TEXT NOT NULL,
          holdout_end_date TEXT NOT NULL,
          oos_metrics_json TEXT NOT NULL,
          climatology_baseline_metrics_json TEXT NOT NULL,
          evaluated_at_utc TEXT NOT NULL,
          content_sha256 TEXT NOT NULL
        );
        """
    )


def _insert_activation(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO calibration_cross_native_ensemble_artifact VALUES(?,?,?,?,?"
        ",?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "artifact-1", "KATL", "JJA", "d2", "18Z",
            "official_daily_tmax_c", SYSTEM_IDS_JSON, len(SYSTEM_IDS), 2,
            SYSTEM_IDS_JSON, SELECTION_SHA, "test", "{}",
            "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE",
            "2026-08-01T00:00:00.000Z", "2026-09-01T00:00:00.000Z",
            "b" * 64, CYCLE_JSON, CYCLE_SHA,
        ),
    )
    connection.execute(
        "INSERT INTO calibration_native_ensemble_activation VALUES(?,?,?,?,?"
        ",?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "activation-1", "CROSS_NATIVE_ENSEMBLE", "artifact-1", "KATL",
            "*", "JJA", "d2", "18Z", "official_daily_tmax_c", MASK_SHA,
            1, "2026-08-01T00:00:00.000Z", "2026-09-01T00:00:00.000Z",
            "2026-08-01T00:00:00.000Z", "PRODUCTION_NO_LIVE_ONLY",
            "c" * 64, CYCLE_JSON, CYCLE_SHA,
        ),
    )


def _insert_evaluation(
    connection: sqlite3.Connection,
    *,
    evaluation_id: str,
    selection_sha: str,
    state: str = "PRODUCTION_NO_LIVE_OOS_PASS",
    evaluated_at_utc: str = "2026-08-01T01:00:00.000Z",
) -> None:
    connection.execute(
        "INSERT INTO calibration_cross_native_ensemble_oos_evaluation "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            evaluation_id, "KATL", "JJA", "d2", "18Z", CYCLE_JSON,
            CYCLE_SHA, "official_daily_tmax_c", SYSTEM_IDS_JSON, MASK_SHA,
            selection_sha, state, "strict", 20, 8, "2026-07-01",
            "2026-07-02", "2026-07-10", "{}", "{}", evaluated_at_utc,
            "d" * 63 + evaluation_id[-1],
        ),
    )


def _report(tmp_path) -> tuple[sqlite3.Connection, object]:
    database = tmp_path / "ssot.db"
    connection = sqlite3.connect(database)
    _schema(connection)
    _insert_activation(connection)
    connection.commit()
    return connection, database


def test_missing_exact_oos_evaluation_is_reported_unconsumable(tmp_path) -> None:
    connection, database = _report(tmp_path)
    try:
        before = database.read_bytes()
        report = build_report(database, as_of_utc=AS_OF)
        assert database.read_bytes() == before
    finally:
        connection.close()

    assert report["audit_state"] == (
        "FAIL_CLOSED_UNCONSUMABLE_ACTIVATIONS_PRESENT"
    )
    assert report["unconsumable_activation_count"] == 1
    record = report["unconsumable_records"][0]
    assert record["state"] == "UNCONSUMABLE_MISSING_EXACT_OOS_EVALUATION"
    assert record["artifact_matching_oos_pass_present"] is False


def test_only_latest_exact_selection_pass_is_consumable(tmp_path) -> None:
    connection, database = _report(tmp_path)
    try:
        _insert_evaluation(
            connection,
            evaluation_id="evaluation-1",
            selection_sha=SELECTION_SHA,
        )
        connection.commit()
        passed = build_report(database, as_of_utc=AS_OF)
        assert passed["audit_state"] == (
            "PASS_ALL_ACTIVATIONS_EXACT_OOS_CONSUMABLE"
        )
        assert passed["consumable_exact_oos_pass_activation_count"] == 1

        # An exact-mask evaluation with a later, different selection invalidates
        # the older activation at runtime even though its historical PASS stays
        # in the append-only evidence table.
        _insert_evaluation(
            connection,
            evaluation_id="evaluation-2",
            selection_sha="e" * 64,
            evaluated_at_utc="2026-08-01T02:00:00.000Z",
        )
        connection.commit()
        stale = build_report(database, as_of_utc=AS_OF)
    finally:
        connection.close()

    record = stale["unconsumable_records"][0]
    assert record["state"] == "UNCONSUMABLE_LATEST_OOS_SELECTION_MISMATCH"
    assert record["artifact_matching_oos_pass_present"] is True


def test_identity_mismatch_never_claims_oos_consumability(tmp_path) -> None:
    connection, database = _report(tmp_path)
    try:
        connection.execute(
            "UPDATE calibration_native_ensemble_activation "
            "SET available_system_set_hash_or_star=?",
            ("f" * 64,),
        )
        connection.commit()
        report = build_report(database, as_of_utc=AS_OF)
    finally:
        connection.close()

    record = report["unconsumable_records"][0]
    assert record["state"] == "UNCONSUMABLE_ACTIVATION_IDENTITY_INVALID"
    assert record["reason"] == "activation_available_system_set_hash_mismatch"


def test_unified_current_pointer_excludes_unbound_active_station(tmp_path) -> None:
    connection, database = _report(tmp_path)
    try:
        _insert_evaluation(
            connection,
            evaluation_id="evaluation-1",
            selection_sha=SELECTION_SHA,
        )
        connection.commit()
        report = build_report(
            database,
            as_of_utc=AS_OF,
            current_pointer_station_ids={"OTHER"},
            current_pointer_source_sha256="f" * 64,
            current_pointer_source_path="/tmp/current-repair-plan.json",
        )
    finally:
        connection.close()

    assert report["audit_state"] == "FAIL_CLOSED_CURRENT_POINTER_STATION_SET_MISMATCH"
    assert report["current_pointer"]["state"] == "BOUND"
    assert report["current_pointer"]["station_ids"] == ["OTHER"]
    assert report["current_pointer"]["station_set_match"] is False
    assert report["consumable_exact_oos_pass_activation_count"] == 0
    record = report["unconsumable_records"][0]
    assert record["state"] == "UNCONSUMABLE_CURRENT_POINTER_NOT_BOUND"
    assert record["reason"] == "station_not_in_unified_current_pointer"


def _write_pointer_plan(
    path,
    *,
    pointer_overrides=None,
    selected_system_ids_filter=None,
) -> None:
    pointer_identity = {
        "schema": "native_ensemble_artifact_pointer_v1",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "selection_evidence_sha256": "a" * 64,
        "cross_ensemble_artifact_id": "cross-artifact",
        "cross_artifact_content_sha256": "c" * 64,
        "activation_id": "activation",
        "activation_identity_sha256": "d" * 64,
        "generation": 1,
    }
    if pointer_overrides:
        pointer_identity.update(pointer_overrides)
    pointer = {
        **pointer_identity,
        "pointer_sha256": content_sha256(pointer_identity),
    }
    candidate_plan = {
        "action": "NOOP_ALREADY_CONSUMABLE",
        "candidate_state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "selection_evidence_sha256": "a" * 64,
        "scope": {"station_id": "KATL"},
        "artifact_pointer": pointer,
        "expected_artifact": {
            "cross_ensemble_artifact_id": "cross-artifact",
            "cross_artifact_content_sha256": "c" * 64,
            "activation_id": "activation",
            "activation_identity_sha256": "d" * 64,
            "generation": 1,
        },
    }
    if selected_system_ids_filter is not None:
        candidate_plan["selected_system_ids"] = selected_system_ids_filter
        candidate_plan["scope"]["ensemble_system_ids_json"] = json.dumps(
            selected_system_ids_filter,
            separators=(",", ":"),
        )
    payload_identity = {
        "schema": "native_ensemble_activation_repair_plan_v1",
        "as_of_utc": AS_OF,
        "artifact_pointer_count": 1,
        "candidate_plans": [candidate_plan],
    }
    if selected_system_ids_filter is not None:
        payload_identity["selected_system_ids_filter"] = selected_system_ids_filter
    path.write_text(
        json.dumps(
            {**payload_identity, "content_sha256": content_sha256(payload_identity)},
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cross_artifact_content_sha256", "e" * 64),
        ("activation_identity_sha256", "f" * 64),
        ("generation", 2),
    ],
)
def test_current_pointer_binds_content_addressed_artifact_pair(
    tmp_path, field, value,
) -> None:
    path = tmp_path / "repair.json"
    _write_pointer_plan(path, pointer_overrides={field: value})
    with pytest.raises(
        ValueError,
        match="native_ensemble_linkage_audit_pointer_identity_invalid",
    ):
        _repair_plan_pointer_station_ids(path)


def test_current_pointer_accepts_legacy_pair_without_optional_fields(tmp_path) -> None:
    path = tmp_path / "legacy-repair.json"
    _write_pointer_plan(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    plan = payload["candidate_plans"][0]
    for field in (
        "cross_artifact_content_sha256",
        "activation_identity_sha256",
        "generation",
    ):
        plan["artifact_pointer"].pop(field, None)
        plan["expected_artifact"].pop(field, None)
    pointer = plan["artifact_pointer"]
    pointer["pointer_sha256"] = content_sha256(
        {key: item for key, item in pointer.items() if key != "pointer_sha256"}
    )
    identity = {key: item for key, item in payload.items() if key != "content_sha256"}
    payload["content_sha256"] = content_sha256(identity)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    stations, _ = _repair_plan_pointer_station_ids(path)
    assert stations == {"KATL"}


def test_current_pointer_context_carries_exact_mask_filter(tmp_path) -> None:
    path = tmp_path / "repair.json"
    _write_pointer_plan(
        path,
        selected_system_ids_filter=SYSTEM_IDS,
    )

    stations, _, selected = _repair_plan_pointer_context(path)

    assert stations == {"KATL"}
    assert selected == SYSTEM_IDS
