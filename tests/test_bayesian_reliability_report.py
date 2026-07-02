from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import bayesian_reliability_report


def _create_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE auto_trade_decisions (
              id INTEGER PRIMARY KEY,
              slug TEXT,
              event_date TEXT,
              title TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              will_order INTEGER,
              details_json TEXT,
              created_at TEXT
            );
            CREATE TABLE calibration_labels (
              id INTEGER PRIMARY KEY,
              decision_id INTEGER,
              label INTEGER
            );
            """
        )
        rows = [
            (1, 0.20, 0),
            (2, 0.80, 1),
            (3, 0.70, 1),
            (4, 0.10, 0),
        ]
        for decision_id, p, label in rows:
            conn.execute(
                """
                INSERT INTO auto_trade_decisions(
                  id, slug, event_date, title, outcome, side, action, will_order,
                  details_json, created_at
                ) VALUES (?, 'highest-temperature-in-test-on-june-1', '2026-06-01',
                          'Highest temperature in Test?', '33C', 'YES', 'entry', 0, ?, ?)
                """,
                (
                    decision_id,
                    json.dumps(
                        {
                            "q_bayes_enabled": True,
                            "q_bayes_posterior": p,
                            "q_bayes_prior_source": "market_history_low_sample",
                            "q_bayes_bias_correction_enabled": True,
                        }
                    ),
                    "2026-05-31T00:00:00+00:00",
                ),
            )
            conn.execute(
                "INSERT INTO calibration_labels(decision_id, label) VALUES (?, ?)",
                (decision_id, label),
            )
        conn.commit()
    finally:
        conn.close()


def test_reliability_report_computes_brier_ece_and_segments(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path)

    report = bayesian_reliability_report.build_report(
        db_path,
        since_days=3650,
        market_scope="highest_temperature",
        bins=5,
        min_samples=2,
    )

    assert report["ok"] is True
    assert report["metrics"]["n"] == 4
    assert round(report["metrics"]["brier"], 4) == 0.045
    assert report["metrics"]["ece"] is not None
    assert report["segments"]["prior_source"]["market_history_low_sample"]["metrics"]["n"] == 4
    assert report["segments"]["bias_correction_enabled"]["True"]["metrics"]["n"] == 4
    assert report["calibration_contract"]["ok"] is True
    assert report["calibration_contract"]["sample_count"] == 4


def test_reliability_report_splits_calibrated_contract_from_all_samples(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO auto_trade_decisions(
              id, slug, event_date, title, outcome, side, action, will_order,
              details_json, created_at
            ) VALUES (5, 'highest-temperature-in-test-on-june-1', '2026-06-01',
                      'Highest temperature in Test?', '34C', 'YES', 'entry', 0, ?, ?)
            """,
            (
                json.dumps(
                    {
                        "q_bayes_enabled": True,
                        "q_bayes_posterior": 0.95,
                        "q_bayes_prior_source": "current_distribution",
                        "q_bayes_bias_correction_enabled": False,
                    }
                ),
                "2026-05-31T00:00:00+00:00",
            ),
        )
        conn.execute("INSERT INTO calibration_labels(decision_id, label) VALUES (5, 0)")
        conn.commit()
    finally:
        conn.close()

    report = bayesian_reliability_report.build_report(
        db_path,
        since_days=3650,
        market_scope="highest_temperature",
        bins=5,
        min_samples=2,
    )

    assert report["metrics"]["n"] == 5
    assert report["calibration_contract"]["sample_count"] == 4
    assert report["calibration_contract"]["metrics"]["n"] == 4


def test_reliability_svg_contains_points(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path)
    report = bayesian_reliability_report.build_report(db_path, since_days=3650, bins=5)

    svg = bayesian_reliability_report.build_svg(report)

    assert "<svg" in svg
    assert "<circle" in svg
