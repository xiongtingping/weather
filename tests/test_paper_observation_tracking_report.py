from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from scripts import paper_observation_tracking_report


def _observation_report() -> dict:
    return {
        "ok": False,
        "blockers": ["paper_validation_not_passed:toronto"],
        "paper_observation_allowed": True,
        "small_capital_candidate_ready": False,
        "pilot_scope": {"cities": ["toronto"], "secondary_only": True},
        "rows": [
            {
                "city": "toronto",
                "event_date": "2026-05-31",
                "slug": "highest-temperature-in-toronto-on-may-31-2026",
                "outcome": "24°C",
                "side": "NO",
                "candidate_strategy": "reverse_edge_buy_no_sell_yes",
                "execution_direction": "buy_no_equivalent_to_sell_yes",
                "review_action": "paper_track_reverse_edge_no_side",
                "raw_lcb_edge": 0.21,
                "risk_edge": 0.18,
            }
        ],
    }


def _init_labels(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE calibration_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                label INTEGER,
                label_source TEXT,
                state TEXT,
                resolved_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_tracking_report_uses_calibration_label(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_labels(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO calibration_labels(
                slug, event_date, outcome, side, label, label_source, state, resolved_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "highest-temperature-in-toronto-on-may-31-2026",
                "2026-05-31",
                "24°C",
                "NO",
                1,
                "settlement_actual_no_hit",
                "settlement_actual_no_hit",
                "2026-06-01T01:00:00+00:00",
                "2026-06-01T01:05:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with patch.object(
        paper_observation_tracking_report.pilot_observation_review,
        "build_report",
        return_value=_observation_report(),
    ):
        report = paper_observation_tracking_report.build_report(
            db_path,
            pilot_cities=["toronto"],
        )

    assert report["summary"]["candidate_rows"] == 1
    assert report["summary"]["resolved_rows"] == 1
    assert report["summary"]["wins"] == 1
    assert report["summary"]["hit_rate"] == 1.0
    row = report["rows"][0]
    assert row["tracking_status"] == "labeled"
    assert row["label"] == 1
    assert row["label_source"] == "settlement_actual_no_hit"


def test_tracking_report_marks_pending_without_label_or_settlement(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_labels(db_path)

    with patch.object(
        paper_observation_tracking_report.pilot_observation_review,
        "build_report",
        return_value=_observation_report(),
    ):
        report = paper_observation_tracking_report.build_report(
            db_path,
            pilot_cities=["toronto"],
        )

    assert report["summary"]["candidate_rows"] == 1
    assert report["summary"]["pending_rows"] == 1
    assert report["summary"]["resolved_rows"] == 0
    assert report["rows"][0]["tracking_status"] == "pending_settlement_label"
    assert report["next_mainline_task"] == "wait_for_settlement_labels_to_score_paper_observations"


def test_tracking_report_derives_label_from_settlement_actual(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _init_labels(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                settlement_actual REAL,
                rounding_rule TEXT,
                market_unit TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs(
                slug, event_date, settlement_actual, rounding_rule, market_unit, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "highest-temperature-in-toronto-on-may-31-2026",
                "2026-05-31",
                24.0,
                "whole_degrees",
                "C",
                "2026-06-01T01:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with patch.object(
        paper_observation_tracking_report.pilot_observation_review,
        "build_report",
        return_value=_observation_report(),
    ):
        report = paper_observation_tracking_report.build_report(
            db_path,
            pilot_cities=["toronto"],
        )

    row = report["rows"][0]
    assert row["tracking_status"] == "resolved_from_settlement"
    assert row["label"] == 0
    assert row["label_source"] == "settlement_actual_no_hit"
    assert report["summary"]["losses"] == 1


def test_attach_tracking_results_reuses_settlement_labeling_for_arbitrary_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "archive.db"
    _init_labels(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                settlement_actual REAL,
                rounding_rule TEXT,
                market_unit TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO event_runs(
                slug, event_date, settlement_actual, rounding_rule, market_unit, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "highest-temperature-in-toronto-on-may-31-2026",
                "2026-05-31",
                24.0,
                "whole_degrees",
                "C",
                "2026-06-01T01:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    tracking = paper_observation_tracking_report.attach_tracking_results(
        db_path,
        [
            {
                "city": "toronto",
                "event_date": "2026-05-31",
                "slug": "highest-temperature-in-toronto-on-may-31-2026",
                "outcome": "24°C",
                "side": "YES",
                "candidate_strategy": "d0_physical_settlement_hybrid",
            }
        ],
    )

    assert tracking["summary"]["candidate_rows"] == 1
    assert tracking["summary"]["resolved_rows"] == 1
    assert tracking["summary"]["wins"] == 1
    assert tracking["rows"][0]["tracking_status"] == "resolved_from_settlement"
