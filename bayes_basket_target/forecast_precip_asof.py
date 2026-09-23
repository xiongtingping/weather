"""The hourly precipitation forecast a decision could have read, resolved at its cutoff.

``calibration_station_forecast_precip_hourly_version`` (written by
``scripts/capture_station_forecast_physics.py`` from the same Open-Meteo
request as the radiation table, 2026-09-23 on) keeps every distinct hourly
series per station-day with the instant it was first captured.  Only
``forward`` versions are as-of values; ``historical`` rows come from the
stitched short-lead archive after the day and serve calibration only.

The aggregates a served term reads -- the day's total, the millimetres
before and after the afternoon peak hour, the first wet hour -- are derived
once, in the capturer, and stored beside the series that made them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

TABLE = "calibration_station_forecast_precip_hourly_version"
FORWARD_MODE = "forward"
HISTORICAL_MODE = "historical"
FIELDS = (
    "precip_sum_mm", "precip_before_peak_mm", "precip_after_peak_mm",
    "first_wet_hour", "wet_hours", "peak_hour",
)


def _instant(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def resolve_precip_hourly_as_of(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    local_date: str,
    cutoff_utc: Any,
    modes: tuple[str, ...] = (FORWARD_MODE,),
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The latest hourly-precipitation version captured at or before ``cutoff_utc``.

    Returns ``(aggregates, receipt)``; ``aggregates`` is None when nothing was
    known by then, and the receipt says why (``NO_VERSIONS`` when the
    station-day has no version at all, ``NOT_KNOWN_AT_CUTOFF`` when every
    version came later).  ``modes`` widens the read to historical rows for
    calibration; production reads forward only.
    """

    cutoff = _instant(cutoff_utc)
    base = {"schema": "precip_hourly_as_of_receipt_v1", "cutoff_utc": str(cutoff_utc)}
    if cutoff is None:
        return None, {**base, "state": "NO_CUTOFF"}
    try:
        rows = conn.execute(
            f"SELECT captured_at_utc, capture_mode, {', '.join(FIELDS)}, content_sha256"
            f" FROM {TABLE} WHERE station_id=? AND local_date=?",
            (str(station_id), str(local_date)),
        ).fetchall()
    except sqlite3.Error:
        return None, {**base, "state": "NO_VERSION_TABLE"}
    usable = []
    for row in rows:
        instant = _instant(row[0])
        if instant is None or str(row[1]) not in modes:
            continue
        usable.append((instant, row))
    if not usable:
        return None, {**base, "state": "NO_VERSIONS", "rows": len(rows)}
    known = [item for item in usable if item[0] <= cutoff]
    if not known:
        return None, {
            **base,
            "state": "NOT_KNOWN_AT_CUTOFF",
            "first_version_utc": min(item[0] for item in usable).isoformat(),
        }
    instant, row = max(known, key=lambda item: item[0])
    aggregates = {name: row[2 + index] for index, name in enumerate(FIELDS)}
    return aggregates, {
        **base,
        "state": "RESOLVED",
        "version_captured_at_utc": instant.isoformat(),
        "capture_mode": str(row[1]),
        "content_sha256": str(row[-1]),
        "version_age_hours": (cutoff - instant).total_seconds() / 3600.0,
        **aggregates,
    }


__all__ = [
    "FIELDS",
    "FORWARD_MODE",
    "HISTORICAL_MODE",
    "TABLE",
    "resolve_precip_hourly_as_of",
]
