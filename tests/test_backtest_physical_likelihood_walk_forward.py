import json
import sqlite3

from scripts.backtest_physical_likelihood_walk_forward import build_report


def _insert_event_run(
    conn: sqlite3.Connection,
    *,
    row_id: int,
    created_at: str,
    settlement_actual: float | None = None,
    market_closed: int = 0,
) -> None:
    payload = {
        "rows": [
            {
                "outcome": "29°C",
                "q_raw_static": 0.9,
                "q_bayes_posterior": 0.1,
                "q_bayes_physical_likelihood_enabled": True,
                "q_bayes_physical_likelihood_prob": 0.2,
                "q_bayes_physical_likelihood_evidence": 12.0,
                "q_bayes_prior_strength": 12.0,
                "q_bayes_model_evidence": 36.0,
                "q_bayes_historical_bucket_count": 4,
                "q_bayes_bias_correction_enabled": True,
                "q_bayes_model_weight_multipliers": {"gfs": 0.95},
            },
            {
                "outcome": "30°C",
                "q_raw_static": 0.1,
                "q_bayes_posterior": 0.9,
                "q_bayes_physical_likelihood_enabled": True,
                "q_bayes_physical_likelihood_prob": 0.8,
                "q_bayes_physical_likelihood_evidence": 12.0,
                "q_bayes_prior_strength": 12.0,
                "q_bayes_model_evidence": 36.0,
                "q_bayes_historical_bucket_count": 8,
                "q_bayes_bias_correction_enabled": True,
                "q_bayes_model_weight_multipliers": {"gfs": 0.95},
            },
        ]
    }
    conn.execute(
        """
        INSERT INTO event_runs(
            id, slug, event_date, created_at, event_end_utc, market_var,
            market_unit, settlement_actual, market_closed, payload_json, tz, lon,
            resolution_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            "highest-temperature-in-chengdu-on-june-1-2026",
            "2026-06-01",
            created_at,
            "2026-06-01T12:00:00+00:00",
            "temperature_2m_max",
            "C",
            settlement_actual,
            market_closed,
            json.dumps(payload),
            "UTC",
            103.945966,
            "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
        ),
    )


def test_walk_forward_excludes_leaky_snapshots(tmp_path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE event_runs(
            id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            created_at TEXT,
            event_end_utc TEXT,
            market_var TEXT,
            market_unit TEXT,
            settlement_actual REAL,
            market_closed INTEGER,
            payload_json TEXT,
            tz TEXT,
            lon REAL,
            resolution_source TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_daily_temps(
            event_date TEXT,
            resolution_source TEXT,
            market_unit TEXT,
            settlement_actual REAL,
            created_at TEXT,
            market_var TEXT
        )
        """
    )
    _insert_event_run(conn, row_id=1, created_at="2026-06-01T06:00:00+00:00")
    _insert_event_run(conn, row_id=2, created_at="2026-06-01T07:00:00+00:00", settlement_actual=30.0)
    _insert_event_run(conn, row_id=3, created_at="2026-06-01T12:30:00+00:00")
    conn.execute(
        """
        INSERT INTO event_daily_temps(
            event_date, resolution_source, market_unit, settlement_actual, created_at, market_var
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-06-01",
            "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
            "C",
            30.0,
            "2026-06-02T00:00:00+00:00",
            "temperature_2m_max",
        ),
    )
    conn.commit()
    conn.close()

    report = build_report(
        db_path=db,
        days=20,
        include_current_day=True,
        baseline_key="q_raw_static",
        posterior_key="q_bayes_posterior",
        peak_hour=16.0,
        thin_minutes=0.0,
        min_samples=1,
        min_market_days=1,
        min_brier_or_logloss_improvement=0.10,
        min_mae_improvement=0.10,
        min_direction_lift_pp=0.0,
    )

    assert report["ok"] is True
    assert report["sample_count"] == 1
    assert report["market_day_count"] == 1
    assert report["diagnostics"]["skipped_counts"]["snapshot_row_has_settlement_actual"] == 1
    assert report["diagnostics"]["skipped_counts"]["snapshot_at_or_after_event_end"] == 1
    assert report["sample_events"][0]["time_bucket"] == "0_to_2h"
    drag = report["probability_drag_diagnostics"]
    assert drag["physical_likelihood"]["enabled_samples"] == 1
    assert drag["physical_likelihood"]["hit_prob"]["median"] == 0.8
    assert drag["historical_prior"]["posterior_pred_count_gt_hit_count"] == 0
    assert drag["bias_correction"]["enabled_samples"] == 1
