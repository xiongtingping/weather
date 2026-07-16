from __future__ import annotations

import json
import sqlite3

from scripts.promote_final_bucket_lock_facts import (
    CONFIRM_PHRASE,
    apply_plan,
    build_plan,
    main,
)
from ui import server


def _setup_db(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    server.ensure_fact_tables(conn)
    return conn


def _insert_latest_fact(conn, payload):
    slug = "highest-temperature-in-chengdu-on-june-22-2026"
    event_date = "2026-06-22"
    conn.execute(
        """
        INSERT INTO event_fact_history(
          run_date, slug, event_date, fact_as_of, model_snapshot_id, market_snapshot_id,
          composer_version, best_outcome, fact_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_date,
            slug,
            event_date,
            "2026-06-23T12:41:31+00:00",
            1007,
            10763,
            "legacy_test",
            "33°C",
            json.dumps(payload, ensure_ascii=False),
            "2026-06-23T12:41:31+00:00",
        ),
    )
    fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (slug, event_date, fact_id, "2026-06-23T12:41:31+00:00"),
    )
    conn.commit()
    return slug, event_date, fact_id


def _legacy_final_lock_payload():
    return {
        "slug": "highest-temperature-in-chengdu-on-june-22-2026",
        "event_date": "2026-06-22",
        "run_date": "2026-06-22",
        "target_bucket": "33°C",
        "settlement_actual": 33.0,
        "market_closed": 1,
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "tz": "Asia/Shanghai",
        "weather_likelihood_features": {
            "settlement_actual": 33.0,
            "settlement_actual_c": 33.0,
            "market_closed": 1,
        },
        "hard_source_posterior_override": {
            "applied": True,
            "final_bucket_lock": True,
            "target_bucket": "33°C",
            "value_c": 33.0,
            "final_lock_reason": "settlement_actual_known",
        },
        "distribution": [
            {"outcome": "32°C", "q_bayes_posterior": 0.0},
            {"outcome": "33°C", "q_bayes_posterior": 1.0},
            {"outcome": "34°C", "q_bayes_posterior": 0.0},
        ],
    }


def _feature_final_lock_payload():
    return {
        "slug": "highest-temperature-in-chengdu-on-june-22-2026",
        "event_date": "2026-06-22",
        "run_date": "2026-06-22",
        "target_bucket": "33°C",
        "rounding_rule": "whole_degrees",
        "market_var": "temperature_2m_max",
        "tz": "Asia/Shanghai",
        "weather_likelihood_features": {
            "zuuu_metar_raw_awc_today_max_c": 33.0,
            "zuuu_metar_raw_awc_today_max_at": "2026-06-22T08:00:00+00:00",
            "observed_today_max_c": 33.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "hard_source_final_daily_max": True,
            "hard_source_settlement_final": True,
        },
        "distribution": [
            {"outcome": "32°C", "q_bayes_posterior": 0.50},
            {"outcome": "33°C", "q_bayes_posterior": 0.03},
            {"outcome": "34°C", "q_bayes_posterior": 0.47},
        ],
    }


def test_build_plan_identifies_legacy_final_lock_fact(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug, event_date, fact_id = _insert_latest_fact(conn, _legacy_final_lock_payload())
    conn.close()

    report = build_plan(db_path, slug=slug, event_date=event_date)

    assert report["eligible_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["eligible"] is True
    assert candidate["fact_id"] == fact_id
    assert candidate["reason"] == "needs_final_lock_promotion"
    assert "final_bucket_lock" in candidate["changed_fields"]
    assert "weather_likelihood_features" in candidate["changed_fields"]


def test_build_plan_identifies_feature_final_lock_fact_without_legacy_meta(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug, event_date, fact_id = _insert_latest_fact(conn, _feature_final_lock_payload())
    conn.close()

    report = build_plan(db_path, slug=slug, event_date=event_date)

    assert report["eligible_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["eligible"] is True
    assert candidate["fact_id"] == fact_id
    assert candidate["meta_final_bucket_lock"] is False
    assert candidate["feature_final_bucket_lock"] is True
    assert candidate["target_bucket"] == "33°C"
    assert candidate["reason"] == "needs_final_lock_promotion"


def test_apply_plan_appends_history_and_updates_latest(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug, event_date, old_fact_id = _insert_latest_fact(conn, _legacy_final_lock_payload())
    conn.close()

    result = apply_plan(
        db_path,
        slug=slug,
        event_date=event_date,
        backup_dir=tmp_path / "backups",
    )

    assert result["ok"] is True
    assert result["applied"] == 1
    assert result["backup"]["created"] is True
    new_fact_id = result["applied_items"][0]["new_fact_id"]
    assert new_fact_id != old_fact_id

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute(
            "SELECT fact_id FROM event_fact_latest WHERE slug=? AND event_date=?",
            (slug, event_date),
        ).fetchone()
        assert latest["fact_id"] == new_fact_id
        old_payload = json.loads(
            conn.execute(
                "SELECT fact_json FROM event_fact_history WHERE id=?",
                (old_fact_id,),
            ).fetchone()["fact_json"]
        )
        new_payload = json.loads(
            conn.execute(
                "SELECT fact_json FROM event_fact_history WHERE id=?",
                (new_fact_id,),
            ).fetchone()["fact_json"]
        )
        summary_payload = json.loads(
            conn.execute(
                "SELECT summary_json FROM event_fact_summary_cache WHERE fact_id=?",
                (new_fact_id,),
            ).fetchone()["summary_json"]
        )
    finally:
        conn.close()

    assert old_payload.get("final_bucket_lock") is None
    assert new_payload["final_bucket_lock"] is True
    assert new_payload["final_bucket"] == "33°C"
    assert new_payload["weather_likelihood_features"]["final_bucket_lock"] is True
    assert new_payload["fact_layer_state"]["write_owner"] == "weather_fact_snapshot_writer"
    assert new_payload["source_acquisition_trace"]["source_observations"]
    assert new_payload["source_acquisition_trace"]["source_observations"][0]["source_id"] == "settlement_actual"
    assert new_payload["lineage_state"]["source_event_ids"]
    assert new_payload["phase0_fact_json_contract"]["historical_backfill"] is False
    assert summary_payload["summary_cache_source"] == "weather_fact_snapshot_writer"
    assert result["applied_items"][0]["write_owner"] == "weather_fact_snapshot_writer"


def test_apply_plan_feature_final_lock_canonicalizes_posterior_ssot_to_one(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    slug, event_date, old_fact_id = _insert_latest_fact(conn, _feature_final_lock_payload())
    conn.close()

    result = apply_plan(
        db_path,
        slug=slug,
        event_date=event_date,
        backup_dir=tmp_path / "backups",
    )

    assert result["ok"] is True
    new_fact_id = result["applied_items"][0]["new_fact_id"]
    assert new_fact_id != old_fact_id

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        new_payload = json.loads(
            conn.execute(
                "SELECT fact_json FROM event_fact_history WHERE id=?",
                (new_fact_id,),
            ).fetchone()["fact_json"]
        )
    finally:
        conn.close()

    assert new_payload["final_bucket_lock"] is True
    assert new_payload["final_bucket"] == "33°C"
    assert new_payload["settlement_actual"] == 33.0
    ssot = new_payload["posterior_ssot_fact"]
    assert ssot["target_bucket"] == "33°C"
    assert ssot["target_probability"] == 1.0
    assert ssot["final_bucket_lock"] is True
    by_bucket = {row["bucket"]: row["probability"] for row in ssot["bucket_probabilities"]}
    assert by_bucket == {"32°C": 0.0, "33°C": 1.0, "34°C": 0.0}


def test_cli_apply_requires_confirmation(tmp_path, capsys):
    db_path = tmp_path / "archive.db"
    conn = _setup_db(db_path)
    _insert_latest_fact(conn, _legacy_final_lock_payload())
    conn.close()

    rc = main(["--db", str(db_path), "--apply", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 2
    assert payload["apply_result"]["block_reason"] == "missing_apply_confirmation"
    assert payload["apply_result"]["required_confirm_phrase"] == CONFIRM_PHRASE
