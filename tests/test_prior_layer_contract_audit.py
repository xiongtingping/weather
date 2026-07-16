import json
import sqlite3
from pathlib import Path

from scripts import prior_layer_contract_audit as audit


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


def _prior_fields() -> dict:
    return {
        "prior_distribution": {"32°C": 0.7, "33°C": 0.3},
        "prior_na_reason": "",
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "boundary": "historical_climate_date_site_terrain_weather_type_only_no_forecast_no_metar_no_market",
            "sample_count": 10,
            "shrinkage_target": "broader_settled_history",
            "date_window_days": 15,
            "prior_na_reason": "",
        },
    }


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
    return fact_id


def test_prior_layer_contract_audit_accepts_latest_fact_with_root_prior(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_tables(conn)
    _insert_fact(
        conn,
        {
            "posterior_temperature_distribution": [
                {"bucket": "32°C", "posterior": 0.7},
                {"bucket": "33°C", "posterior": 0.3},
            ],
            **_prior_fields(),
        },
    )
    conn.commit()
    conn.close()

    report = audit.build_prior_layer_contract_audit(
        db_path=db,
        since_hours=0,
        latest_only=True,
        limit=10,
    )

    assert report["ok"] is True
    assert report["coverage"]["checked_fact_count"] == 1
    assert report["items"][0]["prior_layer_contract"]["state"] == "ready"


def test_prior_layer_contract_audit_flags_missing_prior_distribution(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    conn = sqlite3.connect(db)
    _create_tables(conn)
    _insert_fact(
        conn,
        {
            "posterior_temperature_distribution": [
                {"bucket": "32°C", "posterior": 0.7},
                {"bucket": "33°C", "posterior": 0.3},
            ],
        },
    )
    conn.commit()
    conn.close()

    report = audit.build_prior_layer_contract_audit(
        db_path=db,
        since_hours=0,
        latest_only=True,
        limit=10,
    )

    assert report["ok"] is False
    assert report["issue_counts"]["prior_distribution_missing"] == 1
    assert "prior_meta_schema_invalid" in report["items"][0]["prior_layer_contract"]["issues"]
