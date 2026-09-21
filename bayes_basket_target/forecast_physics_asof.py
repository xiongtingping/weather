"""The forecast radiation a decision could have read, resolved at its cutoff (charter 172).

``calibration_station_forecast_physics`` keeps one row per station-day and
every six-hourly forward capture replaces it, so a day's row is rewritten
until the evening of the day itself.  Serving reads it at decision time and
is as-of by construction.  A holdout that reads it later reads a same-day
value: measured 2026-09-21 over 1,522 station-days, the P0 residual
correlates +0.237 with the table value and +0.112 with the value P0 was
actually served.

The version table (``scripts/capture_station_forecast_physics.py``) keeps
every distinct content with the moment it was first captured.  Two sources
land there:

* ``OPEN_METEO_MODEL_FORECAST`` forward captures, from 2026-09-21 on;
* ``P0_ENVELOPE_RECEIPT`` -- the radiation each served P0 envelope recorded
  in its ``synoptic_cooling_json`` receipt, from 2026-08-16, stamped with the
  envelope's own commit time (the value was current no later than that, so
  the stamp is conservative).

``historical``-mode rows come from the stitched short-lead historical
forecast API, are captured after the day, and are never an as-of forecast.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

TABLE = "calibration_station_forecast_physics"
VERSION_TABLE = f"{TABLE}_version"
FORWARD_SOURCE_ID = "OPEN_METEO_MODEL_FORECAST"
RECEIPT_SOURCE_ID = "P0_ENVELOPE_RECEIPT"
RECEIPT_CAPTURE_MODE = "p0_envelope_receipt"
AS_OF_CAPTURE_MODES = ("forward", RECEIPT_CAPTURE_MODE)


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def resolve_radiation_as_of(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    local_date: str,
    cutoff_utc: Any,
) -> tuple[float | None, dict[str, Any]]:
    """The latest radiation version captured at or before ``cutoff_utc``.

    Timestamps are compared as instants, not strings: the table holds
    millisecond and microsecond stamps side by side.  Returns ``(None,
    receipt)`` with the reason when nothing was known by then -- including
    when the table has no versions at all (``NO_VERSIONS``), which the caller
    may treat differently from a day whose versions all came later.
    """

    cutoff = _instant(cutoff_utc)
    base = {"schema": "radiation_as_of_receipt_v1", "cutoff_utc": str(cutoff_utc)}
    if cutoff is None:
        return None, {**base, "state": "NO_CUTOFF"}
    try:
        rows = conn.execute(
            f"SELECT shortwave_radiation_sum,captured_at_utc,source_id,capture_mode "
            f"FROM {VERSION_TABLE} WHERE station_id=? AND local_date=? "
            "AND shortwave_radiation_sum IS NOT NULL",
            (str(station_id), str(local_date)),
        ).fetchall()
    except sqlite3.Error:
        return None, {**base, "state": "NO_VERSION_TABLE"}
    usable = [
        (instant, float(value), str(source), str(mode))
        for value, captured, source, mode in rows
        if str(mode) in AS_OF_CAPTURE_MODES
        and (instant := _instant(captured)) is not None
    ]
    if not usable:
        return None, {**base, "state": "NO_VERSIONS", "rows": len(rows)}
    known = [row for row in usable if row[0] <= cutoff]
    if not known:
        return None, {
            **base,
            "state": "NOT_KNOWN_AT_CUTOFF",
            "first_version_utc": min(row[0] for row in usable).isoformat(),
        }
    instant, value, source, mode = max(known, key=lambda row: (row[0], row[2]))
    return value, {
        **base,
        "state": "RESOLVED",
        "radiation_sum": value,
        "version_captured_at_utc": instant.isoformat(),
        "source_id": source,
        "capture_mode": mode,
        "version_age_hours": (cutoff - instant).total_seconds() / 3600.0,
    }


__all__ = [
    "AS_OF_CAPTURE_MODES",
    "FORWARD_SOURCE_ID",
    "RECEIPT_CAPTURE_MODE",
    "RECEIPT_SOURCE_ID",
    "TABLE",
    "VERSION_TABLE",
    "resolve_radiation_as_of",
]
