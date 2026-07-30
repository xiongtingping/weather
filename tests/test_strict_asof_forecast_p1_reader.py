from __future__ import annotations

import sqlite3

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from sequential_bayes_schema_contract import apply_forward_schema
from strict_asof_forecast_capture_runtime import classify_strict_asof_forecast_capture
from strict_asof_forecast_p1_ingress_writer import persist_tier_a_forecast_p1_proposal
from strict_asof_forecast_p1_reader import load_strict_forecast_p1_source_observations
from strict_asof_forecast_source_observation_runtime import build_tier_a_forecast_source_observation_proposal


def _initialize_source_observations(conn: sqlite3.Connection) -> None:
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


def _proposal() -> dict:
    envelope = build_external_forecast_raw_envelope(
        source_id="ecmwf",
        response_body_utf8='{"raw":"provider"}',
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="capture-v2",
        source_event_id="forward_ecmwf_2026-07-12",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )
    forecast = {
        "source_id": "ecmwf",
        "target_event_date_local": "2026-07-12",
        "forecast_peak_c": 33.0,
        "forecast_snapshot_id": envelope["response_sha256"],
        "source_event_id": "forward_ecmwf_2026-07-12",
        "raw_provider_response_present": True,
        "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 33.0}],
    }
    capture = classify_strict_asof_forecast_capture(raw_envelope=envelope, normalized_forecast=forecast)
    return build_tier_a_forecast_source_observation_proposal(
        capture=capture,
        normalized_forecast=forecast,
        station_id="ZUUU",
        raw_ref="research://forecast/ecmwf/capture.json.gz",
    )


def test_reader_rehydrates_only_bound_strict_forecast_observations() -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    persist_tier_a_forecast_p1_proposal(conn, _proposal())

    result = load_strict_forecast_p1_source_observations(conn, event_date="2026-07-12")

    assert result["ok"] is True
    assert result["source_observation_count"] == 1
    observation = result["source_observations"][0]
    assert observation["payload"]["response_headers_observed"] is True
    assert observation["forecast_peak_c"] == 33.0


def test_reader_accepts_forward_added_generation_columns_without_weakening_p1() -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    conn.execute("ALTER TABLE posterior_checkpoints ADD COLUMN future_generation_sha256 TEXT")
    persist_tier_a_forecast_p1_proposal(conn, _proposal())

    result = load_strict_forecast_p1_source_observations(conn, event_date="2026-07-12")

    assert result["ok"] is True
    assert result["source_observation_count"] == 1
    assert result["p1_schema"]["verification_mode"] == "additive_runtime_compatibility"
    assert result["p1_schema"]["accepted_extension_columns"]["posterior_checkpoints"] == [
        "future_generation_sha256"
    ]


def test_reader_fails_closed_on_column_json_binding_drift() -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    persist_tier_a_forecast_p1_proposal(conn, _proposal())
    conn.execute("UPDATE source_observations SET raw_sha256='tampered'")

    result = load_strict_forecast_p1_source_observations(conn, event_date="2026-07-12")

    assert result["ok"] is False
    assert result["source_observation_count"] == 0
    assert "stored_observation_raw_sha256_binding_mismatch" in result["blocked"][0]["block_reasons"]


def test_reader_quarantines_only_tampered_source_and_keeps_other_tier_a_source() -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    first = _proposal()
    # Build the second proposal through the public path so all JSON/column
    # bindings remain legitimate before deliberately corrupting only it.
    envelope = build_external_forecast_raw_envelope(
        source_id="gfs", response_body_utf8='{"raw":"gfs"}',
        requested_at="2026-07-11T00:00:00.000Z", response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z", http_status=200,
        parser_version="capture-v2", source_event_id="forward_gfs_2026-07-12",
        capture_time_provenance="observed_transport", response_headers_observed=True,
    )
    forecast = {
        "source_id": "gfs", "target_event_date_local": "2026-07-12", "forecast_peak_c": 32.0,
        "forecast_snapshot_id": envelope["response_sha256"], "source_event_id": "forward_gfs_2026-07-12",
        "raw_provider_response_present": True,
        "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 32.0}],
    }
    persist_tier_a_forecast_p1_proposal(conn, first)
    persist_tier_a_forecast_p1_proposal(conn, build_tier_a_forecast_source_observation_proposal(
        capture=classify_strict_asof_forecast_capture(raw_envelope=envelope, normalized_forecast=forecast),
        normalized_forecast=forecast, station_id="ZUUU", raw_ref="research://forecast/gfs/capture.json.gz",
    ))
    conn.execute("UPDATE source_observations SET raw_sha256='tampered' WHERE source_id='gfs'")

    result = load_strict_forecast_p1_source_observations(conn, event_date="2026-07-12")

    assert result["ok"] is True
    assert result["state"] == "available_with_quarantined_sources"
    assert result["source_observation_count"] == 1
    assert result["source_observations"][0]["source_id"] == "ecmwf"
    assert result["blocked"][0]["block_reasons"] == ["stored_observation_raw_sha256_binding_mismatch"]
