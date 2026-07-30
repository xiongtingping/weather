"""Build a no-network, provider-resolution-aware daily curve collection plan."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "official_nwp_daily_curve_plan_v1"
CONTRACT_SCHEMA_VERSION = "official_nwp_daily_curve_run_contract_v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def load_daily_curve_run_contract(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return _mapping(value)


def build_official_nwp_daily_curve_plan(
    *, contract: Mapping[str, Any], provider_id: str, run_at_utc: str, target_event_date: str
) -> dict[str, Any]:
    """Plan only declared model-time points that fall in the local target day."""

    value = _mapping(contract)
    provider = _mapping(_mapping(value.get("providers")).get(_text(provider_id)))
    run = _time(run_at_utc)
    try:
        target_day = date.fromisoformat(_text(target_event_date)[:10])
        target_zone = ZoneInfo(_text(value.get("target_timezone")))
    except (ValueError, Exception):
        target_day, target_zone = None, None
    blockers: list[str] = []
    if value.get("schema") != CONTRACT_SCHEMA_VERSION:
        blockers.append("official_nwp_daily_curve_contract_schema_invalid")
    if not provider:
        blockers.append("official_nwp_daily_curve_provider_missing")
    if provider.get("state") == "authorized_delivery_not_configured":
        blockers.append("official_nwp_daily_curve_authorized_provider_not_configured")
    if run is None or target_day is None or target_zone is None:
        blockers.append("official_nwp_daily_curve_run_or_target_time_invalid")
    hours = provider.get("allowed_run_hours_utc") if isinstance(provider.get("allowed_run_hours_utc"), list) else []
    try:
        resolution_hours = int(provider.get("curve_time_resolution_hours"))
        max_lead_hours = int(provider.get("maximum_planned_lead_hours"))
    except (TypeError, ValueError):
        resolution_hours, max_lead_hours = 0, 0
    if resolution_hours <= 0 or max_lead_hours <= 0:
        blockers.append("official_nwp_daily_curve_resolution_or_lead_invalid")
    if run is not None and run.hour not in hours:
        blockers.append("official_nwp_daily_curve_run_hour_not_allowed")
    if blockers:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": sorted(set(blockers)), "boundary": {"network_acquisition": False, "writes_posterior": False, "places_orders": False}}
    assert run is not None and target_day is not None and target_zone is not None
    start_local = datetime.combine(target_day, time.min, tzinfo=target_zone)
    end_local = start_local + timedelta(days=1)
    start_utc, end_utc = start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
    # A same-day 06Z/12Z/18Z cycle is valid evidence for the remaining part
    # of that local day.  The previous guard compared the run time with the
    # local-day boundary and therefore rejected every intraday refresh after
    # midnight (for Chengdu, 06Z is already 14:00 local).  Run selection is
    # bounded by the caller's current time; this planner only projects the
    # exact declared points that lie inside the target day.
    expected: list[str] = []
    for lead in range(0, max_lead_hours + 1, resolution_hours):
        valid = run + timedelta(hours=lead)
        if start_utc <= valid < end_utc:
            expected.append(valid.isoformat().replace("+00:00", "Z"))
    if not expected:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "blockers": ["official_nwp_daily_curve_target_window_outside_planned_lead"], "boundary": {"network_acquisition": False, "writes_posterior": False, "places_orders": False}}
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "planned", "provider_id": _text(provider_id), "run_at_utc": run.isoformat().replace("+00:00", "Z"), "target_event_date": target_day.isoformat(), "target_timezone": str(target_zone), "curve_time_resolution_hours": resolution_hours, "expected_valid_times_utc": expected, "expected_point_count": len(expected), "maximum_planned_lead_hours": max_lead_hours, "official_schedule_url": _text(provider.get("official_schedule_url")), "boundary": {"actual_transport_and_raw_availability_must_be_verified": True, "interpolation_or_cross_run_fill_forbidden": True, "network_acquisition": False, "writes_posterior": False, "places_orders": False}}


__all__ = ["CONTRACT_SCHEMA_VERSION", "SCHEMA_VERSION", "build_official_nwp_daily_curve_plan", "load_daily_curve_run_contract"]
