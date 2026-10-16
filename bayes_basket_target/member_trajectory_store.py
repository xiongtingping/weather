"""Each native ensemble member's station temperature at every forecast step.

User 2026-10-16 「开始在原生集合采集里保存各成员逐时（逐步长）站点 2 米温度，写入新表」.
The six native collectors decode every member's station value at every step
and keep only the maximum (``raw_tmax_c``); the step series is what a
member-trajectory update needs -- which members the morning's reports agree
with -- and it was thrown away.  Here it is kept, beside the capture, as a
record: nothing in P0, P0-M or P1 reads it, and it is not the SSOT.

Store: its own SQLite file (``PM_MEMBER_TRAJECTORY_DB``, default under the
NWP runtime directory), one row per system x station x event date x run,
first capture wins (the earliest the run was in hand).  Values are the
collector's own decoded numbers, rounded to 0.01 C; ``value_kind`` says what a
step value is (ECMWF IFS ENS decodes the period maximum ``mx2t3/mx2t6``, the
others the instantaneous 2 m temperature).  Rows whose event date is older
than ``RETENTION_DAYS`` are pruned once a day.

Recording never fails a capture: any error is logged and the capture goes on.
``PM_MEMBER_TRAJECTORY_DB=off`` turns it off (the test suite does).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

LOG = logging.getLogger(__name__)
SCHEMA = "native_member_trajectory_v1"
DEFAULT_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "runtime" / "bayes_basket_target_nwp"
    / "member_trajectories.db"
)
RETENTION_DAYS = 120
VALUE_KIND_INSTANT = "t2m_instant"
VALUE_KIND_PERIOD_MAX = "t2m_period_max"
_LOCK = threading.Lock()
_PRUNED_ON: dict[str, str] = {}

_DDL = """
CREATE TABLE IF NOT EXISTS native_member_trajectory (
  ensemble_system_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  run_init_utc TEXT NOT NULL,
  archive_query_at_utc TEXT NOT NULL,
  recorded_at_utc TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  value_kind TEXT NOT NULL,
  valid_times_json TEXT NOT NULL,
  members_json TEXT NOT NULL,
  member_count INTEGER NOT NULL,
  schema TEXT NOT NULL,
  PRIMARY KEY (ensemble_system_id, station_id, event_date, run_init_utc)
);
CREATE INDEX IF NOT EXISTS native_member_trajectory_day_idx
  ON native_member_trajectory(station_id, event_date);
"""


def store_path() -> Path | None:
    raw = os.environ.get("PM_MEMBER_TRAJECTORY_DB", "").strip()
    if raw.lower() == "off":
        return None
    return Path(raw) if raw else DEFAULT_PATH


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def record_member_trajectories(
    *,
    ensemble_system_id: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    run_init: Any,
    archive_query_at: Any,
    step_values_by_member: Mapping[str, Mapping[Any, float]],
    valid_time_of_step: Any,
    value_kind: str = VALUE_KIND_INSTANT,
) -> bool:
    """Keep one capture's member step series; True when a row was written.

    ``step_values_by_member``: member id -> {step: temperature C};
    ``valid_time_of_step(step)`` -> the step's valid instant (UTC datetime or
    ISO text).  Never raises.
    """

    path = store_path()
    if path is None:
        return False
    try:
        steps = sorted({step for values in step_values_by_member.values() for step in values})
        times = [_iso(valid_time_of_step(step)) for step in steps]
        members = {
            str(member): [
                None if values.get(step) is None else round(float(values[step]), 2)
                for step in steps
            ]
            for member, values in sorted(step_values_by_member.items())
        }
        now = datetime.now(timezone.utc)
        row = (
            str(ensemble_system_id), str(station_id), str(event_date), _iso(run_init),
            _iso(archive_query_at), _iso(now), str(settlement_timezone), str(value_kind),
            json.dumps(times, separators=(",", ":")), json.dumps(members, separators=(",", ":")),
            len(members), SCHEMA,
        )
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=30)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_DDL)
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO native_member_trajectory VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row,
                )
                written = cursor.rowcount == 1
                today = now.date().isoformat()
                if _PRUNED_ON.get(str(path)) != today:
                    cutoff = (date.fromisoformat(today) - timedelta(days=RETENTION_DAYS)).isoformat()
                    conn.execute("DELETE FROM native_member_trajectory WHERE event_date < ?", (cutoff,))
                    _PRUNED_ON[str(path)] = today
                conn.commit()
            finally:
                conn.close()
        return written
    except Exception:  # noqa: BLE001 -- a record must never fail the capture
        LOG.warning("member trajectory record failed for %s %s %s", ensemble_system_id, station_id, event_date,
                    exc_info=True)
        return False


__all__ = [
    "RETENTION_DAYS", "SCHEMA", "VALUE_KIND_INSTANT", "VALUE_KIND_PERIOD_MAX",
    "record_member_trajectories", "store_path",
]
