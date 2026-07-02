import json
import sqlite3
from pathlib import Path

from scripts import backtest_weather_com_limited_posterior as bt


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            title TEXT,
            city TEXT,
            station_id TEXT,
            tz TEXT,
            rounding_rule TEXT,
            market_var TEXT,
            market_unit TEXT,
            settlement_actual REAL,
            payload_json TEXT,
            created_at TEXT,
            event_peak_time_utc TEXT,
            event_end_utc TEXT,
            market_closed INTEGER
        )
        """
    )
    labels = ["29°C or below", "30°C", "31°C", "32°C or higher"]
    rows = [
        ("2026-01-01", 30.0, [0.1, 0.7, 0.2, 0.0], 30.0),
        ("2026-01-02", 31.0, [0.0, 0.6, 0.3, 0.1], 31.0),
        ("2026-01-03", 29.0, [0.7, 0.2, 0.1, 0.0], 29.0),
    ]
    for day, actual, probs, weather_peak in rows:
        payload_rows = [
            {"outcome": label, "q_ensemble_qm_bucket": prob}
            for label, prob in zip(labels, probs)
        ]
        payload = {
            "rows": payload_rows,
            "weather_com_forecast_peak_c": weather_peak,
            "rounding_rule": "whole_degrees",
        }
        conn.execute(
            """
            INSERT INTO event_runs(
                run_date, event_date, slug, title, city, station_id, tz, rounding_rule,
                market_var, market_unit, settlement_actual, payload_json, created_at,
                event_peak_time_utc, event_end_utc, market_closed
            ) VALUES (?, ?, ?, 'Highest temperature in Chengdu?', 'chengdu', 'ZUUU',
                'Asia/Shanghai', 'whole_degrees', 'temperature_2m_max', 'C', NULL, ?, ?,
                ?, ?, 0)
            """,
            (
                day,
                day,
                f"highest-temperature-in-chengdu-on-{day}",
                json.dumps(payload),
                f"{day}T03:00:00+00:00",
                f"{day}T08:00:00+00:00",
                f"{day}T12:00:00Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO event_runs(
                run_date, event_date, slug, title, city, station_id, tz, rounding_rule,
                market_var, market_unit, settlement_actual, payload_json, created_at,
                event_peak_time_utc, event_end_utc, market_closed
            ) VALUES (?, ?, ?, 'Highest temperature in Chengdu?', 'chengdu', 'ZUUU',
                'Asia/Shanghai', 'whole_degrees', 'temperature_2m_max', 'C', ?, ?, ?,
                ?, ?, 1)
            """,
            (
                day,
                day,
                f"highest-temperature-in-chengdu-on-{day}",
                actual,
                json.dumps(payload),
                f"{day}T18:00:00+00:00",
                f"{day}T08:00:00+00:00",
                f"{day}T12:00:00Z",
            ),
        )
    conn.commit()
    conn.close()


def test_weather_com_limited_backtest_loads_pre_end_snapshots_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    _create_db(db_path)

    args = bt.parse_args(
        [
            "--db",
            str(db_path),
            "--end-date",
            "2026-01-03",
            "--days",
            "3",
            "--gate-min-segment-n",
            "1",
        ]
    )
    samples, meta = bt.load_samples(args)
    rows = bt.score_samples(samples, args)
    report = bt.build_report(rows, meta, args)

    assert meta["read_only"] is True
    assert len(rows) == 3
    assert {row["dte_bucket"] for row in rows} == {"D0"}
    assert report["summary"]["overall"]["weather_com_visible_n"] == 3
    assert "weather_com_limited_vs_qm" in report["summary"]["overall"]
    assert report["daily"][0]["snapshot_id"] == 1
