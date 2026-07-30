from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.materialize_official_nwp_daily_peak_p1_ingress import (
    _summary,
    materialize_official_nwp_daily_peak_p1,
    plan_official_nwp_daily_peak_p1_materialization,
)
from sequential_bayes_schema_contract import apply_forward_schema


def _initialize(path: Path) -> None:
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


def _revision(*, source_id: str = "gfs", tier_a: bool = True) -> dict:
    valid_times = ["2026-07-13T08:00:00Z", "2026-07-13T09:00:00Z"]
    return {
        "schema": "official_nwp_daily_peak_forecast_revision_v1",
        "ok": True,
        "state": "available",
        "source_id": source_id,
        "event_date": "2026-07-13",
        "available_at": "2026-07-12T11:12:13.920Z",
        "forecast_peak_c": 32.815,
        "forecast_peak_valid_time": valid_times[-1],
        "run_at_utc": "2026-07-12T06:00:00Z",
        "expected_valid_times_utc": valid_times,
        "hourly_curve": [
            {"valid_time_utc": valid_times[0], "temperature_c": 31.2, "snapshot_sha256": "s1"},
            {"valid_time_utc": valid_times[1], "temperature_c": 32.815, "snapshot_sha256": "s2"},
        ],
        "raw_sha256": "aggregate-raw",
        "raw_sha256s": ["r1", "r2"],
        "raw_payload_sha256s": [["payload-1"], ["payload-2"]],
        "raw_object_refs": ["/immutable/raw-1.grib2", "/immutable/raw-2.grib2"],
        "snapshot_sha256s": ["s1", "s2"],
        "strict_asof_tier_a": tier_a,
        "response_headers_observed": tier_a,
    }


def _write_revision(root: Path, *, provider: str, name: str, revision: dict) -> None:
    path = root / provider / "daily_peak_revisions" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(revision), encoding="utf-8")


def test_materializer_appends_valid_official_revision_and_quarantines_other_provider(tmp_path: Path) -> None:
    db = tmp_path / "p1.db"
    root = tmp_path / "curves"
    _initialize(db)
    _write_revision(root, provider="gfs", name="valid.json", revision=_revision())
    _write_revision(root, provider="ecmwf", name="not-tier-a.json", revision=_revision(source_id="ecmwf", tier_a=False))

    plan = plan_official_nwp_daily_peak_p1_materialization(db=db, curve_source_root=root)
    assert plan["ok"] is True
    assert len(plan["eligible_proposals"]) == 1
    assert plan["eligible_proposals"][0]["provider_id"] == "gfs"
    assert plan["quarantined_revisions"][0]["provider_id"] == "ecmwf"

    result = materialize_official_nwp_daily_peak_p1(db=db, curve_source_root=root, apply=True)
    assert result["ok"] is True
    assert result["outcomes"][0]["state"] == "appended"
    repeated = materialize_official_nwp_daily_peak_p1(db=db, curve_source_root=root, apply=True)
    assert repeated["outcomes"][0]["state"] == "already_materialized_immutable"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT source_id FROM source_observations").fetchone()[0] == "gfs"

    compact = _summary(result)
    assert compact["ok"] is True
    assert compact["eligible_count"] == 1
    assert compact["quarantined_count"] == 1
    assert compact["outcome_state_counts"] == {"appended": 1}
    assert compact["newly_appended"] == [
        {"provider_id": "gfs", "event_date": "2026-07-13", "state": "appended"}
    ]
