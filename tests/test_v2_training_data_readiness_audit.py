from __future__ import annotations

import hashlib
import sqlite3
import gzip
import json

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from resolution_bridge_runtime import build_final_resolution_label
from resolution_label_shadow_writer import (
    initialize_isolated_resolution_label_staging_schema,
    persist_final_resolution_label_shadow,
)
from scripts.v2_training_data_readiness_audit import build_v2_training_data_readiness_audit
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema


def _source_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE source_observations (
            source_id TEXT, source_class TEXT, event_date TEXT,
            valid_time TEXT, fetched_at TEXT
        )
        """
    )
    conn.execute("CREATE TABLE event_daily_temps (event_date TEXT)")


def test_readiness_audit_blocks_short_history_missing_p1_and_unproven_resolution_bridge(tmp_path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _source_schema(conn)
        conn.execute(
            "INSERT INTO source_observations VALUES ('ecmwf', 'forecast_weak_source', '2026-07-02', '2026-07-02T00:00:00Z', '2026-07-02T00:01:00Z')"
        )
        conn.execute("INSERT INTO event_daily_temps VALUES ('2026-07-02')")

    report = build_v2_training_data_readiness_audit(db_path=db, min_history_days=365)

    assert report["ok"] is False
    assert "canonical_observation_events" in report["p1_missing_tables"]
    assert "source_observation_history_below_minimum_seasonal_coverage" in report["blockers"]
    assert "forecast_source_family_history_below_minimum_seasonal_coverage" in report["blockers"]
    assert "strict_x_s_b_resolution_label_bridge_not_yet_proven" in report["blockers"]
    assert report["guards"]["writes_archive_db"] is False


def test_readiness_audit_reports_history_coverage_independently_from_resolution_promotion(tmp_path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _source_schema(conn)
        for table in (
            "canonical_observation_events",
            "canonical_observation_event_sources",
            "strong_observation_updates",
            "posterior_checkpoints",
            "realization_states",
            "posterior_commit_outbox",
        ):
            conn.execute(f"CREATE TABLE {table} (id TEXT)")
        conn.execute(
            "INSERT INTO source_observations VALUES ('ecmwf', 'forecast_weak_source', '2025-01-01', '2025-01-01T00:00:00Z', '2025-01-01T00:01:00Z')"
        )
        conn.execute(
            "INSERT INTO source_observations VALUES ('ecmwf', 'forecast_weak_source', '2026-01-01', '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z')"
        )
        conn.execute("INSERT INTO event_daily_temps VALUES ('2025-01-01')")
        conn.execute("INSERT INTO event_daily_temps VALUES ('2026-01-01')")

    report = build_v2_training_data_readiness_audit(db_path=db, min_history_days=365)

    assert report["p1_missing_tables"] == []
    assert report["source_observations"]["coverage_days"] == 366
    assert "source_observation_history_below_minimum_seasonal_coverage" not in report["blockers"]
    assert report["ok"] is False
    assert report["blockers"] == ["strict_x_s_b_resolution_label_bridge_not_yet_proven"]


def test_readiness_audit_adds_tier_a_capture_gate_only_when_the_capture_root_is_explicitly_in_scope(tmp_path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _source_schema(conn)
        conn.execute("INSERT INTO event_daily_temps VALUES ('2026-07-02')")
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    envelope = build_external_forecast_raw_envelope(
        source_id="ecmwf_model_peak",
        response_body_utf8="{}",
        requested_at="2026-07-11T00:00:00Z",
        response_started_at="2026-07-11T00:00:00Z",
        response_completed_at="2026-07-11T00:00:01Z",
        http_status=200,
        parser_version="test-v1",
        capture_time_provenance="batch_clock_proxy",
    )
    with gzip.open(raw_root / "capture.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle)

    report = build_v2_training_data_readiness_audit(
        db_path=db,
        min_history_days=365,
        strict_capture_root=raw_root,
        strict_capture_required_sources=["ecmwf_model_peak"],
    )

    assert "strict_asof_tier_a_capture_coverage_not_ready" in report["blockers"]
    assert report["strict_capture_readiness"]["ok"] is False


def _final_label(day: str) -> dict:
    def lineage(source_id: str, observation_id: str, observed_at: str) -> dict:
        return {
            "source_id": source_id,
            "source_observation_ids": [observation_id],
            "raw_sha256": hashlib.sha256(observation_id.encode("utf-8")).hexdigest(),
            "raw_object_ref": f"research://raw/{observation_id}.json",
            "observed_at": observed_at,
            "finality": "final",
        }

    return build_final_resolution_label(
        target_identity=f"target-{day}",
        event_date=day,
        physical_high_label={
            "final_physical_high_c": 30.0,
            **lineage("physical_truth", f"physical-{day}", f"{day}T18:00:00Z"),
        },
        settlement_label={
            "settlement_scalar_c": 30.0,
            **lineage("official_resolution", f"settlement-{day}", f"{day}T19:00:00Z"),
        },
        resolution_contract={
            "resolution_contract_id": f"contract-{day}",
            "resolution_source_id": "official_resolution",
            "resolution_local_date": day,
            "settlement_unit": "C",
            "physical_to_settlement_kernel": {"30": [{"settlement_value_c": 30.0, "probability": 1.0}]},
            "bucket_contract": {"30C": {"lower_inclusive": 29.5, "upper_exclusive": 30.5}},
            "bridge_closed": True,
            "validated": True,
        },
        label_available_at=f"{day}T19:00:00Z",
    )


def test_readiness_audit_counts_only_isolated_final_label_registry_for_x_s_b_gate(tmp_path) -> None:
    db = tmp_path / "archive.db"
    registry = tmp_path / "labels.sqlite"
    dates = ("2025-01-01", "2026-01-01")
    with sqlite3.connect(db) as conn:
        _source_schema(conn)
        for table in (
            "canonical_observation_events",
            "canonical_observation_event_sources",
            "strong_observation_updates",
            "posterior_checkpoints",
            "realization_states",
            "posterior_commit_outbox",
        ):
            conn.execute(f"CREATE TABLE {table} (id TEXT)")
        for day in dates:
            conn.execute(
                "INSERT INTO source_observations VALUES ('ecmwf', 'forecast_weak_source', ?, ?, ?)",
                (day, f"{day}T00:00:00Z", f"{day}T00:01:00Z"),
            )
            conn.execute("INSERT INTO event_daily_temps VALUES (?)", (day,))
    with sqlite3.connect(registry) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_isolated_resolution_label_staging_schema(conn)
        for day in dates:
            assert persist_final_resolution_label_shadow(conn, _final_label(day))["ok"] is True

    report = build_v2_training_data_readiness_audit(
        db_path=db,
        min_history_days=2,
        resolution_label_registry_path=registry,
    )

    assert report["ok"] is True
    assert report["resolution_label_registry"]["strict_x_s_b_bridge_proven"] is True
    assert "strict_x_s_b_resolution_label_bridge_not_yet_proven" not in report["blockers"]


def test_v2_readiness_requires_seasonal_complete_a_capture_days_not_one_contract_day(tmp_path) -> None:
    db = tmp_path / "archive.db"
    with sqlite3.connect(db) as conn:
        _source_schema(conn)
        for table in (
            "canonical_observation_events",
            "canonical_observation_event_sources",
            "strong_observation_updates",
            "posterior_checkpoints",
            "realization_states",
            "posterior_commit_outbox",
        ):
            conn.execute(f"CREATE TABLE {table} (id TEXT)")
        for day in ("2025-01-01", "2026-01-01"):
            conn.execute(
                "INSERT INTO source_observations VALUES ('ecmwf', 'forecast_weak_source', ?, ?, ?)",
                (day, f"{day}T00:00:00Z", f"{day}T00:01:00Z"),
            )
            conn.execute("INSERT INTO event_daily_temps VALUES (?)", (day,))
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    for source_id in ("ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"):
        envelope = build_external_forecast_raw_envelope(
            source_id=source_id,
            response_body_utf8=json.dumps(
                {
                    "schema": "forecast_source_forward_capture_payload_v2",
                    "derived_forecast": {
                        "source_id": source_id,
                        "target_event_date_local": "2026-07-12",
                        "forecast_peak_c": 33.0,
                    },
                    "raw_provider_response_utf8": "{}",
                }
            ),
            requested_at="2026-07-11T00:00:00Z",
            response_started_at="2026-07-11T00:00:00Z",
            response_completed_at="2026-07-11T00:00:01Z",
            http_status=200,
            parser_version="forward-capture-v2",
                source_event_id=f"forward_{source_id}_2026-07-12",
                capture_time_provenance="observed_transport",
                response_headers_observed=True,
        )
        with gzip.open(raw_root / f"{source_id}.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(envelope, handle)

    report = build_v2_training_data_readiness_audit(
        db_path=db,
        min_history_days=2,
        strict_capture_root=raw_root,
        strict_capture_required_sources=["ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"],
    )

    assert report["strict_capture_readiness"]["manifest"]["capture_contract_ready"] is True
    assert report["strict_capture_readiness"]["manifest"]["minimum_complete_event_days"] == 2
    assert report["strict_capture_readiness"]["ok"] is False
    assert "strict_asof_tier_a_capture_coverage_not_ready" in report["blockers"]
