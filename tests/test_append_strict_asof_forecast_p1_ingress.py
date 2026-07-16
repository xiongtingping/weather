from __future__ import annotations

import gzip
import json
import sqlite3

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from scripts.append_strict_asof_forecast_p1_ingress import (
    CONFIRMATION_PHRASE,
    apply_ingress_contract,
    build_ingress_contract,
)
from sequential_bayes_schema_contract import apply_forward_schema


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


def _write_tier_a_capture(root, *, source_id: str = "ecmwf_model_peak") -> None:
    payload = {
        "schema": "forecast_source_forward_capture_payload_v2",
        "derived_forecast": {
            "source_id": source_id,
            "target_event_date_local": "2026-07-12",
            "forecast_peak_c": 33.0,
            "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 33.0}],
        },
        "raw_provider_response_utf8": '{"hourly":{"temperature_2m":[33.0]}}',
    }
    envelope = build_external_forecast_raw_envelope(
        source_id=source_id,
        response_body_utf8=json.dumps(payload),
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="forward-capture-v2",
        source_event_id=f"forward_{source_id}_2026-07-12",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )
    path = root / source_id / "capture.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle)


def test_ingress_scan_is_read_only_and_blocks_without_p1_schema(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    _write_tier_a_capture(raw_root)
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _initialize_source_observations(conn)

    contract = build_ingress_contract(db_path=db, root=raw_root, station_id="ZUUU")

    assert contract["state"] == "blocked"
    assert contract["scan"]["tier_counts"] == {"A": 1, "B": 0, "C": 0}
    assert len(contract["scan"]["eligible"]) == 1
    assert "p1_ingress_schema_not_ready" in contract["blockers"]
    assert contract["guards"]["writes_archive_db"] is False
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0


def test_explicit_apply_appends_tier_a_only_after_p1_schema_and_is_idempotent(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    _write_tier_a_capture(raw_root)
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _initialize_source_observations(conn)
        apply_forward_schema(conn)

    first = apply_ingress_contract(
        db_path=db,
        root=raw_root,
        station_id="ZUUU",
        confirm=CONFIRMATION_PHRASE,
        writer_quiescent=True,
    )
    second = apply_ingress_contract(
        db_path=db,
        root=raw_root,
        station_id="ZUUU",
        confirm=CONFIRMATION_PHRASE,
        writer_quiescent=True,
    )

    assert first["state"] == "applied"
    assert first["append_results"][0]["state"] == "appended"
    assert second["append_results"][0]["state"] == "idempotent"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 0
        payload = conn.execute("SELECT observation_json FROM source_observations").fetchone()[0]
    assert '"response_headers_observed":true' in payload
