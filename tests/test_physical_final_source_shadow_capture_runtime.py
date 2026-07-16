from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from physical_final_source_shadow_capture_runtime import run_physical_final_source_shadow_capture_cycle
from resolution_final_source_shadow_writer import (
    SOURCE_TABLE,
    initialize_isolated_resolution_final_source_staging_schema,
)
from resolution_target_capture_cursor import initialize_resolution_target_capture_cursor
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from zuuu_metar_reporting_schedule import (
    EXPECTED_REGULAR_SLOT_COUNT,
    SCHEDULE_ID,
    SCHEDULE_SCHEMA,
    expected_regular_metar_slots_utc,
)


DAY = "2026-07-03"
ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = json.loads((ROOT / "config/resolution_authorities/zuuu_amsc_wunderground_daily_high_v4.json").read_text())


def _strict() -> dict:
    rows = []
    for index, valid_text in enumerate(expected_regular_metar_slots_utc(DAY)):
        valid = datetime.fromisoformat(valid_text.replace("Z", "+00:00"))
        rows.append(
            {
                "source_id": "zuuu_metar_raw_amsc",
                "role": "metar_hard_observation_primary",
                "report_type": "METAR",
                "station_id": "ZUUU",
                "event_date": DAY,
                "valid_time": valid.isoformat().replace("+00:00", "Z"),
                "temperature_c": 24.0 + (index % 13),
                "source_observation_id": f"amsc-{index:02d}",
                "raw_sha256": f"{index:064x}",
            }
        )
    return {"schema": "strict_amsc_hard_observation_reader_v1", "ok": True, "event_date": DAY, "source_observations": rows}


def _coverage() -> dict:
    return {
        "event_date": DAY,
        "station_id": "ZUUU",
        "day_state": "forward_arrival_complete_pending_label",
        "distinct_valid_time_count": EXPECTED_REGULAR_SLOT_COUNT,
        "event_raw_object_count": EXPECTED_REGULAR_SLOT_COUNT,
        "correction_count": 0,
        "late_arrival_count": 0,
        "missing_first_seen_count": 0,
        "raw_archive_integrity": {"ok": True},
        "coverage_sha256": "a" * 64,
        "raw_object_sha256_list": [f"{index:064x}" for index in range(EXPECTED_REGULAR_SLOT_COUNT)],
        "reporting_schedule_schema": SCHEDULE_SCHEMA,
        "reporting_schedule_id": SCHEDULE_ID,
        "expected_regular_metar_slot_count": EXPECTED_REGULAR_SLOT_COUNT,
        "expected_regular_metar_slots_utc": expected_regular_metar_slots_utc(DAY),
        "missing_regular_metar_slots_utc": [],
        "unexpected_regular_metar_slots_utc": [],
    }


def test_physical_sidecar_appends_only_closed_complete_strict_amsc_x(tmp_path) -> None:
    archive_db = tmp_path / "archive.db"
    shadow_db = tmp_path / "shadow.db"
    sqlite3.connect(archive_db).close()
    with sqlite3.connect(shadow_db) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_resolution_target_capture_cursor(conn, forward_capture_not_before="2026-07-01T00:00:00Z")
        initialize_isolated_resolution_final_source_staging_schema(conn)

    def reader(_conn, **_kwargs):
        return _strict()

    def coverage_loader(_archive_dir, **_kwargs):
        return _coverage()

    result = run_physical_final_source_shadow_capture_cycle(
        archive_db_path=archive_db,
        shadow_db_path=shadow_db,
        amsc_raw_archive_dir=tmp_path / "amsc",
        authority_contract=AUTHORITY,
        finalized_at="2026-07-03T18:00:00Z",
        event_dates=[DAY],
        reader=reader,
        coverage_loader=coverage_loader,
    )
    assert result["ok"] is True
    assert result["outcomes"][0]["state"] == "appended_physical_source"
    assert result["boundary"]["market_finality_not_read"] is True
    with sqlite3.connect(shadow_db) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {SOURCE_TABLE}").fetchone()[0] == 1


def test_physical_x_capture_has_no_market_finality_runtime_dependency() -> None:
    text = (ROOT / "physical_final_source_shadow_capture_runtime.py").read_text(encoding="utf-8")
    assert "market_resolution_finality" not in text
    assert "physical_station_day_not_market_target" in text


def test_physical_x_discovers_closed_station_day_without_market_target(tmp_path) -> None:
    archive_db = tmp_path / "archive.db"
    shadow_db = tmp_path / "shadow.db"
    archive_dir = tmp_path / "amsc"
    coverage_path = archive_dir / "coverage_v2" / "2026" / "07" / "03" / "coverage.json"
    coverage_path.parent.mkdir(parents=True)
    coverage_path.write_text(json.dumps({"schema": "amsc_daily_raw_coverage_manifest_v2", "station_id": "ZUUU", "event_date": DAY}), encoding="utf-8")
    sqlite3.connect(archive_db).close()
    with sqlite3.connect(shadow_db) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_resolution_target_capture_cursor(conn, forward_capture_not_before="2026-07-01T00:00:00Z")
        initialize_isolated_resolution_final_source_staging_schema(conn)

    result = run_physical_final_source_shadow_capture_cycle(
        archive_db_path=archive_db,
        shadow_db_path=shadow_db,
        amsc_raw_archive_dir=archive_dir,
        authority_contract=AUTHORITY,
        finalized_at="2026-07-03T18:00:00Z",
        reader=lambda _conn, **_kwargs: _strict(),
        coverage_loader=lambda _archive_dir, **_kwargs: _coverage(),
    )
    assert result["ok"] is True
    assert result["station_days_considered"] == 1
    assert result["outcomes"][0]["state"] == "appended_physical_source"
