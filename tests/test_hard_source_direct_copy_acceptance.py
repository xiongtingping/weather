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
        # A rung at 33 as well: _force_final_lock_scenario forces the observed
        # daily max to 33.0, and a ladder that stops at 32 leaves the writer
        # with no bucket at or above the hard floor it just created.
        "distribution": [
            {"outcome": "31", "q_bayes_posterior": 0.5},
            {"outcome": "32", "q_bayes_posterior": 0.3},
            {"outcome": "33", "q_bayes_posterior": 0.2},
        ],
        "fact_snapshot_id": "fact_copy_acceptance_unit",
        "posterior_run_id": "post_copy_acceptance_unit",
        # The two layers the writer's apply contract demands of every fact it
        # persists.  The acceptance script under test hands this payload
        # straight to that writer, and without them it was refused on
        # prior_layer_contract_failed before the freshen/final-lock behaviour
        # these tests are about ever ran.  Production facts carry both; this
        # fixture was written before the contract did.
        "prior_distribution": {"31": 0.5, "32": 0.3, "33": 0.2},
        "prior_na_reason": "",
        "prior_meta": {
            "schema": "temperature_prior_meta_v1",
            "state": "available",
            "source": "unit_test_copy_acceptance_prior",
            "boundary": (
                "historical_climate_date_site_terrain_weather_type_only"
                "_no_forecast_no_metar_no_market"
            ),
            "sample_count": 30,
            "date_window_days": 15,
            "shrinkage_target": "broader_settled_history",
            "prior_na_reason": "",
        },
        "posterior_temperature_distribution": [
            {"bucket": "31", "probability": 0.5},
            {"bucket": "32", "probability": 0.3},
            {"bucket": "33", "probability": 0.2},
        ],
        "production_model_version": "joint_weak_physical_strong_composer_v1",
        "production_model_ready": True,
    }


def _with_posterior_ssot_fact(payload: dict) -> dict:
    """The SSOT block the writer would have minted, by its own builder."""

    import weather_fact_snapshot_writer as writer

    ssot = writer.build_posterior_ssot_fact(payload)
    payload["posterior_ssot_fact"] = ssot
    selection = {
        "target_bucket": ssot.get("target_bucket"),
        "target_probability": ssot.get("target_probability"),
        "target_probability_field": "posterior_ssot_fact.target_probability",
        "ssot_source": "posterior_ssot_fact",
        "selection_runtime_recomputed": False,
        "fact_snapshot_id": payload["fact_snapshot_id"],
        "posterior_run_id": payload["posterior_run_id"],
    }
    payload["posterior_target_bucket_selection"] = dict(selection)
    payload["target_bucket_selection"] = dict(selection)
    return payload


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
    payload = _with_posterior_ssot_fact(_sample_payload())
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
    # The superseded row is compacted when the new fact replaces it, and
    # compaction is what strips its weather_likelihood_features -- see
    # test_event_fact_latest_compacts_replaced_history_but_keeps_latest_full.
    # What matters here is that the freshen appended rather than updating in
    # place: the old row is still its own row, and it is not the latest.
    original_payload = json.loads(original)
    assert original_payload["history_compacted"] is True
    assert "weather_likelihood_features" not in original_payload


def test_force_final_lock_scenario_uses_fact_snapshot_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    payload = _with_posterior_ssot_fact(_sample_payload())
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
