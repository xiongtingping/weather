from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.build_settlement_calibration_labels import (
    APPLY_CONFIRM_PHRASE,
    apply_label_plan,
    build_label_plan,
)


def _setup_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE event_runs (
          id INTEGER PRIMARY KEY,
          slug TEXT,
          event_date TEXT,
          settlement_actual REAL,
          market_closed INTEGER,
	          event_end_utc TEXT,
	          resolution_source TEXT DEFAULT 'https://www.wunderground.com/history/daily/cn/chengdu/ZUUU',
	          station_id TEXT DEFAULT 'ZUUU',
	          rounding_rule TEXT DEFAULT 'whole_degrees',
	          market_unit TEXT DEFAULT 'C',
	          created_at TEXT
	        );
        CREATE TABLE auto_trade_decisions (
          id INTEGER PRIMARY KEY,
          slug TEXT,
          event_date TEXT,
          title TEXT,
          outcome TEXT,
          side TEXT,
          action TEXT,
          details_json TEXT,
          created_at TEXT
        );
        CREATE TABLE calibration_samples (
          id INTEGER PRIMARY KEY,
          decision_id INTEGER
        );
        CREATE TABLE calibration_labels (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sample_id INTEGER,
          decision_id INTEGER UNIQUE,
          slug TEXT,
          event_date TEXT,
          outcome TEXT,
          side TEXT,
          label INTEGER,
          label_source TEXT,
          realized_pnl REAL,
          path_hit INTEGER,
          path_timeout INTEGER,
          path_adverse INTEGER,
          state TEXT,
          resolved_at TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE event_fact_history (
          id INTEGER PRIMARY KEY,
          slug TEXT NOT NULL,
          event_date TEXT NOT NULL,
          fact_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE event_fact_latest (
          slug TEXT NOT NULL,
          event_date TEXT NOT NULL,
          fact_id INTEGER NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY(slug, event_date)
        );
        """
    )
    return conn


def _insert_awc_fact(conn, fact_id: int, slug: str, event_date: str, today_max_c: float) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "weather_likelihood_features": {
            "zuuu_metar_raw_awc_today_max_c": today_max_c,
            "zuuu_metar_raw_awc_target_event_date": event_date,
            "zuuu_metar_raw_awc_date_mismatch": False,
        }
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(id, slug, event_date, fact_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (fact_id, slug, event_date, json.dumps(payload), now),
    )
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (slug, event_date, fact_id, now),
    )


def test_build_label_plan_uses_settlement_actual_and_bayes_filter(tmp_path):
    db_path = tmp_path / "archive.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settled_date = "2026-06-01"
    conn = _setup_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_runs(id, slug, event_date, settlement_actual, market_closed, event_end_utc, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "highest-temperature-in-chengdu-on-june-1-2026", settled_date, 33.0, 1, None, now),
        )
        rows = [
            (10, "33°C", "YES", True),
            (11, "33°C", "NO", True),
            (12, "34°C", "YES", True),
            (13, "33°C", "YES", False),
        ]
        for decision_id, outcome, side, bayes_enabled in rows:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions
                  (id, slug, event_date, title, outcome, side, action, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'entry', ?, ?)
                """,
                (
                    decision_id,
                    "highest-temperature-in-chengdu-on-june-1-2026",
                    settled_date,
                    "Chengdu high temperature",
                    outcome,
                    side,
                    json.dumps({"q_bayes_enabled": bayes_enabled}),
                    now,
                ),
            )
        conn.execute("INSERT INTO calibration_samples(id, decision_id) VALUES (?, ?)", (100, 10))
        conn.commit()
    finally:
        conn.close()

    report = build_label_plan(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 3
    labels = {item["decision_id"]: item for item in report["labels"]}
    assert labels[10]["label"] == 1
    assert labels[11]["label"] == 0
    assert labels[12]["label"] == 0
    assert 13 not in labels
    assert report["label_counts"] == {"1": 1, "0": 2}


def test_apply_label_plan_inserts_missing_labels_idempotently(tmp_path):
    db_path = tmp_path / "archive.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settled_date = "2026-06-01"
    conn = _setup_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_runs(id, slug, event_date, settlement_actual, market_closed, event_end_utc, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "highest-temperature-in-chengdu-on-june-1-2026", settled_date, 33.0, 1, None, now),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, slug, event_date, title, outcome, side, action, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'entry', ?, ?)
            """,
            (
                10,
                "highest-temperature-in-chengdu-on-june-1-2026",
                settled_date,
                "Chengdu high temperature",
                "33°C",
                "YES",
                json.dumps({"q_bayes_enabled": True}),
                now,
            ),
        )
        conn.execute("INSERT INTO calibration_samples(id, decision_id) VALUES (?, ?)", (100, 10))
        conn.commit()
    finally:
        conn.close()

    report = build_label_plan(db_path, since_days=30, city="chengdu")
    first = apply_label_plan(db_path, report["labels"])
    second = apply_label_plan(db_path, report["labels"])

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT sample_id, decision_id, label, label_source, path_hit, path_adverse, state FROM calibration_labels"
        ).fetchall()
    finally:
        conn.close()

    assert first == {"inserted": 1, "skipped_existing": 0}
    assert second == {"inserted": 0, "skipped_existing": 1}
    assert rows == [(100, 10, 1, "settlement_actual_bucket", 1, 0, "settled")]


def _seed_single_ready_label_db(db_path):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settled_date = "2026-06-01"
    conn = _setup_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_runs(id, slug, event_date, settlement_actual, market_closed, event_end_utc, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "highest-temperature-in-chengdu-on-june-1-2026", settled_date, 33.0, 1, None, now),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, slug, event_date, title, outcome, side, action, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'entry', ?, ?)
            """,
            (
                10,
                "highest-temperature-in-chengdu-on-june-1-2026",
                settled_date,
                "Chengdu high temperature",
                "33°C",
                "YES",
                json.dumps({"q_bayes_enabled": True}),
                now,
            ),
        )
        conn.execute("INSERT INTO calibration_samples(id, decision_id) VALUES (?, ?)", (100, 10))
        conn.commit()
    finally:
        conn.close()


def test_cli_apply_requires_explicit_confirmation(tmp_path):
    db_path = tmp_path / "archive.db"
    _seed_single_ready_label_db(db_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_settlement_calibration_labels.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(db_path),
            "--since-days",
            "30",
            "--city",
            "chengdu",
            "--apply",
            "--json",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        text=True,
        capture_output=True,
        check=False,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM calibration_labels").fetchone()[0]
    finally:
        conn.close()

    payload = json.loads(result.stdout)
    assert result.returncode == 2
    assert payload["apply_result"]["block_reason"] == "missing_apply_confirmation"
    assert count == 0


def test_cli_apply_with_confirmation_inserts_labels(tmp_path):
    db_path = tmp_path / "archive.db"
    _seed_single_ready_label_db(db_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_settlement_calibration_labels.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(db_path),
            "--since-days",
            "30",
            "--city",
            "chengdu",
            "--apply",
            "--confirm-apply",
            APPLY_CONFIRM_PHRASE,
            "--backup-dir",
            str(tmp_path / "backups"),
            "--json",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        text=True,
        capture_output=True,
        check=False,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT decision_id, label FROM calibration_labels").fetchall()
    finally:
        conn.close()

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["pre_apply_backup"]["created"] is True
    assert payload["apply_result"] == {"inserted": 1, "skipped_existing": 0}
    assert rows == [(10, 1)]


def test_cli_apply_with_confirmation_blocks_when_backup_disabled(tmp_path):
    db_path = tmp_path / "archive.db"
    _seed_single_ready_label_db(db_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_settlement_calibration_labels.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db",
            str(db_path),
            "--since-days",
            "30",
            "--city",
            "chengdu",
            "--apply",
            "--confirm-apply",
            APPLY_CONFIRM_PHRASE,
            "--no-backup-before-apply",
            "--json",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        text=True,
        capture_output=True,
        check=False,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM calibration_labels").fetchone()[0]
    finally:
        conn.close()

    payload = json.loads(result.stdout)
    assert result.returncode == 2
    assert payload["pre_apply_backup"]["reason"] == "disabled_by_no_backup_before_apply"
    assert payload["apply_result"]["block_reason"] == "pre_apply_backup_not_created"
    assert count == 0


def test_build_label_plan_skips_open_future_settlements_by_default(tmp_path):
    db_path = tmp_path / "archive.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _setup_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_runs(id, slug, event_date, settlement_actual, market_closed, event_end_utc, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "highest-temperature-in-chengdu-on-june-1-2026", "2026-06-01", 33.0, 0, None, now),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, slug, event_date, title, outcome, side, action, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'entry', ?, ?)
            """,
            (
                10,
                "highest-temperature-in-chengdu-on-june-1-2026",
                "2026-06-01",
                "Chengdu high temperature",
                "33°C",
                "YES",
                json.dumps({"q_bayes_enabled": True}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    safe_report = build_label_plan(db_path, since_days=30, city="chengdu")
    unsafe_report = build_label_plan(
        db_path,
        since_days=30,
        city="chengdu",
        allow_open_events=True,
    )

    assert safe_report["planned_labels"] == 0
    assert safe_report["settlement_skipped"] == {"settlement_not_closed": 1}
    assert safe_report["skipped"] == {"settlement_missing": 1}
    assert unsafe_report["planned_labels"] == 1


def test_build_label_plan_skips_slug_event_date_mismatch(tmp_path):
    db_path = tmp_path / "archive.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _setup_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_runs(id, slug, event_date, settlement_actual, market_closed, event_end_utc, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, "highest-temperature-in-chengdu-on-may-31-2026", "2026-05-30", 29.9, 1, None, now),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, slug, event_date, title, outcome, side, action, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'entry', ?, ?)
            """,
            (
                10,
                "highest-temperature-in-chengdu-on-may-31-2026",
                "2026-05-30",
                "Highest temperature in Chengdu on May 31?",
                "30°C",
                "YES",
                json.dumps({"q_bayes_enabled": True}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_label_plan(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 0
    assert report["skipped"] == {"event_date_mismatch": 1}


def test_build_label_plan_requires_strict_resolution_source(tmp_path):
    db_path = tmp_path / "archive.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settled_date = "2026-06-01"
    conn = _setup_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_runs(
              id, slug, event_date, settlement_actual, market_closed, event_end_utc,
              resolution_source, station_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "highest-temperature-in-chengdu-on-june-1-2026",
                settled_date,
                33.0,
                1,
                None,
                "https://www.wunderground.com/history/daily/cn/chengdu/ZSPD",
                "ZUUU",
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, slug, event_date, title, outcome, side, action, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'entry', ?, ?)
            """,
            (
                10,
                "highest-temperature-in-chengdu-on-june-1-2026",
                settled_date,
                "Chengdu high temperature",
                "33°C",
                "YES",
                json.dumps({"q_bayes_enabled": True}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    strict_report = build_label_plan(db_path, since_days=30, city="chengdu")
    unsafe_report = build_label_plan(
        db_path,
        since_days=30,
        city="chengdu",
        strict_resolution_source=False,
    )

    assert strict_report["planned_labels"] == 0
    assert strict_report["settlement_skipped"] == {"resolution_station_mismatch": 1}
    assert strict_report["skipped"] == {"settlement_missing": 1}
    assert unsafe_report["planned_labels"] == 1


def test_build_label_plan_skips_reverse_fahrenheit_boundary_settlements(tmp_path):
    db_path = tmp_path / "archive.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settled_date = "2026-06-22"
    slug = "highest-temperature-in-chengdu-on-june-22-2026"
    conn = _setup_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_runs(
              id, slug, event_date, settlement_actual, market_closed, event_end_utc,
              resolution_source, station_id, rounding_rule, market_unit, created_at
            )
            VALUES (?, ?, ?, ?, 1, NULL, ?, 'ZUUU', 'whole_degrees', 'C', ?)
            """,
            (
                1,
                slug,
                settled_date,
                32.7777777778,
                "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, slug, event_date, title, outcome, side, action, details_json, created_at)
            VALUES (?, ?, ?, ?, '33°C', 'YES', 'entry', ?, ?)
            """,
            (
                10,
                slug,
                settled_date,
                "Chengdu high temperature",
                json.dumps({"q_bayes_enabled": True}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_label_plan(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 0
    assert report["settlement_skipped"] == {"settlement_unit_boundary_ambiguous": 1}
    assert report["skipped"] == {"settlement_missing": 1}


def test_build_label_plan_skips_settlement_below_awc_hard_observation(tmp_path):
    db_path = tmp_path / "archive.db"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    settled_date = "2026-06-23"
    slug = "highest-temperature-in-chengdu-on-june-23-2026"
    conn = _setup_db(db_path)
    try:
        conn.execute(
            """
            INSERT INTO event_runs(
              id, slug, event_date, settlement_actual, market_closed, event_end_utc,
              resolution_source, station_id, rounding_rule, market_unit, created_at
            )
            VALUES (?, ?, ?, ?, 1, NULL, ?, 'ZUUU', 'whole_degrees', 'C', ?)
            """,
            (
                1,
                slug,
                settled_date,
                27.5,
                "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                now,
            ),
        )
        _insert_awc_fact(conn, 100, slug, settled_date, 30.0)
        conn.execute(
            """
            INSERT INTO auto_trade_decisions
              (id, slug, event_date, title, outcome, side, action, details_json, created_at)
            VALUES (?, ?, ?, ?, '30°C', 'YES', 'entry', ?, ?)
            """,
            (
                10,
                slug,
                settled_date,
                "Chengdu high temperature",
                json.dumps({"q_bayes_enabled": True}),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = build_label_plan(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 0
    assert report["settlement_skipped"] == {"settlement_below_awc_hard_observation": 1}
    assert report["skipped"] == {"settlement_missing": 1}
