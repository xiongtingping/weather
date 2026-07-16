import json
import sqlite3

import scan_weather_market as swm


def _init_fact_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            fact_as_of TEXT,
            model_snapshot_id INTEGER,
            market_snapshot_id INTEGER,
            composer_version TEXT,
            market_updated_at TEXT,
            model_updated_at TEXT,
            market_stale_sec INTEGER,
            model_cache_age_sec INTEGER,
            best_side TEXT,
            best_outcome TEXT,
            best_edge_score REAL,
            best_signal_edge_entry REAL,
            fact_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT,
            event_date TEXT,
            fact_id INTEGER,
            updated_at TEXT,
            PRIMARY KEY(slug, event_date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_summary_cache (
            fact_id INTEGER PRIMARY KEY,
            slug TEXT,
            event_date TEXT,
            run_date TEXT,
            fact_as_of TEXT,
            summary_json TEXT,
            created_at TEXT
        )
        """
    )
    return conn


def test_q_calibrated_writeback_plan_uses_derived_lineage_and_new_writer_metadata():
    original_payload = {
        "slug": "chengdu-high-temp",
        "event_date": "2026-07-03",
        "station_id": "ZUUU",
        "fact_snapshot_id": "old-fact-snapshot",
        "source_observations": [{"source_id": "old"}],
        "fact_layer_state": {"write_owner": "legacy"},
        "rows": [{"outcome": "30C", "q_raw": 0.4, "q_calibrated": 0.5}],
    }
    row = {
        "id": 7,
        "run_date": "2026-07-03",
        "slug": "chengdu-high-temp",
        "event_date": "2026-07-03",
        "fact_as_of": "2026-07-03T08:00:00Z",
        "composer_version": "legacy_composer",
    }

    plan = swm.build_q_calibrated_writeback_fact_snapshot_writer_apply_plan(
        row=row,
        original_payload=original_payload,
        updated_payload=original_payload,
        parent_raw_json=json.dumps(original_payload),
        created_at="2026-07-03T09:00:00Z",
        historical_backfill=False,
    )
    payload = plan["effective_payload"]

    assert plan["schema"] == "weather_fact_snapshot_writer_plan_v1"
    assert plan["write_owner"] == "weather_fact_snapshot_writer"
    assert plan["write_latest"] is True
    assert payload["fact_as_of"] == "2026-07-03T08:00:00Z"
    assert payload["parent_fact_id"] == 7
    assert payload["q_calibrated_writeback_state"]["method"] == "platt"
    assert payload["q_calibrated_writeback_state"]["parent_fact_snapshot_id"] == "old-fact-snapshot"
    assert payload["fact_layer_state"]["write_owner"] == "weather_fact_snapshot_writer"
    assert payload["lineage_state"]["write_owner"] == "weather_fact_snapshot_writer"
    assert plan["fact_snapshot_id"] != "old-fact-snapshot"
    assert payload.get("fact_snapshot_id") != "old-fact-snapshot"
    assert payload["lineage_state"]["fact_snapshot_id"] == plan["fact_snapshot_id"]
    assert payload["source_observations"][0]["source_id"] == swm.Q_CALIBRATED_WRITEBACK_SOURCE_ID
    assert payload["source_observation_ids"]
    assert payload["source_observations"][0]["source_observation_id"] == payload["source_observation_ids"][0]
    assert payload["source_latency_trace"][0]["source_observation_id"] == payload["source_observation_ids"][0]
    assert plan["source_observation_ids"] == payload["source_observation_ids"]
    assert payload["source_observations"][0]["role"] == "derived_calibration_writeback"
    assert payload["source_observations"][0]["hard_eligible"] is False


def test_q_calibrated_writeback_appends_writer_fact_without_overwriting_parent(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    conn = _init_fact_db(db_path)
    run_date = swm.today_date_str("America/New_York")
    payload = {
        "slug": "chengdu-high-temp",
        "event_date": run_date,
        "station_id": "ZUUU",
        "rows": [
            {"outcome": "30C", "q_raw": 0.4, "best_ask": 0.2},
            {"outcome": "31C", "q_raw": 0.6, "best_ask": 0.3},
        ],
        "fact_snapshot_id": "parent-fact-snapshot",
        "fact_layer_state": {"write_owner": "legacy_parent"},
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(
            run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_date,
            "chengdu-high-temp",
            run_date,
            "2026-07-03T08:00:00Z",
            "legacy_composer",
            json.dumps(payload),
            "2026-07-03T08:00:01Z",
        ),
    )
    parent_id = conn.execute("SELECT id FROM event_fact_history").fetchone()["id"]
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES(?, ?, ?, ?)",
        ("chengdu-high-temp", run_date, parent_id, "2026-07-03T08:00:01Z"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(swm, "DB_PATH", db_path)
    monkeypatch.setattr(swm, "init_db", lambda: None)
    monkeypatch.setattr(swm, "_load_q_platt_calibration", lambda _days: {"enabled": True, "a": 1.0, "b": 0.0})

    result = swm.writeback_q_calibrated_for_run_date(run_date)

    assert result["ok"] is True
    assert result["write_mode"] == "append_only_writer_apply"
    assert result["updated"] == 1
    assert result["latest_repointed"] == 1
    assert result["summary_cache_written"] == 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 2
    parent_raw = conn.execute(
        "SELECT fact_json FROM event_fact_history WHERE id = ?",
        (parent_id,),
    ).fetchone()["fact_json"]
    assert "q_calibrated" not in parent_raw

    latest = conn.execute(
        """
        SELECT h.*
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        ("chengdu-high-temp", run_date),
    ).fetchone()
    latest_payload = json.loads(latest["fact_json"])
    assert latest["id"] != parent_id
    assert latest["fact_as_of"] == "2026-07-03T08:00:00Z"
    assert latest["composer_version"] == swm.Q_CALIBRATED_WRITEBACK_COMPOSER_VERSION
    assert latest_payload["fact_layer_state"]["write_owner"] == "weather_fact_snapshot_writer"
    assert latest_payload["q_calibrated_writeback_state"]["parent_fact_id"] == parent_id
    assert latest_payload["q_calibrated_writeback_state"]["fact_as_of_preserved_from_parent"] is True
    assert all("q_calibrated" in row for row in latest_payload["rows"])
    assert abs(sum(float(row["q_calibrated"]) for row in latest_payload["rows"]) - 1.0) < 1e-9

    summary = conn.execute(
        "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id = ?",
        (latest["id"],),
    ).fetchone()
    summary_payload = json.loads(summary["summary_json"])
    assert summary_payload["summary_cache_source"] == "weather_fact_snapshot_writer"
    assert summary_payload["fact_id"] == latest["id"]
    conn.close()


def test_q_calibrated_writeback_blocks_non_et_today_without_explicit_allow(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.db"
    db_path.touch()
    monkeypatch.setattr(swm, "DB_PATH", db_path)

    result = swm.writeback_q_calibrated_for_run_date("2001-01-01")

    assert result["ok"] is False
    assert result["error"] == "historical_writeback_requires_explicit_allow"
    assert result["historical_backfill"] is True
    assert result["write_mode"] == "blocked_no_apply"
