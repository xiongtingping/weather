from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

from scripts import settlement_actual_repair_plan as plan


def _init_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT,
            event_date TEXT,
            title TEXT,
            event_url TEXT,
            resolution_source TEXT,
            station_id TEXT,
            market_var TEXT,
            market_unit TEXT,
            rounding_rule TEXT,
            lat REAL,
            lon REAL,
            tz TEXT,
            market_closed INTEGER,
            event_end_utc TEXT,
            settlement_actual REAL,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def test_repair_plan_surfaces_closed_missing_settlement_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    wakeup_path = tmp_path / "wakeup.json"
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO event_runs(
            slug, event_date, title, event_url, resolution_source, station_id,
            market_var, market_unit, market_closed, event_end_utc, settlement_actual, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "highest-temperature-in-toronto-on-may-31-2026",
            "2026-05-31",
            "Highest temperature in Toronto on May 31?",
            "https://polymarket.com/event/highest-temperature-in-toronto-on-may-31-2026",
            "https://www.wunderground.com/history/daily/ca/toronto/CYYZ",
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
    wakeup_path.write_text(
        json.dumps(
            {
                "triggered_markets": [
                    {
                        "city": "toronto",
                        "slug": "highest-temperature-in-toronto-on-may-31-2026",
                        "event_date": "2026-05-31",
                        "current_status": "closed_without_settlement_actual",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = plan.build_report(db_path, wakeup_path=wakeup_path)

    assert report["ok"] is False
    assert report["missing_settlement_actual_count"] == 1
    row = report["rows"][0]
    assert row["city"] == "toronto"
    assert row["source_station_id"] == "CYYZ"
    assert row["station_source_match"] is True
    assert row["metadata_ok_for_settlement_fetch"] is True
    assert row["safe_default_action"] == "confirm_resolution_source_then_fetch_settlement_actual"
    assert row["suggested_after_confirmation"]["function"] == (
        "scan_weather_market.refresh_settlement_actual_for_pending_event"
    )


def test_repair_plan_blocks_station_source_mismatch(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    wakeup_path = tmp_path / "wakeup.json"
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO event_runs(
            slug, event_date, title, resolution_source, station_id,
            market_var, market_unit, market_closed, settlement_actual, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "highest-temperature-in-nyc-on-may-31-2026",
            "2026-05-31",
            "Highest temperature in NYC on May 31?",
            "https://www.wunderground.com/history/daily/us/new-york/KNYC",
            "KLGA",
            "temperature_2m_max",
            "F",
            1,
            None,
            "2026-06-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    wakeup_path.write_text(
        json.dumps(
            {
                "triggered_markets": [
                    {
                        "city": "nyc",
                        "slug": "highest-temperature-in-nyc-on-may-31-2026",
                        "event_date": "2026-05-31",
                        "current_status": "closed_without_settlement_actual",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = plan.build_report(db_path, wakeup_path=wakeup_path)

    row = report["rows"][0]
    assert row["metadata_ok_for_settlement_fetch"] is False
    assert "station_id_source_mismatch" in row["blockers"]
    assert row["safe_default_action"] == "repair_resolution_source_station_metadata_before_fetch"


def test_repair_plan_ok_when_no_missing_settlement_actual(tmp_path: Path) -> None:
    wakeup_path = tmp_path / "wakeup.json"
    wakeup_path.write_text(json.dumps({"triggered_markets": []}), encoding="utf-8")

    report = plan.build_report(tmp_path / "missing.db", wakeup_path=wakeup_path)

    assert report["ok"] is True
    assert report["rows"] == []
    assert report["next_action"] == "no_missing_settlement_actual_repairs"


def test_repair_plan_fetch_preview_is_read_only_and_uses_metadata(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "archive.db"
    wakeup_path = tmp_path / "wakeup.json"
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO event_runs(
            slug, event_date, title, resolution_source, station_id,
            market_var, market_unit, market_closed, settlement_actual, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "highest-temperature-in-toronto-on-may-31-2026",
            "2026-05-31",
            "Highest temperature in Toronto on May 31?",
            "https://www.wunderground.com/history/daily/ca/mississauga/CYYZ",
            "CYYZ",
            "temperature_2m_max",
            "C",
            1,
            None,
            "2026-06-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    wakeup_path.write_text(
        json.dumps(
            {
                "triggered_markets": [
                    {
                        "city": "toronto",
                        "slug": "highest-temperature-in-toronto-on-may-31-2026",
                        "event_date": "2026-05-31",
                        "current_status": "closed_without_settlement_actual",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_fetch(resolution_source, event_date, market_var, market_unit, **kwargs):
        calls.append(
            {
                "resolution_source": resolution_source,
                "event_date": event_date,
                "market_var": market_var,
                "market_unit": market_unit,
                "station_id": kwargs.get("station_id"),
            }
        )
        return 21.5

    monkeypatch.setitem(
        sys.modules,
        "scan_weather_market",
        types.SimpleNamespace(fetch_settlement_actual=fake_fetch),
    )

    report = plan.build_report(db_path, wakeup_path=wakeup_path, fetch_preview=True)

    preview = report["rows"][0]["fetch_preview"]
    assert report["fetch_preview_requested"] is True
    assert preview["ok"] is True
    assert preview["candidate_settlement_actual"] == 21.5
    assert calls == [
        {
            "resolution_source": "https://www.wunderground.com/history/daily/ca/mississauga/CYYZ",
            "event_date": "2026-05-31",
            "market_var": "temperature_2m_max",
            "market_unit": "C",
            "station_id": "CYYZ",
        }
    ]


def test_repair_plan_fetch_preview_skips_bad_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    wakeup_path = tmp_path / "wakeup.json"
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO event_runs(
            slug, event_date, title, resolution_source, station_id,
            market_var, market_unit, market_closed, settlement_actual, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "highest-temperature-in-nyc-on-may-31-2026",
            "2026-05-31",
            "Highest temperature in NYC on May 31?",
            "",
            "",
            "",
            "F",
            1,
            None,
            "2026-06-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()
    wakeup_path.write_text(
        json.dumps(
            {
                "triggered_markets": [
                    {
                        "city": "nyc",
                        "slug": "highest-temperature-in-nyc-on-may-31-2026",
                        "event_date": "2026-05-31",
                        "current_status": "closed_without_settlement_actual",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = plan.build_report(db_path, wakeup_path=wakeup_path, fetch_preview=True)

    preview = report["rows"][0]["fetch_preview"]
    assert preview["attempted"] is False
    assert preview["reason"] == "metadata_not_ok_for_settlement_fetch"
