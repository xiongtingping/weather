from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from production_probability_activation_contract import activation_contract_sha256
from production_probability_deployment_attestation import CHANGE_SCOPE, attestation_sha256
from production_probability_runtime_manifest import (
    current_retirement_receipt_sha256,
    current_runtime_manifest_sha256,
)
from scripts.production_sequential_bayes_chain_audit import (
    ROOT,
    _load_fast_lane_durable_receipts,
    build_audit,
)
from weak_forecast_revision_schema_contract import ensure_weak_forecast_revision_schema


TARGET = "physical_weather_day_v1:ZUUU:2026-07-14"
P0_ID = "pchk-p0"
HEAD_ID = "pchk-head"
EVENT_ID = "canobs-forward"
RUN_P0 = "post-p0"
RUN_HEAD = "post-head"
UPDATE_ID = "stupd-forward"


def _activation(path: Path) -> dict:
    value = {
        "schema": "production_probability_runtime_activation_v2",
        "state": "production_enabled",
        "production_transaction_b_authorized": True,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "no_order_validation_accepted": True,
        "cutover_approval_id": "unit-test",
        "activation_effective_at": "2026-07-14T07:59:59Z",
        "prior_model_artifact_sha256": "a" * 64,
        "weak_likelihood_artifact_sha256": "b" * 64,
        "strong_lr_artifact_sha256": "c" * 64,
        "runtime_manifest_sha256": current_runtime_manifest_sha256(ROOT),
        "legacy_writer_retirement_receipt_sha256": current_retirement_receipt_sha256(ROOT),
        "bayes_formula_id": "p0_prior_x_weak_once_then_canonical_event_raw_log_lr_then_weak_revision_replacement_log_lr_v1",
        "composer_schema": "sequential_bayes_filter_v3",
        "checkpoint_schema": "posterior_checkpoint_v2",
        "strong_update_schema": "strong_observation_update_v3",
        "public_ssot_schema": "sequential_temperature_posterior_ssot_v2",
        "places_orders": False,
        "authorizes_clob": False,
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return {**value, "activation_contract_sha256": activation_contract_sha256(value)}


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE canonical_observation_events(
          canonical_observation_event_id TEXT PRIMARY KEY,target_identity TEXT,station_id TEXT,
          report_type TEXT,valid_time TEXT,first_actionable_seen_at TEXT,
          selected_source_id TEXT,state TEXT
        );
        CREATE TABLE canonical_observation_event_sources(
          canonical_observation_event_id TEXT,source_observation_id TEXT,source_event_id TEXT,
          source_id TEXT,relation TEXT,seen_at TEXT
        );
        CREATE TABLE production_initial_posterior_lineage(
          posterior_checkpoint_id TEXT PRIMARY KEY,target_identity TEXT,posterior_run_id TEXT,
          prior_context_id TEXT,prior_artifact_sha256 TEXT,prior_model_artifact_sha256 TEXT,
          weak_likelihood_artifact_sha256 TEXT,weak_likelihood_sha256 TEXT,
          forecast_evidence_snapshot_id TEXT,strong_lr_artifact_sha256 TEXT,
          source_reliability_initial_state_sha256 TEXT,resolution_contract_id TEXT,
          initial_evidence_sha256 TEXT,activation_contract_sha256 TEXT,lineage_json TEXT,created_at TEXT
        );
        CREATE TABLE strong_observation_updates(
          strong_observation_update_id TEXT PRIMARY KEY,target_identity TEXT,
          previous_posterior_run_id TEXT,previous_posterior_sha256 TEXT,
          previous_checkpoint_id TEXT,canonical_observation_event_id TEXT,
          forecast_evidence_snapshot_id TEXT,prior_context_id TEXT,model_artifact_sha256 TEXT,
          state TEXT,path_auxiliary_json TEXT,started_at TEXT,completed_at TEXT,created_at TEXT
        );
        CREATE TABLE posterior_checkpoints(
          posterior_checkpoint_id TEXT PRIMARY KEY,target_identity TEXT,posterior_run_id TEXT,
          previous_checkpoint_id TEXT,checkpoint_kind TEXT,forecast_evidence_snapshot_id TEXT,
          prior_context_id TEXT,resolution_contract_id TEXT,last_strong_update_id TEXT,
          last_canonical_event_id TEXT,joint_posterior_sha256 TEXT,physical_high_distribution_json TEXT,
          consumed_event_ids_json TEXT,model_artifact_sha256 TEXT,replay_generation INTEGER,
          supersedes_checkpoint_id TEXT,state TEXT,committed_at TEXT
        );
        CREATE TABLE event_fact_history(
          id INTEGER PRIMARY KEY,slug TEXT,event_date TEXT,fact_json TEXT,
          fact_snapshot_id TEXT,posterior_run_id TEXT
        );
        CREATE TABLE event_fact_latest(slug TEXT,event_date TEXT,fact_id INTEGER,fact_snapshot_id TEXT);
        CREATE TABLE realization_states(realization_state_id TEXT,posterior_run_id TEXT);
        CREATE TABLE posterior_commit_outbox(outbox_event_id TEXT,posterior_run_id TEXT);
        CREATE TABLE posterior_outbox_execution_contracts(outbox_event_id TEXT,posterior_run_id TEXT);
        """
    )


def _fixture(tmp_path: Path, *, head_committed_at: str = "2026-07-14T08:00:01Z") -> tuple[Path, Path]:
    db = tmp_path / "archive.db"
    activation_path = tmp_path / "activation.json"
    activation = _activation(activation_path)
    conn = sqlite3.connect(db)
    _schema(conn)
    ensure_weak_forecast_revision_schema(conn)
    conn.execute(
        """INSERT INTO weak_forecast_evidence_snapshots(
          forecast_evidence_snapshot_id,target_identity,event_date,decision_as_of,
          weak_likelihood_artifact_sha256,weak_likelihood_sha256,source_set_key,
          source_revision_ids_json,source_revisions_json,temperature_grid_json,
          likelihood_by_x_json,forecast_peak_timing_prior_json,snapshot_json,
          snapshot_sha256,state,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "forecast-1", TARGET, "2026-07-14", "2026-07-14T07:59:59.500Z",
            "b" * 64, "e" * 64, "empty", "[]", "[]", "[37.5,38.0]",
            '{"37.5":0.5,"38":0.5}', "{}", "{}", "9" * 64, "active",
            "2026-07-14T07:59:59.500Z",
        ),
    )
    conn.execute(
        "INSERT INTO canonical_observation_events VALUES(?,?,?,?,?,?,?,?)",
        (EVENT_ID, TARGET, "ZUUU", "SPECI", "2026-07-14T08:00:00Z", "2026-07-14T08:00:00Z", "zuuu_metar_raw_amsc", "accepted_primary"),
    )
    conn.execute(
        "INSERT INTO canonical_observation_event_sources VALUES(?,?,?,?,?,?)",
        (EVENT_ID, "srcobs-1", "srcevt-1", "zuuu_metar_raw_amsc", "selected", "2026-07-14T08:00:00Z"),
    )
    conn.execute(
        "INSERT INTO production_initial_posterior_lineage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            P0_ID, TARGET, RUN_P0, "prior-1", "d" * 64, "a" * 64, "b" * 64,
            "e" * 64, "forecast-1", "c" * 64, "f" * 64, "physical-only-1",
            "1" * 64, activation["activation_contract_sha256"], "{}", "2026-07-14T07:59:59.500Z",
        ),
    )
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            P0_ID, TARGET, RUN_P0, "", "initial", "forecast-1", "prior-1", "physical-only-1",
            "", "", "joint-p0", json.dumps({"37.5": 0.5, "38.0": 0.5}), "[]", "c" * 64,
            0, "", "committed", "2026-07-14T07:59:59.500Z",
        ),
    )
    path_auxiliary = {
        "remaining_warming_marginal": [
            {"remaining_warming_c": -0.25, "probability": 0.4},
            {"remaining_warming_c": 0.25, "probability": 0.6},
        ],
        "termination_risk": {
            "schema": "termination_risk_marginal_v1",
            "state": "available",
            "canonical_observation_event_id": EVENT_ID,
            "same_strong_update_marginal": True,
        },
        "peak_timing_state": {
            "schema": "peak_timing_state_l3_v1",
            "ok": True,
            "state": "available",
            "peak_timing_state_id": "peaktiming-forward",
            "canonical_observation_event_id": EVENT_ID,
            "strong_observation_update_id": UPDATE_ID,
            "forecast_evidence_snapshot_id": "forecast-1",
            "peak_already_probability": 0.4,
            "future_peak_probability": 0.6,
            "distribution": [
                {
                    "time_state": "observed_peak_to_date",
                    "peak_at": "2026-07-14T08:00:00Z",
                    "probability": 0.4,
                },
                {
                    "time_state": "future_forecast_supported_slot",
                    "peak_at": "2026-07-14T09:00:00Z",
                    "probability": 0.6,
                },
            ],
            "mode_time_state": "future_forecast_supported_slot",
            "mode_probability": 0.6,
            "entry_peak_at": "2026-07-14T09:00:00Z",
            "entry_peak_at_na_reason": "",
            "does_not_update_physical_x": True,
        },
    }
    conn.execute(
        "INSERT INTO strong_observation_updates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            UPDATE_ID, TARGET, RUN_P0, "joint-p0", P0_ID, EVENT_ID, "forecast-1", "prior-1",
            "c" * 64, "applied", json.dumps(path_auxiliary), "2026-07-14T08:00:00.100Z",
            head_committed_at, head_committed_at,
        ),
    )
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            HEAD_ID, TARGET, RUN_HEAD, P0_ID, "incremental", "forecast-1", "prior-1", "physical-only-1",
            UPDATE_ID, EVENT_ID, "joint-head", json.dumps({"37.5": 0.7, "38.0": 0.3}),
            json.dumps([EVENT_ID]), "c" * 64, 0, "", "committed", head_committed_at,
        ),
    )
    ssot = {
        "schema": "sequential_temperature_posterior_ssot_v2",
        "state": "physical_only_market_projection_na",
        "writer_mode": "production",
        "fact_snapshot_id": "fact-head",
        "posterior_run_id": RUN_HEAD,
        "posterior_checkpoint_id": HEAD_ID,
        "last_canonical_event_id": EVENT_ID,
        "last_strong_update_id": UPDATE_ID,
        "physical_high_distribution": {"37.5": 0.7, "38.0": 0.3},
        "market_bucket_probabilities": {},
        "market_bucket_probabilities_na_reason": "x_to_s_to_b_bridge_unavailable",
        "peak_timing_state": path_auxiliary["peak_timing_state"],
        "probability_runtime_identity": {
            "activation_contract_sha256": activation["activation_contract_sha256"],
        },
    }
    conn.execute(
        "INSERT INTO event_fact_history VALUES(?,?,?,?,?,?)",
        (1, "highest-temperature-in-chengdu-on-july-14-2026", "2026-07-14", json.dumps({"posterior_ssot_fact": ssot}), "fact-head", RUN_HEAD),
    )
    conn.execute(
        "INSERT INTO event_fact_latest VALUES(?,?,?,?)",
        ("highest-temperature-in-chengdu-on-july-14-2026", "2026-07-14", 1, "fact-head"),
    )
    conn.commit()
    conn.close()
    return db, activation_path


def test_active_production_chain_is_verified_without_market_bridge(tmp_path: Path) -> None:
    db, activation_path = _fixture(tmp_path)
    report = build_audit(
        db,
        event_date="2026-07-14",
        activation_path=activation_path,
        root=ROOT,
        now=datetime(2026, 7, 14, 8, 0, 2, tzinfo=timezone.utc),
    )

    assert report["ok"] is True
    assert report["state"] == "active_chain_verified"
    assert report["blockers"] == []
    assert report["evidence"]["active_activation_p0_lineage_count"] == 1
    assert report["evidence"]["active_generation_checkpoint_count"] == 2
    assert report["evidence"]["market_projection_available"] is False
    assert report["evidence"]["outbox_count_for_head"] == 0
    assert report["evidence"]["peak_timing_state"]["state"] == "available"
    assert report["evidence"]["peak_timing_state"]["entry_peak_at"] == "2026-07-14T09:00:00Z"
    assert report["boundaries"]["peak_timing_is_public_ssot_even_without_market_bridge"] is True
    assert report["forward_three_second_slo"]["events"][0]["t1_to_t3_ms"] == 1000.0
    assert report["boundaries"]["training_samples_block_live"] is False


def test_chain_audit_prefers_durable_fast_lane_receipt_over_logical_row_time(
    tmp_path: Path,
) -> None:
    db, activation_path = _fixture(tmp_path)
    receipt = tmp_path / "p2_result.json"
    receipt.write_text(
        json.dumps(
            {
                "probability": {
                    "schema": "production_canonical_event_drain_v1",
                    "outcomes": [
                        {
                            "canonical_observation_event_id": EVENT_ID,
                            "finished_at": "2026-07-14T08:00:02.543Z",
                            "t1_to_ssot_commit_observed_ms": 2543.0,
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    report = build_audit(
        db,
        event_date="2026-07-14",
        activation_path=activation_path,
        root=ROOT,
        fast_lane_result_path=receipt,
        now=datetime(2026, 7, 14, 8, 0, 3, tzinfo=timezone.utc),
    )

    event = report["current_deployment_forward_three_second_slo"]["events"][0]
    assert event["t1_to_t3_ms"] == 2543.0
    assert event["db_logical_t1_to_t3_ms"] == 1000.0
    assert event["measurement_source"] == "fast_lane_durable_completion_receipt"


def test_chain_audit_rejects_public_head_fork(tmp_path: Path) -> None:
    db, activation_path = _fixture(tmp_path)
    conn = sqlite3.connect(db)
    payload = json.loads(conn.execute("SELECT fact_json FROM event_fact_history WHERE id=1").fetchone()[0])
    payload["posterior_ssot_fact"]["posterior_checkpoint_id"] = "forked-head"
    conn.execute("UPDATE event_fact_history SET fact_json=? WHERE id=1", (json.dumps(payload),))
    conn.commit()
    conn.close()

    report = build_audit(db, event_date="2026-07-14", activation_path=activation_path, root=ROOT)

    assert report["ok"] is False
    assert "public_latest_binding_mismatch:checkpoint" in report["blockers"]


def test_chain_audit_rejects_missing_physical_only_peak_timing_ssot(tmp_path: Path) -> None:
    db, activation_path = _fixture(tmp_path)
    conn = sqlite3.connect(db)
    payload = json.loads(
        conn.execute("SELECT fact_json FROM event_fact_history WHERE id=1").fetchone()[0]
    )
    payload["posterior_ssot_fact"].pop("peak_timing_state")
    conn.execute(
        "UPDATE event_fact_history SET fact_json=? WHERE id=1",
        (json.dumps(payload),),
    )
    conn.commit()
    conn.close()

    report = build_audit(
        db,
        event_date="2026-07-14",
        activation_path=activation_path,
        root=ROOT,
    )

    assert report["ok"] is False
    assert "public_peak_timing_schema_invalid" in report["blockers"]


def test_chain_audit_rejects_forward_event_slower_than_three_seconds(tmp_path: Path) -> None:
    db, activation_path = _fixture(tmp_path, head_committed_at="2026-07-14T08:00:04.001Z")

    report = build_audit(db, event_date="2026-07-14", activation_path=activation_path, root=ROOT)

    assert report["ok"] is False
    assert f"forward_three_second_slo_failed:{EVENT_ID}" in report["blockers"]


def test_physical_only_ssot_cannot_create_trade_outbox(tmp_path: Path) -> None:
    db, activation_path = _fixture(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO posterior_commit_outbox VALUES(?,?)", ("outbox-illegal", RUN_HEAD))
    conn.commit()
    conn.close()

    report = build_audit(db, event_date="2026-07-14", activation_path=activation_path, root=ROOT)

    assert report["ok"] is False
    assert "physical_only_public_ssot_created_market_execution_state" in report["blockers"]


def test_chain_audit_accepts_bound_operational_retirement_patch_attestation(tmp_path: Path) -> None:
    db, activation_path = _fixture(tmp_path)
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation["legacy_writer_retirement_receipt_sha256"] = "0" * 64
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    activation_hash = activation_contract_sha256(activation)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE production_initial_posterior_lineage SET activation_contract_sha256=?",
        (activation_hash,),
    )
    payload = json.loads(conn.execute("SELECT fact_json FROM event_fact_history WHERE id=1").fetchone()[0])
    payload["posterior_ssot_fact"]["probability_runtime_identity"]["activation_contract_sha256"] = activation_hash
    conn.execute("UPDATE event_fact_history SET fact_json=? WHERE id=1", (json.dumps(payload),))
    conn.commit()
    conn.close()
    current_retirement = current_retirement_receipt_sha256(ROOT)
    attestation = {
        "schema": "production_probability_deployment_attestation_v1",
        "state": "deployment_approved",
        "approval_id": "unit-operational-patch",
        "approved_at": "2026-07-14T08:00:01Z",
        "change_scope": CHANGE_SCOPE,
        "base_activation_contract_sha256": activation_hash,
        "runtime_manifest_sha256": current_runtime_manifest_sha256(ROOT),
        "retirement_receipt_sha256": current_retirement,
        "probability_semantics_changed": False,
        "places_orders": False,
        "authorizes_clob": False,
    }
    attestation["attestation_sha256"] = attestation_sha256(attestation)
    attestation_path = tmp_path / "deployment_attestation.json"
    attestation_path.write_text(json.dumps(attestation), encoding="utf-8")

    report = build_audit(
        db,
        event_date="2026-07-14",
        activation_path=activation_path,
        root=ROOT,
        deployment_attestation_path=attestation_path,
        now=datetime(2026, 7, 14, 8, 0, 2, tzinfo=timezone.utc),
    )

    assert "production_probability_retirement_receipt_drift" not in report["blockers"]
    authorization = report["activation"]["retirement_deployment_authorization"]
    assert authorization["source"] == "operational_deployment_attestation"


def test_reported_x_fast_lane_receipt_uses_root_durable_completion(tmp_path: Path) -> None:
    path = tmp_path / "fast-lane.json"
    path.write_text(
        json.dumps(
            {
                "fast_lane": {
                    "schema": "amsc_probability_fast_lane_from_plan_v1",
                    "finished_at": "2026-07-15T07:00:54.471Z",
                },
                "probability": {
                    "outcomes": [
                        {"canonical_observation_event_id": "canobs-current"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    receipts = _load_fast_lane_durable_receipts(path)

    assert receipts["canobs-current"]["durable_finished_at"] == "2026-07-15T07:00:54.471Z"
    assert receipts["canobs-current"]["t1_to_ssot_durable_ms"] is None
