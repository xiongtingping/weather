import json
import sqlite3
from pathlib import Path

from scripts import metar_strong_likelihood_contract_audit as audit


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE event_fact_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            fact_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest(
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            updated_at TEXT,
            PRIMARY KEY(slug, event_date)
        )
        """
    )


def _insert_fact(conn: sqlite3.Connection, payload: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, fact_json)
        VALUES('2026-07-06', 'highest-temperature-in-chengdu-on-july-6-2026', '2026-07-06',
               '2026-07-05T04:00:00+00:00', ?)
        """,
        (json.dumps(payload),),
    )
    fact_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT OR REPLACE INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES('highest-temperature-in-chengdu-on-july-6-2026', '2026-07-06', ?, '2026-07-05T04:00:00+00:00')
        """,
        (fact_id,),
    )
    conn.commit()
    return fact_id


def _payload(source_observations: list[dict]) -> dict:
    return {
        "posterior_temperature_distribution": [
            {"bucket": "32°C", "posterior": 0.4},
            {"bucket": "33°C", "posterior": 0.6},
        ],
        "source_acquisition_trace": {
            "schema": "weather_source_acquisition_trace_v1",
            "source_observations": source_observations,
        },
    }


def test_metar_strong_likelihood_contract_audit_accepts_awc_hard_observation(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_tables(conn)
    _insert_fact(
        conn,
        _payload(
            [
                {
                    "source_event_id": "srcevt_awc",
                    "source_id": "zuuu_metar_raw_awc",
                    "source_class": "hard_observation_candidate",
                    "role": "metar_hard_observation",
                    "temperature_c": 28.0,
                    "dewpoint_c": 22.0,
                    "observed_today_max_c": 28.0,
                    "payload": {
                        "visibility_m": 9999,
                        "cloud_text": "FEW020",
                        "temperature_momentum_1h_c_per_hour": 1.2,
                        "temperature_momentum_45m_c_per_hour": 1.1,
                        "temperature_momentum_30m_c_per_hour": 1.0,
                    },
                },
                {
                    "source_event_id": "srcevt_twc",
                    "source_id": "twc_current_icao_metric",
                    "source_class": "diagnostic_current_source",
                    "temperature_c": 31.0,
                },
                {
                    "source_event_id": "srcevt_taf_previous_day",
                    "source_id": "zuuu_taf_forecast_amsc",
                    "source_class": "forecast_weak_source",
                    "event_date": "2026-07-05",
                    "temperature_c": 31.0,
                },
            ]
        ),
    )
    conn.close()

    report = audit.build_metar_strong_likelihood_contract_audit(db_path=db, since_hours=0, latest_only=True)

    assert report["ok"] is True
    item = report["items"][0]
    assert item["metar_strong_likelihood_contract"]["state"] == "ready"
    assert item["included_source_count"] == 1
    assert item["base_observation_present"] is True
    assert item["trajectory_state"] in {"sustained_warming", "emerging_warming"}


def test_metar_strong_likelihood_contract_audit_allows_neutral_missing_observation(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_tables(conn)
    _insert_fact(conn, _payload([]))
    conn.close()

    report = audit.build_metar_strong_likelihood_contract_audit(db_path=db, since_hours=0, latest_only=True)

    assert report["ok"] is True
    item = report["items"][0]
    assert item["included_source_count"] == 0
    assert item["base_observation_present"] is False
    assert "current_temp_and_observed_today_max_missing" in item["runtime_na_reasons"]
    assert (
        "temperature_momentum_windows_missing" in item["runtime_na_reasons"]
        or "temperature_momentum_single_rate_without_multi_window_confirmation" in item["runtime_na_reasons"]
    )
