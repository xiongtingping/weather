import json
import sqlite3

from scripts.production_probability_v6_live_acceptance import (
    _audit_latest_source_time_bound,
    _audit_weak_source_freshness,
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE weak_forecast_evidence_snapshots ("
        "forecast_evidence_snapshot_id TEXT PRIMARY KEY,decision_as_of TEXT,source_revisions_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE source_observations ("
        "source_id TEXT,source_event_id TEXT,fetched_at TEXT,event_date TEXT,source_class TEXT)"
    )
    conn.execute(
        "CREATE TABLE canonical_observation_events ("
        "canonical_observation_event_id TEXT,target_identity TEXT,"
        "first_actionable_seen_at TEXT,state TEXT)"
    )
    return conn


def test_freshness_accepts_consumed_latest_taf() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO weak_forecast_evidence_snapshots VALUES (?,?,?)",
        ("snap", "2026-07-17T03:56:15Z", json.dumps([{"source_id": "taf", "available_at": "2026-07-16T21:30:36Z"}])),
    )
    conn.execute(
        "INSERT INTO source_observations VALUES (?,?,?,?,?)",
        ("zuuu_taf_forecast_amsc", "rev-new", "2026-07-16T21:30:36Z", "2026-07-17", "forecast_weak_source"),
    )
    result = _audit_weak_source_freshness(
        conn,
        checkpoint={"forecast_evidence_snapshot_id": "snap"},
        event_date="2026-07-17",
        independent_observations=[],
    )
    assert result["ok"] is True
    assert result["state"] == "current"


def test_freshness_blocks_newer_unconsumed_revision() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO weak_forecast_evidence_snapshots VALUES (?,?,?)",
        ("snap", "2026-07-17T03:56:15Z", json.dumps([{"source_id": "taf", "available_at": "2026-07-16T21:30:36Z"}])),
    )
    conn.execute(
        "INSERT INTO source_observations VALUES (?,?,?,?,?)",
        ("zuuu_taf_forecast_amsc", "rev-later", "2026-07-17T04:00:00Z", "2026-07-17", "forecast_weak_source"),
    )
    result = _audit_weak_source_freshness(
        conn,
        checkpoint={"forecast_evidence_snapshot_id": "snap"},
        event_date="2026-07-17",
        independent_observations=[],
    )
    assert result["ok"] is False
    assert result["state"] == "stale_newer_revision_not_consumed"
    assert result["stale_sources"][0]["source_id"] == "taf"


def test_source_time_bound_accepts_latest_source_before_posterior() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO canonical_observation_events VALUES (?,?,?,?)",
        ("event-1", "target", "2026-07-17T04:01:00Z", "accepted_primary"),
    )
    result = _audit_latest_source_time_bound(
        conn,
        checkpoint={"committed_at": "2026-07-17T04:01:00.250Z"},
        target_identity="target",
        weak_source_freshness={"latest_source_time": "2026-07-17T04:00:00Z"},
    )
    assert result["ok"] is True
    assert result["condition"] == "latest_source_time <= posterior_snapshot_time"
    assert result["latest_source_kind"] == "canonical:event-1"
    assert result["delta_ms"] == 250.0


def test_source_time_bound_blocks_source_after_posterior() -> None:
    conn = _db()
    result = _audit_latest_source_time_bound(
        conn,
        checkpoint={"committed_at": "2026-07-17T04:00:00Z"},
        target_identity="target",
        weak_source_freshness={"latest_source_time": "2026-07-17T04:00:01Z"},
    )
    assert result["ok"] is False
    assert result["delta_ms"] == -1000.0
    assert "latest_source_time_after_posterior_snapshot_time" in result["violations"]


def test_weak_snapshot_blocks_consumed_source_after_decision_as_of() -> None:
    conn = _db()
    conn.execute(
        "INSERT INTO weak_forecast_evidence_snapshots VALUES (?,?,?)",
        ("snap", "2026-07-17T03:59:59Z", json.dumps([{"source_id": "taf", "available_at": "2026-07-17T04:00:00Z"}])),
    )
    result = _audit_weak_source_freshness(
        conn,
        checkpoint={"forecast_evidence_snapshot_id": "snap"},
        event_date="2026-07-17",
        independent_observations=[],
    )
    assert result["ok"] is False
    assert result["state"] == "snapshot_asof_order_invalid"
