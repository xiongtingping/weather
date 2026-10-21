"""Which reports a station-day settles on (charter 276).

Until 2026-10-22 every market settled on a page (NOAA's timeseries "Temp"
column, WU's Daily Observations table) that lists both the routine METARs and
the SPECIs, so the truth lane, P1's running high and the hard-fact floors all
took the maximum over both.  From event date 2026-10-23 the 32 non-US markets
settle on AWC's routine METARs only: a report beginning SPECI does not count.

The date each city switched is the verified charter-205 baseline's
``routine_metar_only_from`` (``config/trading/awc_truth_ingress_v1.json``);
the registry refresh holds every market's own rules against it and blocks a
market whose report set disagrees, so this one reading is what the truth
lane, the resident P1 reader and the AWC ingress's running high all ask.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .contracts import ContractError

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "trading" / "awc_truth_ingress_v1.json"
ROUTINE_METAR_ONLY = "ROUTINE_METAR_ONLY"
_STATION_RE = re.compile(r"^[A-Z0-9]{4}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_cache: tuple[str, float, dict[str, str]] | None = None


def _routine_from_by_station(path: Path | None = None) -> dict[str, str]:
    """Station -> first routine-only event date, re-read when the file changes."""

    global _cache
    path = path or CONFIG_PATH
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ContractError("settlement_basis_config_unreadable") from exc
    if _cache is not None and _cache[:2] == (str(path), mtime):
        return _cache[2]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError("settlement_basis_config_unreadable") from exc
    cities = ((document.get("settlement_source_contract") or {}).get("cities")) if isinstance(document, dict) else None
    if not isinstance(cities, dict) or not cities:
        raise ContractError("settlement_basis_config_invalid")
    found: dict[str, str] = {}
    for entry in cities.values():
        if not isinstance(entry, dict):
            raise ContractError("settlement_basis_config_invalid")
        start = entry.get("routine_metar_only_from")
        if start is None:
            continue
        station = str(entry.get("station_id") or "")
        if not _STATION_RE.match(station) or not _DATE_RE.match(str(start)):
            raise ContractError("settlement_basis_config_invalid")
        found[station] = str(start)
    _cache = (str(path), mtime, found)
    return found


def routine_metar_only(station_id: str, event_date: str) -> bool:
    """Whether only routine METARs settle this station-day."""

    start = _routine_from_by_station().get(str(station_id or "").strip().upper())
    return start is not None and str(event_date) >= start


def report_settles(station_id: str, event_date: str, report_type: Any) -> bool:
    """Whether a report of ``report_type`` counts toward this day's settled high."""

    kind = str(report_type or "").strip().upper()
    return kind != "SPECI" or not routine_metar_only(station_id, event_date)


__all__ = ["CONFIG_PATH", "ROUTINE_METAR_ONLY", "report_settles", "routine_metar_only"]
