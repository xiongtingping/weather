import json
import sqlite3
import tempfile
from pathlib import Path

from scripts import audit_hard_source_direct_ssot as audit


def _args(db_path: Path):
    return type(
        "Args",
        (),
        {
            "db": str(db_path),
            "since": "2026-06-19T00:00:00+00:00",
            "since_hours": 48.0,
            "slug": "",
            "event_date": "",
            "max_fetch_to_ssot_ms": 1000.0,
            "max_ssot_to_job_enqueue_ms": 1000.0,
            "max_job_enqueue_to_worker_ms": 5000.0,
            "max_ssot_to_order_ms": 10000.0,
            "max_job_enqueue_to_order_ms": 10000.0,
            "max_worker_to_order_ms": 5000.0,
        },
    )()


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE auto_trade_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            source TEXT,
            state TEXT,
            priority INTEGER,
            payload_json TEXT,
            lease_until TEXT,
            retry_count INTEGER,
            max_retry INTEGER,
            next_retry_at TEXT,
            last_error TEXT,
            started_at TEXT,
            ended_at TEXT,
            run_id INTEGER,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            title TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            decided_state TEXT,
            will_order INTEGER,
            reason_codes_json TEXT,
            reasons_json TEXT,
            details_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_json TEXT,
            created_at TEXT
        );
        """
    )


def _seed_valid_chain(conn: sqlite3.Connection, *, legacy_bypass: bool = False) -> None:
    slug = "highest-temperature-in-chengdu-on-june-19-2026"
    event_date = "2026-06-19"
    ssot_at = "2026-06-19T07:00:00+00:00"
    job_at = "2026-06-19T07:00:00.050000+00:00"
    worker_at = "2026-06-19T07:00:00.110000+00:00"
    order_at = "2026-06-19T07:00:00.190000+00:00"
    target = {
        "slug": slug,
        "event_date": event_date,
        "outcome": "31°C",
        "side": "YES",
        "hard_source_direct": True,
        "hard_source_posterior_override": True,
        "posterior_override": True,
        "q_bayes_posterior": 0.995,
        "hard_source_ssot_written_at": ssot_at,
        "hard_source_job_enqueued_at": job_at,
        "hard_source_worker_started_at": worker_at,
        "hard_source_ssot_to_job_enqueue_ms": 50.0,
        "hard_source_fetch_to_ssot_ms": 40.0,
        "hard_source_job_enqueue_to_worker_ms": 60.0,
    }
    payload = {
        "include_event_keys": [{"slug": slug, "event_date": event_date}],
        "entry_expected_targets": [target],
        "hard_source_direct": {
            "source": "zuuu_metar_raw_awc",
            "target_bucket": "31°C",
            "decision_semantics": "ssot_posterior_constraint",
            "posterior_override": True,
            "ssot_written_at": ssot_at,
            "job_enqueued_at": job_at,
            "worker_started_at": worker_at,
            "fetch_to_ssot_ms": 40.0,
            "ssot_to_job_enqueue_ms": 50.0,
            "job_enqueue_to_worker_ms": 60.0,
        },
        "event_payloads": [
            {
                "slug": slug,
                "event_date": event_date,
                "payload": {
                    "hard_source_direct_snapshot": True,
                    "hard_source_direct_ssot_write": True,
                    "rows": [
                        {
                            "outcome": "30°C",
                            "q_bayes_posterior": 0.0,
                            "q_posterior_observed_constraints_hard_zero": True,
                        },
                        {
                            "outcome": "31°C",
                            "q_bayes_posterior": 0.995,
                            "hard_source_direct": True,
                        },
                    ],
                },
            }
        ],
    }
    conn.execute(
        """
        INSERT INTO auto_trade_jobs(
            id, run_date, source, state, priority, payload_json, run_id,
            created_at, started_at, ended_at, updated_at
        ) VALUES(1, ?, 'hard_source_direct', 'success', 20000, ?, 99, ?, ?, ?, ?)
        """,
        ("2026-06-19", json.dumps(payload), job_at, worker_at, order_at, order_at),
    )
    entry_details = {
        "hard_source_direct": True,
        "hard_source_posterior_override": True,
        "hard_source_ssot_written_at": ssot_at,
        "hard_source_job_enqueued_at": job_at,
        "hard_source_worker_started_at": worker_at,
        "hard_source_order_submitted_at": order_at,
        "hard_source_ssot_to_order_ms": 190.0,
        "hard_source_job_enqueue_to_order_ms": 140.0,
        "hard_source_worker_to_order_ms": 80.0,
    }
    if legacy_bypass:
        entry_details["bypass_bayes"] = True
    exit_details = {
        "hard_source_direct": True,
        "hard_source_posterior_override": True,
        "hard_source_ssot_written_at": ssot_at,
        "hard_source_worker_started_at": worker_at,
        "hard_source_order_submitted_at": order_at,
        "hard_source_ssot_to_order_ms": 190.0,
        "hard_source_job_enqueue_to_order_ms": 140.0,
        "hard_source_worker_to_order_ms": 80.0,
        "exit_reason_codes": [
            "target_realization_exit_clear",
            "target_realization_switch_buy_exit_old",
        ],
    }
    rows = [
        (
            99,
            "2026-06-19",
            slug,
            event_date,
            "31°C",
            "YES",
            "entry",
            "entry",
            1,
            ["order_submitted"],
            entry_details,
            order_at,
        ),
        (
            99,
            "2026-06-19",
            slug,
            event_date,
            "30°C",
            "YES",
            "exit",
            "exit",
            1,
            ["exit_marketable_immediate_confirmed"],
            exit_details,
            order_at,
        ),
    ]
    for row in rows:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
                run_id, run_date, slug, event_date, outcome, side, action,
                decided_state, will_order, reason_codes_json, details_json, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                json.dumps(row[9]),
                json.dumps(row[10]),
                row[11],
            ),
        )
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json, created_at)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        ("2026-06-19", slug, event_date, ssot_at, json.dumps(payload["event_payloads"][0]["payload"]), ssot_at),
    )


def test_hard_source_direct_ssot_audit_accepts_single_ssot_order_chain():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "archive.db"
        conn = sqlite3.connect(db_path)
        _create_schema(conn)
        _seed_valid_chain(conn)
        conn.commit()
        conn.close()

        payload = audit.run_audit(_args(db_path))

    assert payload["ok"] is True
    assert payload["blockers"] == []
    assert payload["details"]["target"]["outcome"] == "31°C"
    assert payload["details"]["decisions"]["entry_found"] is True
    assert payload["details"]["decisions"]["exit_count"] == 1


def test_hard_source_direct_ssot_audit_rejects_legacy_bypass_semantics():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "archive.db"
        conn = sqlite3.connect(db_path)
        _create_schema(conn)
        _seed_valid_chain(conn, legacy_bypass=True)
        conn.commit()
        conn.close()

        payload = audit.run_audit(_args(db_path))

    assert payload["ok"] is False
    assert "entry_uses_legacy_bypass_semantics" in payload["blockers"]


def test_hard_source_direct_ssot_audit_rejects_sla_overrun():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "archive.db"
        conn = sqlite3.connect(db_path)
        _create_schema(conn)
        _seed_valid_chain(conn)
        conn.commit()
        conn.close()
        args = _args(db_path)
        args.max_ssot_to_order_ms = 100.0

        payload = audit.run_audit(args)

    assert payload["ok"] is False
    assert "entry:hard_source_ssot_to_order_ms_over_sla:190.000>100.000" in payload["blockers"]
    assert "exit:30°C:hard_source_ssot_to_order_ms_over_sla:190.000>100.000" in payload["blockers"]


def test_hard_source_direct_ssot_audit_ignores_legacy_job_without_ssot_semantics():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "archive.db"
        conn = sqlite3.connect(db_path)
        _create_schema(conn)
        legacy_payload = {
            "include_event_keys": [
                {
                    "slug": "highest-temperature-in-chengdu-on-june-19-2026",
                    "event_date": "2026-06-19",
                }
            ],
            "hard_source_direct": {
                "source": "zuuu_metar_raw_awc",
                "target_bucket": "29°C or below",
            },
        }
        conn.execute(
            """
            INSERT INTO auto_trade_jobs(
                id, run_date, source, state, priority, payload_json, run_id,
                created_at, started_at, ended_at, updated_at
            ) VALUES(1, ?, 'hard_source_direct', 'success', 20000, ?, 99, ?, ?, ?, ?)
            """,
            (
                "2026-06-19",
                json.dumps(legacy_payload),
                "2026-06-19T07:00:00+00:00",
                "2026-06-19T07:00:00+00:00",
                "2026-06-19T07:00:01+00:00",
                "2026-06-19T07:00:01+00:00",
            ),
        )
        conn.commit()
        conn.close()

        payload = audit.run_audit(_args(db_path))

    assert payload["ok"] is False
    assert payload["blockers"] == ["hard_source_ssot_job_missing"]
    assert payload["warnings"] == ["legacy_hard_source_job_ignored"]
    assert payload["details"]["legacy_hard_source_job_ignored"]["target_bucket"] == "29°C or below"
