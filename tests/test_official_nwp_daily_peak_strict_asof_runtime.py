from __future__ import annotations

import sqlite3

from official_nwp_daily_peak_strict_asof_runtime import build_official_nwp_daily_peak_tier_a_proposal
from sequential_bayes_schema_contract import apply_forward_schema
from strict_asof_forecast_p1_ingress_writer import persist_tier_a_forecast_p1_proposal
from strict_asof_forecast_p1_reader import load_strict_forecast_p1_source_observations


def _revision() -> dict:
    expected = ["2026-07-13T08:00:00Z", "2026-07-13T09:00:00Z"]
    return {
        "schema": "official_nwp_daily_peak_forecast_revision_v1",
        "ok": True,
        "state": "available",
        "source_id": "gfs",
        "event_date": "2026-07-13",
        "available_at": "2026-07-12T11:12:13.920Z",
        "forecast_peak_c": 32.815,
        "forecast_peak_valid_time": "2026-07-13T09:00:00Z",
        "run_at_utc": "2026-07-12T06:00:00Z",
        "expected_valid_times_utc": expected,
        "hourly_curve": [
            {"valid_time_utc": expected[0], "temperature_c": 31.2, "snapshot_sha256": "s1"},
            {"valid_time_utc": expected[1], "temperature_c": 32.815, "snapshot_sha256": "s2"},
        ],
        "raw_sha256": "aggregate-raw",
        "raw_sha256s": ["r1", "r2"],
        "raw_payload_sha256s": [["payload-1"], ["payload-2"]],
        "raw_object_refs": ["/immutable/raw-1.grib2", "/immutable/raw-2.grib2"],
        "snapshot_sha256s": ["s1", "s2"],
        "strict_asof_tier_a": True,
        "response_headers_observed": True,
    }


def _initialize(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE source_observations (
            source_event_id TEXT PRIMARY KEY, source_id TEXT, source_class TEXT,
            station_id TEXT, event_date TEXT, valid_time TEXT, fetched_at TEXT,
            temperature_c REAL, raw_ref TEXT, raw_sha256 TEXT, parser_version TEXT,
            observation_json TEXT, created_at TEXT, source_observation_id TEXT UNIQUE,
            role TEXT, valid_time_local TEXT, parsed_at TEXT,
            response_completed_at TEXT, response_duration_ms REAL, dewpoint_c REAL,
            observed_today_max_c REAL, forecast_peak_c REAL, hard_eligible INTEGER,
            demotion_reason TEXT, quality_flags_json TEXT
        )
        """
    )


def test_complete_tier_a_official_curve_becomes_a_p1_readable_forecast_observation() -> None:
    proposal = build_official_nwp_daily_peak_tier_a_proposal(
        revision=_revision(), station_id="ZUUU", revision_ref="file:///immutable/gfs-revision.json"
    )
    assert proposal["ok"] is True
    observation = proposal["source_observation"]
    assert observation["source_id"] == "gfs"
    assert observation["source_class"] == "forecast_weak_source"
    assert observation["payload"]["official_nwp_daily_curve_complete"] is True
    assert observation["payload"]["official_nwp_raw_sha256s"] == ["r1", "r2"]

    conn = sqlite3.connect(":memory:")
    _initialize(conn)
    apply_forward_schema(conn)
    persisted = persist_tier_a_forecast_p1_proposal(conn, proposal)
    assert persisted["state"] == "appended"
    loaded = load_strict_forecast_p1_source_observations(conn, event_date="2026-07-13")
    assert loaded["ok"] is True
    assert loaded["source_observation_count"] == 1
    assert loaded["source_observations"][0]["source_id"] == "gfs"


def test_incomplete_or_non_tier_a_curve_cannot_cross_to_p1() -> None:
    revision = _revision()
    revision["raw_sha256s"] = ["r1"]
    revision["response_headers_observed"] = False
    result = build_official_nwp_daily_peak_tier_a_proposal(
        revision=revision, station_id="ZUUU", revision_ref="file:///immutable/gfs-revision.json"
    )
    assert result["ok"] is False
    assert "official_nwp_daily_peak_not_all_points_tier_a" in result["block_reasons"]
    assert "official_nwp_daily_peak_complete_curve_lineage_invalid" in result["block_reasons"]
