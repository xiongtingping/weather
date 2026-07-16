from __future__ import annotations

import sqlite3

from resolution_final_source_readiness_runtime import (
    PHYSICAL_ROLE,
    SETTLEMENT_ROLE,
    build_resolution_final_source_readiness_audit,
)


def _db(tmp_path) -> str:
    path = tmp_path / "archive.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE source_observations (
                source_id TEXT, source_class TEXT, role TEXT, event_date TEXT,
                station_id TEXT, raw_sha256 TEXT, raw_ref TEXT, fetched_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE event_daily_temps (event_date TEXT, resolution_source TEXT, settlement_actual REAL)"
        )
        conn.execute(
            "INSERT INTO event_daily_temps VALUES('2026-07-03','legacy-wu',31.0)"
        )
    return str(path)


def test_audit_fails_closed_and_keeps_legacy_scalars_diagnostic_only(tmp_path) -> None:
    report = build_resolution_final_source_readiness_audit(db_path=_db(tmp_path))

    assert report["ok"] is False
    assert "final_physical_daily_high_source_not_configured_or_unavailable" in report["blockers"]
    assert "final_resolution_settlement_source_not_configured_or_unavailable" in report["blockers"]
    assert report["legacy_scalar_diagnostics"]["event_daily_temps"]["eligible_as_final_x_s_b_label"] is False


def test_audit_requires_paired_final_x_and_s_days_with_raw_lineage(tmp_path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        rows = [
            ("physical-final", "settlement_source", PHYSICAL_ROLE, "2026-07-03", "ZUUU", "a" * 64, "raw/x", "2026-07-04T01:00:00Z"),
            ("settlement-final", "settlement_source", SETTLEMENT_ROLE, "2026-07-03", "ZUUU", "b" * 64, "raw/s", "2026-07-04T02:00:00Z"),
        ]
        conn.executemany("INSERT INTO source_observations VALUES(?,?,?,?,?,?,?,?)", rows)

    report = build_resolution_final_source_readiness_audit(
        db_path=path,
        registry_readiness={"strict_x_s_b_bridge_proven": False},
    )

    assert report["physical_final_source"]["state"] == "available"
    assert report["settlement_final_source"]["state"] == "available"
    assert report["paired_final_event_days"] == ["2026-07-03"]
    assert report["ok"] is False
    assert report["blockers"] == ["final_label_registry_not_ready"]
