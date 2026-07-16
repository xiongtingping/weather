import json
import sqlite3
from pathlib import Path

from scripts import forecast_weak_likelihood_contract_audit as audit


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
               '2026-07-05T01:00:00+00:00', ?)
        """,
        (json.dumps(payload),),
    )
    fact_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT OR REPLACE INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES('highest-temperature-in-chengdu-on-july-6-2026', '2026-07-06', ?, '2026-07-05T01:00:00+00:00')
        """,
        (fact_id,),
    )
    conn.commit()
    return fact_id


def _payload(source_observations: list[dict]) -> dict:
    return {
        "posterior_temperature_distribution": [
            {"bucket": "32°C", "posterior": 0.7},
            {"bucket": "33°C", "posterior": 0.3},
        ],
        "source_acquisition_trace": {
            "schema": "weather_source_acquisition_trace_v1",
            "source_observations": source_observations,
        },
    }


def test_forecast_weak_likelihood_contract_audit_accepts_group_capped_source(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_tables(conn)
    _insert_fact(
        conn,
        _payload(
            [
                {
                    "source_event_id": "srcevt_taf",
                    "source_id": "zuuu_taf_forecast_peak",
                    "source_class": "forecast_weak_source",
                    "role": "taf_forecast_weak_likelihood",
                    "forecast_peak_c": 33.0,
                    "sigma_c": 0.7,
                }
            ]
        ),
    )
    conn.close()

    report = audit.build_forecast_weak_likelihood_contract_audit(db_path=db, since_hours=0, latest_only=True)

    assert report["ok"] is True
    item = report["items"][0]
    assert item["forecast_weak_likelihood_contract"]["state"] == "ready"
    assert item["included_source_count"] == 1
    assert item["source_group_count"] == 1


def test_forecast_weak_likelihood_contract_audit_excludes_hard_eligible_forecast(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_tables(conn)
    _insert_fact(
        conn,
        _payload(
            [
                {
                    "source_event_id": "srcevt_bad",
                    "source_id": "twc_forecast_peak",
                    "source_class": "forecast_weak_source",
                    "role": "twc_forecast_weak_likelihood",
                    "forecast_peak_c": 33.0,
                    "hard_eligible": True,
                }
            ]
        ),
    )
    conn.close()

    report = audit.build_forecast_weak_likelihood_contract_audit(db_path=db, since_hours=0, latest_only=True)

    assert report["ok"] is True
    item = report["items"][0]
    assert item["included_source_count"] == 0
    assert item["runtime_na_reasons"] == ["forecast_weak_sources_missing_or_unusable"]
