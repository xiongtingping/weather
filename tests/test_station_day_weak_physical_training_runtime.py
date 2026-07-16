from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from resolution_final_source_shadow_writer import (
    initialize_isolated_resolution_final_source_staging_schema,
    persist_final_source_observation_shadow,
)
from scripts import materialize_station_day_weak_physical_training as materializer
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_schema_contract import apply_forward_schema
from station_day_weak_physical_training_runtime import build_station_day_weak_context_requests
from tests.test_build_joint_forecast_weak_training_events import _persist_strict_gfs, _physical_source


def _p1(path: Path) -> None:
    with sqlite3.connect(path) as conn:
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
        apply_forward_schema(conn)
        _persist_strict_gfs(conn)


def _final_source(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_isolated_resolution_final_source_staging_schema(conn)
        persist_final_source_observation_shadow(conn, _physical_source())


def test_station_day_requests_have_no_market_target_and_use_fixed_asof_cutoffs() -> None:
    result = build_station_day_weak_context_requests(
        event_dates=["2026-07-12"],
        local_cutoffs=["14:30"],
    )

    assert result["ok"] is True
    assert result["requests"] == [{
        "station_id": "ZUUU",
        "event_date": "2026-07-12",
        "decision_as_of": "2026-07-12T06:30:00Z",
        "target_timezone": "Asia/Shanghai",
        "identity_kind": "station_day",
        "training_identity": "station_day:ZUUU:2026-07-12",
    }]
    assert result["boundary"]["market_target_not_required"] is True


def test_materializer_appends_immutable_physical_x_candidate_without_market_state(tmp_path: Path, monkeypatch) -> None:
    p1_path = tmp_path / "p1.db"
    final_path = tmp_path / "final.db"
    research_root = tmp_path / "research"
    _p1(p1_path)
    _final_source(final_path)
    monkeypatch.setattr(materializer, "DEFAULT_RESEARCH_ROOT", research_root)

    result = materializer.materialize_station_day_weak_physical_training(
        p1_db=p1_path,
        final_source_db=final_path,
        research_root=research_root,
        local_cutoffs=("14:30",),
        allow_research_write=True,
    )

    path = research_root / "2026-07-12.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    event = payload["weak_training_event_envelope"]["events"][0]
    assert result["state"] == "materialized"
    assert path.exists()
    assert event["training_identity"] == "station_day:ZUUU:2026-07-12"
    assert "target_identity" not in event
    assert payload["boundary"]["market_finality_not_read"] is True
    assert payload["boundary"]["training_or_oos_is_not_live_ingest_gate"] is True


def test_materializer_uses_event_scoped_final_x_and_appends_successor_on_real_input_change(tmp_path: Path, monkeypatch) -> None:
    p1_path = tmp_path / "p1.db"
    final_path = tmp_path / "final.db"
    research_root = tmp_path / "research"
    _p1(p1_path)
    _final_source(final_path)
    monkeypatch.setattr(materializer, "DEFAULT_RESEARCH_ROOT", research_root)
    first = materializer.materialize_station_day_weak_physical_training(
        p1_db=p1_path, final_source_db=final_path, research_root=research_root,
        local_cutoffs=("14:30",), allow_research_write=True,
    )
    assert first["state"] == "materialized"

    # A different closed day must not perturb this station-day artifact.
    other = _physical_source()
    other.update({"source_observation_id": "physical-other-day", "event_date": "2026-07-11", "raw_sha256": "f" * 64})
    other["payload"] = {**other["payload"], "physical_raw_sha256_list": ["e" * 64]}
    with sqlite3.connect(final_path) as conn:
        persist_final_source_observation_shadow(conn, other)
    unchanged = materializer.materialize_station_day_weak_physical_training(
        p1_db=p1_path, final_source_db=final_path, research_root=research_root,
        local_cutoffs=("14:30",), allow_research_write=True,
    )
    unchanged_day = next(row for row in unchanged["outcomes"] if row["event_date"] == "2026-07-12")
    assert unchanged_day["state"] == "skipped_existing_immutable_candidate"

    # An immutable prior candidate whose scoped input genuinely differs gets
    # an append-only successor rather than overwrite or a permanent conflict.
    primary = research_root / "2026-07-12.json"
    legacy = json.loads(primary.read_text(encoding="utf-8"))
    legacy["input_lineage_sha256"] = "0" * 64
    primary.write_text(json.dumps(legacy), encoding="utf-8")
    corrected = materializer.materialize_station_day_weak_physical_training(
        p1_db=p1_path, final_source_db=final_path, research_root=research_root,
        local_cutoffs=("14:30",), allow_research_write=True,
    )
    outcome = next(row for row in corrected["outcomes"] if row["event_date"] == "2026-07-12")
    assert outcome["state"] == "appended_immutable_candidate"
    assert outcome["candidate_revision"] == 2
    successor = json.loads(Path(outcome["path"]).read_text(encoding="utf-8"))
    assert successor["supersedes"]["path"] == str(primary)
