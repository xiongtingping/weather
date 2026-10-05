"""Which market a live station-day links to: the SSOT registry first, then the archive.

(The live order panel's per-station-day listing this file used to render left
the board on 2026-10-05, replaced by the P&L calendar.)
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import calibration_dashboard as C  # noqa: E402

def _registry(path, table: str, rows: list[tuple]) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE TABLE {table} (station_id TEXT, event_date TEXT, slug TEXT, refreshed_at TEXT)"
    )
    conn.executemany(f"INSERT INTO {table} VALUES (?,?,?,?)", rows)
    conn.commit()
    return conn


def test_slugs_come_from_the_ssot_registry_then_the_archive(tmp_path):
    ssot = _registry(tmp_path / "ssot.db", "basket_target_market_registry", [
        ("ZBAA", "2026-09-13", "old-listing", "2026-09-12T01:00:00Z"),
        ("ZBAA", "2026-09-13", "beijing-13", "2026-09-12T08:00:00Z"),
    ])
    archive_path = tmp_path / "archive.db"
    _registry(archive_path, "reported_trajectory_market_registry", [
        ("ZBAA", "2026-09-13", "archive-disagrees", "2026-09-13T00:00:00Z"),
        ("ZGSZ", "2026-09-06", "shenzhen-6", "2026-09-05T00:00:00Z"),
    ]).close()
    rows = [
        {"station_id": "ZBAA", "event_date": "2026-09-13"},
        {"station_id": "ZGSZ", "event_date": "2026-09-06"},
        {"station_id": "EPWA", "event_date": "2026-09-06"},
    ]

    got = C._live_market_slugs(ssot, rows, archive=archive_path)

    # The newest SSOT refresh wins a re-listing; the archive only fills a
    # gap; a station-day in neither is absent rather than guessed.
    assert got == {"ZBAA|2026-09-13": "beijing-13", "ZGSZ|2026-09-06": "shenzhen-6"}


def test_a_missing_archive_is_not_an_error(tmp_path):
    ssot = _registry(tmp_path / "ssot.db", "basket_target_market_registry", [])

    assert C._live_market_slugs(
        ssot, [{"station_id": "ZBAA", "event_date": "2026-09-13"}],
        archive=tmp_path / "absent.db",
    ) == {}
