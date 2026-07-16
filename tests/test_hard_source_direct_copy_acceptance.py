import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "hard_source_direct_copy_acceptance.py"
spec = importlib.util.spec_from_file_location("hard_source_direct_copy_acceptance", SCRIPT_PATH)
copy_acceptance = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(copy_acceptance)


def _sample_payload() -> dict:
    return {
        "slug": "highest-temperature-in-chengdu-on-july-3-2026",
        "event_date": "2026-07-03",
        "run_date": "2026-07-03",
        "station_id": "ZUUU",
        "weather_likelihood_features": {
            "station_id": "ZUUU",
            "zuuu_metar_raw_awc_fetched_at": "2026-07-03T08:00:00+00:00",
            "zuuu_metar_raw_awc_request_started_at": "2026-07-03T08:00:00+00:00",
            "zuuu_metar_raw_awc_latest_at": "2026-07-03T08:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_at": "2026-07-03T08:00:00+00:00",
            "zuuu_metar_raw_awc_today_max_c": 31.0,
            "zuuu_metar_raw_awc_age_minutes": 120.0,
            "wu_twc_observed_intraday_age_minutes": 120.0,
        },
        "distribution": [
            {"outcome": "31", "q_bayes_posterior": 0.6},
            {"outcome": "32", "q_bayes_posterior": 0.4},
        ],
    }


def _write_db(path: Path, payload: dict) -> int:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            model_snapshot_id INTEGER,
            market_snapshot_id INTEGER,
            composer_version TEXT,
            fact_json TEXT NOT NULL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL,
            updated_at TEXT,
            PRIMARY KEY(slug, event_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            run_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur = conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, model_snapshot_id,
            market_snapshot_id, composer_version, fact_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["run_date"],
            payload["slug"],
            payload["event_date"],
            "2026-07-03T08:00:00+00:00",
            1,
            2,
            "test",
            json.dumps(payload),
            "2026-07-03T08:00:00+00:00",
        ),
    )
    fact_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        (payload["slug"], payload["event_date"], fact_id, "2026-07-03T08:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return fact_id


def _latest_payload(path: Path) -> tuple[int, dict]:
    conn = sqlite3.connect(path)
    row = conn.execute(
        """
        SELECT l.fact_id, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        """
    ).fetchone()
    conn.close()
    return int(row[0]), json.loads(row[1])


def test_freshen_hard_source_timestamps_uses_fact_snapshot_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    payload = _sample_payload()
    original_fact_id = _write_db(db_path, payload)

    result = copy_acceptance._freshen_hard_source_timestamps(
        db_path,
        slug=payload["slug"],
        event_date=payload["event_date"],
    )

    assert result["ok"] is True
    assert result["write_owner"] == "weather_fact_snapshot_writer"
    assert result["latest_repointed"] is True
    assert result["new_fact_id"] != original_fact_id
    latest_fact_id, latest = _latest_payload(db_path)
    assert latest_fact_id == result["new_fact_id"]
    assert latest["copy_acceptance_fixture_state"]["write_reason"] == (
        "copy_acceptance_freshen_hard_source_timestamps"
    )
    assert latest["weather_likelihood_features"]["zuuu_metar_raw_awc_age_minutes"] == 0.0
    assert latest["source_acquisition_trace"]["hard_observation_candidate_ids"]
    conn = sqlite3.connect(db_path)
    history_count = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    summary_count = conn.execute("SELECT COUNT(*) FROM event_fact_summary_cache").fetchone()[0]
    original = conn.execute("SELECT fact_json FROM event_fact_history WHERE id = ?", (original_fact_id,)).fetchone()[0]
    conn.close()
    assert history_count == 2
    assert summary_count == 1
    assert json.loads(original)["weather_likelihood_features"]["zuuu_metar_raw_awc_age_minutes"] == 120.0


def test_force_final_lock_scenario_uses_fact_snapshot_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    payload = _sample_payload()
    original_fact_id = _write_db(db_path, payload)

    result = copy_acceptance._force_final_lock_scenario(
        db_path,
        slug=payload["slug"],
        event_date=payload["event_date"],
    )

    assert result["ok"] is True
    assert result["write_owner"] == "weather_fact_snapshot_writer"
    assert result["latest_repointed"] is True
    assert result["new_fact_id"] != original_fact_id
    latest_fact_id, latest = _latest_payload(db_path)
    features = latest["weather_likelihood_features"]
    assert latest_fact_id == result["new_fact_id"]
    assert latest["copy_acceptance_fixture_state"]["write_reason"] == "copy_acceptance_force_final_lock_scenario"
    assert features["hard_source_final_daily_max"] is True
    assert features["zuuu_metar_raw_awc_today_max_c"] == 33.0
