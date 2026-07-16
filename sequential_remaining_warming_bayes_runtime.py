"""Continuous joint-weak plus physical-strong Bayes posterior.

This pure runtime is the production composer for daily maximum temperature.
It keeps the target continuous until the final settlement-bucket integral:

    Y = M_t + Delta_t,  Delta_t >= 0

The one approved joint weak model provides a forecast-only likelihood for X.
Distinct METAR valid times update a latent physical state sequentially.  The
physical state, momentum, trajectory, cloud, rain and dewpoint evidence drive
a remaining-daily-high distribution.  A continuous observation-information
gate mixes the pre-observation density with that physical distribution before
the hard mask and settlement-bucket integral.  The retired v3.1 probability
model and the legacy bucket-product posterior are not used.

The module has no side effects and deliberately contains structural, unfitted
parameters only.  Historical fitting belongs to the governed calibration
path, not this production runtime.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from source_observation_runtime import (
    PRIMARY_METAR_SOURCE_ID,
    REFERENCE_METAR_SOURCE_IDS,
    SOURCE_CLASSES,
    SourceObservation,
)
from weather_ensemble_bias_correction import parse_outcome_label


SCHEMA_VERSION = "sequential_remaining_warming_bayes_runtime_v31"
CONTINUOUS_SCHEMA_VERSION = "sequential_remaining_warming_continuous_distribution_v31"
STATE_SCHEMA_VERSION = "sequential_metar_state_filter_v1"
POLICY_VERSION = "joint_weak_physical_strong_continuous_composer_v1"
EPS = 1e-12
GRID_STEP_C = 0.1
LOCAL_DAY_LAST_HALF_HOUR = 23.5
DEFAULT_QUANTILES = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)
STATE_ORDER = (
    "wet_layer_closed",
    "opening",
    "open_boundary_layer",
    "plateau",
    "convective_cutoff",
)


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "reads_market_prices": False,
    }


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _clamp(value: float, low: float, high: float) -> float:
    return max(float(low), min(float(high), float(value)))


def _normalize(values: Sequence[float]) -> list[float]:
    cleaned = [max(0.0, float(value)) for value in values]
    total = sum(cleaned)
    if total <= EPS:
        return [1.0 / len(cleaned) for _ in cleaned] if cleaned else []
    return [value / total for value in cleaned]


def _round(value: float | None, digits: int = 12) -> float | None:
    return None if value is None else round(float(value), digits)


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("payload", "observation_json", "raw_json"):
        parsed = _as_mapping(row.get(key))
        if parsed:
            return parsed
    return {}


def _first_value(row: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ""):
            return row.get(key)
        if payload.get(key) not in (None, ""):
            return payload.get(key)
    return None


def _first_float(row: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> float | None:
    return _to_float(_first_value(row, payload, *keys))


def _structured_text(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    *keys: str,
    raw_fallback_keys: Sequence[str] = (),
) -> str:
    for key in keys:
        if key in row:
            return str(row.get(key) or "").strip()
        if key in payload:
            return str(payload.get(key) or "").strip()
    return str(_first_value(row, payload, *raw_fallback_keys) or "").strip()


def _bucket_interval(label: str, rounding_rule: str) -> tuple[float, float, float] | None:
    try:
        spec = parse_outcome_label(str(label or ""))
    except ValueError:
        return None
    rounded = str(rounding_rule or "").strip().lower() in {
        "",
        "rounded_degrees",
        "whole_degrees",
        "nearest_whole",
        "nearest",
        "round",
    }
    if spec.kind == "eq":
        center = float(spec.k)
        return (center - 0.5, center + 0.5, center) if rounded else (center, center, center)
    if spec.kind == "range" and spec.k2 is not None:
        low = min(float(spec.k), float(spec.k2))
        high = max(float(spec.k), float(spec.k2))
        if rounded:
            low -= 0.5
            high += 0.5
        return low, high, (low + high) / 2.0
    if spec.kind == "le":
        high = float(spec.k) + 0.5 if rounded else float(spec.k)
        return float("-inf"), high, float(spec.k)
    if spec.kind == "ge":
        low = float(spec.k) - 0.5 if rounded else float(spec.k)
        return low, float("inf"), float(spec.k)
    return None


def _grid(
    labels: Sequence[str],
    *,
    rounding_rule: str,
    observed_floor_c: float | None,
    forecast_output: Mapping[str, Any],
) -> tuple[list[float], dict[str, tuple[float, float, float]]]:
    intervals: dict[str, tuple[float, float, float]] = {}
    finite_lows: list[float] = []
    finite_highs: list[float] = []
    anchors: list[float] = []
    for label in labels:
        interval = _bucket_interval(label, rounding_rule)
        if interval is None:
            continue
        intervals[label] = interval
        low, high, anchor = interval
        if math.isfinite(low):
            finite_lows.append(low)
        if math.isfinite(high):
            finite_highs.append(high)
        anchors.append(anchor)
    forecast_trace = forecast_output.get("source_group_trace")
    if isinstance(forecast_trace, list):
        for row in forecast_trace:
            if not isinstance(row, Mapping):
                continue
            mean = _to_float(row.get("forecast_peak_mean_c"))
            sigma = _to_float(row.get("forecast_sigma_effective_c"))
            if mean is not None:
                anchors.extend([mean - 5.0 * max(0.5, sigma or 1.0), mean + 5.0 * max(0.5, sigma or 1.0)])
    if observed_floor_c is not None:
        anchors.extend([float(observed_floor_c), float(observed_floor_c) + 8.0])
    base_low = min(finite_lows or anchors or [15.0])
    base_high = max(finite_highs or anchors or [45.0])
    low = min([base_low - 5.0, *(anchors or [base_low])])
    high = max([base_high + 5.0, *(anchors or [base_high])])
    if observed_floor_c is not None:
        low = min(low, float(observed_floor_c) - 1.0)
        high = max(high, float(observed_floor_c) + 8.0)
    low = math.floor(low / GRID_STEP_C) * GRID_STEP_C
    high = math.ceil(high / GRID_STEP_C) * GRID_STEP_C
    count = min(1200, max(2, int(round((high - low) / GRID_STEP_C)) + 1))
    values = [round(low + index * GRID_STEP_C, 6) for index in range(count)]
    return values, intervals


def _bucket_mass_to_grid(
    labels: Sequence[str],
    distribution: Mapping[str, Any],
    grid: Sequence[float],
    intervals: Mapping[str, tuple[float, float, float]],
) -> list[float]:
    out = [0.0 for _ in grid]
    raw = [max(0.0, float(_to_float(distribution.get(label)) or 0.0)) for label in labels]
    probabilities = _normalize(raw)
    for label, probability in zip(labels, probabilities):
        interval = intervals.get(label)
        if interval is None or probability <= 0.0:
            continue
        low, high, _anchor = interval
        indices = [
            index
            for index, temp in enumerate(grid)
            if (not math.isfinite(low) or temp >= low - EPS)
            and (not math.isfinite(high) or temp < high - EPS)
        ]
        if not indices:
            continue
        share = probability / len(indices)
        for index in indices:
            out[index] += share
    return _normalize(out)


def _continuous_prior_mass(
    *,
    labels: Sequence[str],
    prior_distribution: Mapping[str, Any],
    prior_continuous_distribution: Mapping[str, Any] | None,
    grid: Sequence[float],
    intervals: Mapping[str, tuple[float, float, float]],
) -> tuple[list[float], str]:
    continuous = prior_continuous_distribution if isinstance(prior_continuous_distribution, Mapping) else {}
    rows = continuous.get("grid") if isinstance(continuous.get("grid"), list) else []
    points: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        temp = _to_float(row.get("temp_c"))
        mass = _to_float(row.get("mass"))
        if mass is None:
            pdf = _to_float(row.get("pdf"))
            mass = pdf * GRID_STEP_C if pdf is not None else None
        if temp is not None and mass is not None and mass >= 0.0:
            points.append((temp, mass))
    if points:
        out = []
        for temp in grid:
            nearest = min(points, key=lambda item: abs(item[0] - temp))
            out.append(nearest[1] if abs(nearest[0] - temp) <= GRID_STEP_C * 1.5 else 0.0)
        if sum(out) > EPS:
            return _normalize(out), "continuous_climate_prior"
    return (
        _bucket_mass_to_grid(labels, prior_distribution, grid, intervals),
        "bucket_prior_piecewise_density_fallback",
    )


def _normal_pdf(value: float, mean: float, sigma: float) -> float:
    scale = max(0.05, float(sigma))
    z = (float(value) - float(mean)) / scale
    return math.exp(-0.5 * z * z) / (scale * math.sqrt(2.0 * math.pi))


def _forecast_error_likelihood(
    *,
    labels: Sequence[str],
    forecast_output: Mapping[str, Any],
    grid: Sequence[float],
    intervals: Mapping[str, tuple[float, float, float]],
) -> tuple[list[float], list[dict[str, Any]], str]:
    joint_rows = forecast_output.get("joint_forecast_weak_likelihood")
    if isinstance(joint_rows, list):
        points: list[tuple[float, float]] = []
        for row in joint_rows:
            if not isinstance(row, Mapping):
                continue
            temperature = _to_float(row.get("temperature_c"))
            likelihood = _to_float(row.get("likelihood"))
            if temperature is not None and likelihood is not None and likelihood >= 0.0:
                points.append((float(temperature), float(likelihood)))
        points.sort(key=lambda item: item[0])
        if len(points) >= 2 and sum(value for _temperature, value in points) > EPS:
            projected: list[float] = []
            cursor = 0
            for temperature in grid:
                while cursor + 1 < len(points) and points[cursor + 1][0] < temperature:
                    cursor += 1
                if temperature < points[0][0] or temperature > points[-1][0]:
                    projected.append(0.0)
                    continue
                left = points[cursor]
                right = points[min(cursor + 1, len(points) - 1)]
                if abs(right[0] - left[0]) <= EPS:
                    projected.append(max(0.0, left[1]))
                else:
                    fraction = _clamp((float(temperature) - left[0]) / (right[0] - left[0]), 0.0, 1.0)
                    projected.append(max(0.0, left[1] + fraction * (right[1] - left[1])))
            if sum(projected) > EPS:
                return (
                    _normalize(projected),
                    [
                        {
                            "model": "joint_forecast_weak_likelihood_v1",
                            "artifact_sha256": str(forecast_output.get("artifact_sha256") or ""),
                            "forecast_evidence_snapshot_id": str(
                                forecast_output.get("forecast_evidence_snapshot_id") or ""
                            ),
                            "source_set_key": str(
                                (forecast_output.get("weak_source_set") or {}).get("source_set_key") or ""
                            ),
                        }
                    ],
                    "approved_joint_forecast_weak_continuous_likelihood",
                )
    group_rows = forecast_output.get("source_group_trace")
    components: list[dict[str, Any]] = []
    if isinstance(group_rows, list):
        for row in group_rows:
            if not isinstance(row, Mapping):
                continue
            mean = _to_float(row.get("forecast_peak_mean_c"))
            sigma = _to_float(row.get("forecast_sigma_effective_c"))
            weight = _to_float(row.get("capped_precision"))
            if mean is None or sigma is None or weight is None or weight <= EPS:
                continue
            components.append(
                {
                    "group": str(row.get("group") or "forecast"),
                    "mean_c": float(mean),
                    "sigma_c": max(0.35, float(sigma)),
                    "raw_weight": float(weight),
                    "source_count": int(row.get("source_count") or 0),
                }
            )
    if components:
        weights = _normalize([float(row["raw_weight"]) for row in components])
        likelihood = [0.0 for _ in grid]
        for component, weight in zip(components, weights):
            component["weight"] = _round(weight)
            for index, temp in enumerate(grid):
                likelihood[index] += weight * _normal_pdf(temp, component["mean_c"], component["sigma_c"])
        return _normalize(likelihood), components, "calibrated_forecast_error_mixture"
    weak_map = forecast_output.get("forecast_weak_likelihood_map")
    if isinstance(weak_map, Mapping) and weak_map:
        return (
            _bucket_mass_to_grid(labels, weak_map, grid, intervals),
            [],
            "forecast_bucket_distribution_fallback",
        )
    return [1.0 for _ in grid], [], "forecast_missing_neutral_likelihood"


def _physical_strong_remaining_mass(
    *,
    labels: Sequence[str],
    grid: Sequence[float],
    intervals: Mapping[str, tuple[float, float, float]],
    metar_output: Mapping[str, Any],
    pre_observation: Sequence[float],
    observed_floor: float | None,
) -> tuple[list[float], list[dict[str, Any]], str]:
    """Project the current physical observation model onto the X grid.

    This consumes the strong layer exactly once.  Its state rows are already
    joint functions of trajectory, momentum and physical weather evidence;
    they must not be multiplied again as independent pseudo-likelihoods.
    """

    rows = metar_output.get("remaining_warming_by_state")
    components: list[dict[str, Any]] = []
    if isinstance(rows, list):
        density = [0.0 for _temperature in grid]
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            probability = _to_float(raw.get("state_prob"))
            mean = _to_float(raw.get("peak_mu_c"))
            sigma = _to_float(raw.get("sigma_c"))
            if probability is None or probability <= 0.0 or mean is None or sigma is None or sigma <= 0.0:
                continue
            component = dict(raw)
            components.append(component)
            for index, temperature in enumerate(grid):
                density[index] += float(probability) * _normal_pdf(float(temperature), float(mean), float(sigma))
        if sum(density) > EPS:
            return (
                _hard_condition_mass(_normalize(density), grid, observed_floor_c=observed_floor),
                components,
                "physical_observation_state_mixture",
            )

    strong_map = metar_output.get("metar_strong_likelihood_map")
    if isinstance(strong_map, Mapping) and strong_map:
        projected = _bucket_mass_to_grid(labels, strong_map, grid, intervals)
        if sum(projected) > EPS:
            return (
                _hard_condition_mass(projected, grid, observed_floor_c=observed_floor),
                [],
                "physical_observation_bucket_projection",
            )
    return list(pre_observation), [], "physical_observation_missing_neutral"


def _coerce_source_row(raw: Any) -> dict[str, Any]:
    if isinstance(raw, SourceObservation):
        return raw.as_dict(include_payload=True)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _source_priority(source_id: str) -> int:
    if source_id == PRIMARY_METAR_SOURCE_ID:
        return 100
    if source_id in REFERENCE_METAR_SOURCE_IDS:
        return 50
    return 10


def _canonical_metar_points(
    source_observations: Iterable[Any],
    *,
    as_of: Any,
    event_date: str,
    station_timezone: str,
    metar_output: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    as_of_dt = _parse_dt(as_of)
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for raw in source_observations or []:
        row = _coerce_source_row(raw)
        if not row:
            continue
        payload = _payload(row)
        source_class = str(_first_value(row, payload, "source_class") or "")
        if source_class != SOURCE_CLASSES["hard_observation"]:
            continue
        source_event_date = str(_first_value(row, payload, "event_date", "target_event_date") or "")[:10]
        if event_date and source_event_date and source_event_date != str(event_date)[:10]:
            excluded.append({"source_event_id": row.get("source_event_id"), "reason": "event_date_mismatch"})
            continue
        valid_text = str(_first_value(row, payload, "valid_time", "valid_time_local", "observed_at") or "")
        fetched_text = str(_first_value(row, payload, "fetched_at", "response_completed_at", "parsed_at") or "")
        valid_dt = _parse_dt(valid_text)
        fetched_dt = _parse_dt(fetched_text)
        if as_of_dt is not None and valid_dt is not None and (valid_dt - as_of_dt).total_seconds() > 60.0:
            excluded.append({"source_event_id": row.get("source_event_id"), "reason": "future_valid_time"})
            continue
        if as_of_dt is not None and fetched_dt is not None and (fetched_dt - as_of_dt).total_seconds() > 60.0:
            excluded.append({"source_event_id": row.get("source_event_id"), "reason": "future_fetch_time"})
            continue
        temperature = _first_float(row, payload, "temperature_c", "temp_c", "value_c")
        if temperature is None:
            excluded.append({"source_event_id": row.get("source_event_id"), "reason": "temperature_missing"})
            continue
        source_id = str(_first_value(row, payload, "source_id", "source") or "")
        candidates.append(
            {
                "source_event_id": str(row.get("source_event_id") or ""),
                "source_observation_id": str(row.get("source_observation_id") or ""),
                "source_id": source_id,
                "priority": _source_priority(source_id),
                "valid_time": valid_text,
                "valid_dt": valid_dt,
                "fetched_at": fetched_text,
                "fetched_dt": fetched_dt,
                "temperature_c": float(temperature),
                "dewpoint_c": _first_float(row, payload, "dewpoint_c", "dew_point_c"),
                "observed_today_max_c": _first_float(row, payload, "observed_today_max_c", "today_max_c"),
                "weather_text": _structured_text(
                    row,
                    payload,
                    "weather_text",
                    "weather",
                    "wx",
                    raw_fallback_keys=("raw_text", "raw_ref"),
                ).upper(),
                "metar_current_no_significant_weather": bool(
                    _first_value(row, payload, "metar_current_no_significant_weather")
                ),
                "cloud_text": _structured_text(
                    row,
                    payload,
                    "cloud_text",
                    "clouds",
                    raw_fallback_keys=("raw_text", "raw_ref"),
                ).upper(),
                "metar_trend_text": _structured_text(row, payload, "metar_trend_text").upper(),
                "metar_trend_cloud_text": _structured_text(row, payload, "metar_trend_cloud_text").upper(),
                "metar_trend_weather_text": _structured_text(row, payload, "metar_trend_weather_text").upper(),
                "metar_trend_nosig": bool(_first_value(row, payload, "metar_trend_nosig")),
                "visibility_m": _first_float(row, payload, "visibility_m", "visibility"),
                "cloud_cover_pct": _first_float(row, payload, "cloud_cover_pct", "cloud_pct"),
                "low_cloud_cover_pct": _first_float(row, payload, "low_cloud_cover_pct", "low_cloud_pct"),
                "wind_speed_mps": _first_float(row, payload, "wind_speed_mps", "wind_speed"),
                "wind_dir_degrees": _first_float(
                    row,
                    payload,
                    "wind_dir_degrees",
                    "wind_dir_deg",
                    "wind_direction_degrees",
                ),
                "wind_gust_mps": _first_float(row, payload, "wind_gust_mps", "gust_mps"),
                "wind_variable": bool(_first_value(row, payload, "wind_variable")),
                "wind_variable_from_deg": _first_float(row, payload, "wind_variable_from_deg"),
                "wind_variable_to_deg": _first_float(row, payload, "wind_variable_to_deg"),
                "pressure_hpa": _first_float(row, payload, "pressure_hpa", "altimeter_hpa", "pressure"),
            }
        )
    by_valid: dict[str, dict[str, Any]] = {}
    for row in candidates:
        valid_dt = row.get("valid_dt")
        valid_key = valid_dt.astimezone(timezone.utc).isoformat() if isinstance(valid_dt, datetime) else ""
        if not valid_key:
            valid_key = str(row.get("valid_time") or "")
        if not valid_key:
            valid_key = f"missing:{row.get('source_event_id') or len(by_valid)}"
        current = by_valid.get(valid_key)
        candidate_key = (
            int(row.get("priority") or 0),
            row.get("fetched_dt") or datetime.min.replace(tzinfo=timezone.utc),
        )
        current_key = (
            int(current.get("priority") or 0),
            current.get("fetched_dt") or datetime.min.replace(tzinfo=timezone.utc),
        ) if current else None
        if current is None or candidate_key > current_key:
            by_valid[valid_key] = row
    points = sorted(
        by_valid.values(),
        key=lambda row: (
            row.get("valid_dt") or row.get("fetched_dt") or datetime.min.replace(tzinfo=timezone.utc),
            str(row.get("source_event_id") or ""),
        ),
    )
    features = metar_output.get("observation_features") if isinstance(metar_output.get("observation_features"), Mapping) else {}
    if points:
        latest = points[-1]
        for key in (
            "weather_text",
            "cloud_text",
            "visibility_m",
            "wind_speed_mps",
            "wind_dir_degrees",
            "wind_gust_mps",
            "wind_variable",
            "wind_variable_from_deg",
            "wind_variable_to_deg",
            "pressure_hpa",
            "dewpoint_c",
            "cloud_cover_pct",
            "low_cloud_cover_pct",
            "metar_trend_text",
            "metar_trend_cloud_text",
            "metar_trend_weather_text",
            "metar_trend_nosig",
            "metar_current_no_significant_weather",
        ):
            if latest.get(key) in (None, "") and features.get(key) not in (None, ""):
                latest[key] = features.get(key)
    else:
        constraints = metar_output.get("hard_constraints") if isinstance(metar_output.get("hard_constraints"), Mapping) else {}
        current = _to_float(constraints.get("current_temperature_c"))
        observed = _to_float(constraints.get("observed_today_max_c"))
        if current is not None or observed is not None:
            points = [
                {
                    "source_event_id": "",
                    "source_observation_id": "",
                    "source_id": "observation_inputs_fallback",
                    "priority": 0,
                    "valid_time": str(as_of or ""),
                    "valid_dt": as_of_dt,
                    "fetched_at": str(as_of or ""),
                    "fetched_dt": as_of_dt,
                    "temperature_c": float(current if current is not None else observed),
                    "dewpoint_c": _to_float(features.get("dewpoint_c")),
                    "observed_today_max_c": observed,
                    "weather_text": str(features.get("weather_text") or "").upper(),
                    "cloud_text": str(features.get("cloud_text") or "").upper(),
                    "visibility_m": _to_float(features.get("visibility_m")),
                    "wind_speed_mps": _to_float(features.get("wind_speed_mps")),
                    "wind_dir_degrees": _to_float(features.get("wind_dir_degrees")),
                    "wind_gust_mps": _to_float(features.get("wind_gust_mps")),
                    "wind_variable": bool(features.get("wind_variable")),
                    "wind_variable_from_deg": _to_float(features.get("wind_variable_from_deg")),
                    "wind_variable_to_deg": _to_float(features.get("wind_variable_to_deg")),
                    "pressure_hpa": _to_float(features.get("pressure_hpa")),
                    "cloud_cover_pct": _to_float(features.get("cloud_cover_pct")),
                    "low_cloud_cover_pct": _to_float(features.get("low_cloud_cover_pct")),
                    "metar_trend_text": str(features.get("metar_trend_text") or "").upper(),
                    "metar_trend_cloud_text": str(features.get("metar_trend_cloud_text") or "").upper(),
                    "metar_trend_weather_text": str(features.get("metar_trend_weather_text") or "").upper(),
                    "metar_trend_nosig": bool(features.get("metar_trend_nosig")),
                    "metar_current_no_significant_weather": bool(
                        features.get("metar_current_no_significant_weather")
                    ),
                }
            ]
    for row in points:
        row.pop("valid_dt", None)
        row.pop("fetched_dt", None)
        row.pop("priority", None)
    return points, {
        "candidate_count": len(candidates),
        "distinct_valid_time_count": len(points),
        "excluded": excluded,
        "dedupe_key": "normalized_utc_valid_time_keep_highest_source_priority_then_latest_fetch",
        "forecast_revision_semantics": "not_applicable_to_metar_points",
    }


def _local_hour(value: Any, station_timezone: str) -> float:
    dt = _parse_dt(value)
    if dt is None:
        return 12.0
    local = dt.astimezone(ZoneInfo(str(station_timezone or "Asia/Shanghai")))
    return local.hour + local.minute / 60.0 + local.second / 3600.0


def _softmax(scores: Mapping[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    high = max(float(value) for value in scores.values())
    raw = {key: math.exp(float(value) - high) for key, value in scores.items()}
    total = sum(raw.values())
    return {key: value / total for key, value in raw.items()}


def _initial_state_probs(hour: float) -> dict[str, float]:
    if hour < 8.0:
        values = (0.42, 0.28, 0.10, 0.17, 0.03)
    elif hour < 11.0:
        values = (0.28, 0.36, 0.18, 0.15, 0.03)
    elif hour < 14.0:
        values = (0.12, 0.25, 0.31, 0.26, 0.06)
    else:
        values = (0.05, 0.10, 0.16, 0.52, 0.17)
    return dict(zip(STATE_ORDER, values))


TRANSITION = {
    "wet_layer_closed": (0.72, 0.20, 0.03, 0.04, 0.01),
    "opening": (0.12, 0.62, 0.18, 0.06, 0.02),
    "open_boundary_layer": (0.03, 0.12, 0.65, 0.15, 0.05),
    "plateau": (0.02, 0.08, 0.14, 0.65, 0.11),
    "convective_cutoff": (0.01, 0.03, 0.04, 0.12, 0.80),
}


def _transition_state(prior: Mapping[str, float], step_scale: float) -> dict[str, float]:
    moved = {state: 0.0 for state in STATE_ORDER}
    for source in STATE_ORDER:
        source_prob = float(prior.get(source) or 0.0)
        for target, probability in zip(STATE_ORDER, TRANSITION[source]):
            moved[target] += source_prob * probability
    scale = _clamp(step_scale, 0.0, 1.0)
    return {
        state: (1.0 - scale) * float(prior.get(state) or 0.0) + scale * moved[state]
        for state in STATE_ORDER
    }


def _angular_difference_degrees(current: float | None, previous: float | None) -> float:
    if current is None or previous is None:
        return 0.0
    return abs((float(current) - float(previous) + 180.0) % 360.0 - 180.0)


def _emission_evidence(
    point: Mapping[str, Any],
    previous_point: Mapping[str, Any] | None,
    *,
    hour: float,
    rate: float,
) -> dict[str, Any]:
    previous = previous_point if isinstance(previous_point, Mapping) else {}
    weather = str(point.get("weather_text") or "").upper()
    cloud = str(point.get("cloud_text") or "").upper()
    joined = f"{weather} {cloud}"
    trend_weather = str(point.get("metar_trend_weather_text") or "").upper()
    trend_cloud = str(point.get("metar_trend_cloud_text") or "").upper()
    trend_text = str(point.get("metar_trend_text") or "").upper()
    trend_joined = f"{trend_weather} {trend_cloud} {trend_text}"
    precip = 1.0 if any(token in joined for token in ("RA", "SHRA", "TS", "DZ")) else 0.0
    convective = 1.0 if any(token in joined for token in ("TS", "CB", "TCU", "SHRA")) else 0.0
    trend_convective = 1.0 if any(token in trend_joined for token in ("TS", "CB", "TCU", "SHRA")) else 0.0
    no_significant_weather = 1.0 if point.get("metar_current_no_significant_weather") else 0.0
    low_cloud = 1.0 if any(token in cloud for token in ("BKN", "OVC")) else 0.0
    clear = 1.0 if any(token in cloud for token in ("CAVOK", "CLR", "SKC", "FEW", "SCT")) else 0.0
    temp = _to_float(point.get("temperature_c"))
    dew = _to_float(point.get("dewpoint_c"))
    previous_dew = _to_float(previous.get("dewpoint_c"))
    spread = (temp - dew) if temp is not None and dew is not None else 4.0
    spread_closed = _clamp((4.0 - spread) / 4.0, 0.0, 1.0)
    spread_open = _clamp((spread - 3.0) / 5.0, 0.0, 1.0)
    dewpoint_delta = (float(dew) - float(previous_dew)) if dew is not None and previous_dew is not None else 0.0
    moistening = _clamp(dewpoint_delta / 2.0, 0.0, 1.0)
    drying = _clamp(-dewpoint_delta / 2.0, 0.0, 1.0)
    positive = _clamp(rate / 1.5, 0.0, 1.5)
    negative = _clamp(-rate / 1.5, 0.0, 1.5)
    flat = _clamp(1.0 - abs(rate) / 0.6, 0.0, 1.0)
    after_noon = _clamp((hour - 12.0) / 4.0, 0.0, 1.0)

    visibility = _to_float(point.get("visibility_m"))
    previous_visibility = _to_float(previous.get("visibility_m"))
    visibility_good = 1.0 if visibility is not None and visibility >= 9000.0 else 0.0
    visibility_poor = 1.0 if visibility is not None and visibility < 8000.0 else 0.0
    visibility_drop = (
        max(0.0, float(previous_visibility) - float(visibility))
        if visibility is not None and previous_visibility is not None
        else 0.0
    )
    visibility_deteriorating = _clamp(visibility_drop / 2000.0, 0.0, 1.0)

    wind_speed = _to_float(point.get("wind_speed_mps"))
    previous_wind_speed = _to_float(previous.get("wind_speed_mps"))
    wind_gust = _to_float(point.get("wind_gust_mps"))
    wind_dir = _to_float(point.get("wind_dir_degrees"))
    previous_wind_dir = _to_float(previous.get("wind_dir_degrees"))
    wind_jump = (
        float(wind_speed) - float(previous_wind_speed)
        if wind_speed is not None and previous_wind_speed is not None
        else 0.0
    )
    wind_shift = _angular_difference_degrees(wind_dir, previous_wind_dir)
    wind_ventilation = _clamp(((wind_speed or 0.0) - 3.0) / 4.0, 0.0, 1.0) if wind_speed is not None else 0.0
    calm_wind = _clamp((3.0 - (wind_speed or 0.0)) / 3.0, 0.0, 1.0) if wind_speed is not None else 0.0
    variable_wind = 1.0 if point.get("wind_variable") else 0.0
    outflow_wind = 1.0 if (
        (wind_gust is not None and wind_gust >= 7.0)
        or (wind_jump >= 3.0 and wind_shift >= 60.0)
    ) else 0.0

    pressure = _to_float(point.get("pressure_hpa"))
    previous_pressure = _to_float(previous.get("pressure_hpa"))
    pressure_delta = (
        float(pressure) - float(previous_pressure)
        if pressure is not None and previous_pressure is not None
        else 0.0
    )
    pressure_rising = _clamp(pressure_delta / 1.5, 0.0, 1.0)
    pressure_falling = _clamp(-pressure_delta / 1.5, 0.0, 1.0)

    cloud_cover = _to_float(point.get("cloud_cover_pct"))
    previous_cloud_cover = _to_float(previous.get("cloud_cover_pct"))
    cloud_growth = (
        _clamp((float(cloud_cover) - float(previous_cloud_cover)) / 35.0, 0.0, 1.0)
        if cloud_cover is not None and previous_cloud_cover is not None
        else 0.0
    )
    return {
        "precip": precip,
        "convective": convective,
        "trend_convective_weak": trend_convective,
        "trend_nosig": 1.0 if point.get("metar_trend_nosig") else 0.0,
        "no_significant_current_weather": no_significant_weather,
        "low_cloud": low_cloud,
        "clear_or_few_scattered": clear,
        "cloud_growth": cloud_growth,
        "temp_dewpoint_spread_c": spread,
        "spread_closed": spread_closed,
        "spread_open": spread_open,
        "dewpoint_delta_c": dewpoint_delta,
        "moistening": moistening,
        "drying": drying,
        "positive_temperature_rate": positive,
        "negative_temperature_rate": negative,
        "flat_temperature_rate": flat,
        "visibility_good": visibility_good,
        "visibility_poor": visibility_poor,
        "visibility_deteriorating": visibility_deteriorating,
        "wind_ventilation": wind_ventilation,
        "calm_wind": calm_wind,
        "variable_wind": variable_wind,
        "outflow_wind": outflow_wind,
        "wind_speed_jump_mps": wind_jump,
        "wind_direction_shift_deg": wind_shift,
        "pressure_delta_hpa": pressure_delta,
        "pressure_rising": pressure_rising,
        "pressure_falling": pressure_falling,
        "after_noon": after_noon,
        "availability": {
            "temperature": temp is not None,
            "dewpoint": dew is not None,
            "cloud": bool(cloud),
            "current_weather": bool(weather),
            "metar_trend": bool(trend_text or trend_weather or trend_cloud or point.get("metar_trend_nosig")),
            "visibility": visibility is not None,
            "wind_speed": wind_speed is not None,
            "wind_direction": wind_dir is not None or bool(point.get("wind_variable")),
            "wind_gust": wind_gust is not None,
            "pressure": pressure is not None,
        },
    }


def _emission_probs(evidence: Mapping[str, Any]) -> dict[str, float]:
    def value(key: str) -> float:
        return float(_to_float(evidence.get(key)) or 0.0)

    precip = value("precip")
    convective = value("convective")
    trend_convective = value("trend_convective_weak")
    low_cloud = value("low_cloud")
    clear = value("clear_or_few_scattered")
    spread_closed = value("spread_closed")
    spread_open = value("spread_open")
    positive = value("positive_temperature_rate")
    negative = value("negative_temperature_rate")
    flat = value("flat_temperature_rate")
    after_noon = value("after_noon")
    scores = {
        "wet_layer_closed": 0.20 + 1.05 * low_cloud + 0.90 * precip + 0.75 * spread_closed + 0.35 * negative - 0.45 * positive + 0.40 * value("visibility_poor") + 0.25 * value("moistening") + 0.15 * value("calm_wind"),
        "opening": 0.35 + 0.65 * positive + 0.40 * clear + 0.35 * spread_open + 0.20 * flat - 0.35 * precip + 0.25 * value("visibility_good") + 0.18 * value("drying") + 0.10 * value("trend_nosig") + 0.12 * value("no_significant_current_weather"),
        "open_boundary_layer": 0.05 + 1.10 * positive + 0.70 * clear + 0.55 * spread_open - 0.65 * low_cloud - 0.75 * precip + 0.30 * value("visibility_good") + 0.20 * value("drying") + 0.15 * value("wind_ventilation") * (1.0 - after_noon) + 0.15 * value("no_significant_current_weather"),
        "plateau": 0.10 + 0.95 * flat + 0.55 * after_noon + 0.20 * clear - 0.30 * convective + 0.25 * value("wind_ventilation") * after_noon + 0.14 * value("variable_wind") + 0.12 * value("trend_nosig"),
        "convective_cutoff": -0.20 + 1.55 * convective + 0.85 * precip + 0.75 * negative + 0.45 * after_noon + 0.55 * trend_convective + 0.65 * value("outflow_wind") + 0.30 * value("visibility_poor") + 0.20 * value("visibility_deteriorating") + 0.25 * value("moistening") + 0.20 * max(value("pressure_rising"), value("pressure_falling")) + 0.20 * value("cloud_growth"),
    }
    return _softmax(scores)


def _sequential_state_filter(
    points: Sequence[Mapping[str, Any]],
    *,
    as_of: Any,
    station_timezone: str,
) -> dict[str, Any]:
    initial_hour = _local_hour(points[0].get("valid_time") if points else as_of, station_timezone)
    posterior = _initial_state_probs(initial_hour)
    trace: list[dict[str, Any]] = []
    previous_temp: float | None = None
    previous_dt: datetime | None = None
    previous_point: Mapping[str, Any] | None = None
    recent_rates: list[float] = []
    observed_max: float | None = None
    for point in points:
        valid_dt = _parse_dt(point.get("valid_time"))
        hour = _local_hour(point.get("valid_time") or as_of, station_timezone)
        temp = _to_float(point.get("temperature_c"))
        row_max = _to_float(point.get("observed_today_max_c"))
        if temp is not None:
            observed_max = max(value for value in (observed_max, temp, row_max) if value is not None)
        elif row_max is not None:
            observed_max = max(value for value in (observed_max, row_max) if value is not None)
        elapsed_hours = 0.5
        if valid_dt is not None and previous_dt is not None:
            elapsed_hours = max(1.0 / 60.0, (valid_dt - previous_dt).total_seconds() / 3600.0)
        rate = 0.0
        if temp is not None and previous_temp is not None:
            raw_delta = float(temp) - float(previous_temp)
            # Whole-degree METAR plateaus are interval observations.  A single
            # equal integer report is neutral, not evidence of exact stasis.
            if abs(raw_delta) >= 0.5:
                rate = _clamp(raw_delta / elapsed_hours, -6.0, 6.0)
                recent_rates.append(rate)
        predicted = _transition_state(posterior, min(1.0, elapsed_hours / 0.5))
        evidence = _emission_evidence(point, previous_point, hour=hour, rate=rate)
        emission = _emission_probs(evidence)
        posterior = dict(
            zip(
                STATE_ORDER,
                _normalize([predicted[state] * emission[state] for state in STATE_ORDER]),
            )
        )
        trace.append(
            {
                "source_event_id": str(point.get("source_event_id") or ""),
                "source_id": str(point.get("source_id") or ""),
                "valid_time": str(point.get("valid_time") or ""),
                "hour_local": _round(hour, 6),
                "temperature_c": temp,
                "observed_max_c": observed_max,
                "elapsed_hours": _round(elapsed_hours, 6),
                "quantized_rate_c_per_hour": _round(rate, 6),
                "evidence_inputs": evidence,
                "emission_probs": {key: _round(value) for key, value in emission.items()},
                "state_probs": {key: _round(value) for key, value in posterior.items()},
            }
        )
        if temp is not None:
            previous_temp = temp
        if valid_dt is not None:
            previous_dt = valid_dt
        previous_point = point
    latest_rate = sum(recent_rates[-3:]) / len(recent_rates[-3:]) if recent_rates else 0.0
    return {
        "schema": STATE_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "state_probs": {key: float(_round(value) or 0.0) for key, value in posterior.items()},
        "state_prob_sum": _round(sum(posterior.values())),
        "dominant_state": max(posterior, key=posterior.get) if posterior else "",
        "point_count": len(points),
        "observed_max_c": _round(observed_max),
        "latest_effective_rate_c_per_hour": _round(latest_rate),
        "trace": trace[-64:],
        "boundary": {
            "distinct_valid_times_update_once": True,
            "integer_plateau_is_interval_observation": True,
            "metar_fields_consumed_once_in_state_emission": [
                "temperature_path",
                "dewpoint_and_trend",
                "cloud_and_cloud_growth",
                "current_weather_and_precipitation",
                "metar_trend_weak_context",
                "visibility_and_deterioration",
                "wind_speed_direction_variability_gust_and_jump",
                "pressure_and_change",
            ],
            "current_observation_separated_from_metar_trend": True,
            "no_display_threshold_selects_a_hard_state": True,
        },
        "parameters_fitted_from_history": False,
    }


def _forecast_peak_window_end_hour(metar_output: Mapping[str, Any]) -> float | None:
    features = metar_output.get("observation_features") if isinstance(metar_output.get("observation_features"), Mapping) else {}
    for key in ("expected_peak_window_end_hour_local", "peak_window_end_hour_local"):
        value = _to_float(features.get(key))
        if value is not None:
            return _clamp(value, 0.0, 23.99)
    rows = metar_output.get("remaining_warming_by_state") if isinstance(metar_output.get("remaining_warming_by_state"), list) else []
    for row in rows:
        if isinstance(row, Mapping):
            value = _to_float(row.get("peak_window_end_hour_local"))
            if value is not None:
                return _clamp(value, 0.0, 23.99)
    return None


def _predicted_peak_time_prior(metar_output: Mapping[str, Any]) -> dict[str, Any]:
    features = metar_output.get("observation_features") if isinstance(metar_output.get("observation_features"), Mapping) else {}
    peak_window = metar_output.get("predicted_peak_window") if isinstance(metar_output.get("predicted_peak_window"), Mapping) else {}
    raw = features.get("expected_peak_hour_distribution") or features.get("peak_hour_distribution")
    items: list[tuple[float, float]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            hour = _to_float(key)
            probability = _to_float(value)
            if hour is not None and probability is not None and probability > 0.0:
                items.append((_clamp(hour, 0.0, 23.99), probability))
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            hour = _to_float(row.get("hour_local") or row.get("hour") or row.get("peak_hour"))
            probability = _to_float(row.get("prob") or row.get("probability") or row.get("weight"))
            if hour is not None and probability is not None and probability > 0.0:
                items.append((_clamp(hour, 0.0, 23.99), probability))
    center = _to_float(
        features.get("expected_peak_hour_local")
        or features.get("peak_hour_local")
        or peak_window.get("center_hour_local")
    )
    start = _to_float(
        features.get("expected_peak_window_start_hour_local")
        or features.get("peak_window_start_hour_local")
        or peak_window.get("start_hour_local")
    )
    end = _to_float(
        features.get("expected_peak_window_end_hour_local")
        or features.get("peak_window_end_hour_local")
        or peak_window.get("end_hour_local")
    )
    if not items and center is not None:
        start = center if start is None else start
        end = center if end is None else end
        slot = math.floor(max(0.0, start) * 2.0) / 2.0
        while slot <= min(23.5, end) + EPS:
            items.append((slot, 1.0 / (1.0 + abs(slot - center))))
            slot += 0.5
    probabilities = _normalize([probability for _, probability in items])
    distribution = [
        {"hour_local": _round(hour, 3), "probability": _round(probability)}
        for (hour, _), probability in zip(items, probabilities)
    ]
    mode = max(distribution, key=lambda row: float(row["probability"] or 0.0))["hour_local"] if distribution else None
    return {
        "schema": "predicted_peak_time_prior_v1",
        "role": "weak_forecast_timing_prior",
        "status": "available" if distribution else "missing",
        "distribution": distribution,
        "mode_hour_local": mode,
        "window_start_hour_local": _round(start),
        "window_end_hour_local": _round(end),
        "source": str(
            features.get("expected_peak_window_source")
            or features.get("expected_peak_hour_source")
            or peak_window.get("source")
            or ""
        ),
        "basis": str(features.get("expected_peak_window_basis") or peak_window.get("basis") or ""),
        "predicted_not_observed": True,
        "cannot_finalize_daily_peak": True,
    }


def _effective_remaining_warming_hours(
    *,
    hour: float,
    observation_horizon_end_hour: float,
    predicted_peak_time_prior: Mapping[str, Any],
) -> float:
    support_hours = _clamp(float(observation_horizon_end_hour) - float(hour), 0.0, 23.5)
    if support_hours <= EPS:
        return 0.0
    distribution = predicted_peak_time_prior.get("distribution")
    expected_to_predicted_peak = 0.0
    probability_sum = 0.0
    if isinstance(distribution, Sequence) and not isinstance(distribution, (str, bytes, bytearray)):
        for raw in distribution:
            if not isinstance(raw, Mapping):
                continue
            peak_hour = _to_float(raw.get("hour_local"))
            probability = _to_float(raw.get("probability"))
            if peak_hour is None or probability is None or probability <= 0.0:
                continue
            probability_sum += float(probability)
            expected_to_predicted_peak += float(probability) * max(0.0, float(peak_hour) - float(hour))
    if probability_sum > EPS:
        expected_to_predicted_peak /= probability_sum
    else:
        window_end = _to_float(predicted_peak_time_prior.get("window_end_hour_local"))
        if window_end is not None:
            expected_to_predicted_peak = 0.65 * max(0.0, float(window_end) - float(hour))
        else:
            expected_to_predicted_peak = 0.25 * support_hours
    # Keep a small late-rebound tail until the local day closes, but do not
    # treat every remaining wall-clock hour as an hour of continued warming.
    late_rebound_tail_hours = 0.08 * support_hours
    return _clamp(expected_to_predicted_peak + late_rebound_tail_hours, 0.0, support_hours)


def _observed_peak_platform(
    points: Sequence[Mapping[str, Any]],
    *,
    station_timezone: str,
    official_final: float | None,
) -> dict[str, Any]:
    eligible = [row for row in points if _to_float(row.get("temperature_c")) is not None]
    if not eligible:
        return {
            "schema": "observed_peak_platform_v1",
            "status": "missing",
            "observed_peak_to_date_c": None,
            "na_reason": "canonical_metar_temperature_points_missing",
        }
    max_c = max(float(_to_float(row.get("temperature_c")) or 0.0) for row in eligible)
    peak_rows = [row for row in eligible if abs(float(_to_float(row.get("temperature_c")) or 0.0) - max_c) <= EPS]

    def local_text(row: Mapping[str, Any]) -> str:
        dt = _parse_dt(row.get("valid_time"))
        if dt is None:
            return str(row.get("valid_time") or "")
        return dt.astimezone(ZoneInfo(str(station_timezone or "Asia/Shanghai"))).isoformat()

    first = peak_rows[0]
    last = peak_rows[-1]

    def local_hour(row: Mapping[str, Any]) -> float | None:
        dt = _parse_dt(row.get("valid_time"))
        if dt is None:
            return None
        local = dt.astimezone(ZoneInfo(str(station_timezone or "Asia/Shanghai")))
        return float(local.hour) + float(local.minute) / 60.0

    peak_hours = [value for value in (local_hour(row) for row in peak_rows) if value is not None]
    pre_dawn_carryover = bool(
        official_final is None
        and peak_hours
        and all(0.0 <= value < 6.0 for value in peak_hours)
    )
    return {
        "schema": "observed_peak_platform_v1",
        "status": (
            "final"
            if official_final is not None
            else "provisional_pre_dawn_carryover_lower_bound"
            if pre_dawn_carryover
            else "provisional"
        ),
        "observed_peak_to_date_c": _round(max_c),
        "observed_peak_first_time_local": local_text(first),
        "observed_peak_last_time_local": local_text(last),
        "observed_peak_report_count": len(peak_rows),
        "source_event_ids": [str(row.get("source_event_id") or "") for row in peak_rows if str(row.get("source_event_id") or "")],
        "source_observation_ids": [str(row.get("source_observation_id") or "") for row in peak_rows if str(row.get("source_observation_id") or "")],
        "complete_day_required_for_final": True,
        "prediction_window_used": False,
        "settlement_calendar_day_lower_bound": True,
        "peak_origin": (
            "previous_solar_day_thermal_carryover_after_midnight"
            if pre_dawn_carryover
            else "current_local_day_observation"
        ),
        "pre_dawn_carryover": pre_dawn_carryover,
        "is_daytime_peak_platform": not pre_dawn_carryover,
        "physical_interpretation": (
            "midnight_temperature_belongs_to_current_settlement_day_but_is_thermal_carryover_not_evidence_that_current_daytime_peak_has_formed"
            if pre_dawn_carryover
            else "provisional_observed_local_day_maximum"
        ),
    }


def _predicted_peak_time_posterior(
    *,
    hour: float,
    observation_horizon_end_hour: float,
    predicted_peak_time_prior: Mapping[str, Any],
    observed_peak_platform: Mapping[str, Any],
    state_filter: Mapping[str, Any],
    peak_already_probability: float,
    explicit_final: bool,
) -> dict[str, Any]:
    already_probability = 1.0 if explicit_final else _clamp(peak_already_probability, 0.0, 1.0)
    support_end = float(observation_horizon_end_hour)
    prior_rows = predicted_peak_time_prior.get("distribution")
    prior_by_hour: dict[float, float] = {}
    if isinstance(prior_rows, Sequence) and not isinstance(prior_rows, (str, bytes, bytearray)):
        for raw in prior_rows:
            if not isinstance(raw, Mapping):
                continue
            slot = _to_float(raw.get("hour_local"))
            probability = _to_float(raw.get("probability"))
            if slot is not None and probability is not None and probability > 0.0:
                normalized_slot = round(math.floor(float(slot) * 2.0 + 0.5) / 2.0, 1)
                prior_by_hour[normalized_slot] = prior_by_hour.get(normalized_slot, 0.0) + float(probability)
    state_probs = state_filter.get("state_probs") if isinstance(state_filter.get("state_probs"), Mapping) else {}
    still_warming_probability = _clamp(
        float(_to_float(state_probs.get("opening")) or 0.0)
        + float(_to_float(state_probs.get("open_boundary_layer")) or 0.0),
        0.0,
        1.0,
    )
    rate = float(_to_float(state_filter.get("latest_effective_rate_c_per_hour")) or 0.0)
    next_slot = math.floor(float(hour) * 2.0 + 1.0 + EPS) / 2.0
    future_raw: list[tuple[float, float]] = []
    slot = next_slot
    support_hours = max(0.5, support_end - float(hour))
    while slot <= support_end + EPS:
        delay = max(0.0, slot - float(hour))
        prior_weight = prior_by_hour.get(round(slot, 1), 0.0)
        late_tail_weight = math.exp(-0.55 * delay)
        momentum_shift = 1.0 + 0.45 * still_warming_probability * delay / support_hours
        if rate < 0.0:
            momentum_shift *= math.exp(-0.12 * min(4.0, abs(rate)) * delay)
        elif rate > 0.0:
            momentum_shift *= 1.0 + 0.10 * min(4.0, rate) * delay / support_hours
        future_raw.append((round(slot, 1), max(EPS, prior_weight + 0.08 * late_tail_weight) * momentum_shift))
        slot += 0.5
    future_probs = _normalize([weight for _, weight in future_raw]) if future_raw else []
    future_mass = max(0.0, 1.0 - already_probability)
    distribution: list[dict[str, Any]] = []
    if already_probability > 0.0:
        already_state = (
            "already_observed_pre_dawn_carryover_lower_bound"
            if observed_peak_platform.get("pre_dawn_carryover")
            else "already_observed_peak_platform"
        )
        distribution.append(
            {
                "time_state": already_state,
                "probability": _round(already_probability),
                "platform_first_time_local": observed_peak_platform.get("observed_peak_first_time_local"),
                "platform_last_time_local": observed_peak_platform.get("observed_peak_last_time_local"),
            }
        )
    for (future_hour, _), probability in zip(future_raw, future_probs):
        distribution.append(
            {
                "time_state": "future_half_hour_slot",
                "hour_local": future_hour,
                "probability": _round(future_mass * probability),
            }
        )
    total = sum(float(row.get("probability") or 0.0) for row in distribution)
    if distribution and abs(total - 1.0) > 1e-9:
        distribution[-1]["probability"] = _round(float(distribution[-1].get("probability") or 0.0) + 1.0 - total)
    mode = max(distribution, key=lambda row: float(row.get("probability") or 0.0)) if distribution else {}
    return {
        "schema": "predicted_peak_time_posterior_v1",
        "status": "available" if distribution else "missing",
        "distribution": distribution,
        "peak_already_probability": _round(already_probability),
        "peak_already_probability_semantics": (
            "probability_no_future_observation_exceeds_pre_dawn_calendar_day_lower_bound_not_probability_daytime_peak_has_formed"
            if observed_peak_platform.get("pre_dawn_carryover")
            else "probability_no_future_observation_exceeds_current_observed_peak_platform"
        ),
        "future_peak_probability": _round(future_mass),
        "mode_state": mode.get("time_state"),
        "mode_hour_local": mode.get("hour_local"),
        "prior_schema": predicted_peak_time_prior.get("schema"),
        "prior_source": predicted_peak_time_prior.get("source"),
        "live_update_source": "sequential_metar_state_filter_all_physical_channels",
        "predicted_not_observed": True,
        "cannot_finalize_daily_peak": True,
        "future_support_end_hour_local": support_end,
    }


def _hard_condition_mass(
    mass: Sequence[float],
    grid: Sequence[float],
    *,
    observed_floor_c: float | None,
) -> list[float]:
    values = [
        float(value) if observed_floor_c is None or temp >= float(observed_floor_c) - EPS else 0.0
        for value, temp in zip(mass, grid)
    ]
    return _normalize(values)


def _temp_in_bucket_interval(temp: float, interval: tuple[float, float, float]) -> bool:
    low, high, _anchor = interval
    return bool(
        (not math.isfinite(low) or float(temp) >= low - EPS)
        and (not math.isfinite(high) or float(temp) < high - EPS)
    )


def _hard_mask_condition_mass(
    mass: Sequence[float],
    grid: Sequence[float],
    *,
    labels: Sequence[str],
    intervals: Mapping[str, tuple[float, float, float]],
    hard_mask: Mapping[str, Any],
) -> list[float]:
    """Condition the continuous density on the settlement-bucket hard mask.

    Applying the mask only after bucket integration would leave forbidden mass
    visible in the continuous SSOT.  This conditions the continuous target
    first, so the continuous and bucket projections are one probability law.
    """

    eligible_indices: list[int] = []
    values: list[float] = []
    for index, (temp, value) in enumerate(zip(grid, mass)):
        eligible = any(
            float(_to_float(hard_mask.get(label)) or 0.0) > 0.0
            and label in intervals
            and _temp_in_bucket_interval(float(temp), intervals[label])
            for label in labels
        )
        if eligible:
            eligible_indices.append(index)
        values.append(max(0.0, float(value)) if eligible else 0.0)
    total = sum(values)
    if total > EPS:
        return [value / total for value in values]
    if not eligible_indices:
        return [0.0 for _ in values]
    # A degenerate input density can miss the only eligible interval on a
    # finite grid.  Project to the eligible point nearest its original mode.
    original_mode = max(range(len(values)), key=lambda index: float(mass[index])) if values else 0
    selected = min(eligible_indices, key=lambda index: abs(index - original_mode))
    out = [0.0 for _ in values]
    out[selected] = 1.0
    return out


def _observation_gate(
    *,
    phase: str,
    hour: float,
    point_count: int,
    information_quality: Mapping[str, Any],
    hard_lock: bool,
) -> dict[str, Any]:
    quality_row = information_quality.get("metar_strong") if isinstance(information_quality.get("metar_strong"), Mapping) else {}
    quality = _clamp(float(_to_float(quality_row.get("quality")) or 0.0), 0.0, 1.0)
    if hard_lock:
        time_gate = 1.0
        coverage_gate = 1.0
        gate = 1.0
        reason = "hard_lock"
    elif str(phase or "").startswith("D1") or point_count <= 0:
        time_gate = 0.0
        coverage_gate = 0.0 if point_count <= 0 else min(1.0, point_count / 4.0)
        gate = 0.0
        reason = "future_event_or_observation_missing"
    else:
        time_gate = _clamp((hour - 7.0) / 7.0, 0.0, 1.0)
        coverage_gate = _clamp(point_count / 4.0, 0.0, 1.0)
        gate = _clamp(time_gate * coverage_gate * quality, 0.0, 1.0)
        reason = "continuous_local_time_times_distinct_observation_coverage_times_contract_quality"
    return {
        "schema": "continuous_observation_information_gate_v1",
        "observation_weight": _round(gate),
        "forecast_climate_weight": _round(1.0 - gate),
        "time_gate": _round(time_gate),
        "coverage_gate": _round(coverage_gate),
        "contract_quality": _round(quality),
        "point_count": int(point_count),
        "hour_local": _round(hour, 6),
        "reason": reason,
        "fitted_all_day_posterior_primary": False,
        "step_function_phase_weight_not_used": True,
        "parameters_fitted_from_history": False,
    }


def _bucket_probabilities(
    labels: Sequence[str],
    mass: Sequence[float],
    grid: Sequence[float],
    intervals: Mapping[str, tuple[float, float, float]],
    hard_mask: Mapping[str, Any],
) -> list[float]:
    out: list[float] = []
    for label in labels:
        interval = intervals.get(label)
        if interval is None:
            out.append(0.0)
            continue
        low, high, _anchor = interval
        probability = sum(
            value
            for temp, value in zip(grid, mass)
            if (not math.isfinite(low) or temp >= low - EPS)
            and (not math.isfinite(high) or temp < high - EPS)
        )
        if _to_float(hard_mask.get(label)) is not None and float(hard_mask.get(label)) <= 0.0:
            probability = 0.0
        out.append(probability)
    total = sum(out)
    return [value / total for value in out] if total > EPS else [0.0 for _ in out]


def _quantile(grid: Sequence[float], mass: Sequence[float], q: float) -> float | None:
    running = 0.0
    for temp, value in zip(grid, mass):
        running += max(0.0, float(value))
        if running + EPS >= float(q):
            return float(temp)
    return float(grid[-1]) if grid else None


def _distribution_map(labels: Sequence[str], values: Sequence[float]) -> dict[str, float]:
    return {label: float(_round(value) or 0.0) for label, value in zip(labels, values)}


def build_sequential_remaining_warming_posterior(
    *,
    buckets: Sequence[Any],
    prior_distribution: Mapping[str, Any] | None,
    forecast_output: Mapping[str, Any],
    metar_output: Mapping[str, Any],
    source_observations: Iterable[Any] | None,
    hard_mask: Mapping[str, Any] | None,
    event_date: str,
    as_of: Any,
    station_timezone: str = "Asia/Shanghai",
    rounding_rule: str = "rounded_degrees",
    phase: str = "",
    phase_reason: str = "",
    hard_mask_reason: str = "",
    beta_weak: Any = 0.0,
    beta_strong: Any = 0.0,
    information_quality: Mapping[str, Any] | None = None,
    prior_continuous_distribution: Mapping[str, Any] | None = None,
    posterior_quantile_levels: Sequence[Any] | None = None,
    posterior_run_id: str = "",
    fact_snapshot_id: str = "",
    source_event_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    labels = [str(item.get("bucket") if isinstance(item, Mapping) else item).strip() for item in buckets or []]
    labels = [label for label in labels if label]
    prior_map = dict(prior_distribution or {})
    mask = {label: float(_to_float((hard_mask or {}).get(label)) or 0.0) if label in (hard_mask or {}) else 1.0 for label in labels}
    constraints = metar_output.get("hard_constraints") if isinstance(metar_output.get("hard_constraints"), Mapping) else {}
    observed_floor = _to_float(constraints.get("observed_today_max_c"))
    current_temp = _to_float(constraints.get("current_temperature_c"))
    if observed_floor is None:
        observed_floor = current_temp
    hard_lock = bool(constraints.get("hard_lock"))
    official_final = _to_float(constraints.get("official_final_result"))
    points, canonical_trace = _canonical_metar_points(
        source_observations or [],
        as_of=as_of,
        event_date=str(event_date or ""),
        station_timezone=station_timezone,
        metar_output=metar_output,
    )
    state_filter = _sequential_state_filter(points, as_of=as_of, station_timezone=station_timezone)
    canonical_observed_max = _to_float(state_filter.get("observed_max_c"))
    if canonical_observed_max is not None:
        observed_floor = max(float(observed_floor), canonical_observed_max) if observed_floor is not None else canonical_observed_max
    observed_peak_platform = _observed_peak_platform(
        points,
        station_timezone=station_timezone,
        official_final=official_final,
    )
    predicted_peak_time_prior = _predicted_peak_time_prior(metar_output)
    grid, intervals = _grid(
        labels,
        rounding_rule=rounding_rule,
        observed_floor_c=observed_floor,
        forecast_output=forecast_output,
    )
    prior_mass, prior_mode = _continuous_prior_mass(
        labels=labels,
        prior_distribution=prior_map,
        prior_continuous_distribution=prior_continuous_distribution,
        grid=grid,
        intervals=intervals,
    )
    forecast_likelihood, forecast_components, forecast_mode = _forecast_error_likelihood(
        labels=labels,
        forecast_output=forecast_output,
        grid=grid,
        intervals=intervals,
    )
    weak_power = max(0.0, float(_to_float(beta_weak) or 0.0))
    if forecast_mode == "forecast_missing_neutral_likelihood":
        pre_observation = list(prior_mass)
    else:
        pre_observation = _normalize(
            [
                max(EPS, prior_value) * math.exp(weak_power * math.log(max(EPS, likelihood_value)))
                for prior_value, likelihood_value in zip(prior_mass, forecast_likelihood)
            ]
        )
    hour = _local_hour(as_of, station_timezone)
    effective_warming_hours = _effective_remaining_warming_hours(
        hour=hour,
        observation_horizon_end_hour=LOCAL_DAY_LAST_HALF_HOUR,
        predicted_peak_time_prior=predicted_peak_time_prior,
    )
    remaining_mass, remaining_rows, remaining_warming_source = _physical_strong_remaining_mass(
        labels=labels,
        grid=grid,
        intervals=intervals,
        metar_output=metar_output,
        pre_observation=pre_observation,
        observed_floor=observed_floor,
    )
    termination_risk = (
        _as_mapping(metar_output.get("termination_risk"))
        if isinstance(metar_output.get("termination_risk"), Mapping)
        else {}
    )
    weak_model_ready = bool(
        forecast_output.get("ok") is True
        and forecast_output.get("production_model_ready") is not False
        and forecast_mode == "approved_joint_forecast_weak_continuous_likelihood"
    )
    strong_model_ready = bool(metar_output.get("ok") is True)
    production_model_ready = bool(
        (weak_model_ready and strong_model_ready)
        or (hard_lock and official_final is not None)
    )
    if production_model_ready:
        production_block_reason = ""
    elif not weak_model_ready:
        production_block_reason = str(
            next(iter(forecast_output.get("block_reasons") or []), "approved_joint_weak_model_unavailable")
        )
    else:
        production_block_reason = "physical_strong_likelihood_contract_unavailable"
    if hard_lock and official_final is not None:
        peak_already_probability = 1.0
    elif observed_floor is not None and grid and remaining_mass:
        peak_already_probability = _clamp(
            sum(
                probability
                for temperature, probability in zip(grid, remaining_mass)
                if float(temperature) <= float(observed_floor) + GRID_STEP_C / 2.0
            ),
            0.0,
            1.0,
        )
    else:
        peak_already_probability = 0.0
    predicted_peak_time_posterior = _predicted_peak_time_posterior(
        hour=hour,
        observation_horizon_end_hour=LOCAL_DAY_LAST_HALF_HOUR,
        predicted_peak_time_prior=predicted_peak_time_prior,
        observed_peak_platform=observed_peak_platform,
        state_filter=state_filter,
        peak_already_probability=peak_already_probability,
        explicit_final=bool(hard_lock and official_final is not None),
    )
    quality = information_quality if isinstance(information_quality, Mapping) else {}
    gate = _observation_gate(
        phase=phase,
        hour=hour,
        point_count=int(state_filter.get("point_count") or 0),
        information_quality=quality,
        hard_lock=hard_lock,
    )
    observation_weight = float(_to_float(gate.get("observation_weight")) or 0.0)
    if hard_lock and official_final is not None:
        strong_stage_mass = [0.0 for _ in grid]
        if grid:
            strong_stage_mass[min(range(len(grid)), key=lambda index: abs(grid[index] - official_final))] = 1.0
        final_mass = list(strong_stage_mass)
    elif hard_lock and observed_floor is not None:
        strong_stage_mass = [0.0 for _ in grid]
        if grid:
            strong_stage_mass[min(range(len(grid)), key=lambda index: abs(grid[index] - observed_floor))] = 1.0
        final_mass = list(strong_stage_mass)
    else:
        strong_stage_mass = _normalize(
            [
                (1.0 - observation_weight) * forecast_value + observation_weight * remaining_value
                for forecast_value, remaining_value in zip(pre_observation, remaining_mass)
            ]
        )
        final_mass = _hard_condition_mass(strong_stage_mass, grid, observed_floor_c=observed_floor)

    final_mass = _hard_mask_condition_mass(
        final_mass,
        grid,
        labels=labels,
        intervals=intervals,
        hard_mask=mask,
    )

    bucket_probs = _bucket_probabilities(labels, final_mass, grid, intervals, mask)
    posterior_map = _distribution_map(labels, bucket_probs)
    prior_bucket = _bucket_probabilities(labels, prior_mass, grid, intervals, {label: 1.0 for label in labels})
    forecast_bucket = _bucket_probabilities(labels, forecast_likelihood, grid, intervals, {label: 1.0 for label in labels})
    remaining_bucket = _bucket_probabilities(labels, remaining_mass, grid, intervals, mask)
    forecast_stage_bucket = _bucket_probabilities(
        labels,
        pre_observation,
        grid,
        intervals,
        {label: 1.0 for label in labels},
    )
    strong_stage_bucket = _bucket_probabilities(
        labels,
        strong_stage_mass,
        grid,
        intervals,
        {label: 1.0 for label in labels},
    )

    running = 0.0
    cdf_rows: list[dict[str, Any]] = []
    grid_rows: list[dict[str, Any]] = []
    for temp, mass_value in zip(grid, final_mass):
        running = min(1.0, running + max(0.0, mass_value))
        row = {
            "temp_c": _round(temp, 6),
            "mass": _round(mass_value),
            "pdf": _round(mass_value / GRID_STEP_C),
            "cdf": _round(running),
        }
        grid_rows.append(row)
        cdf_rows.append({"temp_c": row["temp_c"], "cdf": row["cdf"]})
    levels: list[float] = []
    for raw in posterior_quantile_levels or DEFAULT_QUANTILES:
        value = _to_float(raw)
        if value is not None and 0.0 <= value <= 1.0:
            levels.append(float(value))
    levels = sorted(set(levels)) or list(DEFAULT_QUANTILES)
    quantile_grid: list[dict[str, Any]] = []
    quantiles: dict[str, float | None] = {}
    for level in levels:
        value = _quantile(grid, final_mass, level)
        quantiles[f"{level:g}"] = _round(value, 6)
        quantiles[f"p{int(round(level * 100)):02d}"] = _round(value, 6)
        quantile_grid.append({"q": level, "value_c": _round(value, 6), "temp_c": _round(value, 6)})

    cdf_by_bucket: dict[str, float] = {}
    cumulative = 0.0
    sorted_labels = sorted(
        labels,
        key=lambda label: intervals.get(label, (float("inf"), float("inf"), 0.0))[0],
    )
    for label in sorted_labels:
        cumulative += posterior_map.get(label, 0.0)
        cdf_by_bucket[label] = float(_round(min(1.0, cumulative)) or 0.0)
    weak_map = forecast_output.get("forecast_weak_likelihood_map") if isinstance(forecast_output.get("forecast_weak_likelihood_map"), Mapping) else {}
    strong_map = metar_output.get("metar_strong_likelihood_map") if isinstance(metar_output.get("metar_strong_likelihood_map"), Mapping) else {}
    posterior_rows: list[dict[str, Any]] = []
    log_trace: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        interval = intervals.get(label)
        low, high, anchor = interval if interval is not None else (float("nan"), float("nan"), float("nan"))
        probability = bucket_probs[index]
        row = {
            "bucket": label,
            "posterior": float(_round(probability) or 0.0),
            "prior": float(_round(prior_bucket[index]) or 0.0),
            "prior_stage_probability": float(_round(prior_bucket[index]) or 0.0),
            "forecast_weak_stage_probability": float(_round(forecast_stage_bucket[index]) or 0.0),
            "metar_strong_stage_probability": float(_round(strong_stage_bucket[index]) or 0.0),
            "posterior_stage_probability": float(_round(probability) or 0.0),
            "forecast_weak_likelihood": float(_round(_to_float(weak_map.get(label)) or forecast_bucket[index]) or 0.0),
            "metar_strong_likelihood": float(_round(remaining_bucket[index]) or 0.0),
            "physical_strong_distribution": float(_round(remaining_bucket[index]) or 0.0),
            "metar_strong_likelihood_raw": float(_round(_to_float(strong_map.get(label))) or 0.0),
            "hard_mask": 1.0 if float(mask.get(label, 1.0)) > 0.0 else 0.0,
            "log_score": _round(math.log(max(EPS, probability))) if probability > 0.0 else None,
            "cdf": cdf_by_bucket.get(label),
            "bucket_lower_bound_c": _round(low) if math.isfinite(low) else None,
            "bucket_upper_bound_c": _round(high) if math.isfinite(high) else None,
            "bucket_representative_c": _round(anchor) if math.isfinite(anchor) else None,
            "bucket_open_lower": bool(not math.isfinite(low)),
            "bucket_open_upper": bool(not math.isfinite(high)),
        }
        posterior_rows.append(row)
        log_trace.append(
            {
                "bucket": label,
                "prior_log": _round(math.log(max(EPS, prior_bucket[index]))) if prior_bucket[index] > 0.0 else None,
                "forecast_weak_log": _round(math.log(max(EPS, forecast_bucket[index]))) if forecast_bucket[index] > 0.0 else None,
                "metar_strong_log": _round(math.log(max(EPS, remaining_bucket[index]))) if remaining_bucket[index] > 0.0 else None,
                "hard_mask_log": 0.0 if row["hard_mask"] > 0 else None,
                "log_score": row["log_score"],
                "masked": row["hard_mask"] <= 0,
                "composition_role": "continuous_density_integrated_to_bucket_not_bucket_log_product",
                "component_logs_are_diagnostics_not_additive_formula_terms": True,
            }
        )
    violation_count = sum(
        1
        for label, probability in posterior_map.items()
        if probability > EPS and float(mask.get(label, 1.0)) <= 0.0
    )
    continuous = {
        "schema": CONTINUOUS_SCHEMA_VERSION,
        "source": SCHEMA_VERSION,
        "target_variable": "settlement_daily_max_c",
        "grid_step_c": GRID_STEP_C,
        "grid": grid_rows,
        "cdf_values": cdf_rows,
        "cdf_by_bucket": cdf_by_bucket,
        "quantiles_c": quantiles,
        "quantile_grid": quantile_grid,
        "bucket_probabilities": [
            {"bucket": label, "outcome": label, "probability": posterior_map[label]}
            for label in labels
        ],
        "quantile_method": "continuous_grid_cdf",
        "prior_mode": prior_mode,
        "forecast_mode": forecast_mode,
    }
    return {
        "schema": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "ok": bool(labels) and bool(grid) and violation_count == 0 and production_model_ready,
        "read_only": True,
        "production_entry_enabled": False,
        "activation_mode": "live_no_shadow",
        "guards": _guards(),
        "formula": "hard_condition((1-lambda_obs)*normalize(prior*joint_weak^beta_weak)+lambda_obs*physical_strong_X_distribution)",
        "production_model_version": "joint_weak_physical_strong_composer_v1",
        "production_model_ready": production_model_ready,
        "production_block_reason": production_block_reason,
        "remaining_warming_source": remaining_warming_source,
        "v31_multimodel_posterior": {
            "state": "retired_not_consumed",
            "state_reason": "unique_joint_weak_and_physical_strong_composer_replaces_v31_probability_model",
            "applied_to_metar_strong_likelihood": False,
        },
        "target_variable": "settlement_daily_max_c",
        "event_date": str(event_date or ""),
        "as_of": str(as_of or ""),
        "phase": str(phase or "phase_unknown"),
        "phase_reason": str(phase_reason or "phase_reason_missing"),
        "beta_weak": weak_power,
        "beta_strong": max(0.0, float(_to_float(beta_strong) or 0.0)),
        "beta_role": "joint_weak_likelihood_tempering;strong_information_enters_once_through_lambda_obs",
        "hard_mask_reason": str(hard_mask_reason or constraints.get("hard_mask_reason") or "none"),
        "posterior_run_id": str(posterior_run_id or ""),
        "fact_snapshot_id": str(fact_snapshot_id or ""),
        "source_event_ids": [str(item) for item in source_event_ids or [] if str(item)],
        "posterior_temperature_distribution": posterior_rows,
        "posterior_temperature_distribution_map": posterior_map,
        "bayes_stage_distributions": {
            "schema": "bayes_stage_distributions_v1",
            "source": "joint_weak_physical_strong_composer_v1",
            "frontend_recompute_allowed": False,
            "prior": _distribution_map(labels, prior_bucket),
            "forecast_weak": _distribution_map(labels, forecast_stage_bucket),
            "metar_strong": _distribution_map(labels, strong_stage_bucket),
            "posterior_after_hard_condition": posterior_map,
        },
        "posterior_continuous_distribution": continuous,
        "posterior_bucket_cdf": cdf_by_bucket,
        "posterior_quantiles_c": quantiles,
        "posterior_quantile_grid": quantile_grid,
        "log_score_trace": log_trace,
        "sequential_state_filter": state_filter,
        "canonical_metar_snapshot": {
            "schema": "sequential_canonical_metar_snapshot_v1",
            "points": points,
            "observed_peak_platform": observed_peak_platform,
            **canonical_trace,
        },
        "predicted_peak_time_prior": predicted_peak_time_prior,
        "predicted_peak_time_posterior": predicted_peak_time_posterior,
        "observed_peak_platform": observed_peak_platform,
        "remaining_warming_mixture": {
            "schema": "physical_strong_remaining_daily_high_mixture_v1",
            "observed_max_c": _round(observed_floor),
            "forecast_peak_window_end_hour_local": _round(_forecast_peak_window_end_hour(metar_output)),
            "observation_horizon_end_hour_local": LOCAL_DAY_LAST_HALF_HOUR,
            "effective_remaining_warming_hours": _round(effective_warming_hours),
            "forecast_peak_window_is_weak_context_not_support_cutoff": True,
            "state_components": remaining_rows,
            "termination_risk": termination_risk,
            "peak_already_probability": _round(peak_already_probability),
            "zero_inflated": False,
            "positive_support_only": True,
            "parameters_fitted_from_history": False,
            "operational_baseline": True,
        },
        "observation_information_gate": gate,
        "forecast_error_components": forecast_components,
        "hard_constraints": {
            "hard_mask": {label: 1.0 if float(mask.get(label, 1.0)) > 0.0 else 0.0 for label in labels},
            "masked_zero_buckets": [label for label in labels if float(mask.get(label, 1.0)) <= 0.0],
            "hard_constraint_violation_count": violation_count,
            "observed_today_max_c": _round(observed_floor),
            "current_temperature_c": _round(current_temp),
            "hard_lock": hard_lock,
            "official_final_result": _round(official_final),
            "hard_mask_reason": str(hard_mask_reason or constraints.get("hard_mask_reason") or "none"),
            "hard_mask_applied_after_continuous_composition": True,
        },
        "layer_contributions": {
            "prior": {
                "distribution": _distribution_map(labels, prior_bucket),
                "continuous_mode": prior_mode,
                "role": "root_prior",
                "boundary": "historical_climate_date_site_terrain_weather_type_only",
            },
            "forecast_weak_likelihood": {
                "distribution": _distribution_map(labels, forecast_bucket),
                "role": "approved_joint_forecast_weak_likelihood_on_continuous_X_grid",
                "components": forecast_components,
                "mode": forecast_mode,
            },
            "metar_strong_likelihood": {
                "distribution": _distribution_map(labels, remaining_bucket),
                "role": "physical_remaining_daily_high_distribution_from_observation_state_mixture",
                "legacy_static_metar_distribution_used": False,
                "termination_risk": termination_risk,
            },
            "sequential_state_bayes": {
                "state_probs": state_filter.get("state_probs"),
                "dominant_state": state_filter.get("dominant_state"),
                "observation_information_gate": gate,
            },
            "phase_weight": {
                "beta_weak": weak_power,
                "beta_strong": max(0.0, float(_to_float(beta_strong) or 0.0)),
                "strong_beta_used_as_bucket_exponent": False,
                "continuous_observation_weight": gate.get("observation_weight"),
            },
            "hard_mask": {"distribution": {label: float(mask.get(label, 1.0)) for label in labels}},
        },
        "normalization": {
            "posterior_sum": _round(sum(bucket_probs)),
            "continuous_mass_sum": _round(sum(final_mass)),
            "prior_mass_sum": _round(sum(prior_mass)),
            "forecast_error_likelihood_sum": _round(sum(forecast_likelihood)),
            "remaining_warming_mass_sum": _round(sum(remaining_mass)),
            "state_prob_sum": state_filter.get("state_prob_sum"),
        },
        "boundary": {
            "live_primary_composer": True,
            "live_no_shadow": True,
            "shadow_posterior_emitted": False,
            "legacy_bucket_product_used": False,
            "continuous_before_bucket_integration": True,
            "one_joint_weak_model_forms_forecast_likelihood": True,
            "metar_valid_times_update_state_once": True,
            "strong_physical_evidence_consumed_once": True,
            "weather_direction_not_reused_as_beta": True,
            "market_prices_excluded": True,
            "legacy_v31_probability_model_used": False,
        },
        "na_reasons": [],
        "next_boundary": "posterior_ssot_fact_writer_live_primary",
    }


__all__ = [
    "SCHEMA_VERSION",
    "CONTINUOUS_SCHEMA_VERSION",
    "POLICY_VERSION",
    "STATE_ORDER",
    "build_sequential_remaining_warming_posterior",
]
