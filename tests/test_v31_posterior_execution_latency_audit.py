from __future__ import annotations

import json
import sqlite3

from v31_posterior_execution_latency_audit import audit_v31_posterior_execution_latency
from posterior_materialization_attestation_runtime import (
    append_posterior_materialization_commit_attestation,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            created_at TEXT,
            fact_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            created_at TEXT,
            action TEXT,
            decided_state TEXT,
            will_order INTEGER,
            reason_codes_json TEXT,
            details_json TEXT
        )
        """
    )
    return conn


def test_audit_reconstructs_legacy_v31_source_to_decision_timing_without_claiming_p10() -> None:
    conn = _conn()
    payload = {
        "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31",
        "fact_snapshot_id": "fact_1",
        "posterior_run_id": "post_1",
        "amsc_posterior_materialization_timing": {
            "schema": "source_to_posterior_timing_v1",
            "latest_source_response_completed_at": "2026-07-12T00:00:00Z",
            "posterior_apply_completed_at": "2026-07-12T00:00:01.000Z",
            "posterior_commit_fact_id": 5,
            "task_queue_id": 7,
            "task_queue_wait_ms": 0,
            "source_response_to_posterior_apply_start_ms": 900,
        },
    }
    conn.execute(
        "INSERT INTO event_fact_history VALUES (?,?,?,?,?,?)",
        (
            5,
            "highest-temperature-in-chengdu-on-july-12-2026",
            "2026-07-12",
            "2026-07-12T00:00:00Z",
            "2026-07-12T00:00:01.000Z",
            json.dumps(payload),
        ),
    )
    details = {
        "fact_id": 5,
        "decision_started_at": "2026-07-12T00:00:01.250Z",
        "sequential_bayes_execution_contract": {
            "blockers": [],
            "signal_to_order_slo_ms": 3000,
        },
    }
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            9,
            "highest-temperature-in-chengdu-on-july-12-2026",
            "2026-07-12",
            "2026-07-12T00:00:01.300Z",
            "entry",
            "ready",
            1,
            "[]",
            json.dumps(details),
        ),
    )

    result = audit_v31_posterior_execution_latency(conn, latest_only=True)

    assert result["ok"] is True
    assert result["p10_live_slo_claimed"] is False
    record = result["records"][0]
    assert record["timings_ms"]["source_to_posterior_commit"] == 1000.0
    assert record["timings_ms"]["posterior_commit_to_decision_start"] == 250.0
    assert record["timings_ms"]["source_to_decision_start"] == 1250.0
    assert record["materialization"]["legacy_task_queue_path"] is True
    assert record["posterior_commit_attestation"]["state"] == "present"
    assert record["materialization"]["segment_timing"] == {
        "state": "derived_from_persisted_durations",
        "reported_source_to_posterior_apply_start_ms": 900.0,
        "source_to_posterior_commit_ms": 1000.0,
        "derived_apply_start_to_posterior_commit_ms": 100.0,
    }
    assert record["execution_contract_observation_t1_gate"]["state"] == "missing"
    assert record["execution_contract_observation_t1_gate"]["reason"] == (
        "runtime_contract_missing_observation_t1_gate"
    )
    assert record["p10_live_slo"]["eligible"] is False
    assert record["p10_live_slo"]["t1_to_t5_ms"] is None


def test_audit_marks_missing_decision_timestamp_as_partial() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO event_fact_history VALUES (?,?,?,?,?,?)",
        (
            7,
            "slug",
            "2026-07-12",
            "2026-07-12T00:00:00Z",
            "2026-07-12T00:00:01Z",
            json.dumps(
                {
                    "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31",
                        "source_to_posterior_timing": {
                            "schema": "source_to_posterior_timing_v1",
                        "latest_source_response_completed_at": "2026-07-12T00:00:00Z"
                    },
                }
            ),
        ),
    )

    result = audit_v31_posterior_execution_latency(conn, latest_only=True)

    record = result["records"][0]
    assert record["evidence_state"] == "partial"
    assert record["missing_timestamps"] == [
        "posterior_commit_attestation",
        "decision_started_at",
        "gateway_attested_exchange_post_t5",
    ]
    assert record["posterior_commit_attestation"]["reason"] == "materializer_posterior_apply_completed_at_missing"


def test_audit_uses_append_only_commit_attestation_without_rewriting_fact_json() -> None:
    conn = _conn()
    payload = {
        "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31",
        "fact_snapshot_id": "fact_11",
        "posterior_run_id": "post_11",
        "source_to_posterior_timing": {
            "schema": "source_to_posterior_timing_v1",
            "latest_source_response_completed_at": "2026-07-12T00:00:00Z",
            "posterior_apply_started_at": "2026-07-12T00:00:00.900Z",
        },
    }
    conn.execute(
        "INSERT INTO event_fact_history VALUES (?,?,?,?,?,?)",
        (11, "slug", "2026-07-12", "2026-07-12T00:00:00Z", "2026-07-12T00:00:01Z", json.dumps(payload)),
    )
    result = append_posterior_materialization_commit_attestation(
        conn,
        materialized_fact_id=11,
        task_queue_id=77,
        source_fact_id=10,
        source_received_at="2026-07-12T00:00:00Z",
        posterior_apply_started_at="2026-07-12T00:00:00.900Z",
        posterior_committed_at="2026-07-12T00:00:01.000Z",
        fact_snapshot_id="fact_11",
        posterior_run_id="post_11",
    )
    assert result["state"] == "appended"

    audit = audit_v31_posterior_execution_latency(conn, latest_only=True)

    record = audit["records"][0]
    assert record["posterior_committed_at"] == "2026-07-12T00:00:01.000Z"
    assert record["posterior_commit_attestation"]["reason"] == (
        "append_only_materializer_task_to_fact_commit_attestation"
    )
    assert record["timings_ms"]["source_to_posterior_commit"] == 1000.0


def test_append_only_commit_attestation_rejects_conflicting_replay() -> None:
    conn = _conn()
    append_posterior_materialization_commit_attestation(
        conn,
        materialized_fact_id=12,
        posterior_committed_at="2026-07-12T00:00:01.000Z",
    )

    result = append_posterior_materialization_commit_attestation(
        conn,
        materialized_fact_id=12,
        posterior_committed_at="2026-07-12T00:00:02.000Z",
    )

    assert result["ok"] is False
    assert result["reason"] == "materialized_fact_commit_attestation_conflict"


def test_audit_marks_impossible_persisted_apply_start_duration_as_inconsistent() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO event_fact_history VALUES (?,?,?,?,?,?)",
        (
            8,
            "slug",
            "2026-07-12",
            "2026-07-12T00:00:00Z",
            "2026-07-12T00:00:01Z",
            json.dumps(
                    {
                        "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31",
                        "source_to_posterior_timing": {
                            "schema": "source_to_posterior_timing_v1",
                            "latest_source_response_completed_at": "2026-07-12T00:00:00Z",
                            "posterior_apply_completed_at": "2026-07-12T00:00:01Z",
                            "posterior_commit_fact_id": 8,
                            "source_response_to_posterior_apply_start_ms": 1001,
                        },
                }
            ),
        ),
    )

    result = audit_v31_posterior_execution_latency(conn, latest_only=True)

    assert result["records"][0]["materialization"]["segment_timing"]["state"] == (
        "inconsistent_reported_apply_start_after_commit"
    )


def test_audit_never_treats_later_ui_fact_or_decision_as_materializer_commit() -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO event_fact_history VALUES (?,?,?,?,?,?)",
        (
            9,
            "slug",
            "2026-07-12",
            "2026-07-12T00:30:00Z",
            "2026-07-12T00:30:00Z",
            json.dumps(
                {
                    "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31",
                    "source_to_posterior_timing": {
                        "schema": "source_to_posterior_timing_v1",
                        "latest_source_response_completed_at": "2026-07-12T00:00:00Z",
                        "posterior_apply_started_at": "2026-07-12T00:00:02Z",
                        "task_queue_id": 42,
                    },
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            10,
            "slug",
            "2026-07-12",
            "2026-07-12T00:30:01Z",
            "entry",
            "blocked",
            0,
            "[]",
            json.dumps({"fact_id": 9, "decision_started_at": "2026-07-12T00:30:01Z"}),
        ),
    )

    result = audit_v31_posterior_execution_latency(conn, latest_only=True)

    record = result["records"][0]
    assert record["posterior_committed_at"] == ""
    assert record["timings_ms"]["source_to_posterior_commit"] is None
    assert record["decision"] == {}
    assert record["posterior_commit_attestation"]["reason"] == (
        "materializer_posterior_apply_completed_at_missing"
    )


def test_audit_reads_gateway_attested_t5_but_legacy_queue_remains_ineligible() -> None:
    conn = _conn()
    conn.execute(
        "CREATE TABLE signal_to_order_timing (signal_id TEXT PRIMARY KEY, order_submitted_at TEXT, timing_json TEXT)"
    )
    payload = {
        "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31",
        "fact_snapshot_id": "fact_t5",
        "posterior_run_id": "post_t5",
        "source_to_posterior_timing": {
            "schema": "source_to_posterior_timing_v1",
            "latest_source_response_completed_at": "2026-07-12T00:00:00Z",
            "posterior_apply_completed_at": "2026-07-12T00:00:01Z",
            "posterior_commit_fact_id": 20,
            "task_queue_id": 99,
        },
    }
    conn.execute(
        "INSERT INTO event_fact_history VALUES (?,?,?,?,?,?)",
        (20, "slug", "2026-07-12", "2026-07-12T00:00:00Z", "2026-07-12T00:00:01Z", json.dumps(payload)),
    )
    details = {
        "fact_id": 20,
        "signal_id": "sig_t5",
        "decision_started_at": "2026-07-12T00:00:01.100Z",
        "sequential_bayes_execution_contract": {
            "blockers": [],
            "observation_trigger_at": "2026-07-12T00:00:00Z",
            "observation_trigger_time_source": "first_actionable_seen_at",
            "observation_to_order_hard_deadline_at": "2026-07-12T00:00:03Z",
        },
    }
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES (?,?,?,?,?,?,?,?,?)",
        (21, "slug", "2026-07-12", "2026-07-12T00:00:01.200Z", "entry", "submitted", 1, '[\"order_submitted\"]', json.dumps(details)),
    )
    t5 = "2026-07-12T00:00:02.500Z"
    conn.execute(
        "INSERT INTO signal_to_order_timing VALUES (?,?,?)",
        (
            "sig_t5",
            t5,
            json.dumps(
                {
                    "signal_id": "sig_t5",
                    "order_submitted_at": t5,
                    "exchange_post_started": True,
                    "exchange_post_started_at": t5,
                    "order_submission_time_quality": "exchange_post_started_at",
                }
            ),
        ),
    )

    record = audit_v31_posterior_execution_latency(conn, latest_only=True)["records"][0]

    assert record["live_clob_t5_evidence"]["state"] == "verified"
    assert record["p10_live_slo"]["t1_to_t5_ms"] == 2500.0
    assert record["p10_live_slo"]["eligible"] is False
    assert record["p10_live_slo"]["reason"] == "legacy_path_has_task_queue"
    assert record["p10_live_slo"]["met"] is False


def test_audit_rejects_latest_pointer_repointed_to_unattested_v31_copy() -> None:
    conn = _conn()
    conn.execute("CREATE TABLE event_fact_latest (slug TEXT,event_date TEXT,fact_id INTEGER)")
    payload = {
        "posterior_composer_schema": "sequential_remaining_warming_bayes_runtime_v31",
        "fact_snapshot_id": "fact_copy",
        "posterior_run_id": "post_copy",
        "source_to_posterior_timing": {
            "schema": "source_to_posterior_timing_v1",
            "latest_source_response_completed_at": "2026-07-12T00:00:00Z",
        },
    }
    conn.execute(
        "INSERT INTO event_fact_history VALUES (?,?,?,?,?,?)",
        (30, "slug", "2026-07-12", "2026-07-12T00:00:00Z", "2026-07-12T00:00:01Z", json.dumps(payload)),
    )
    conn.execute("INSERT INTO event_fact_latest VALUES (?,?,?)", ("slug", "2026-07-12", 30))

    audit = audit_v31_posterior_execution_latency(conn, latest_only=True)

    assert audit["ok"] is False
    assert audit["state"] == "latest_pointer_integrity_violation"
    integrity = audit["latest_pointer_integrity"]
    assert integrity["checks"][0]["fact_id"] == 30
    assert integrity["checks"][0]["attested"] is False
