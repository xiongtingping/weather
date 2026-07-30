"""Market settlement-day time boundary sourced from the target market registry."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError


OPERATIONAL_TIMEZONE = "America/New_York"


def _zone(name: str) -> ZoneInfo:
    value = str(name or "").strip()
    if not value:
        raise ContractError("settlement_timezone_missing")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ContractError("settlement_timezone_invalid") from exc


def normalize_event_clock_utc(value: str | datetime) -> str:
    """Normalize an aware event timestamp to the canonical UTC clock."""

    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ContractError("event_clock_missing")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("event_clock_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("event_clock_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolution_local_date_at(
    value: str | datetime,
    settlement_timezone: str,
) -> str:
    """Resolve the market date using only registry settlement timezone."""

    event_clock = datetime.fromisoformat(
        normalize_event_clock_utc(value).replace("Z", "+00:00")
    )
    return event_clock.astimezone(_zone(settlement_timezone)).date().isoformat()


def operational_day_at(
    value: str | datetime,
    *,
    operational_timezone: str = OPERATIONAL_TIMEZONE,
) -> str:
    """Resolve the reporting/operations day without changing market scope."""

    event_clock = datetime.fromisoformat(
        normalize_event_clock_utc(value).replace("Z", "+00:00")
    )
    return event_clock.astimezone(_zone(operational_timezone)).date().isoformat()


def time_contract_snapshot(
    *,
    event_date: str,
    settlement_timezone: str,
    event_at_utc: str | datetime,
    operational_timezone: str = OPERATIONAL_TIMEZONE,
) -> dict[str, str]:
    """Materialize the three independent clocks used by the target lane.

    ``event_date`` and its day window are controlled by the market registry.
    The ET operations day is reporting metadata only.  ``event_clock_utc`` is
    the canonical ordering clock and is never inferred from either local day.
    """

    event_clock_utc = normalize_event_clock_utc(event_at_utc)
    window = settlement_day_window(event_date, settlement_timezone)
    return {
        **window,
        "event_clock_utc": event_clock_utc,
        "resolution_local_date_at_event": resolution_local_date_at(
            event_clock_utc,
            settlement_timezone,
        ),
        "operational_timezone": str(operational_timezone),
        "operational_day": operational_day_at(
            event_clock_utc,
            operational_timezone=operational_timezone,
        ),
    }


def settlement_day_window(event_date: str, settlement_timezone: str) -> dict[str, str]:
    try:
        day = date.fromisoformat(str(event_date or "").strip())
    except ValueError as exc:
        raise ContractError("settlement_event_date_invalid") from exc
    zone = _zone(settlement_timezone)
    start_local = datetime(day.year, day.month, day.day, tzinfo=zone)
    next_day = day + timedelta(days=1)
    end_local = datetime(next_day.year, next_day.month, next_day.day, tzinfo=zone)
    return {
        "event_date": day.isoformat(),
        "settlement_timezone": str(settlement_timezone),
        "target_day_start_utc": start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "target_day_end_utc": end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def settlement_day_state(
    event_date: str,
    *,
    as_of_utc: str | datetime,
    settlement_timezone: str,
) -> dict[str, str]:
    """Classify a market day using only its registry settlement timezone.

    This is deliberately separate from the ET reporting/history helpers.  A
    market day can be open or closed at a different UTC instant than the ET
    operational day, including across DST and non-integral offsets.
    """

    raw = as_of_utc if isinstance(as_of_utc, datetime) else str(as_of_utc or "")
    if isinstance(raw, datetime):
        decision = raw
    else:
        try:
            decision = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError("settlement_as_of_invalid_iso8601") from exc
    if decision.tzinfo is None or decision.utcoffset() is None:
        raise ContractError("settlement_as_of_timezone_required")
    decision = decision.astimezone(timezone.utc)
    window = settlement_day_window(event_date, settlement_timezone)
    start = datetime.fromisoformat(window["target_day_start_utc"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(window["target_day_end_utc"].replace("Z", "+00:00"))
    if decision < start:
        state, reason = "not_started", "target_settlement_day_not_started"
    elif decision < end:
        state, reason = "open", "target_settlement_day_not_closed"
    else:
        state, reason = "closed", "target_settlement_day_closed"
    return {
        **window,
        "as_of_utc": decision.isoformat().replace("+00:00", "Z"),
        "state": state,
        "reason": reason,
    }


def registry_settlement_timezone(
    registry_rows: Iterable[Mapping[str, Any]],
    *,
    station_id: str,
    event_date: str,
) -> str:
    rows = [
        row
        for row in registry_rows
        if str(row.get("station_id") or "").upper() == station_id.upper()
        and str(row.get("event_date") or "") == event_date
    ]
    if not rows:
        raise ContractError("market_registry_settlement_scope_missing")
    timezones = {str(row.get("settlement_timezone") or "").strip() for row in rows}
    if len(timezones) != 1 or not next(iter(timezones)):
        raise ContractError("market_registry_settlement_timezone_not_unique")
    value = next(iter(timezones))
    _zone(value)
    return value


def registry_scope_settlement_timezone(
    registry_rows: Iterable[Mapping[str, Any]],
    *,
    station_ids: Iterable[str],
    event_dates: Iterable[str],
) -> str:
    """Resolve one timezone for an artifact that cannot mix settlement zones.

    Every requested station/date pair must exist in the registry. Callers must
    split mixed-timezone scopes into separate artifacts instead of applying a
    global operational timezone.
    """

    rows = list(registry_rows)
    stations = sorted({str(value or "").strip().upper() for value in station_ids if str(value or "").strip()})
    dates = sorted({str(value or "").strip() for value in event_dates if str(value or "").strip()})
    if not stations or not dates:
        raise ContractError("market_registry_settlement_scope_empty")
    timezones: set[str] = set()
    for station_id in stations:
        for event_date in dates:
            timezones.add(
                registry_settlement_timezone(
                    rows,
                    station_id=station_id,
                    event_date=event_date,
                )
            )
    if len(timezones) != 1:
        raise ContractError("market_registry_mixed_settlement_timezones_forbidden")
    return next(iter(timezones))


def historical_registry_scope_settlement_timezone(
    registry_rows: Iterable[Mapping[str, Any]],
    *,
    station_ids: Iterable[str],
    event_dates: Iterable[str],
) -> str:
    """Resolve pre-market training dates from a registry-proven invariant.

    If a historical weather day predates the first retained market, its
    timezone may be carried backward only when every market-registry row for
    that station has the same timezone. Existing exact-date rows remain
    authoritative and any conflict fails closed.
    """

    rows = list(registry_rows)
    stations = sorted({
        str(value or "").strip().upper()
        for value in station_ids
        if str(value or "").strip()
    })
    dates = sorted({
        str(value or "").strip()
        for value in event_dates
        if str(value or "").strip()
    })
    if not stations or not dates:
        raise ContractError("market_registry_settlement_scope_empty")
    resolved: set[str] = set()
    for station_id in stations:
        station_rows = [
            row
            for row in rows
            if str(row.get("station_id") or "").strip().upper() == station_id
        ]
        if not station_rows:
            raise ContractError("market_registry_station_scope_missing")
        station_timezones = {
            str(row.get("settlement_timezone") or "").strip()
            for row in station_rows
        }
        if len(station_timezones) != 1 or not next(iter(station_timezones)):
            raise ContractError("market_registry_station_timezone_not_invariant")
        station_timezone = next(iter(station_timezones))
        _zone(station_timezone)
        for event_date in dates:
            exact_rows = [
                row
                for row in station_rows
                if str(row.get("event_date") or "") == event_date
            ]
            if exact_rows and registry_settlement_timezone(
                exact_rows,
                station_id=station_id,
                event_date=event_date,
            ) != station_timezone:
                raise ContractError("market_registry_historical_timezone_conflict")
        resolved.add(station_timezone)
    if len(resolved) != 1:
        raise ContractError("market_registry_mixed_settlement_timezones_forbidden")
    return next(iter(resolved))


def assert_probability_timezone_matches_market(
    *,
    probability_timezone: str,
    market_timezone: str,
) -> None:
    probability = str(probability_timezone or "").strip()
    market = str(market_timezone or "").strip()
    _zone(probability)
    _zone(market)
    if probability != market:
        raise ContractError("probability_market_settlement_timezone_mismatch")


__all__ = [
    "OPERATIONAL_TIMEZONE",
    "assert_probability_timezone_matches_market",
    "historical_registry_scope_settlement_timezone",
    "normalize_event_clock_utc",
    "operational_day_at",
    "registry_scope_settlement_timezone",
    "registry_settlement_timezone",
    "resolution_local_date_at",
    "settlement_day_state",
    "settlement_day_window",
    "time_contract_snapshot",
]
