from __future__ import annotations

import sqlite3

from resolution_authority_candidate_runtime import build_resolution_authority_candidate_audit


def _event_runs(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date TEXT, slug TEXT, resolution_source TEXT,
            station_id TEXT, market_var TEXT, market_unit TEXT,
            tz TEXT, rounding_rule TEXT, created_at TEXT
        )
        """
    )


def test_market_declared_wunderground_is_a_candidate_not_a_final_source(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _event_runs(conn)
        conn.execute(
            """
            INSERT INTO event_runs(
                event_date,slug,resolution_source,station_id,market_var,
                market_unit,tz,rounding_rule,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "2026-07-12",
                "highest-temperature-in-chengdu-on-july-12-2026",
                "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                "ZUUU",
                "temperature_2m_max",
                "C",
                "Asia/Shanghai",
                "whole_degrees",
                "2026-07-11T16:00:00Z",
            ),
        )
        result = build_resolution_authority_candidate_audit(
            conn,
            event_date="2026-07-12",
            station_id="ZUUU",
            slug="highest-temperature-in-chengdu-on-july-12-2026",
            archive_dir=tmp_path / "missing_raw_archive",
        )
        assert result["state"] == "authority_decision_required"
        candidate = result["settlement_candidates"][0]
        assert candidate["candidate_kind"] == "market_declared_wunderground_daily_history"
        assert candidate["eligible_to_promote_now"] is False
        assert result["physical_candidate"]["candidate_id"] == "amsc_zuuu_half_hour_daily_max_candidate_v1"
        assert result["physical_candidate"]["eligible_to_promote_now"] is False
        assert result["boundary"]["does_not_assign_final_source_roles"] is True
    finally:
        conn.close()


def test_missing_market_candidate_remains_blocked_without_inventing_settlement_source(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        _event_runs(conn)
        result = build_resolution_authority_candidate_audit(
            conn,
            event_date="2026-07-12",
            station_id="ZUUU",
            archive_dir=tmp_path,
        )
        assert result["settlement_candidates"] == []
        assert "market_resolution_source_candidate_missing" in result["blockers"]
        assert result["boundary"]["does_not_promote_legacy_scalar"] is True
    finally:
        conn.close()
