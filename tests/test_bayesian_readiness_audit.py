from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from scripts.bayesian_readiness_audit import build_report


def test_bayesian_readiness_audit_reports_ok_for_fully_covered_sample(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              decision_id INTEGER PRIMARY KEY,
              label INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs
              (id, run_date, event_date, slug, title, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-31",
                "2026-06-01",
                "chengdu-high-temperature-on-june-1",
                "Chengdu high temperature on June 1",
                json.dumps(
                    {
                        "rows": [
                            {
                                "q_bayes_enabled": True,
                                "q_raw_static": 0.22,
                                "q_bayes_posterior": 0.25,
                            },
                            {
                                "q_bayes_enabled": True,
                                "q_raw_static": 0.78,
                                "q_bayes_posterior": 0.75,
                            },
                        ]
                    }
                ),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, run_id, run_date, slug, event_date, title, outcome, side,
               action, will_order, reason_codes_json, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                10,
                1,
                "2026-05-31",
                "chengdu-high-temperature-on-june-1",
                "2026-06-01",
                "Chengdu high temperature on June 1",
                "33C",
                "YES",
                "entry",
                1,
                "[]",
                json.dumps(
                    {
                        "q_bayes_enabled": True,
                        "q_bayes_source": "dirichlet_bma",
                        "q_raw_static": 0.22,
                        "q_bayes_posterior": 0.25,
                        "q_bayes_lcb": 0.18,
                    }
                ),
                now,
            ),
        )
        conn.execute(
            "INSERT INTO calibration_labels (decision_id, label) VALUES (?, ?)",
            (10, 1),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=14, city="chengdu")

    assert report["ok"] is True
    assert report["blockers"] == []
    assert report["warnings"] == []
    assert report["readiness"]["event_run_bayes_status"] == "ok"
    assert report["readiness"]["decision_bayes_status"] == "ok"
    assert report["readiness"]["calibration_status"] == "ok"
    assert report["event_runs"]["bayes_bucket_coverage"] == 1.0
    assert report["decisions"]["bayes_coverage"] == 1.0
    assert report["decisions"]["calibration_label_coverage"] == 1.0
    assert report["decisions"]["sources"] == {"dirichlet_bma": 1}
    assert report["decisions"]["reliability_bins"] == [
        {"bin": "0.2-0.3", "n": 1, "wins": 1, "observed_rate": 1.0}
    ]
    assert report["settlement_label_plan"]["planned_labels"] == 0
    assert report["readiness"]["pending_label_status"] == "clear"
    assert report["readiness"]["paper_trading_status"] == "has_order_intents"
    assert report["decisions"]["raw_will_order_rows"] == 1
    assert report["decisions"]["raw_bayesian_will_order_rows"] == 1


def test_bayesian_readiness_counts_raw_bayesian_order_intents_when_latest_blocks(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              decision_id INTEGER PRIMARY KEY,
              label INTEGER
            );
            """
        )
        for decision_id, will_order, created_at in (
            (10, 1, "2026-05-31T05:00:00+00:00"),
            (11, 0, "2026-05-31T06:00:00+00:00"),
        ):
            conn.execute(
                """
                INSERT INTO auto_trade_decisions
                  (id, run_id, run_date, slug, event_date, title, outcome, side,
                   action, will_order, reason_codes_json, details_json, created_at)
                VALUES (?, 1, '2026-05-31', 'chengdu-high-temperature-on-june-1',
                        '2026-06-01', 'Chengdu high temperature', '33C', 'YES',
                        'entry', ?, '[]', ?, ?)
                """,
                (
                    decision_id,
                    will_order,
                    json.dumps(
                        {
                            "q_bayes_enabled": True,
                            "q_bayes_source": "dirichlet_bma",
                            "q_raw_static": 0.22,
                            "q_bayes_posterior": 0.25,
                            "q_bayes_lcb": 0.18,
                        }
                    ),
                    created_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=14, city="chengdu")

    assert report["decisions"]["will_order_rows"] == 0
    assert report["decisions"]["raw_will_order_rows"] == 1
    assert report["decisions"]["raw_bayesian_will_order_rows"] == 1
    assert report["readiness"]["paper_trading_status"] == "has_order_intents"


def test_bayesian_readiness_audit_surfaces_pending_settlement_labels(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              settlement_actual REAL,
              market_closed INTEGER,
              event_end_utc TEXT,
              resolution_source TEXT,
              station_id TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              id INTEGER PRIMARY KEY,
              decision_id INTEGER UNIQUE,
              label INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs
              (id, run_date, event_date, slug, title, payload_json,
               settlement_actual, market_closed, event_end_utc, resolution_source, station_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-31",
                "2026-05-01",
                "chengdu-high-temperature-on-may-1",
                "Chengdu high temperature on May 1",
                json.dumps(
                    {
                        "rows": [
                            {
                                "q_bayes_enabled": True,
                                "q_raw_static": 0.22,
                                "q_bayes_posterior": 0.25,
                            }
                        ]
                    }
                ),
                33.0,
                1,
                None,
                "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                "ZUUU",
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, run_id, run_date, slug, event_date, title, outcome, side,
               action, will_order, reason_codes_json, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                10,
                1,
                "2026-05-31",
                "chengdu-high-temperature-on-may-1",
                "2026-05-01",
                "Chengdu high temperature on May 1",
                "33C",
                "YES",
                "entry",
                0,
                "[]",
                json.dumps(
                    {
                        "q_bayes_enabled": True,
                        "q_bayes_source": "dirichlet_bma",
                        "q_raw_static": 0.22,
                        "q_bayes_posterior": 0.25,
                        "q_bayes_lcb": 0.18,
                    }
                ),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=14, city="chengdu")

    assert report["settlement_label_plan"]["planned_labels"] == 1
    assert report["settlement_label_plan"]["label_counts"] == {"1": 1}
    assert report["settlement_label_plan"]["examples"][0]["decision_id"] == 10
    assert report["readiness"]["pending_label_status"] == "pending_apply"
    assert "calibration_labels_pending_apply" in report["warnings"]


def test_bayesian_readiness_counts_all_pending_labels_but_limits_examples(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              settlement_actual REAL,
              market_closed INTEGER,
              event_end_utc TEXT,
              resolution_source TEXT,
              station_id TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              id INTEGER PRIMARY KEY,
              decision_id INTEGER UNIQUE,
              label INTEGER
            );
            """
        )
        slug = "highest-temperature-in-chengdu-on-may-1-2026"
        conn.execute(
            """
            INSERT INTO event_runs
              (id, run_date, event_date, slug, title, payload_json,
               settlement_actual, market_closed, event_end_utc, resolution_source, station_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-01",
                "2026-05-01",
                slug,
                "Highest temperature in Chengdu on May 1?",
                json.dumps({"rows": [{"q_bayes_enabled": True}]}),
                33.0,
                1,
                None,
                "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                "ZUUU",
                now,
            ),
        )
        for idx in range(30):
            conn.execute(
                """
                INSERT INTO auto_trade_decisions
                  (id, run_id, run_date, slug, event_date, title, outcome, side,
                   action, will_order, reason_codes_json, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    100 + idx,
                    1,
                    "2026-05-01",
                    slug,
                    "2026-05-01",
                    "Highest temperature in Chengdu on May 1?",
                    "33°C",
                    "YES" if idx % 2 == 0 else "NO",
                    "entry",
                    0,
                    "[]",
                    json.dumps(
                        {
                            "q_bayes_enabled": True,
                            "q_bayes_source": "dirichlet_bma",
                            "q_raw_static": 0.22,
                            "q_bayes_posterior": 0.25,
                            "q_bayes_lcb": 0.18,
                        }
                    ),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=60, city="chengdu")

    assert report["settlement_label_plan"]["planned_labels"] == 30
    assert len(report["settlement_label_plan"]["examples"]) == 8


def test_bayesian_readiness_excludes_observed_source_rows_from_decision_coverage(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              decision_id INTEGER PRIMARY KEY,
              label INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs
              (id, run_date, event_date, slug, title, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-31",
                "2026-06-01",
                "chengdu-high-temperature-on-june-1",
                "Chengdu high temperature on June 1",
                json.dumps(
                    {
                        "rows": [
                            {
                                "q_bayes_enabled": True,
                                "q_raw_static": 0.22,
                                "q_bayes_posterior": 0.25,
                            }
                        ]
                    }
                ),
                now,
            ),
        )
        rows = [
            (
                10,
                "chengdu-high-temperature-on-june-1",
                "2026-06-01",
                {
                    "q_bayes_enabled": True,
                    "q_bayes_source": "dirichlet_bma",
                    "q_raw_static": 0.22,
                    "q_bayes_posterior": 0.25,
                    "q_bayes_lcb": 0.18,
                },
            ),
            (
                11,
                "chengdu-high-temperature-on-may-31",
                "2026-05-31",
                {
                    "source_kind": "wunderground_history",
                    "observed_nowcast_source_trust_tier": "settlement_source",
                    "q_raw_static": 0.95,
                },
            ),
        ]
        for decision_id, slug, event_date, details in rows:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions
                  (id, run_id, run_date, slug, event_date, title, outcome, side,
                   action, will_order, reason_codes_json, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    1,
                    "2026-05-31",
                    slug,
                    event_date,
                    "Chengdu high temperature",
                    "33C",
                    "YES",
                    "entry",
                    0,
                    "[]",
                    json.dumps(details),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=14, city="chengdu")

    assert report["readiness"]["decision_bayes_status"] == "ok"
    assert "decision_bayes_coverage_low" not in report["warnings"]
    assert report["decisions"]["bayes_coverage"] == 1.0
    assert report["decisions"]["raw_bayes_coverage"] == 0.5
    assert report["decisions"]["bayes_not_applicable_observed_rows"] == 1


def test_bayesian_readiness_excludes_observed_source_event_runs_from_current_coverage(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              decision_id INTEGER PRIMARY KEY,
              label INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs
              (id, run_date, event_date, slug, title, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-31",
                "2026-06-01",
                "highest-temperature-in-test-on-june-1",
                "Highest temperature in Test on June 1?",
                json.dumps(
                    {
                        "rows": [
                            {
                                "q_bayes_enabled": True,
                                "source_kind": "model_forecast",
                                "q_bayes_posterior": 0.6,
                            },
                            {
                                "source_kind": "wunderground_history",
                                "source_trust_tier": "settlement_source",
                            },
                        ]
                    }
                ),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=14, city="", market_scope="highest_temperature")

    assert report["readiness"]["event_run_bayes_status"] == "ok"
    assert report["event_runs"]["bayes_bucket_coverage"] == 1.0
    assert report["event_runs"]["latest_total_buckets"] == 2
    assert report["event_runs"]["latest_applicable_buckets"] == 1
    assert report["event_runs"]["latest_not_applicable_observed_buckets"] == 1


def test_bayesian_readiness_uses_latest_observed_event_run_for_failed_decision_applicability(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              decision_id INTEGER PRIMARY KEY,
              label INTEGER
            );
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs
              (id, run_date, event_date, slug, title, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "2026-05-31",
                "2026-05-31",
                "highest-temperature-in-test-on-may-31",
                "Highest temperature in Test on May 31?",
                json.dumps(
                    {
                        "rows": [
                            {
                                "source_kind": "wunderground_history",
                                "source_trust_tier": "settlement_source",
                            }
                        ]
                    }
                ),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, run_id, run_date, slug, event_date, title, outcome, side,
               action, will_order, reason_codes_json, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                10,
                1,
                "2026-05-31",
                "highest-temperature-in-test-on-may-31",
                "2026-05-31",
                "Highest temperature in Test on May 31?",
                "33C",
                "YES",
                "entry",
                0,
                "[]",
                json.dumps({"entry_usable_edge_na_reason": "event_worker_failed"}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=14, city="", market_scope="highest_temperature")

    assert report["readiness"]["decision_bayes_status"] == "missing"
    assert report["decisions"]["bayes_applicable_rows"] == 0
    assert report["decisions"]["bayes_not_applicable_observed_rows"] == 1


def test_bayesian_readiness_uses_latest_event_run_coverage_for_status(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              decision_id INTEGER PRIMARY KEY,
              label INTEGER
            );
            """
        )
        for run_id, enabled in [(1, False), (2, True)]:
            conn.execute(
                """
                INSERT INTO event_runs
                  (id, run_date, event_date, slug, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "2026-05-31",
                    "2026-06-01",
                    "chengdu-high-temperature-on-june-1",
                    "Chengdu high temperature on June 1",
                    json.dumps({"rows": [{"q_bayes_enabled": enabled}]}),
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=14, city="chengdu")

    assert report["readiness"]["event_run_bayes_status"] == "ok"
    assert "event_run_bayes_coverage_partial" not in report["warnings"]
    assert report["event_runs"]["bayes_bucket_coverage"] == 1.0
    assert report["event_runs"]["raw_bayes_bucket_coverage"] == 0.5


def test_bayesian_readiness_excludes_settled_event_runs_from_current_event_coverage(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        conn.executescript(
            """
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY,
              run_date TEXT,
              event_date TEXT,
              slug TEXT,
              title TEXT,
              payload_json TEXT,
              settlement_actual REAL,
              market_closed INTEGER,
              created_at TEXT
            );
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              run_id INTEGER,
              run_date TEXT,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              reason_codes_json TEXT,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              decision_id INTEGER PRIMARY KEY,
              label INTEGER
            );
            """
        )
        rows = [
            (1, "2026-05-30", False, 29.9),
            (2, "2026-06-01", True, None),
        ]
        for run_id, event_date, enabled, settlement_actual in rows:
            conn.execute(
                """
                INSERT INTO event_runs
                  (id, run_date, event_date, slug, title, payload_json,
                   settlement_actual, market_closed, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    "2026-05-31",
                    event_date,
                    f"chengdu-high-temperature-on-{event_date}",
                    "Chengdu high temperature",
                    json.dumps({"rows": [{"q_bayes_enabled": enabled}]}),
                    settlement_actual,
                    0,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=14, city="chengdu")

    assert report["readiness"]["event_run_bayes_status"] == "ok"
    assert report["event_runs"]["bayes_bucket_coverage"] == 1.0
    assert report["event_runs"]["raw_bayes_bucket_coverage"] == 0.5
    assert report["event_runs"]["latest_total_buckets"] == 1
