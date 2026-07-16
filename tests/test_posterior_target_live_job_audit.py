import json
import sqlite3
from pathlib import Path

from scripts import posterior_target_live_job_audit as audit


SLUG = "highest-temperature-in-chengdu-on-july-3-2026"
EVENT_DATE = "2026-07-03"


def _create_db(
    path: Path,
    *,
    legacy_consumer: bool = False,
    lineage_mismatch: bool = False,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE auto_trade_jobs (
                id INTEGER PRIMARY KEY,
                source TEXT,
                state TEXT,
                payload_json TEXT,
                created_at TEXT,
                started_at TEXT,
                ended_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
                id INTEGER PRIMARY KEY,
                slug TEXT,
                event_date TEXT,
                action TEXT,
                outcome TEXT,
                side TEXT,
                decided_state TEXT,
                will_order INTEGER,
                reason_codes_json TEXT,
                details_json TEXT,
                created_at TEXT
            );
            """
        )
        decision_ssot = "posterior_temperature_distribution" if legacy_consumer else "posterior_ssot_fact"
        consumer_fact_snapshot_id = "fact_other" if lineage_mismatch else "fact_unit"
        consumer_posterior_run_id = "post_other" if lineage_mismatch else "post_unit"
        ssot_fact = {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "fact_snapshot_id": "fact_unit",
            "posterior_run_id": "post_unit",
            "target_bucket": "31°C",
            "target_probability": 0.66,
            "target_probability_field": "posterior_ssot_fact.target_probability",
        }
        payload = {
            "fact_snapshot_id": "fact_unit",
            "posterior_run_id": "post_unit",
            "posterior_target_direct": {
                "target_bucket": "31°C",
                "target_posterior": 0.66,
                "decision_ssot": decision_ssot,
                "posterior_ssot_fact_used": not legacy_consumer,
                "fact_snapshot_id": consumer_fact_snapshot_id,
                "posterior_run_id": consumer_posterior_run_id,
                "target_probability_field": (
                    "posterior_temperature_distribution.bucket_probabilities"
                    if legacy_consumer
                    else "posterior_ssot_fact.target_probability"
                ),
                "station_id": "ZUUU",
            },
            "entry_expected_targets": [
                {
                    "outcome": "31°C",
                    "side": "YES",
                    "decision_ssot": decision_ssot,
                    "posterior_ssot_fact_used": not legacy_consumer,
                    "fact_snapshot_id": consumer_fact_snapshot_id,
                    "posterior_run_id": consumer_posterior_run_id,
                    "target_probability": 0.66,
                    "target_probability_field": (
                        "posterior_temperature_distribution.bucket_probabilities"
                        if legacy_consumer
                        else "posterior_ssot_fact.target_probability"
                    ),
                    "posterior_target_direct": True,
                    "source_usable_for_settlement": True,
                    "rules_contract_ok": True,
                    "settlement_station": "ZUUU",
                }
            ],
            "event_payloads": [
                {
                    "payload": {
                        "slug": SLUG,
                        "event_date": EVENT_DATE,
                        "posterior_ssot_fact": ssot_fact,
                    }
                }
            ],
        }
        order_details = {
            "decision_ssot": decision_ssot,
            "posterior_ssot_fact_used": not legacy_consumer,
            "target_bucket": "31°C",
            "target_posterior": 0.66,
            "fact_snapshot_id": consumer_fact_snapshot_id,
            "posterior_run_id": consumer_posterior_run_id,
            "target_probability_field": (
                "posterior_temperature_distribution.bucket_probabilities"
                if legacy_consumer
                else "posterior_ssot_fact.target_probability"
            ),
            "posterior_target_job_enqueue_to_order_ms": 1200,
            "posterior_target_worker_to_order_ms": 900,
            "order_submit_ms": 400,
        }
        conn.execute(
            """
            INSERT INTO auto_trade_jobs(source, state, payload_json, created_at, started_at, ended_at)
            VALUES('posterior_target_direct', 'success', ?, ?, ?, ?)
            """,
            (
                json.dumps(payload),
                "2026-07-03T09:05:38+00:00",
                "2026-07-03T09:05:39+00:00",
                "2026-07-03T09:05:40+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
                slug, event_date, action, outcome, side, decided_state, will_order,
                reason_codes_json, details_json, created_at
            ) VALUES(?, ?, 'entry', '31°C', 'YES', 'entry', 1, ?, ?, ?)
            """,
            (
                SLUG,
                EVENT_DATE,
                json.dumps(["order_submitted"]),
                json.dumps(order_details),
                "2026-07-03T09:05:40+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_posterior_target_live_job_audit_proves_posterior_ssot_decision_chain(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_db(db_path)

    result = audit.audit(
        db_path,
        slug=SLUG,
        event_date=EVENT_DATE,
        since="2026-07-03T00:00:00+00:00",
        require_job=True,
    )

    assert result["ok"] is True
    proof = result["details"]["posterior_ssot_decision_proof"]
    assert proof["status"] == "posterior_ssot_fact_present"
    assert proof["fact_snapshot_id"] == "fact_unit"
    assert proof["posterior_run_id"] == "post_unit"
    assert proof["target_bucket"] == "31°C"
    assert result["checks"]["posterior_ssot_fact_decision_chain_ok"] is True
    assert {row["name"] for row in proof["consumer_results"]} == {
        "job.posterior_target_direct",
        "job.entry_expected_targets[0]",
        "auto_trade_decisions.details_json",
    }
    assert all(row["fact_snapshot_id"] == "fact_unit" for row in proof["consumer_results"])
    assert all(row["posterior_run_id"] == "post_unit" for row in proof["consumer_results"])


def test_posterior_target_live_job_audit_blocks_legacy_consumer_when_ssot_fact_exists(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_db(db_path, legacy_consumer=True)

    result = audit.audit(
        db_path,
        slug=SLUG,
        event_date=EVENT_DATE,
        since="2026-07-03T00:00:00+00:00",
        require_job=True,
    )

    assert result["ok"] is False
    assert "posterior_ssot_fact_decision_chain_ok" in result["blockers"]
    proof = result["details"]["posterior_ssot_decision_proof"]
    assert proof["status"] == "posterior_ssot_fact_present"
    assert proof["issues"]
    assert all(
        "consumer_not_marked_posterior_ssot_fact" in row["issues"]
        for row in proof["issues"]
    )
    assert all(
        "consumer_posterior_ssot_fact_used_missing" in row["issues"]
        for row in proof["issues"]
    )


def test_posterior_target_live_job_audit_blocks_lineage_mismatch_when_ssot_fact_exists(tmp_path):
    db_path = tmp_path / "archive.db"
    _create_db(db_path, lineage_mismatch=True)

    result = audit.audit(
        db_path,
        slug=SLUG,
        event_date=EVENT_DATE,
        since="2026-07-03T00:00:00+00:00",
        require_job=True,
    )

    assert result["ok"] is False
    assert "posterior_ssot_fact_decision_chain_ok" in result["blockers"]
    proof = result["details"]["posterior_ssot_decision_proof"]
    assert proof["fact_snapshot_id"] == "fact_unit"
    assert proof["posterior_run_id"] == "post_unit"
    assert all(
        "consumer_fact_snapshot_id_mismatch" in row["issues"]
        for row in proof["issues"]
    )
    assert all(
        "consumer_posterior_run_id_mismatch" in row["issues"]
        for row in proof["issues"]
    )
