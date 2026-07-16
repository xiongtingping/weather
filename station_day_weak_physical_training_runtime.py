"""Build station-day weak-training cutoff requests without a market target.

This module owns only the deterministic calendar schedule for offline physical
X learning.  It does not read AMSC observations, market state, settlement S,
orders, or posterior state.  The caller supplies closed station days and may
then build strict-as-of forecast contexts from P1 for each requested cutoff.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "station_day_weak_physical_training_requests_v1"
# 00:00 is the frozen D1/opening forecast baseline.  The remaining cutoffs
# are D0 revisions and deliberately remain separate decision times.
DEFAULT_LOCAL_CUTOFFS = ("00:00", "09:00", "11:00", "13:00", "15:00", "17:00")
STATION_ID = "ZUUU"
STATION_TIMEZONE = "Asia/Shanghai"


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_station_day_weak_context_requests(
    *,
    event_dates: Sequence[str],
    local_cutoffs: Sequence[str] = DEFAULT_LOCAL_CUTOFFS,
    station_id: str = STATION_ID,
    station_timezone: str = STATION_TIMEZONE,
) -> dict[str, Any]:
    """Return immutable station-day cutoff requests for forecast-only capture.

    Invalid dates/cutoffs are explicitly quarantined.  A request intentionally
    carries no market target identity: the downstream context builder derives
    ``station_day:<station>:<date>`` as its training identity.
    """

    station = _text(station_id).upper()
    try:
        zone = ZoneInfo(_text(station_timezone) or STATION_TIMEZONE)
    except Exception:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["station_day_weak_context_timezone_invalid"],
            "read_only": True,
            "places_orders": False,
        }
    requests: list[dict[str, str]] = []
    quarantined: list[dict[str, str]] = []
    for raw_day in sorted({_text(value)[:10] for value in event_dates if _text(value)}):
        try:
            day = datetime.fromisoformat(raw_day).date()
        except ValueError:
            quarantined.append({"event_date": raw_day, "reason": "station_day_weak_context_event_date_invalid"})
            continue
        for raw_cutoff in local_cutoffs:
            cutoff = _text(raw_cutoff)
            try:
                local = datetime.fromisoformat(f"{day.isoformat()}T{cutoff}:00").replace(tzinfo=zone)
            except ValueError:
                quarantined.append({"event_date": day.isoformat(), "cutoff_local": cutoff, "reason": "station_day_weak_context_cutoff_invalid"})
                continue
            requests.append(
                {
                    "station_id": station,
                    "event_date": day.isoformat(),
                    "decision_as_of": local.astimezone(ZoneInfo("UTC")).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    "target_timezone": str(zone),
                    "identity_kind": "station_day",
                    "training_identity": f"station_day:{station}:{day.isoformat()}",
                }
            )
    return {
        "schema": SCHEMA_VERSION,
        "ok": bool(requests),
        "state": "available" if requests else "blocked",
        "requests": requests,
        "quarantined": quarantined,
        "boundary": {
            "station_day_identity_only": True,
            "market_target_not_required": True,
            "x_to_s_to_b_bridge_not_required": True,
            "writes_archive_db": False,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["DEFAULT_LOCAL_CUTOFFS", "SCHEMA_VERSION", "build_station_day_weak_context_requests"]
