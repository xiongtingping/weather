from __future__ import annotations

import copy
import sqlite3

import pytest

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from sequential_bayes_schema_contract import apply_forward_schema
from strict_asof_forecast_capture_runtime import classify_strict_asof_forecast_capture
from strict_asof_forecast_p1_ingress_writer import (
    StrictForecastP1IngressError,
    persist_tier_a_forecast_p1_proposal,
    verify_strict_forecast_p1_ingress_schema,
)
from strict_asof_forecast_source_observation_runtime import (
    build_tier_a_forecast_source_observation_proposal,
)


def _initialize_source_observations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE source_observations (
            source_event_id TEXT PRIMARY KEY,
            source_id TEXT,
            source_class TEXT,
            station_id TEXT,
            event_date TEXT,
            valid_time TEXT,
            fetched_at TEXT,
            temperature_c REAL,
            raw_ref TEXT,
            raw_sha256 TEXT,
            parser_version TEXT,
            observation_json TEXT,
            created_at TEXT,
            source_observation_id TEXT UNIQUE,
            role TEXT,
            valid_time_local TEXT,
            parsed_at TEXT,
            response_completed_at TEXT,
            response_duration_ms REAL,
            dewpoint_c REAL,
            observed_today_max_c REAL,
            forecast_peak_c REAL,
            hard_eligible INTEGER,
            demotion_reason TEXT,
            quality_flags_json TEXT
        )
        """
    )


def _proposal(*, daily_window: bool = False) -> dict:
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
    if daily_window:
        forecast.update(
            {
                "forecast_peak_c": 34.0,
                "forecast_value_kind": "daily_max_event_window",
                "forecast_record_valid_time": "2026-07-12T07:00:00+08:00",
            }
        )
    capture = classify_strict_asof_forecast_capture(raw_envelope=envelope, normalized_forecast=forecast)
    proposal = build_tier_a_forecast_source_observation_proposal(
        capture=capture,
        normalized_forecast=forecast,
        station_id="ZUUU",
        raw_ref="research://forecast/ecmwf/20260711T000000Z.json.gz",
    )
    assert proposal["ok"] is True
    return proposal


def test_ingress_requires_completed_p1_schema_before_any_append() -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)

    schema = verify_strict_forecast_p1_ingress_schema(conn)

    assert schema["ok"] is False
    assert "canonical_observation_events" in schema["missing_p1_tables"]
    with pytest.raises(StrictForecastP1IngressError, match="strict_forecast_p1_schema_not_ready"):
        persist_tier_a_forecast_p1_proposal(conn, _proposal())
    assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0


def test_tier_a_ingress_appends_once_and_does_not_create_posterior_or_fact() -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    proposal = _proposal()

    first = persist_tier_a_forecast_p1_proposal(conn, proposal)
    second = persist_tier_a_forecast_p1_proposal(conn, proposal)

    assert first["state"] == "appended"
    assert second["state"] == "idempotent"
    row = conn.execute(
        "SELECT source_class,hard_eligible,demotion_reason,observation_json FROM source_observations"
    ).fetchone()
    assert row[:3] == ("forecast_weak_source", 0, "forecast_source_not_hard_observation")
    assert '"response_headers_observed":true' in row[3]
    assert conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0


def test_tier_a_ingress_rejects_header_claim_loss_and_immutable_drift() -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    proposal = _proposal()
    persist_tier_a_forecast_p1_proposal(conn, proposal)

    header_lost = copy.deepcopy(proposal)
    header_lost["source_observation"]["payload"]["response_headers_observed"] = False
    with pytest.raises(StrictForecastP1IngressError, match="strict_forecast_p1_response_headers_not_observed"):
        persist_tier_a_forecast_p1_proposal(conn, header_lost)

    drift = copy.deepcopy(proposal)
    drift["source_observation"]["forecast_peak_c"] = 34.0
    with pytest.raises(StrictForecastP1IngressError, match="strict_forecast_p1_source_identity_binding_mismatch"):
        persist_tier_a_forecast_p1_proposal(conn, drift)
    assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 1


def test_tier_a_ingress_accepts_daily_window_without_turning_it_into_a_trajectory() -> None:
    conn = sqlite3.connect(":memory:")
    _initialize_source_observations(conn)
    apply_forward_schema(conn)
    proposal = _proposal(daily_window=True)

    result = persist_tier_a_forecast_p1_proposal(conn, proposal)

    assert result["state"] == "appended"
    row = conn.execute("SELECT valid_time,quality_flags_json,observation_json FROM source_observations").fetchone()
    assert row[0] == "2026-07-12T07:00:00+08:00"
    assert "daily_max_event_window" in row[1]
    assert '"trajectory_eligible":false' in row[2]
