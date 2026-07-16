from __future__ import annotations

import json
import sqlite3

from posterior_ssot_projection_runtime import (
    _model_member_cache_source_observations_from_conn,
    _query_newer_source_observations,
    _soft_forecast_event_run_source_observations_from_conn,
)


def _source_fact(source_event_id: str) -> str:
    return json.dumps(
        {
            "source_observations": [
                {
                    "source_event_id": source_event_id,
                    "source_observation_id": f"obs_{source_event_id}",
                    "source_id": "zuuu_metar_raw_amsc",
                    "source_class": "hard_observation_candidate",
                    "station_id": "ZUUU",
                    "event_date": "2026-07-12",
                    "valid_time": "2026-07-12T10:00:00Z",
                    "fetched_at": "2026-07-12T10:00:10Z",
                    "temperature_c": 30.0,
                    "raw_sha256": f"sha_{source_event_id}",
                }
            ]
        }
    )


def test_source_history_cutoff_excludes_facts_written_after_queued_source_time() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE event_fact_history (id INTEGER PRIMARY KEY, slug TEXT, event_date TEXT, fact_as_of TEXT, composer_version TEXT, fact_json TEXT)"
    )
    rows = [
        (1, "slug", "2026-07-12", "2026-07-12T10:00:00Z", "source", _source_fact("base")),
        (2, "slug", "2026-07-12", "2026-07-12T10:30:00Z", "source", _source_fact("allowed")),
        (3, "slug", "2026-07-12", "2026-07-12T11:30:00Z", "source", _source_fact("future")),
    ]
    conn.executemany("INSERT INTO event_fact_history VALUES (?,?,?,?,?,?)", rows)

    observations, context = _query_newer_source_observations(
        conn,
        slug="slug",
        event_date="2026-07-12",
        current_fact_id=1,
        current_fact_as_of="2026-07-12T10:00:00Z",
        as_of_cutoff="2026-07-12T11:00:00Z",
    )

    assert [row["source_event_id"] for row in observations] == ["allowed"]
    assert context["source_history_fact_ids"] == [2]
    assert context["as_of_cutoff"] == "2026-07-12T11:00:00Z"


def test_forecast_context_adapters_respect_asof_cutoff() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE event_runs (id INTEGER PRIMARY KEY, slug TEXT, event_date TEXT, created_at TEXT, payload_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE model_member_cache (id INTEGER PRIMARY KEY, model TEXT, loc_key TEXT, event_date TEXT, market_var TEXT, market_unit TEXT, members_json TEXT, created_at TEXT, official_updated_at TEXT, official_updated_source TEXT, members_signature TEXT)"
    )
    old_forecast = json.dumps({"weather_likelihood_features": {"wu_forecast_peak_c": 31.0}})
    future_forecast = json.dumps({"weather_likelihood_features": {"wu_forecast_peak_c": 39.0}})
    conn.executemany(
        "INSERT INTO event_runs VALUES (?,?,?,?,?)",
        [
            (1, "slug", "2026-07-12", "2026-07-12T10:00:00Z", old_forecast),
            (2, "slug", "2026-07-12", "2026-07-12T12:00:00Z", future_forecast),
        ],
    )
    conn.executemany(
        "INSERT INTO model_member_cache VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "gfs", "ZUUU", "2026-07-12", "temperature_2m_max", "C", "[31.0]", "2026-07-12T10:00:00Z", "2026-07-12T10:00:00Z", "official", "old"),
            (2, "gfs", "ZUUU", "2026-07-12", "temperature_2m_max", "C", "[39.0]", "2026-07-12T12:00:00Z", "2026-07-12T12:00:00Z", "official", "future"),
        ],
    )

    soft, soft_context = _soft_forecast_event_run_source_observations_from_conn(
        conn, slug="slug", event_date="2026-07-12", station_id="ZUUU", fact_as_of="2026-07-12T11:00:00Z"
    )
    model, model_context = _model_member_cache_source_observations_from_conn(
        conn, event_date="2026-07-12", station_id="ZUUU", fact_as_of="2026-07-12T11:00:00Z"
    )

    assert soft_context["event_run_ids"] == [1]
    assert soft and soft[0]["forecast_peak_c"] == 31.0
    assert model_context["model_member_cache_ids"] == [1]
    assert model and model[0]["forecast_peak_c"] == 31.0
