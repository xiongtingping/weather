from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from resolution_final_source_shadow_writer import (
    initialize_isolated_resolution_final_source_staging_schema,
    persist_final_source_observation_shadow,
)
from scripts.build_joint_forecast_weak_training_events import build_event_envelope
from scripts.build_forecast_weak_context_captures import build_context_capture_envelope
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_schema_contract import apply_forward_schema
from strict_asof_forecast_capture_runtime import classify_strict_asof_forecast_capture
from strict_asof_forecast_p1_ingress_writer import persist_tier_a_forecast_p1_proposal
from strict_asof_forecast_source_observation_runtime import build_tier_a_forecast_source_observation_proposal


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _context() -> dict:
    return {
        "schema": "forecast_weak_context_capture_v1",
        "context_capture_id": "context-1",
        "context_raw_sha256": _sha("context"),
        "context_source_class": "forecast_weak_context",
        "target_identity": "target-2026-07-12",
        "event_date": "2026-07-12",
        "decision_as_of": "2026-07-12T06:30:00Z",
        "context_available_at": "2026-07-12T06:00:00Z",
        "forecast_context": {"season": "summer", "weather_regime": "clear", "lead_bucket": "0_6h"},
    }


def _station_day_context() -> dict:
    context = _context()
    context.pop("target_identity")
    context.update({"identity_kind": "station_day", "training_identity": "station_day:ZUUU:2026-07-12", "station_id": "ZUUU"})
    return context


def _physical_source() -> dict:
    return {
        "source_observation_id": "physical-1",
        "source_id": "amsc_zuuu_scheduled_metar_daily_max_v2",
        "source_class": "settlement_source",
        "role": "physical_daily_high_final_truth",
        "station_id": "ZUUU",
        "event_date": "2026-07-12",
        "fetched_at": "2026-07-12T18:00:00Z",
        "raw_ref": "raw://physical",
        "raw_sha256": _sha("physical"),
        "payload": {
            "final_physical_high_c": 32.0,
            "label_finality": "final",
            "physical_x_label_quality": "strict_asof_complete_schedule",
            "strict_asof_training_eligible": True,
            "late_arrival_count": 0,
            "physical_x_provenance_schema": "amsc_station_day_raw_membership_v1",
            "physical_source_observation_ids": ["amsc-physical-1"],
            "physical_raw_sha256_list": [_sha("physical-member")],
        },
    }


def _persist_strict_gfs(conn: sqlite3.Connection) -> None:
    envelope = build_external_forecast_raw_envelope(
        source_id="gfs",
        response_body_utf8='{"provider":"gfs"}',
        requested_at="2026-07-12T05:59:00.000Z",
        response_started_at="2026-07-12T05:59:00.010Z",
        response_completed_at="2026-07-12T06:00:00.000Z",
        http_status=200,
        parser_version="gfs-capture-v1",
        source_event_id="gfs-20260712",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )
    forecast = {
        "source_id": "gfs",
        "target_event_date_local": "2026-07-12",
        "forecast_peak_c": 31.5,
        "forecast_snapshot_id": envelope["response_sha256"],
        "source_event_id": "gfs-20260712",
        "raw_provider_response_present": True,
        "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 31.5}],
    }
    proposal = build_tier_a_forecast_source_observation_proposal(
        capture=classify_strict_asof_forecast_capture(raw_envelope=envelope, normalized_forecast=forecast),
        normalized_forecast=forecast,
        station_id="ZUUU",
        raw_ref="raw://gfs",
    )
    persist_tier_a_forecast_p1_proposal(conn, proposal)


def test_event_envelope_reads_only_p1_and_isolated_final_labels(tmp_path: Path) -> None:
    p1_path = tmp_path / "p1.db"
    with sqlite3.connect(p1_path) as p1:
        p1.execute(
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
        apply_forward_schema(p1)
        _persist_strict_gfs(p1)
    final_source_path = tmp_path / "final-sources.db"
    with sqlite3.connect(final_source_path) as labels:
        initialize_isolated_public_ssot_shadow_schema(labels)
        initialize_isolated_resolution_final_source_staging_schema(labels)
        persist_final_source_observation_shadow(labels, _physical_source())

    result = build_event_envelope(context_captures=[_context()], p1_db=p1_path, final_source_db=final_source_path)

    assert result["state"] == "available"
    assert result["events"][0]["source_revisions"][0]["source_id"] == "gfs"
    assert result["boundary"]["training_or_oos_is_not_live_ingest_gate"] is True


def test_event_envelope_fails_closed_when_label_registry_is_not_available(tmp_path: Path) -> None:
    result = build_event_envelope(
        context_captures=[_context()], p1_db=tmp_path / "missing-p1.db", final_source_db=tmp_path / "missing-final-sources.db"
    )

    assert result["state"] == "blocked"
    assert any(reason.startswith("final_physical_source_registry_open_failed") for reason in result["blockers"])


def test_event_envelope_accepts_station_day_context_without_market_target(tmp_path: Path) -> None:
    p1_path = tmp_path / "p1.db"
    with sqlite3.connect(p1_path) as p1:
        p1.execute(
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
        apply_forward_schema(p1)
        _persist_strict_gfs(p1)
    final_source_path = tmp_path / "final-sources.db"
    with sqlite3.connect(final_source_path) as labels:
        initialize_isolated_public_ssot_shadow_schema(labels)
        initialize_isolated_resolution_final_source_staging_schema(labels)
        persist_final_source_observation_shadow(labels, _physical_source())

    result = build_event_envelope(context_captures=[_station_day_context()], p1_db=p1_path, final_source_db=final_source_path)

    assert result["state"] == "available"
    assert result["events"][0]["training_identity"] == "station_day:ZUUU:2026-07-12"
    assert "target_identity" not in result["events"][0]
    assert result["boundary"]["station_day_training_identity_without_market_target_supported"] is True


def test_event_envelope_identity_ignores_unrelated_closed_physical_days(tmp_path: Path) -> None:
    p1_path = tmp_path / "p1.db"
    with sqlite3.connect(p1_path) as p1:
        p1.execute(
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
        apply_forward_schema(p1)
        _persist_strict_gfs(p1)
    final_source_path = tmp_path / "final-sources.db"
    with sqlite3.connect(final_source_path) as labels:
        initialize_isolated_public_ssot_shadow_schema(labels)
        initialize_isolated_resolution_final_source_staging_schema(labels)
        persist_final_source_observation_shadow(labels, _physical_source())
        unrelated = _physical_source()
        unrelated.update({"source_observation_id": "physical-irrelevant", "event_date": "2026-07-11", "raw_sha256": _sha("irrelevant")})
        unrelated["payload"] = {**unrelated["payload"], "physical_raw_sha256_list": [_sha("irrelevant-member")]}
        persist_final_source_observation_shadow(labels, unrelated)

    result = build_event_envelope(context_captures=[_station_day_context()], p1_db=p1_path, final_source_db=final_source_path)

    assert result["state"] == "available"
    assert result["requested_event_dates"] == ["2026-07-12"]
    assert result["final_physical_source_count"] == 1


def test_context_capture_envelope_derives_context_from_p1_only(tmp_path: Path) -> None:
    p1_path = tmp_path / "p1.db"
    with sqlite3.connect(p1_path) as p1:
        p1.execute(
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
        apply_forward_schema(p1)
        _persist_strict_gfs(p1)

    result = build_context_capture_envelope(
        cutoffs=[{"target_identity": "target-2026-07-12", "event_date": "2026-07-12", "decision_as_of": "2026-07-12T06:30:00Z"}],
        p1_db=p1_path,
    )

    assert result["state"] == "available"
    assert result["context_captures"][0]["forecast_context"]["weather_regime"] == "unknown_forecast_regime"
    assert result["boundary"]["live_amsc_ingest_blocked"] is False


def test_context_capture_envelope_accepts_cutoff_without_market_target(tmp_path: Path) -> None:
    p1_path = tmp_path / "p1.db"
    with sqlite3.connect(p1_path) as p1:
        p1.execute(
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
        apply_forward_schema(p1)
        _persist_strict_gfs(p1)

    result = build_context_capture_envelope(
        cutoffs=[{"event_date": "2026-07-12", "decision_as_of": "2026-07-12T06:30:00Z", "station_id": "ZUUU"}],
        p1_db=p1_path,
    )

    assert result["state"] == "available"
    assert result["context_captures"][0]["training_identity"] == "station_day:ZUUU:2026-07-12"
