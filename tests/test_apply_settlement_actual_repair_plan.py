from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import apply_settlement_actual_repair_plan as apply_plan


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            title TEXT,
            event_url TEXT,
            resolution_source TEXT,
            station_id TEXT,
            market_var TEXT,
            market_unit TEXT,
            market_closed INTEGER,
            event_end_utc TEXT,
            settlement_actual REAL,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_runs(
            run_date, slug, event_date, title, event_url, resolution_source,
            station_id, market_var, market_unit, market_closed, event_end_utc,
            settlement_actual, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-05-31",
            "highest-temperature-in-toronto-on-may-31-2026",
            "2026-05-31",
            "Highest temperature in Toronto on May 31?",
            "https://polymarket.com/event/highest-temperature-in-toronto-on-may-31-2026",
            "https://www.wunderground.com/history/daily/ca/mississauga/CYYZ",
            "CYYZ",
            "temperature_2m_max",
            "C",
            1,
            "2026-05-31T23:59:00Z",
            None,
            "2026-06-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


def _repair_plan(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "fetch_preview_requested": True,
                "rows": [
                    {
                        "city": "toronto",
                        "slug": "highest-temperature-in-toronto-on-may-31-2026",
                        "event_date": "2026-05-31",
                        "title": "Highest temperature in Toronto on May 31?",
                        "event_url": "https://polymarket.com/event/highest-temperature-in-toronto-on-may-31-2026",
                        "resolution_source": "https://www.wunderground.com/history/daily/ca/mississauga/CYYZ",
                        "station_id": "CYYZ",
                        "source_station_id": "CYYZ",
                        "market_var": "temperature_2m_max",
                        "market_unit": "C",
                        "metadata_ok_for_settlement_fetch": True,
                        "blockers": [],
                        "fetch_preview": {
                            "attempted": True,
                            "ok": True,
                            "candidate_settlement_actual": 24.4,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _settled_count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM event_runs WHERE settlement_actual IS NOT NULL"
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_dry_run_reports_ready_without_writing(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    plan_path = tmp_path / "repair.json"
    _init_db(db_path)
    _repair_plan(plan_path)

    report = apply_plan.build_report(db_path, repair_plan_path=plan_path)

    assert report["read_only"] is True
    assert report["apply_allowed"] is False
    assert report["ready_count"] == 1
    assert report["results"][0]["status"] == "dry_run_ready"
    packet = report["review_packet"]
    assert packet["requires_manual_review"] is True
    assert packet["would_insert_event_run_rows"] == 1
    assert packet["write_policy"] == "append_only_event_runs_settlement_actual_rows"
    assert "APPLY_SETTLEMENT_ACTUALS" in packet["confirm_command"]
    assert packet["items"][0]["station_source_match"] is True
    assert packet["items"][0]["source"].endswith("/CYYZ")
    assert packet["items"][0]["review_checksum"]
    assert _settled_count(db_path) == 0


def test_apply_requires_confirmation_and_does_not_write_without_it(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    plan_path = tmp_path / "repair.json"
    _init_db(db_path)
    _repair_plan(plan_path)

    report = apply_plan.build_report(
        db_path,
        repair_plan_path=plan_path,
        apply=True,
        confirm_apply="WRONG",
    )

    assert report["read_only"] is True
    assert report["apply_requested"] is True
    assert report["apply_confirmation_ok"] is False
    assert report["results"][0]["status"] == "missing_apply_confirmation"
    assert _settled_count(db_path) == 0


def test_apply_with_confirmation_appends_settlement_row(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    plan_path = tmp_path / "repair.json"
    _init_db(db_path)
    _repair_plan(plan_path)

    report = apply_plan.build_report(
        db_path,
        repair_plan_path=plan_path,
        apply=True,
        confirm_apply=apply_plan.APPLY_CONFIRM_PHRASE,
        backup_dir=tmp_path / "backups",
    )

    assert report["read_only"] is False
    assert report["pre_apply_backup"]["created"] is True
    assert report["inserted_count"] == 1
    assert report["results"][0]["status"] == "settlement_actual_inserted"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT settlement_actual, market_closed, station_id, resolution_source
            FROM event_runs
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    assert rows[0][0] is None
    assert rows[1][0] == 24.4
    assert rows[1][1] == 1
    assert rows[1][2] == "CYYZ"
    assert rows[1][3].endswith("/CYYZ")


def test_apply_skips_when_already_settled(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    plan_path = tmp_path / "repair.json"
    _init_db(db_path)
    _repair_plan(plan_path)
    apply_plan.build_report(
        db_path,
        repair_plan_path=plan_path,
        apply=True,
        confirm_apply=apply_plan.APPLY_CONFIRM_PHRASE,
        backup_dir=tmp_path / "backups",
    )

    report = apply_plan.build_report(
        db_path,
        repair_plan_path=plan_path,
        apply=True,
        confirm_apply=apply_plan.APPLY_CONFIRM_PHRASE,
        backup_dir=tmp_path / "backups",
    )

    assert report["inserted_count"] == 0
    assert report["results"][0]["status"] == "already_settled"
    assert _settled_count(db_path) == 1


def test_direct_apply_blocks_when_backup_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    plan_path = tmp_path / "repair.json"
    _init_db(db_path)
    _repair_plan(plan_path)

    report = apply_plan.build_report(
        db_path,
        repair_plan_path=plan_path,
        apply=True,
        confirm_apply=apply_plan.APPLY_CONFIRM_PHRASE,
        backup_before_apply=False,
        backup_dir=tmp_path / "backups",
    )

    assert report["read_only"] is True
    assert report["apply_allowed"] is False
    assert report["pre_apply_backup"]["reason"] == "disabled_by_no_backup_before_apply"
    assert report["results"][0]["status"] == "pre_apply_backup_not_created"
    assert _settled_count(db_path) == 0
