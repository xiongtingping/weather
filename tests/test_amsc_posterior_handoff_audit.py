import json
import sqlite3

from scripts.amsc_posterior_handoff_audit import audit_amsc_posterior_handoff


def _db(tmp_path):
    path = tmp_path / "archive.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE event_fact_history (
          id INTEGER PRIMARY KEY, slug TEXT, event_date TEXT, fact_as_of TEXT,
          created_at TEXT, source_event_id TEXT, fact_snapshot_id TEXT, fact_json TEXT NOT NULL
        );
        CREATE TABLE task_queue (
          id INTEGER PRIMARY KEY, task_type TEXT, status TEXT, created_at TEXT,
          started_at TEXT, completed_at TEXT, error TEXT, payload_json TEXT
        );
        """
    )
    return conn


def _source_payload():
    return {
        "amsc_observation_bundle_schema": "amsc_zuuu_metar_taf_observation_bundle_v1",
        "weather_likelihood_features": {"zuuu_metar_raw_amsc_latest_at": "2026-07-12T14:30:00Z"},
    }


def test_reports_source_only_without_historical_requeue(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO event_fact_history VALUES (7,'s','2026-07-12','2026-07-12T14:30:41Z','2026-07-12T14:30:41Z','e','f',?)",
        (json.dumps(_source_payload()),),
    )
    report = audit_amsc_posterior_handoff(conn, latest_only=True)
    assert report["ok"] is True
    assert report["sources"][0]["transaction_a"] == "committed"
    assert report["sources"][0]["transaction_a1"] == "not_enqueued"
    assert report["sources"][0]["warnings"] == ["posterior_handoff_not_enqueued"]
    assert report["guards"]["requeues_tasks"] is False
    required = audit_amsc_posterior_handoff(conn, latest_only=True, require_enqueued=True)
    assert required["ok"] is False
    assert "posterior_handoff_required_but_not_enqueued" in required["sources"][0]["blockers"]


def test_validates_task_is_bound_to_source_and_official_generation(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO event_fact_history VALUES (8,'s','2026-07-12','t','t','e','f',?)",
        (json.dumps(_source_payload()),),
    )
    payload = {
        "schema": "amsc_posterior_materialization_task_payload_v1",
        "amsc_source_fact_id": 8,
        "official_nwp_context_policy": {
            "schema": "official_nwp_context_policy_v1",
            "mode": "prewarmed_cache_only_v1",
            "cache_generation_ref": {"generation_id": "g-1"},
        },
    }
    conn.execute(
        "INSERT INTO task_queue VALUES (9,'amsc_posterior_materialize','pending','t',NULL,NULL,'',?)",
        (json.dumps(payload),),
    )
    report = audit_amsc_posterior_handoff(conn, latest_only=True)
    source = report["sources"][0]
    assert report["ok"] is True
    assert source["transaction_a1"] == "enqueued"
    assert source["task"]["official_nwp_generation_id"] == "g-1"


def test_rejects_unpinned_task_payload(tmp_path):
    conn = _db(tmp_path)
    conn.execute(
        "INSERT INTO event_fact_history VALUES (10,'s','2026-07-12','t','t','e','f',?)",
        (json.dumps(_source_payload()),),
    )
    conn.execute(
        "INSERT INTO task_queue VALUES (11,'amsc_posterior_materialize','pending','t',NULL,NULL,'',?)",
        (json.dumps({"amsc_source_fact_id": 10}),),
    )
    report = audit_amsc_posterior_handoff(conn, latest_only=True)
    assert report["ok"] is False
    assert "task_payload_schema_invalid" in report["sources"][0]["blockers"]
    assert "official_nwp_generation_reference_missing" in report["sources"][0]["blockers"]
