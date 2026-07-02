from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from scripts.settlement_label_eligibility_report import build_report


def _setup_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE event_runs (
          id INTEGER PRIMARY KEY,
          slug TEXT,
          event_date TEXT,
          title TEXT,
          settlement_actual REAL,
          market_closed INTEGER,
	          event_end_utc TEXT,
	          resolution_source TEXT,
	          station_id TEXT,
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
        CREATE TABLE calibration_labels (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          decision_id INTEGER UNIQUE,
          label INTEGER
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


def _insert_awc_fact(conn, fact_id: int, slug: str, event_date: str, today_max_c: float):
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


def _insert_decision(conn, decision_id: int, slug: str, event_date: str, outcome: str = "33°C"):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO auto_trade_decisions
          (id, slug, event_date, title, outcome, side, action, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, 'YES', 'entry', ?, ?)
        """,
        (
            decision_id,
            slug,
            event_date,
            f"Highest temperature in test on {event_date}",
            outcome,
            json.dumps({"q_bayes_enabled": True}),
            now,
        ),
    )


def _insert_event(
    conn,
    event_id: int,
    slug: str,
    event_date: str,
    *,
    settlement_actual=33.0,
    market_closed=1,
    resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
    station_id="ZUUU",
):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO event_runs
          (id, slug, event_date, title, settlement_actual, market_closed, event_end_utc,
           resolution_source, station_id, rounding_rule, market_unit, created_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'whole_degrees', 'C', ?)
        """,
        (
            event_id,
            slug,
            event_date,
            f"Highest temperature in test on {event_date}",
            settlement_actual,
            market_closed,
            resolution_source,
            station_id,
            now,
        ),
    )


def test_settlement_label_eligibility_reports_ready_market(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug = "highest-temperature-in-chengdu-on-may-1-2026"
    try:
        _insert_event(conn, 1, slug, "2026-05-01")
        _insert_decision(conn, 10, slug, "2026-05-01")
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 1
    assert report["status_counts"] == {"label_ready": 1}
    assert report["apply_command"] == (
        ".venv/bin/python scripts/build_settlement_calibration_labels.py --since-days 30 "
        "--market-scope highest_temperature --apply --confirm-apply APPLY_SETTLEMENT_LABELS --city chengdu"
    )
    assert report["markets"][0]["status"] == "label_ready"
    assert report["markets"][0]["label_counts"] == {"1": 1}


def test_settlement_label_eligibility_reports_closed_without_actual(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug = "highest-temperature-in-chengdu-on-may-1-2026"
    try:
        _insert_event(conn, 1, slug, "2026-05-01", settlement_actual=None, market_closed=1)
        _insert_decision(conn, 10, slug, "2026-05-01")
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 0
    assert report["status_counts"] == {"closed_without_settlement_actual": 1}
    assert report["apply_command"] == ""
    assert report["markets"][0]["status"] == "closed_without_settlement_actual"


def test_settlement_label_eligibility_reports_not_closed_with_actual(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug = "highest-temperature-in-chengdu-on-june-1-2026"
    try:
        _insert_event(conn, 1, slug, "2026-06-01", settlement_actual=33.0, market_closed=0)
        _insert_decision(conn, 10, slug, "2026-06-01")
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 0
    assert report["status_counts"] == {"settlement_not_closed": 1}


def test_settlement_label_eligibility_reports_station_mismatch(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug = "highest-temperature-in-chengdu-on-may-1-2026"
    try:
        _insert_event(
            conn,
            1,
            slug,
            "2026-05-01",
            resolution_source="https://www.wunderground.com/history/daily/cn/chengdu/ZSPD",
            station_id="ZUUU",
        )
        _insert_decision(conn, 10, slug, "2026-05-01")
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 0
    assert report["status_counts"] == {"resolution_station_mismatch": 1}


def test_settlement_label_eligibility_reports_boundary_ambiguous(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug = "highest-temperature-in-chengdu-on-june-22-2026"
    try:
        _insert_event(
            conn,
            1,
            slug,
            "2026-06-22",
            settlement_actual=32.7777777778,
        )
        _insert_decision(conn, 10, slug, "2026-06-22")
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 0
    assert report["status_counts"] == {"settlement_unit_boundary_ambiguous": 1}
    assert report["apply_command"] == ""
    assert report["markets"][0]["status"] == "settlement_unit_boundary_ambiguous"
    assert report["markets"][0]["settlement_actual"] == 32.7777777778


def test_settlement_label_eligibility_reports_settlement_below_awc_hard_observation(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug = "highest-temperature-in-chengdu-on-june-23-2026"
    try:
        _insert_event(
            conn,
            1,
            slug,
            "2026-06-23",
            settlement_actual=27.5,
        )
        _insert_awc_fact(conn, 100, slug, "2026-06-23", 30.0)
        _insert_decision(conn, 10, slug, "2026-06-23", outcome="30°C")
        conn.commit()
    finally:
        conn.close()

    report = build_report(db_path, since_days=30, city="chengdu")

    assert report["planned_labels"] == 0
    assert report["status_counts"] == {"settlement_below_awc_hard_observation": 1}
    assert report["apply_command"] == ""
    assert report["markets"][0]["status"] == "settlement_below_awc_hard_observation"
    assert report["markets"][0]["settlement_actual"] == 27.5
