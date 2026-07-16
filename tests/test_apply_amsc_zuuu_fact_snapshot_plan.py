import sqlite3
import json
import os
import subprocess
import sys
from pathlib import Path

import weather_fact_snapshot_writer as writer
from scripts.apply_amsc_zuuu_fact_snapshot_plan import (
    CONFIRM_PHRASE,
    apply_amsc_zuuu_fact_snapshot_plan,
)
from scripts.fetch_zuuu_metar_taf import (
    AMSC_LATEST_REPOINT_BLOCK_REASON,
    build_amsc_fact_snapshot_write_plan,
    build_amsc_observation_bundle,
)
from tests.test_fetch_zuuu_metar_taf import SAMPLE_PAYLOAD


ROOT = Path(__file__).resolve().parents[1]


def _create_fact_tables(conn):
    conn.execute(
        """
        CREATE TABLE event_fact_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_as_of TEXT NOT NULL,
            model_snapshot_id INTEGER,
            market_snapshot_id INTEGER,
            composer_version TEXT NOT NULL,
            market_updated_at TEXT,
            model_updated_at TEXT,
            market_stale_sec INTEGER,
            model_cache_age_sec INTEGER,
            best_side TEXT,
            best_outcome TEXT,
            best_edge_score REAL,
            best_signal_edge_entry REAL,
            fact_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE event_fact_latest (
            slug TEXT NOT NULL,
            event_date TEXT NOT NULL,
            fact_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (slug, event_date)
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
    conn.commit()


def _create_task_queue_table(conn):
    conn.execute(
        """
        CREATE TABLE task_queue (
            id INTEGER PRIMARY KEY,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT,
            error TEXT,
            worker_pid INTEGER,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3
        )
        """
    )
    conn.commit()


def _sample_plan():
    bundle = build_amsc_observation_bundle(
        SAMPLE_PAYLOAD,
        fetched_at="2026-07-05T14:31:00Z",
        duration_ms=321.0,
        include_payload=True,
    )
    return build_amsc_fact_snapshot_write_plan(
        bundle,
        enable_apply_contract=True,
        write_latest=True,
    )


def test_apply_amsc_plan_writes_archive_fact_and_skips_duplicate(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_fact_tables(conn)
    conn.close()
    old_dirty_path = writer.UI_SNAPSHOT_DIRTY_STAMP_PATH
    writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = tmp_path / "ui_snapshot_dirty.stamp"
    plan = _sample_plan()

    try:
        first = apply_amsc_zuuu_fact_snapshot_plan(
            plan,
            db_path=db_path,
            allow_apply=True,
            confirm=CONFIRM_PHRASE,
        )
        second = apply_amsc_zuuu_fact_snapshot_plan(
            plan,
            db_path=db_path,
            allow_apply=True,
            confirm=CONFIRM_PHRASE,
        )
    finally:
        writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = old_dirty_path

    assert first["ok"] is True
    assert first["applied"] is True
    assert first["guards"]["writes_archive_db"] is True
    assert first["guards"]["writes_event_fact_history"] is True
    assert first["guards"]["writes_event_fact_latest"] is False
    assert first["guards"]["places_orders"] is False
    assert first["writer_apply_result_summary"]["lineage_verification_state"] == "persisted_verified"
    assert first["latest_repointed"] is False

    assert second["ok"] is True
    assert second["applied"] is False
    assert second["skipped"] is True
    assert second["skip_reason"] == "duplicate_amsc_observation_signature"

    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
        latest = check.execute("SELECT fact_id FROM event_fact_latest").fetchone()
        assert latest is None
    finally:
        check.close()


def test_duplicate_amsc_fact_can_enqueue_later_without_second_fact_write(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_fact_tables(conn)
    _create_task_queue_table(conn)
    conn.close()
    old_dirty_path = writer.UI_SNAPSHOT_DIRTY_STAMP_PATH
    writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = tmp_path / "ui_snapshot_dirty.stamp"
    plan = _sample_plan()

    try:
        source_only = apply_amsc_zuuu_fact_snapshot_plan(
            plan,
            db_path=db_path,
            allow_apply=True,
            confirm=CONFIRM_PHRASE,
            enqueue_posterior=False,
        )
        delayed_enqueue = apply_amsc_zuuu_fact_snapshot_plan(
            plan,
            db_path=db_path,
            allow_apply=True,
            confirm=CONFIRM_PHRASE,
            enqueue_posterior=True,
        )
    finally:
        writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = old_dirty_path

    assert source_only["ok"] is True
    assert source_only["applied"] is True
    assert source_only["posterior_materialization_enqueue"]["state"] == "disabled"
    assert delayed_enqueue["ok"] is True
    assert delayed_enqueue["applied"] is False
    assert delayed_enqueue["skipped"] is True
    assert delayed_enqueue["skip_reason"] == "duplicate_amsc_observation_signature"
    assert delayed_enqueue["fact_id"] == source_only["fact_id"]
    assert delayed_enqueue["posterior_materialization_enqueue"]["enqueued"] is True

    check = sqlite3.connect(db_path)
    try:
        fact_count = check.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
        task = check.execute("SELECT payload_json FROM task_queue").fetchone()
    finally:
        check.close()
    assert fact_count == 1
    payload = json.loads(task[0])
    assert payload["amsc_source_fact_id"] == source_only["fact_id"]


def test_apply_amsc_plan_requires_confirmation(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_fact_tables(conn)
    conn.close()

    result = apply_amsc_zuuu_fact_snapshot_plan(
        _sample_plan(),
        db_path=db_path,
        allow_apply=True,
        confirm="",
    )

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["block_reason"] == "confirmation_phrase_missing_or_invalid"
    assert result["guards"]["writes_archive_db"] is False


def test_apply_amsc_plan_overrides_stale_current_weather_features(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_fact_tables(conn)
    stale_payload = {
        "schema": "legacy_fact_payload",
        "weather_likelihood_features": {
            "schema": "legacy_features",
            "source": "legacy_source",
            "zuuu_metar_raw_amsc_latest_at": "",
        },
        "distribution": [{"outcome": "32", "q_temperature_bucket_posterior": 1.0}],
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-05",
            "highest-temperature-in-chengdu-on-july-5-2026",
            "2026-07-05",
            "2026-07-05T14:00:00Z",
            "legacy",
            json.dumps(stale_payload),
            "2026-07-05T14:00:00Z",
        ),
    )
    old_fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        (
            "highest-temperature-in-chengdu-on-july-5-2026",
            "2026-07-05",
            old_fact_id,
            "2026-07-05T14:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    old_dirty_path = writer.UI_SNAPSHOT_DIRTY_STAMP_PATH
    writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = tmp_path / "ui_snapshot_dirty.stamp"

    try:
        result = apply_amsc_zuuu_fact_snapshot_plan(
            _sample_plan(),
            db_path=db_path,
            allow_apply=True,
            confirm=CONFIRM_PHRASE,
        )
    finally:
        writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = old_dirty_path

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["guards"]["writes_event_fact_latest"] is False
    assert result["latest_repointed"] is False
    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    try:
        latest = check.execute(
            "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
            ("highest-temperature-in-chengdu-on-july-5-2026", "2026-07-05"),
        ).fetchone()
        assert latest["fact_id"] == old_fact_id
        new_row = check.execute(
            """
            SELECT fact_json
            FROM event_fact_history
            WHERE id = ?
            """,
            (result["fact_id"],),
        ).fetchone()
        payload = json.loads(new_row["fact_json"])
    finally:
        check.close()
    features = payload["weather_likelihood_features"]
    assert features["schema"] == "amsc_zuuu_metar_taf_features_v1"
    assert features["source"] == "amsc_packet_retrieval"
    # The sample contains a later SPECI at 14:31Z.  `latest_at` must retain
    # that most recent observed report; daily-max time is a distinct field.
    assert features["zuuu_metar_raw_amsc_latest_at"] == "2026-07-05T14:31:00Z"


def test_apply_amsc_plan_preserves_existing_posterior_latest(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_fact_tables(conn)
    posterior_payload = {
        "schema": "posterior_fact_payload",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "37°C",
            "target_probability": 1.0,
            "bucket_probabilities": [
                {"bucket": "37°C", "probability": 1.0},
                {"bucket": "38°C", "probability": 0.0},
            ],
        },
        "distribution": [
            {"outcome": "37°C", "q_temperature_bucket_posterior": 1.0},
            {"outcome": "38°C", "q_temperature_bucket_posterior": 0.0},
        ],
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-05",
            "highest-temperature-in-chengdu-on-july-5-2026",
            "2026-07-05",
            "2026-07-05T14:00:00Z",
            "posterior_writer",
            json.dumps(posterior_payload),
            "2026-07-05T14:00:00Z",
        ),
    )
    posterior_fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        (
            "highest-temperature-in-chengdu-on-july-5-2026",
            "2026-07-05",
            posterior_fact_id,
            "2026-07-05T14:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    old_dirty_path = writer.UI_SNAPSHOT_DIRTY_STAMP_PATH
    writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = tmp_path / "ui_snapshot_dirty.stamp"

    try:
        result = apply_amsc_zuuu_fact_snapshot_plan(
            _sample_plan(),
            db_path=db_path,
            allow_apply=True,
            confirm=CONFIRM_PHRASE,
        )
    finally:
        writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = old_dirty_path

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["latest_repointed"] is False
    assert result["source_acquisition_latest_repoint_policy"]["block_reason"] == AMSC_LATEST_REPOINT_BLOCK_REASON

    check = sqlite3.connect(db_path)
    try:
        latest_fact_id = check.execute(
            "SELECT fact_id FROM event_fact_latest WHERE slug = ? AND event_date = ?",
            ("highest-temperature-in-chengdu-on-july-5-2026", "2026-07-05"),
        ).fetchone()[0]
        assert latest_fact_id == posterior_fact_id
        assert check.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 2
    finally:
        check.close()


def test_apply_amsc_plan_enqueues_posterior_materialization_without_repointing_latest(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_fact_tables(conn)
    _create_task_queue_table(conn)
    posterior_payload = {
        "schema": "posterior_fact_payload",
        "slug": "highest-temperature-in-chengdu-on-july-5-2026",
        "event_date": "2026-07-05",
        "run_date": "2026-07-05",
        "station_id": "ZUUU",
        "posterior_ssot_fact": {
            "schema": "bayes_posterior_ssot_fact_v1",
            "state": "available",
            "target_bucket": "36°C",
            "target_probability": 0.7,
        },
        "prior_distribution": {"36°C": 0.7, "37°C": 0.3},
        "distribution": [
            {"outcome": "36°C", "q_temperature_bucket_posterior": 0.7},
            {"outcome": "37°C", "q_temperature_bucket_posterior": 0.3},
        ],
    }
    conn.execute(
        """
        INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-07-05",
            "highest-temperature-in-chengdu-on-july-5-2026",
            "2026-07-05",
            "2026-07-05T14:00:00Z",
            "posterior_writer",
            json.dumps(posterior_payload),
            "2026-07-05T14:00:00Z",
        ),
    )
    posterior_fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES (?, ?, ?, ?)",
        (
            "highest-temperature-in-chengdu-on-july-5-2026",
            "2026-07-05",
            posterior_fact_id,
            "2026-07-05T14:00:00Z",
        ),
    )
    conn.commit()
    conn.close()
    old_dirty_path = writer.UI_SNAPSHOT_DIRTY_STAMP_PATH
    writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = tmp_path / "ui_snapshot_dirty.stamp"

    try:
        result = apply_amsc_zuuu_fact_snapshot_plan(
            _sample_plan(),
            db_path=db_path,
            allow_apply=True,
            confirm=CONFIRM_PHRASE,
        )
    finally:
        writer.UI_SNAPSHOT_DIRTY_STAMP_PATH = old_dirty_path

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["latest_repointed"] is False
    enqueue = result["posterior_materialization_enqueue"]
    assert enqueue["ok"] is True
    assert enqueue["enqueued"] is True
    assert enqueue["task_type"] == "amsc_posterior_materialize"
    assert enqueue["guards"]["places_orders"] is False

    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    try:
        latest = check.execute(
            "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
            ("highest-temperature-in-chengdu-on-july-5-2026", "2026-07-05"),
        ).fetchone()
        assert int(latest["fact_id"]) == int(posterior_fact_id)
        task = check.execute("SELECT * FROM task_queue").fetchone()
        payload = json.loads(task["payload_json"])
    finally:
        check.close()
    assert task["task_type"] == "amsc_posterior_materialize"
    assert task["status"] == "pending"
    assert payload["schema"] == "amsc_posterior_materialization_task_payload_v1"
    assert payload["amsc_source_fact_id"] == result["fact_id"]
    assert payload["write_latest_policy"] == "posterior_writer_only"
    assert payload["official_nwp_context_policy"] == {
        "schema": "official_nwp_context_policy_v1",
        "mode": "prewarmed_cache_only_v1",
        "cache_generation_ref": {
            "schema": "official_nwp_multimodel_generation_v1",
            "state": "missing_or_invalid",
        },
    }


def test_fast_lane_returns_structured_deferral_without_waiting_for_standard_queue(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = sqlite3.connect(db_path)
    _create_fact_tables(conn)
    conn.close()
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_sample_plan()), encoding="utf-8")
    result_path = tmp_path / "result.json"
    lock_dir = tmp_path / "archive_writer.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

    env = dict(os.environ)
    env["PM_ARCHIVE_DB_WRITE_LOCK_DIR"] = str(lock_dir)
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "apply_amsc_zuuu_fact_snapshot_plan.py"),
            "--plan",
            str(plan_path),
            "--db",
            str(db_path),
            "--apply",
            "--confirm",
            CONFIRM_PHRASE,
            "--fast-lane",
            "--archive-write-queue-wait-sec",
            "0",
            "--db-timeout-sec",
            "0.1",
            "--db-busy-timeout-ms",
            "50",
            "--result-json",
            str(result_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert proc.returncode == 1
    assert result["block_reason"] == "archive_db_fast_lane_deferred"
    assert result["archive_write_budget"] == {
        "mode": "source_fast_lane",
        "queue_wait_sec": 0.0,
        "db_timeout_sec": 0.1,
        "db_busy_timeout_ms": 50,
    }
    assert result["guards"]["writes_archive_db"] is False
