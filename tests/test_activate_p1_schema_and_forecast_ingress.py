from __future__ import annotations

import gzip
import json
import sqlite3

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
import scripts.activate_p1_schema_and_forecast_ingress as activation
from strict_asof_forecast_p1_ingress_writer import StrictForecastP1IngressError


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


def _write_tier_a_capture(root) -> None:
    payload = {
        "schema": "forecast_source_forward_capture_payload_v2",
        "derived_forecast": {
            "source_id": "ecmwf_model_peak",
            "target_event_date_local": "2026-07-12",
            "forecast_peak_c": 33.0,
            "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 33.0}],
        },
        "raw_provider_response_utf8": '{"hourly":{"temperature_2m":[33.0]}}',
    }
    envelope = build_external_forecast_raw_envelope(
        source_id="ecmwf_model_peak",
        response_body_utf8=json.dumps(payload),
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="forward-capture-v2",
        source_event_id="forward_ecmwf_model_peak_2026-07-12",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )
    path = root / "ecmwf_model_peak" / "capture.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle)


def test_atomic_activation_plan_is_read_only_and_combines_ddl_with_ingress(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    _write_tier_a_capture(raw_root)
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _initialize_source_observations(conn)

    plan = activation.build_activation_contract(db_path=db, root=raw_root, station_id="ZUUU")

    assert plan["state"] == "planned"
    assert plan["eligible_append_count"] == 1
    assert plan["schema_plan"]["actions"]
    assert plan["transaction_contract"]["schema_ddl_and_forecast_append_atomic"] is True
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='canonical_observation_events'"
        ).fetchone()[0] == 0


def test_atomic_activation_applies_or_idempotently_replays_everything(tmp_path) -> None:
    raw_root = tmp_path / "raw"
    _write_tier_a_capture(raw_root)
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _initialize_source_observations(conn)

    first = activation.apply_activation_contract(
        db_path=db,
        root=raw_root,
        station_id="ZUUU",
        confirm=activation.CONFIRMATION_PHRASE,
        writer_quiescent=True,
    )
    second = activation.apply_activation_contract(
        db_path=db,
        root=raw_root,
        station_id="ZUUU",
        confirm=activation.CONFIRMATION_PHRASE,
        writer_quiescent=True,
    )

    assert first["state"] == "applied"
    assert first["p1_schema_after"]["ok"] is True
    assert first["append_results"][0]["state"] == "appended"
    assert second["schema_apply"]["applied"] == []
    assert second["append_results"][0]["state"] == "idempotent"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM canonical_observation_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 0


def test_atomic_activation_rolls_back_ddl_when_forecast_append_fails(tmp_path, monkeypatch) -> None:
    raw_root = tmp_path / "raw"
    _write_tier_a_capture(raw_root)
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _initialize_source_observations(conn)
    monkeypatch.setattr(
        activation,
        "persist_tier_a_forecast_p1_proposal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StrictForecastP1IngressError("injected_append_failure")),
    )

    result = activation.apply_activation_contract(
        db_path=db,
        root=raw_root,
        station_id="ZUUU",
        confirm=activation.CONFIRMATION_PHRASE,
        writer_quiescent=True,
    )

    assert result["execution_mode"] == "rolled_back"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='canonical_observation_events'"
        ).fetchone()[0] == 0


def test_atomic_activation_refuses_apply_when_memory_replay_preflight_fails(tmp_path, monkeypatch) -> None:
    raw_root = tmp_path / "raw"
    _write_tier_a_capture(raw_root)
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _initialize_source_observations(conn)
    monkeypatch.setattr(
        activation,
        "build_p1_forecast_ingress_staging_probe",
        lambda **_kwargs: {"ok": False, "state": "blocked"},
    )

    result = activation.apply_activation_contract(
        db_path=db,
        root=raw_root,
        station_id="ZUUU",
        confirm=activation.CONFIRMATION_PHRASE,
        writer_quiescent=True,
    )

    assert result["execution_mode"] == "blocked"
    assert "p1_forecast_ingress_staging_probe_failed" in result["blockers"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0] == 0
