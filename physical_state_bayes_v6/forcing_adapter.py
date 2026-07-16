"""Convert the frozen weak mixture into candidate-conditioned future controls."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .transition_model import ForecastScenarioControl


@dataclass(frozen=True)
class ScenarioControlBundle:
    weak_likelihood_sha256: str
    scenario_paths_sha256: str
    responsibilities_sha256: str
    event_valid_at: str
    controls_by_y: Mapping[float, tuple[ForecastScenarioControl, ...]]
    lineage_sha256: str


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _curve_points(path: Mapping[str, Any]) -> list[tuple[datetime, Mapping[str, Any]]]:
    result = []
    for raw in path.get("hourly_curve") or []:
        if not isinstance(raw, Mapping):
            continue
        valid = _time(raw.get("valid_time_utc") or raw.get("valid_time"))
        temperature = _finite(raw.get("temperature_c"))
        if valid is not None and temperature is not None:
            result.append((valid, dict(raw)))
    return sorted(result, key=lambda item: item[0])


def _interpolate(points: list[tuple[datetime, Mapping[str, Any]]], event: datetime, field: str) -> float | None:
    available = [(time, _finite(row.get(field))) for time, row in points]
    available = [(time, value) for time, value in available if value is not None]
    if not available:
        return None
    exact = [value for time, value in available if time == event]
    if exact:
        return float(exact[-1])
    before = [(time, value) for time, value in available if time < event]
    after = [(time, value) for time, value in available if time > event]
    if not before or not after:
        return None
    left_time, left_value = before[-1]
    right_time, right_value = after[0]
    width = (right_time - left_time).total_seconds()
    fraction = (event - left_time).total_seconds() / width
    return float(left_value) + fraction * (float(right_value) - float(left_value))


def _temperature_control(path: Mapping[str, Any], event: datetime) -> dict[str, float]:
    points = _curve_points(path)
    current = _interpolate(points, event, "temperature_c")
    future = [(time, float(row["temperature_c"])) for time, row in points if time >= event]
    if current is None or not future:
        return {
            "forecast_rate_c_per_hour": 0.0,
            "future_max_increment_c": 0.0,
            "future_max_drawdown_c": 0.0,
            "minutes_to_curve_peak": 0.0,
            "forcing_availability": 0.0,
        }
    surrounding = [(time, float(row["temperature_c"])) for time, row in points]
    before = [(time, value) for time, value in surrounding if time <= event]
    after = [(time, value) for time, value in surrounding if time >= event]
    rate = 0.0
    if before and after and before[-1][0] != after[0][0]:
        hours = (after[0][0] - before[-1][0]).total_seconds() / 3600.0
        rate = (after[0][1] - before[-1][1]) / hours
    elif len(after) >= 2:
        hours = (after[1][0] - after[0][0]).total_seconds() / 3600.0
        rate = (after[1][1] - after[0][1]) / hours
    max_temperature = max(value for _time_value, value in future)
    min_temperature = min(value for _time_value, value in future)
    peak_time = min(time for time, value in future if value == max_temperature)
    return {
        "forecast_rate_c_per_hour": rate,
        "future_max_increment_c": max_temperature - current,
        "future_max_drawdown_c": max(0.0, current - min_temperature),
        "minutes_to_curve_peak": (peak_time - event).total_seconds() / 60.0,
        "forcing_availability": 1.0,
    }


def _probability_control(
    path: Mapping[str, Any], event: datetime, fields: tuple[str, ...]
) -> tuple[float, float]:
    points = _curve_points(path)
    for field in fields:
        value = _interpolate(points, event, field)
        if value is not None:
            if value > 1.0:
                value /= 100.0
            return min(1.0, max(0.0, value)), 1.0
    return 0.0, 0.0


def build_candidate_scenario_controls(
    *, weak_likelihood: Mapping[str, Any], event_valid_at: str
) -> ScenarioControlBundle:
    event = _time(event_valid_at)
    if event is None:
        raise ValueError("forcing_adapter_event_time_invalid")
    paths_object = weak_likelihood.get("forecast_scenario_paths")
    responsibilities_object = weak_likelihood.get("candidate_scenario_responsibilities")
    if not isinstance(paths_object, Mapping) or not isinstance(responsibilities_object, Mapping):
        raise ValueError("forcing_adapter_weak_scenario_contract_missing")
    paths = {
        _text(row.get("scenario_id")): dict(row)
        for row in paths_object.get("paths") or []
        if isinstance(row, Mapping) and _text(row.get("scenario_id"))
    }
    controls_by_y: dict[float, tuple[ForecastScenarioControl, ...]] = {}
    for raw_row in responsibilities_object.get("rows") or []:
        if not isinstance(raw_row, Mapping):
            continue
        y_c = _finite(raw_row.get("continuous_y_c"))
        if y_c is None:
            raise ValueError("forcing_adapter_candidate_y_invalid")
        controls = []
        for raw_scenario in raw_row.get("scenarios") or []:
            if not isinstance(raw_scenario, Mapping):
                continue
            scenario_id = _text(raw_scenario.get("scenario_id"))
            responsibility = _finite(raw_scenario.get("responsibility"))
            if not scenario_id or responsibility is None or responsibility < 0.0:
                raise ValueError("forcing_adapter_scenario_identity_invalid")
            path = paths.get(scenario_id, {})
            temperature = _temperature_control(path, event)
            precipitation, precipitation_available = _probability_control(
                path, event, ("precipitation_probability", "precip_probability")
            )
            cloud, cloud_available = _probability_control(
                path, event, ("cloud_suppression_probability", "cloud_cover_probability")
            )
            cb, cb_available = _probability_control(
                path, event, ("cb_probability", "convective_probability")
            )
            controls.append(
                ForecastScenarioControl(
                    scenario_id=scenario_id,
                    responsibility=responsibility,
                    **temperature,
                    precipitation_probability=precipitation,
                    precipitation_feature_available=precipitation_available,
                    cloud_suppression_probability=cloud,
                    cloud_feature_available=cloud_available,
                    cb_probability=cb,
                    cb_feature_available=cb_available,
                )
            )
        if not controls or abs(sum(item.responsibility for item in controls) - 1.0) > 1e-8:
            raise ValueError(f"forcing_adapter_responsibility_not_normalized:{y_c:g}")
        controls_by_y[y_c] = tuple(controls)
    if not controls_by_y:
        raise ValueError("forcing_adapter_candidate_controls_empty")
    lineage = {
        "weak_likelihood_sha256": _text(weak_likelihood.get("joint_forecast_weak_likelihood_sha256")),
        "scenario_paths_sha256": _text(paths_object.get("sha256")),
        "responsibilities_sha256": _text(responsibilities_object.get("sha256")),
        "event_valid_at": event_valid_at,
        "controls": {
            f"{y:g}": [asdict(item) for item in rows]
            for y, rows in sorted(controls_by_y.items())
        },
        "interpolation_policy": "linear_between_frozen_asof_curve_points_for_transition_control_only",
    }
    return ScenarioControlBundle(
        weak_likelihood_sha256=lineage["weak_likelihood_sha256"],
        scenario_paths_sha256=lineage["scenario_paths_sha256"],
        responsibilities_sha256=lineage["responsibilities_sha256"],
        event_valid_at=event_valid_at,
        controls_by_y=controls_by_y,
        lineage_sha256=_sha(lineage),
    )


__all__ = ["ScenarioControlBundle", "build_candidate_scenario_controls"]
