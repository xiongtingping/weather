"""A station the P0 compile never reached says why: no cross-system certificate."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
_spec = importlib.util.spec_from_file_location("calibration_dashboard", ROOT / "scripts" / "calibration_dashboard.py")
C = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("calibration_dashboard", C)
_spec.loader.exec_module(C)


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE calibration_native_ensemble_activation (station_id TEXT, artifact_kind TEXT,"
                 " ensemble_system_id_or_star TEXT, authority_scope TEXT, valid_from_utc TEXT, valid_until_utc TEXT)")
    conn.executemany("INSERT INTO calibration_native_ensemble_activation VALUES (?,?,?,'PRODUCTION_NO_LIVE_ONLY',"
                     "'2026-10-01T00:00:00Z','9999-12-31T00:00:00Z')", rows)
    return conn


NOW = datetime(2026, 10, 23, 11, tzinfo=timezone.utc)


def test_a_new_station_with_one_family_is_short_of_a_cross_certificate() -> None:
    conn = _db([("KDEN", "NATIVE_ENSEMBLE_SYSTEM_CALIBRATION", "NOAA_GEFS_05")] * 3)
    assert C._no_p0_certificate_reason(conn, "KDEN", NOW) == "还没有跨系统证书（单系统证书 1 家，至少要两家）"
    assert C._no_p0_certificate_reason(_db([]), "KDEN", NOW) == "还没有任何证书"


def test_a_station_with_a_cross_certificate_keeps_the_unrecorded_reason() -> None:
    conn = _db([("KLGA", "CROSS_NATIVE_ENSEMBLE", "*")])
    assert C._no_p0_certificate_reason(conn, "KLGA", NOW) == "原因未记录"
