"""Registry-timezone sampling schedule for native daily-temperature curves."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Sequence
from zoneinfo import ZoneInfo


TARGET_LOCAL_SAMPLE_HOURS = (2, 8, 14, 20, 23)


def aligned_target_sample_offsets(
    *,
    target_day_start_utc: datetime,
    run_initialization: datetime,
    common_step_hours: int = 3,
    sample_spacing_hours: int = 6,
) -> tuple[float, ...]:
    """Choose a bounded common valid-time grid inside one settlement day.

    ECMWF operational forecast steps lie on a three-hour grid anchored to the
    run initialization. A fixed local-hour tuple only works accidentally for
    some timezones. This selector finds the provider-aligned points in the
    actual registry day, samples them at six-hour spacing, and always retains
    the last available point so start/end coverage remains explicit.
    """

    if target_day_start_utc.tzinfo is None or target_day_start_utc.utcoffset() is None:
        raise ValueError("model_curve_target_start_timezone_required")
    if run_initialization.tzinfo is None or run_initialization.utcoffset() is None:
        raise ValueError("model_curve_run_initialization_timezone_required")
    step = int(common_step_hours)
    spacing = int(sample_spacing_hours)
    if step <= 0 or spacing < step or spacing % step:
        raise ValueError("model_curve_sample_grid_invalid")
    start = target_day_start_utc.astimezone(timezone.utc)
    run = run_initialization.astimezone(timezone.utc)
    available: list[float] = []
    window_end = start + timedelta(days=1)
    first_lead = max(
        0,
        math.ceil((start - run).total_seconds() / 3600.0),
    )
    first_aligned_lead = (
        ((first_lead + step - 1) // step) * step
    )
    lead = first_aligned_lead
    while True:
        valid_at = run + timedelta(hours=lead)
        if valid_at >= window_end:
            break
        if valid_at >= start:
            available.append(
                (valid_at - start).total_seconds() / 3600.0
            )
        lead += step
    if not available:
        raise ValueError("model_curve_no_provider_aligned_target_points")
    selected = available[:: spacing // step]
    if selected[-1] != available[-1]:
        selected.append(available[-1])
    if selected[0] > 3 or 24 - selected[-1] > 3:
        raise ValueError("model_curve_provider_grid_boundary_incomplete")
    if any(right - left > spacing for left, right in zip(selected, selected[1:])):
        raise ValueError("model_curve_provider_grid_gap_exceeds_spacing")
    return tuple(
        int(offset) if float(offset).is_integer() else float(offset)
        for offset in selected
    )


def forecast_hours_for_target_day(
    *,
    target_local_date: str,
    run_initialization: datetime,
    target_timezone: str,
    local_sample_hours: Sequence[int] = TARGET_LOCAL_SAMPLE_HOURS,
    max_forecast_hour: int,
) -> tuple[int, ...]:
    """Map a local-day sample grid to exact forecast lead hours."""

    try:
        target_day = datetime.strptime(target_local_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("model_curve_target_local_date_invalid") from exc
    if run_initialization.tzinfo is None or run_initialization.utcoffset() is None:
        raise ValueError("model_curve_run_initialization_timezone_required")
    run_utc = run_initialization.astimezone(timezone.utc)
    zone = ZoneInfo(target_timezone)
    hours: list[int] = []
    for raw_local_hour in local_sample_hours:
        local_hour = int(raw_local_hour)
        if local_hour < 0 or local_hour > 23:
            raise ValueError("model_curve_local_sample_hour_invalid")
        valid_local = datetime(
            target_day.year,
            target_day.month,
            target_day.day,
            local_hour,
            tzinfo=zone,
        )
        delta_seconds = (valid_local.astimezone(timezone.utc) - run_utc).total_seconds()
        if delta_seconds % 3600:
            raise ValueError("model_curve_valid_time_not_integer_forecast_hour")
        forecast_hour = int(delta_seconds // 3600)
        if forecast_hour < 0:
            raise ValueError("model_curve_run_newer_than_target_sample")
        if forecast_hour > int(max_forecast_hour):
            raise ValueError("model_curve_forecast_hour_exceeds_supported_horizon")
        hours.append(forecast_hour)
    if len(set(hours)) != len(tuple(local_sample_hours)):
        raise ValueError("model_curve_forecast_hours_not_unique")
    if any(right - left > 6 for left, right in zip(hours, hours[1:])):
        raise ValueError("model_curve_forecast_hour_gap_exceeds_six_hours")
    return tuple(hours)


__all__ = [
    "TARGET_LOCAL_SAMPLE_HOURS",
    "aligned_target_sample_offsets",
    "forecast_hours_for_target_day",
]
