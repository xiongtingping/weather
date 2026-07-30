"""Astronomical and frozen-forecast context for interpreting canonical observations."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from .transition_model import ForecastScenarioControl


@dataclass(frozen=True)
class CandidateTemporalCondition:
    forecast_rate_c_per_hour: float
    future_max_increment_c: float
    minutes_to_curve_peak: float
    forcing_availability: float


@dataclass(frozen=True)
class ObservationTemporalContext:
    local_time: str
    local_hour: float
    solar_elevation_sine: float
    hours_until_sunset: float
    conditions_by_y: Mapping[float, CandidateTemporalCondition]
    forecast_regime_probabilities: Mapping[str, float] = field(default_factory=dict)
    forecast_regime_unknown_probability: float = 1.0


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation_temporal_event_time_invalid")
    return parsed.astimezone(timezone.utc)


def _solar_elevation_sine(*, local: datetime, latitude_deg: float, longitude_deg: float) -> float:
    day = local.timetuple().tm_yday
    hour = local.hour + local.minute / 60.0 + local.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (day - 1 + (hour - 12.0) / 24.0)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.00148 * math.sin(3.0 * gamma)
    )
    utc_offset_hours = float(local.utcoffset().total_seconds()) / 3600.0
    true_solar_minutes = hour * 60.0 + equation_of_time + 4.0 * longitude_deg - 60.0 * utc_offset_hours
    hour_angle = math.radians(true_solar_minutes / 4.0 - 180.0)
    latitude = math.radians(latitude_deg)
    return max(
        0.0,
        math.sin(latitude) * math.sin(declination)
        + math.cos(latitude) * math.cos(declination) * math.cos(hour_angle),
    )


def _hours_until_sunset(*, local: datetime, latitude_deg: float, longitude_deg: float) -> float:
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    current = local.hour + local.minute / 60.0 + local.second / 3600.0
    last_positive = None
    for minute in range(0, 24 * 60, 5):
        probe = midnight + timedelta(minutes=minute)
        if _solar_elevation_sine(local=probe, latitude_deg=latitude_deg, longitude_deg=longitude_deg) > 0.0:
            last_positive = minute / 60.0
    if last_positive is None or current > last_positive:
        return 0.0
    return max(0.0, last_positive - current)


def _aggregate(rows: Sequence[ForecastScenarioControl]) -> CandidateTemporalCondition:
    total = sum(float(row.responsibility) for row in rows)
    if not rows or abs(total - 1.0) > 1e-8:
        raise ValueError("observation_temporal_responsibility_invalid")
    availability = sum(row.responsibility * row.forcing_availability for row in rows)
    if availability <= 0.0:
        return CandidateTemporalCondition(0.0, 0.0, 0.0, 0.0)

    def available_mean(field: str) -> float:
        return sum(
            row.responsibility * row.forcing_availability * float(getattr(row, field))
            for row in rows
        ) / availability

    return CandidateTemporalCondition(
        forecast_rate_c_per_hour=available_mean("forecast_rate_c_per_hour"),
        future_max_increment_c=available_mean("future_max_increment_c"),
        minutes_to_curve_peak=available_mean("minutes_to_curve_peak"),
        forcing_availability=availability,
    )


def build_observation_temporal_context(
    *,
    event_valid_at: str,
    controls_by_y: Mapping[float, Sequence[ForecastScenarioControl]],
    timezone_name: str,
    station_latitude_deg: float,
    station_longitude_deg: float,
    forecast_background_context: Mapping[str, object] | None = None,
) -> ObservationTemporalContext:
    regime_vector: Mapping[str, float] = {}
    unknown_probability = 1.0
    if forecast_background_context:
        from forecast_background_context_runtime import validate_forecast_background_context

        validated = validate_forecast_background_context(forecast_background_context)
        regime_vector = {
            str(key): float(value)
            for key, value in validated["regime_probability_vector"].items()
        }
        unknown_probability = float(validated["unknown_probability"])
    local = _parse_time(event_valid_at).astimezone(ZoneInfo(timezone_name))
    local_hour = local.hour + local.minute / 60.0 + local.second / 3600.0
    return ObservationTemporalContext(
        local_time=local.isoformat(),
        local_hour=local_hour,
        solar_elevation_sine=_solar_elevation_sine(
            local=local, latitude_deg=station_latitude_deg, longitude_deg=station_longitude_deg
        ),
        hours_until_sunset=_hours_until_sunset(
            local=local, latitude_deg=station_latitude_deg, longitude_deg=station_longitude_deg
        ),
        conditions_by_y={float(y): _aggregate(rows) for y, rows in controls_by_y.items()},
        forecast_regime_probabilities=regime_vector,
        forecast_regime_unknown_probability=unknown_probability,
    )


__all__ = [
    "CandidateTemporalCondition",
    "ObservationTemporalContext",
    "build_observation_temporal_context",
]
