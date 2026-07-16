"""METAR strong likelihood runtime for hierarchical temperature posterior.

This read-only module folds observed temperature, observed max, cloud/visibility,
dewpoint, wind/pressure, precipitation, convection, and temperature momentum
into one observation likelihood. It avoids treating physical evidence and
temperature trajectory as independent same-level likelihood channels.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from source_observation_runtime import (
    PRIMARY_METAR_SOURCE_ID,
    REFERENCE_METAR_SOURCE_IDS,
    SOURCE_CLASSES,
    SourceObservation,
    source_defaults,
)
from zuuu_daily_high_candidate_runtime import build_zuuu_daily_high_candidate


SCHEMA_VERSION = "metar_strong_likelihood_runtime_v1"
RUNTIME_POLICY_VERSION = "metar_strong_likelihood_policy_v16_local_day_source_filter"
CONTRACT_SCHEMA_VERSION = "metar_strong_likelihood_contract_v1"
TERMINATION_RISK_SCHEMA_VERSION = "strong_likelihood_termination_risk_v1"
TERMINATION_RISK_MATERIAL_WARMING_C = 0.5
EPS = 1e-12
DEFAULT_STATION_TIMEZONE = "Asia/Shanghai"
METAR_SOURCE_IDS_IN_PRIORITY_ORDER = (PRIMARY_METAR_SOURCE_ID, *REFERENCE_METAR_SOURCE_IDS)
STATE_ORDER = (
    "S0_wet_layer_closed",
    "S1_opening",
    "S2_open_boundary_layer",
    "S3_convective_cutoff",
)
FINAL_OBSERVATION_FLAG_FIELDS = (
    "hard_source_final_daily_max",
    "hard_source_settlement_final",
    "observed_today_max_final",
    "observed_today_max_is_final",
    "observed_today_max_is_official",
    "observed_today_max_trusted_final",
    "temperature_observation_final_daily_max",
    "no_more_warming_possible",
    "final_bucket_lock",
    "final_daily_max_bucket_lock",
)
TEMPERATURE_MOMENTUM_WINDOWS = (
    "24h",
    "12h",
    "6h",
    "330m",
    "5h",
    "270m",
    "4h",
    "210m",
    "3h",
    "150m",
    "2h",
    "90m",
    "1h",
    "30m",
    "15m",
    "10m",
    "5m",
    "3m",
    "2m",
    "1m",
)
TEMPERATURE_MOMENTUM_CONFIRM_WINDOWS = (
    "30m",
    "1h",
    "90m",
    "2h",
    "150m",
    "3h",
    "210m",
    "4h",
    "270m",
    "5h",
    "330m",
    "6h",
)
TEMPERATURE_MOMENTUM_BACKGROUND_CONFIRM_WINDOWS = (
    "90m",
    "2h",
    "150m",
    "3h",
    "210m",
    "4h",
    "270m",
    "5h",
    "330m",
    "6h",
)
TEMPERATURE_MOMENTUM_RECENT_CONFIRM_WINDOWS = ("30m",)
TEMPERATURE_MOMENTUM_RECENT_WINDOWS = ("1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h", "90m")
TEMPERATURE_MOMENTUM_SHORT_WINDOWS = ("1m", "2m", "3m", "5m", "10m", "15m")
TEMPERATURE_MOMENTUM_RATE_ABS_MAX = 6.0
TEMPERATURE_MOMENTUM_RECENT_CONFIRM_LEAD_HOURS = 1.5
TEMPERATURE_MOMENTUM_RECENT_CONFIRM_MIN_LOCAL_HOUR = 12.0
PRE_PEAK_INTEGER_PLATEAU_MIN_REMAINING_HOURS = 1.5
METAR_INTEGER_QUANTIZATION_NOISE_C = 0.5
METAR_ADJACENT_STEP_NOISE_C = 1.0
METAR_PLATFORM_WITHIN_ROLLING_MAX_C = 1.0
METAR_PLATFORM_END_DROP_C = 2.0
METAR_PLATFORM_END_MIN_CONSECUTIVE_REPORTS = 2
ROLLING_TEMPERATURE_OBSERVATION_WINDOW = 5
RECOIL_STATE_LABELS = {
    "pre_dawn_carryover": "跨午夜热量延续",
    "platform_noise": "平台波动",
    "pullback_watch": "待确认回落",
    "confirmed_cooling_cutoff": "真实降温/截断",
    "final_lock": "硬源终局锁定",
    "not_applicable": "不适用",
    "unknown": "未知",
}
RECOIL_ACTIONS = {
    "pre_dawn_carryover": "retain_calendar_day_floor_do_not_infer_daytime_peak_recoil",
    "platform_noise": "protect_high_tail_expand_uncertainty_slight_residual_warming",
    "pullback_watch": "lower_momentum_confidence_do_not_reverse",
    "confirmed_cooling_cutoff": "disable_recoil_compress_high_tail",
    "final_lock": "disable_recoil_final_bucket_lock",
    "not_applicable": "no_recoil_without_hard_metar_path",
    "unknown": "wait_for_more_observations",
}
TEMPERATURE_MOMENTUM_WEIGHTS = {
    "1m": 0.04,
    "2m": 0.04,
    "3m": 0.05,
    "5m": 0.06,
    "10m": 0.07,
    "15m": 0.08,
    "30m": 0.16,
    "1h": 0.16,
    "90m": 0.12,
    "2h": 0.10,
    "150m": 0.08,
    "3h": 0.06,
    "210m": 0.05,
    "4h": 0.045,
    "270m": 0.04,
    "5h": 0.035,
    "330m": 0.03,
    "6h": 0.03,
    "12h": 0.01,
    "24h": 0.01,
}


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "restarts_launchd": False,
        "historical_backfill": False,
        "changes_parameters": False,
    }


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "ok", "present"}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _round_float(value: float | None, ndigits: int = 12) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


def _round_mapping(values: Mapping[str, float | None], ndigits: int = 12) -> dict[str, float | None]:
    return {str(key): _round_float(value, ndigits=ndigits) for key, value in values.items()}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _normalize(values: Sequence[float]) -> list[float]:
    cleaned = [max(0.0, float(v)) for v in values]
    total = sum(cleaned)
    if total <= EPS:
        return [1.0 / len(cleaned) for _ in cleaned] if cleaned else []
    return [v / total for v in cleaned]


def _softmax(scores: Mapping[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    max_score = max(float(v) for v in scores.values())
    exp_values = {key: math.exp(float(value) - max_score) for key, value in scores.items()}
    total = sum(exp_values.values())
    if total <= EPS:
        return {key: 1.0 / len(exp_values) for key in exp_values}
    return {key: value / total for key, value in exp_values.items()}


def _coerce_source_row(row: Any) -> dict[str, Any]:
    if isinstance(row, SourceObservation):
        return row.as_dict(include_payload=True)
    if isinstance(row, Mapping):
        return dict(row)
    return {}


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _first_text(row: Mapping[str, Any], payload: Mapping[str, Any], *fields: str) -> str:
    for field in fields:
        value = str(row.get(field) or "").strip()
        if value:
            return value
        value = str(payload.get(field) or "").strip()
        if value:
            return value
    return ""


def _structured_text(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    *fields: str,
    raw_fallback_fields: Sequence[str] = (),
) -> str:
    """Prefer an explicitly parsed field, including an explicit empty value.

    An empty parsed weather field means no current weather. Falling through to
    raw METAR text would incorrectly promote BECMG/TEMPO forecast tokens to a
    current hard observation.
    """
    for field in fields:
        if field in row:
            return str(row.get(field) or "").strip()
        if field in payload:
            return str(payload.get(field) or "").strip()
    return _first_text(row, payload, *raw_fallback_fields)


def _first_value_present(row: Mapping[str, Any], payload: Mapping[str, Any], *fields: str) -> Any:
    for field in fields:
        if field in row:
            return row.get(field)
        if field in payload:
            return payload.get(field)
    return None


def _first_float(row: Mapping[str, Any], payload: Mapping[str, Any], *fields: str) -> float | None:
    for field in fields:
        parsed = _to_float(row.get(field))
        if parsed is not None:
            return parsed
        parsed = _to_float(payload.get(field))
        if parsed is not None:
            return parsed
    return None


def _parse_time_sort_value(value: Any, fallback: float) -> float:
    text = str(value or "").strip()
    if not text:
        return float(fallback)
    try:
        parsed_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        return datetime.fromisoformat(parsed_text).timestamp()
    except Exception:
        return float(fallback)


def _temperature_point_from_row(row: Mapping[str, Any], payload: Mapping[str, Any], *, index: int) -> dict[str, Any] | None:
    temperature_c = _first_float(
        row,
        payload,
        "temperature_c",
        "temp_c",
        "metar_temperature_c",
        "current_temp_c",
        "observed_temp_c",
    )
    if temperature_c is None:
        return None
    valid_time = _first_text(
        row,
        payload,
        "valid_time",
        "valid_time_utc",
        "observation_time",
        "observed_at",
        "time",
        "timestamp",
    )
    valid_time_local = _first_text(row, payload, "valid_time_local", "observed_at_local", "local_time")
    return {
        "temperature_c": float(temperature_c),
        "dewpoint_c": _first_float(row, payload, "dewpoint_c", "dewPt", "temperatureDewPoint"),
        "observed_today_max_c": _first_float(
            row,
            payload,
            "observed_today_max_c",
            "today_max_c",
            "zuuu_metar_raw_amsc_today_max_c",
            "zuuu_metar_raw_awc_today_max_c",
        ),
        "visibility_m": _first_float(row, payload, "visibility_m", "visibility", "visibilityMeters"),
        "cloud_cover_pct": _first_float(row, payload, "cloud_cover_pct", "cloud_pct", "cloudCover"),
        "low_cloud_cover_pct": _first_float(row, payload, "low_cloud_cover_pct", "low_cloud_pct"),
        "wind_speed_mps": _first_float(row, payload, "wind_speed_mps", "wind_speed", "windSpeed"),
        "wind_dir_degrees": _first_float(
            row,
            payload,
            "wind_dir_degrees",
            "wind_dir_deg",
            "wind_direction_degrees",
            "wind_direction",
            "wind_dir",
        ),
        "wind_gust_mps": _first_float(row, payload, "wind_gust_mps", "gust_mps", "wind_gust", "windGust"),
        "wind_variable": bool(_first_value_present(row, payload, "wind_variable")),
        "wind_variable_from_deg": _first_float(row, payload, "wind_variable_from_deg"),
        "wind_variable_to_deg": _first_float(row, payload, "wind_variable_to_deg"),
        "pressure_hpa": _first_float(row, payload, "pressure_hpa", "altimeter_hpa", "pressure"),
        "weather_text": _structured_text(
            row,
            payload,
            "weather_text",
            "weather",
            "wx",
            "metar_weather",
            raw_fallback_fields=("raw_text", "raw_ref"),
        ),
        "metar_current_no_significant_weather": bool(
            _first_value_present(row, payload, "metar_current_no_significant_weather")
        ),
        "cloud_text": _structured_text(
            row,
            payload,
            "cloud_text",
            "clouds",
            raw_fallback_fields=("raw_text", "raw_ref"),
        ),
        "metar_trend_text": _structured_text(row, payload, "metar_trend_text"),
        "metar_trend_cloud_text": _structured_text(row, payload, "metar_trend_cloud_text"),
        "metar_trend_weather_text": _structured_text(row, payload, "metar_trend_weather_text"),
        "metar_trend_nosig": bool(_first_value_present(row, payload, "metar_trend_nosig")),
        "valid_time": valid_time,
        "valid_time_local": valid_time_local,
        "source_id": _first_text(row, payload, "source_id", "source"),
        "source_observation_id": _first_text(row, payload, "source_observation_id"),
        "_sort_value": _parse_time_sort_value(valid_time or valid_time_local, float(index)),
    }


def _source_class(row: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    source_class = _first_text(row, payload, "source_class")
    if source_class:
        return source_class
    source_id = _first_text(row, payload, "source_id", "source")
    return str(source_defaults(source_id).get("source_class") or SOURCE_CLASSES["unknown"])


def _local_date_from_datetime_text(value: Any, *, station_timezone: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        tz = ZoneInfo(str(station_timezone or DEFAULT_STATION_TIMEZONE))
        parsed_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(parsed_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz).date().isoformat()
    except Exception:
        return ""


def _merge_observation_inputs(
    observation_inputs: Mapping[str, Any] | None,
    source_observations: Iterable[Any] | None,
    *,
    event_date: str = "",
    station_timezone: str = DEFAULT_STATION_TIMEZONE,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base_inputs = dict(observation_inputs or {})
    merged = dict(base_inputs)
    source_trace: list[dict[str, Any]] = []
    hard_temperature_points: list[dict[str, Any]] = []
    for index, raw_row in enumerate(source_observations or []):
        row = _coerce_source_row(raw_row)
        payload = _payload(row)
        source_id = _first_text(row, payload, "source_id", "source")
        source_event_id = _first_text(row, payload, "source_event_id")
        source_observation_id = _first_text(row, payload, "source_observation_id")
        source_class = _source_class(row, payload)
        trace_row = {
            "source_event_id": source_event_id,
            "source_observation_id": source_observation_id,
            "source_id": source_id,
            "source_class": source_class,
            "report_type": _first_text(row, payload, "report_type").upper(),
            "event_date": _first_text(row, payload, "event_date", "target_event_date")[:10],
            "valid_time": _first_text(row, payload, "valid_time", "valid_time_utc", "observation_time", "observed_at"),
            "fetched_at": _first_text(row, payload, "fetched_at", "response_completed_at", "parsed_at"),
            "included": False,
            "exclusion_reason": "",
        }
        expected_event_date = str(event_date or "")[:10]
        row_event_date = str(trace_row["event_date"] or "")[:10]
        if not row_event_date:
            row_event_date = _local_date_from_datetime_text(
                trace_row["valid_time"],
                station_timezone=station_timezone,
            )
        if expected_event_date and row_event_date and row_event_date != expected_event_date:
            trace_row["exclusion_reason"] = "event_date_mismatch"
            trace_row["derived_local_event_date"] = row_event_date
            source_trace.append(trace_row)
            continue
        if source_class != SOURCE_CLASSES["hard_observation"]:
            trace_row["exclusion_reason"] = "source_class_not_hard_observation"
            source_trace.append(trace_row)
            continue
        trace_row["included"] = True
        point = _temperature_point_from_row(row, payload, index=index)
        if point:
            hard_temperature_points.append(point)
        for target, fields in {
            "current_temp_c": ("temperature_c", "temp_c", "metar_temperature_c", "current_temp_c"),
            "dewpoint_c": ("dewpoint_c", "dewPt", "temperatureDewPoint"),
            "observed_today_max_c": (
                "observed_today_max_c",
                "today_max_c",
                "settlement_actual",
                "settlement_actual_c",
                "official_final_result",
                "final_daily_max_c",
                "zuuu_metar_raw_amsc_today_max_c",
                "zuuu_metar_raw_awc_today_max_c",
            ),
            "visibility_m": ("visibility_m", "visibility", "visibilityMeters"),
            "cloud_cover_pct": ("cloud_cover_pct", "cloud_pct", "cloudCover"),
            "low_cloud_cover_pct": ("low_cloud_cover_pct", "low_cloud_pct"),
            "wind_speed_mps": ("wind_speed_mps", "wind_speed", "windSpeed"),
            "wind_dir_degrees": ("wind_dir_degrees", "wind_direction_degrees", "wind_direction", "wind_dir"),
            "wind_gust_mps": ("wind_gust_mps", "gust_mps", "wind_gust", "windGust"),
            "pressure_hpa": ("pressure_hpa", "altimeter_hpa", "pressure"),
            "momentum_rate_c_per_hour": ("momentum_rate_c_per_hour", "temperature_rate_c_per_hour"),
        }.items():
            if merged.get(target) in (None, ""):
                parsed = _first_float(row, payload, *fields)
                if parsed is not None:
                    merged[target] = parsed
        for window in TEMPERATURE_MOMENTUM_WINDOWS:
            target = f"temperature_momentum_{window}_c_per_hour"
            parsed = _first_float(
                row,
                payload,
                target,
                f"temperature_rise_rate_{window}_c_per_hour",
                f"zuuu_metar_raw_amsc_temperature_momentum_{window}_c_per_hour",
                f"zuuu_metar_raw_awc_temperature_momentum_{window}_c_per_hour",
            )
            if parsed is not None and (merged.get(target) in (None, "") or source_id == PRIMARY_METAR_SOURCE_ID):
                merged[target] = parsed
                merged["temperature_momentum_source"] = source_id
        parsed = _first_float(
            row,
            payload,
            "temperature_trajectory_stable_rate_c_per_hour",
            "temperature_momentum_stable_c_per_hour",
        )
        if parsed is not None and (
            merged.get("temperature_trajectory_stable_rate_c_per_hour") in (None, "")
            or source_id == PRIMARY_METAR_SOURCE_ID
        ):
            merged["temperature_trajectory_stable_rate_c_per_hour"] = parsed
            merged["temperature_path_source"] = source_id
        text_value = _first_text(
            row,
            payload,
            "temperature_trajectory_realization_state",
            "temperature_momentum_realization_state",
        )
        if text_value and (
            merged.get("temperature_trajectory_realization_state") in (None, "")
            or source_id == PRIMARY_METAR_SOURCE_ID
        ):
            merged["temperature_trajectory_realization_state"] = text_value
            merged["temperature_path_source"] = source_id
        for flag in FINAL_OBSERVATION_FLAG_FIELDS:
            if merged.get(flag) not in (None, ""):
                continue
            raw_value = row.get(flag) if row.get(flag) not in (None, "") else payload.get(flag)
            if raw_value not in (None, ""):
                merged[flag] = raw_value
        if merged.get("official_final_result") in (None, ""):
            official_final = _first_float(row, payload, "official_final_result", "settlement_actual", "settlement_actual_c", "final_daily_max_c")
            if official_final is not None:
                merged["official_final_result"] = official_final
        weather_text = _first_text(row, payload, "weather", "wx", "metar_weather", "raw_text")
        if weather_text and not merged.get("weather_text"):
            merged["weather_text"] = weather_text
        cloud_text = _first_text(row, payload, "clouds", "cloud_text", "raw_text")
        if cloud_text and not merged.get("cloud_text"):
            merged["cloud_text"] = cloud_text
        source_trace.append(trace_row)
    if hard_temperature_points:
        hard_temperature_points.sort(key=lambda item: float(item.get("_sort_value") or 0.0))
        latest = hard_temperature_points[-1]
        primary_points = [
            point for point in hard_temperature_points
            if str(point.get("source_id") or "").strip() == PRIMARY_METAR_SOURCE_ID
        ]
        latest_primary = primary_points[-1] if primary_points else None
        current_source_hint = str(
            base_inputs.get("observed_temp_source")
            or base_inputs.get("current_temp_source")
            or ""
        ).strip()
        if base_inputs.get("current_temp_c") in (None, "") or (latest_primary and current_source_hint != PRIMARY_METAR_SOURCE_ID):
            selected_current_point = latest_primary or latest
            merged["current_temp_c"] = selected_current_point.get("temperature_c")
            merged["observed_temp_source"] = str(selected_current_point.get("source_id") or "")
        observed_max_source_hint = str(base_inputs.get("observed_today_max_source") or "").strip()
        if base_inputs.get("observed_today_max_c") in (None, "") or primary_points or observed_max_source_hint != PRIMARY_METAR_SOURCE_ID:
            source_points = primary_points or hard_temperature_points
            candidates = [
                _to_float(point.get("observed_today_max_c"))
                for point in source_points
                if _to_float(point.get("observed_today_max_c")) is not None
            ]
            candidates.extend(float(point["temperature_c"]) for point in source_points)
            if candidates:
                merged["observed_today_max_c"] = max(candidates)
                merged["observed_today_max_source"] = PRIMARY_METAR_SOURCE_ID if primary_points else str(latest.get("source_id") or "")
                max_temperature = max(float(point["temperature_c"]) for point in source_points)
                max_points = [
                    point
                    for point in source_points
                    if abs(float(point["temperature_c"]) - max_temperature) <= EPS
                ]
                if max_points:
                    merged["observed_today_max_at"] = str(max_points[-1].get("valid_time") or "")
        selected_path_points = primary_points or hard_temperature_points
        selected_latest = latest_primary or latest
        for target in (
            "dewpoint_c",
            "visibility_m",
            "cloud_cover_pct",
            "low_cloud_cover_pct",
            "wind_speed_mps",
            "wind_dir_degrees",
            "wind_gust_mps",
            "wind_variable",
            "wind_variable_from_deg",
            "wind_variable_to_deg",
            "pressure_hpa",
            "weather_text",
            "cloud_text",
            "metar_current_no_significant_weather",
            "metar_trend_text",
            "metar_trend_cloud_text",
            "metar_trend_weather_text",
            "metar_trend_nosig",
        ):
            if selected_latest.get(target) not in (None, "") and (
                base_inputs.get(target) in (None, "") or latest_primary is not None
            ):
                merged[target] = selected_latest.get(target)
        merged["_source_observation_temperature_points"] = [
            {key: value for key, value in point.items() if key != "_sort_value"}
            for point in selected_path_points
        ]
        merged["temperature_path_source"] = PRIMARY_METAR_SOURCE_ID if primary_points else str(latest.get("source_id") or "")
    return merged, source_trace


def _station_local_hour(as_of: Any, station_timezone: str, fallback: float = 12.0) -> float:
    if as_of in (None, ""):
        return fallback
    try:
        tz = ZoneInfo(str(station_timezone or DEFAULT_STATION_TIMEZONE))
        if isinstance(as_of, datetime):
            parsed = as_of if as_of.tzinfo else as_of.replace(tzinfo=tz)
        else:
            text = str(as_of).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
        local = parsed.astimezone(tz)
        return float(local.hour) + float(local.minute) / 60.0 + float(local.second) / 3600.0
    except Exception:
        return fallback


def _bucket_bounds(label: str, rounding_rule: str = "rounded_degrees") -> tuple[float, float] | None:
    raw = str(label or "").strip().replace("–", "-")
    if not raw:
        return None
    num = r"(-?\d+(?:\.\d+)?)"
    rounded = str(rounding_rule or "").strip().lower() in {"rounded_degrees", "whole_degrees"}
    match = re.match(rf"^{num}\s*-\s*{num}", raw)
    if not match:
        match = re.match(rf"^{num}\s+to\s+{num}", raw, flags=re.I)
    if match:
        low = min(float(match.group(1)), float(match.group(2)))
        high = max(float(match.group(1)), float(match.group(2)))
        return (low - 0.5, high + 0.5) if rounded else (low, high)
    match = re.match(rf"^{num}\s*(?:°?[CF])?\s+or\s+below$", raw, flags=re.I)
    if match:
        high = float(match.group(1))
        return float("-inf"), high + 0.5 if rounded else high
    match = re.match(rf"^{num}\s*(?:°?[CF])?\s+or\s+(?:higher|above|more)$", raw, flags=re.I)
    if match:
        low = float(match.group(1))
        return low - 0.5 if rounded else low, float("inf")
    match = re.match(rf"^{num}\s*(?:°?[CF])?$", raw, flags=re.I)
    if match:
        center = float(match.group(1))
        return center - 0.5, center + 0.5
    return None


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    if math.isinf(x):
        return 0.0 if x < 0.0 else 1.0
    z = (float(x) - float(mean)) / max(EPS, float(sigma))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _normal_interval_prob(low: float, high: float, *, mu: float, sigma: float) -> float:
    return max(0.0, _normal_cdf(high, mu, sigma) - _normal_cdf(low, mu, sigma))


def _distribution_map(labels: Sequence[str], values: Sequence[float]) -> dict[str, float]:
    return {label: float(_round_float(value) or 0.0) for label, value in zip(labels, values)}


def _first_input_float(inputs: Mapping[str, Any], *fields: str) -> float | None:
    for field in fields:
        parsed = _to_float(inputs.get(field))
        if parsed is not None:
            return parsed
    return None


def _first_input_text(inputs: Mapping[str, Any], *fields: str) -> str:
    for field in fields:
        text = str(inputs.get(field) or "").strip()
        if text:
            return text
    return ""


def _local_hour_from_datetime_text(value: Any, *, station_timezone: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        tz = ZoneInfo(str(station_timezone or DEFAULT_STATION_TIMEZONE))
        parsed_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(parsed_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        local = parsed.astimezone(tz)
        return float(local.hour) + float(local.minute) / 60.0 + float(local.second) / 3600.0
    except Exception:
        return None


def _peak_window_from_inputs(inputs: Mapping[str, Any], *, station_timezone: str) -> dict[str, Any]:
    start = _first_input_float(
        inputs,
        "expected_peak_window_start_hour_local",
        "peak_window_start_hour_local",
        "local_peak_window_start_hour",
        "taf_forecast_peak_window_start_hour_local",
        "forecast_peak_window_start_hour_local",
    )
    end = _first_input_float(
        inputs,
        "expected_peak_window_end_hour_local",
        "peak_window_end_hour_local",
        "local_peak_window_end_hour",
        "taf_forecast_peak_window_end_hour_local",
        "forecast_peak_window_end_hour_local",
    )
    peak_hour = _first_input_float(
        inputs,
        "expected_peak_hour_local",
        "peak_hour_local",
        "local_peak_hour",
        "taf_forecast_peak_hour_local",
        "forecast_peak_hour_local",
    )
    peak_source = _first_input_text(inputs, "expected_peak_window_source", "peak_window_source", "expected_peak_source")
    peak_basis = _first_input_text(inputs, "expected_peak_window_basis", "peak_window_basis", "forecast_peak_window_basis")
    if peak_hour is None:
        peak_time_text = _first_input_text(
            inputs,
            "expected_peak_at_local",
            "taf_forecast_peak_local",
            "taf_forecast_peak_at",
            "forecast_peak_at",
            "forecast_peak_local",
            "wu_forecast_peak_at",
            "weather_com_forecast_peak_at",
            "twc_forecast_peak_at",
        )
        peak_hour = _local_hour_from_datetime_text(peak_time_text, station_timezone=station_timezone)
        if peak_hour is not None and not peak_source:
            peak_source = "forecast_peak_time_expanded_window"
    generated_window_source = " ".join([str(peak_source or ""), str(peak_basis or "")]).lower()
    if (
        peak_hour is not None
        and start is not None
        and end is not None
        and (
            "taf_tx_time_expanded_not_single_point" in generated_window_source
            or "forecast_peak_hour_expanded_window_not_single_point" in generated_window_source
            or "zuuu_taf_forecast_amsc_tx_expanded_window" in generated_window_source
        )
    ):
        start = None
        end = None
    if start is None and end is None and peak_hour is not None:
        lead = max(0.0, _first_input_float(inputs, "expected_peak_window_lead_hours") or 1.0)
        post = max(0.0, _first_input_float(inputs, "expected_peak_window_post_hours") or 2.0)
        start = max(0.0, float(peak_hour) - lead)
        end = min(23.99, float(peak_hour) + post)
        basis = "forecast_peak_hour_expanded_window_not_single_point"
    elif start is not None or end is not None:
        if start is None:
            start = max(0.0, float(end) - 2.0)
        if end is None:
            end = min(23.99, float(start) + 2.5)
        basis = "explicit_or_forecast_peak_window"
    else:
        start = 13.5
        end = 17.5
        basis = "broad_climatological_peak_window_not_single_point"
        peak_source = peak_source or "chengdu_climatological_peak_window"
    start = _clamp(float(start), 0.0, 23.99)
    end = _clamp(float(end), 0.0, 23.99)
    if end < start:
        end = start
    center = peak_hour if peak_hour is not None else (start + end) / 2.0
    return {
        "schema": "predicted_daily_high_temperature_peak_window_v1",
        "start_hour_local": _round_float(start),
        "end_hour_local": _round_float(end),
        "center_hour_local": _round_float(float(center)),
        "source": peak_source,
        "basis": basis,
        "single_point_forbidden": True,
    }


def _momentum_rates_from_inputs(inputs: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, float]]:
    raw_rates: dict[str, float] = {}
    generic_source = str(inputs.get("temperature_momentum_source") or "").strip()
    for window in TEMPERATURE_MOMENTUM_WINDOWS:
        fields = [
            f"zuuu_metar_raw_amsc_temperature_momentum_{window}_c_per_hour",
        ]
        if generic_source in {"", PRIMARY_METAR_SOURCE_ID, "temperature_momentum"}:
            fields.extend(
                [
                    f"temperature_momentum_{window}_c_per_hour",
                    f"temperature_rise_rate_{window}_c_per_hour",
                ]
            )
        fields.extend(
            f"{source_id}_temperature_momentum_{window}_c_per_hour"
            for source_id in REFERENCE_METAR_SOURCE_IDS
        )
        if generic_source not in {"", PRIMARY_METAR_SOURCE_ID, "temperature_momentum"}:
            fields.extend(
                [
                    f"temperature_momentum_{window}_c_per_hour",
                    f"temperature_rise_rate_{window}_c_per_hour",
                ]
            )
        for field in fields:
            parsed = _to_float(inputs.get(field))
            if parsed is not None:
                raw_rates[window] = float(parsed)
                break
    rates = {
        window: rate
        for window, rate in raw_rates.items()
        if math.isfinite(float(rate)) and abs(float(rate)) <= TEMPERATURE_MOMENTUM_RATE_ABS_MAX
    }
    suppressed = {window: rate for window, rate in raw_rates.items() if window not in rates}
    return rates, suppressed


def _weighted_rate(rates: Mapping[str, float], windows: Sequence[str] | None = None) -> float | None:
    selected = [window for window in (windows or TEMPERATURE_MOMENTUM_WINDOWS) if window in rates]
    if not selected:
        return None
    weight_sum = sum(TEMPERATURE_MOMENTUM_WEIGHTS.get(window, 0.0) for window in selected)
    if weight_sum <= EPS:
        return sum(float(rates[window]) for window in selected) / float(len(selected))
    return sum(float(rates[window]) * TEMPERATURE_MOMENTUM_WEIGHTS.get(window, 0.0) for window in selected) / weight_sum


def _input_temperature_points(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_points: list[Any] = []
    for key in (
        "_source_observation_temperature_points",
        "temperature_observations",
        "metar_temperature_observations",
        "hard_source_temperature_observations",
    ):
        value = inputs.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            raw_points.extend(value)
    points: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_points):
        if not isinstance(raw, Mapping):
            continue
        temperature_c = _to_float(
            raw.get("temperature_c")
            if raw.get("temperature_c") is not None
            else raw.get("temp_c")
            if raw.get("temp_c") is not None
            else raw.get("current_temp_c")
        )
        if temperature_c is None:
            continue
        valid_time = str(
            raw.get("valid_time")
            or raw.get("valid_time_utc")
            or raw.get("observed_at")
            or raw.get("time")
            or raw.get("timestamp")
            or ""
        ).strip()
        points.append(
            {
                "temperature_c": float(temperature_c),
                "valid_time": valid_time,
                "valid_time_local": str(raw.get("valid_time_local") or raw.get("observed_at_local") or "").strip(),
                "source_id": str(raw.get("source_id") or raw.get("source") or "").strip(),
                "source_observation_id": str(raw.get("source_observation_id") or "").strip(),
                "dewpoint_c": _to_float(raw.get("dewpoint_c")),
                "visibility_m": _to_float(raw.get("visibility_m") if raw.get("visibility_m") is not None else raw.get("visibility")),
                "cloud_cover_pct": _to_float(raw.get("cloud_cover_pct")),
                "low_cloud_cover_pct": _to_float(raw.get("low_cloud_cover_pct")),
                "wind_speed_mps": _to_float(raw.get("wind_speed_mps")),
                "wind_dir_degrees": _to_float(raw.get("wind_dir_degrees")),
                "wind_gust_mps": _to_float(raw.get("wind_gust_mps")),
                "pressure_hpa": _to_float(raw.get("pressure_hpa")),
                "weather_text": str(raw.get("weather_text") or "").strip(),
                "cloud_text": str(raw.get("cloud_text") or "").strip(),
                "_sort_value": _parse_time_sort_value(valid_time or raw.get("valid_time_local"), float(index)),
            }
        )
    points.sort(key=lambda item: float(item.get("_sort_value") or 0.0))
    return points[-ROLLING_TEMPERATURE_OBSERVATION_WINDOW:]


def _rolling_temperature_trajectory_summary(inputs: Mapping[str, Any]) -> dict[str, Any]:
    points = _input_temperature_points(inputs)
    clean_points = [
        {key: value for key, value in point.items() if key != "_sort_value"}
        for point in points
    ]
    if len(points) < 3:
        return {
            "schema": "metar_integer_temperature_rolling_trajectory_v1",
            "state": "insufficient_points",
            "point_count": len(points),
            "points": clean_points,
            "quantization_noise_c": METAR_INTEGER_QUANTIZATION_NOISE_C,
            "adjacent_step_noise_c": METAR_ADJACENT_STEP_NOISE_C,
        }
    temps = [float(point["temperature_c"]) for point in points]
    rolling_max = max(temps)
    current = temps[-1]
    total_delta = current - temps[0]
    duration_hours = max((float(points[-1]["_sort_value"]) - float(points[0]["_sort_value"])) / 3600.0, 1e-6)
    if abs(total_delta) <= METAR_ADJACENT_STEP_NOISE_C:
        adjusted_delta = 0.0
    else:
        adjusted_delta = math.copysign(abs(total_delta) - METAR_INTEGER_QUANTIZATION_NOISE_C, total_delta)
    rolling_slope = adjusted_delta / duration_hours
    latest_drop = rolling_max - current
    consecutive_drop_reports = 0
    for temp in reversed(temps):
        if rolling_max - temp >= METAR_PLATFORM_END_DROP_C - EPS:
            consecutive_drop_reports += 1
        else:
            break
    platform_end_confirmed = consecutive_drop_reports >= METAR_PLATFORM_END_MIN_CONSECUTIVE_REPORTS
    within_platform = latest_drop <= METAR_PLATFORM_WITHIN_ROLLING_MAX_C + EPS
    adjacent_deltas = [temps[idx] - temps[idx - 1] for idx in range(1, len(temps))]
    small_step_count = sum(1 for delta in adjacent_deltas if abs(delta) <= METAR_ADJACENT_STEP_NOISE_C + EPS)
    rebound_pattern = len(temps) >= 3 and temps[-3] == temps[-1] and abs(temps[-2] - temps[-1]) <= METAR_ADJACENT_STEP_NOISE_C
    if platform_end_confirmed and rolling_slope < -0.25:
        state = "cooling"
        gate_state = "not_warming"
        gate_reason = "rolling_drop_ge_2c_two_reports_without_rebound"
    elif within_platform and (abs(rolling_slope) < 0.25 or rebound_pattern):
        state = "platform"
        gate_state = "platform_noise"
        gate_reason = "current_within_1c_of_rolling_max_integer_metar_noise"
    elif rolling_slope >= 0.35:
        state = "sustained_warming"
        gate_state = "confirmed"
        gate_reason = "rolling_3_to_5_point_slope_positive"
    elif rolling_slope >= 0.15:
        state = "weak_warming"
        gate_state = "confirmed"
        gate_reason = "rolling_3_to_5_point_slope_weak_positive"
    elif rolling_slope <= -0.35:
        state = "mixed"
        gate_state = "unconfirmed"
        gate_reason = "single_or_unconfirmed_negative_rolling_slope"
    else:
        state = "flat"
        gate_state = "not_warming"
        gate_reason = "rolling_slope_near_zero"
    confidence = 0.45 + min(0.35, 0.08 * max(0, len(points) - 3))
    if small_step_count:
        confidence -= min(0.18, 0.04 * small_step_count)
    if platform_end_confirmed:
        confidence = max(confidence, 0.78)
    elif state == "platform":
        confidence = max(confidence, 0.70)
    return {
        "schema": "metar_integer_temperature_rolling_trajectory_v1",
        "state": state,
        "gate_state": gate_state,
        "gate_reason": gate_reason,
        "point_count": len(points),
        "points": clean_points,
        "current_temp_c": _round_float(current),
        "rolling_max_c": _round_float(rolling_max),
        "latest_drop_from_rolling_max_c": _round_float(latest_drop),
        "within_rolling_max_platform_c": METAR_PLATFORM_WITHIN_ROLLING_MAX_C,
        "platform_end_drop_threshold_c": METAR_PLATFORM_END_DROP_C,
        "platform_end_consecutive_reports": consecutive_drop_reports,
        "platform_end_confirmed": platform_end_confirmed,
        "raw_total_delta_c": _round_float(total_delta),
        "quantization_adjusted_delta_c": _round_float(adjusted_delta),
        "rolling_slope_c_per_hour": _round_float(rolling_slope),
        "duration_hours": _round_float(duration_hours),
        "adjacent_deltas_c": [_round_float(delta) for delta in adjacent_deltas],
        "small_step_noise_count": small_step_count,
        "rebound_pattern": rebound_pattern,
        "quantization_noise_c": METAR_INTEGER_QUANTIZATION_NOISE_C,
        "adjacent_step_noise_c": METAR_ADJACENT_STEP_NOISE_C,
        "confidence": _round_float(_clamp(confidence, 0.0, 1.0)),
        "policy": "integer_metar_quantization_noise_rolling_3_to_5_points",
    }


def _trajectory_state_from_rates(
    *,
    rates: Mapping[str, float],
    stable_rate: float | None,
    recent_rate: float | None,
    short_rate: float | None,
    confirmed_windows: Sequence[str],
    positive_short_windows: Sequence[str],
    local_hour: float,
    explicit_state: str,
    peak_window: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    explicit = str(explicit_state or "").strip().lower()
    if not rates:
        return (explicit, "explicit_state_no_rates") if explicit and explicit not in {"missing", "unknown"} else ("missing", "rates_missing")
    stable = stable_rate
    recent = recent_rate
    short = short_rate
    confirmed_count = len(confirmed_windows)
    nonpositive_confirmed = sum(1 for window in TEMPERATURE_MOMENTUM_CONFIRM_WINDOWS if rates.get(window) is not None and rates[window] <= 0.0)
    # Legacy fetches may stamp an integer-METAR ``platform`` label from the
    # latest display value.  That label is not a physical cutoff and must not
    # erase independently confirmed, continuous multi-window warming.  Keep
    # the override deliberately narrow: one short positive window is still
    # insufficient to displace platform/noise semantics.
    continuous_positive_warming = (
        confirmed_count >= 2
        and stable is not None
        and stable >= 0.35
        and nonpositive_confirmed == 0
    )
    if explicit and explicit not in {"missing", "unknown"}:
        if explicit == "platform" and continuous_positive_warming:
            return "sustained_warming", "continuous_positive_momentum_overrides_explicit_platform"
        return explicit, "explicit_state_preserved"
    if recent is not None and recent <= -0.20 and (stable is None or stable <= 0.05):
        return "cooling", "rate_derived_cooling"
    window = peak_window if isinstance(peak_window, Mapping) else {}
    peak_start = _to_float(window.get("start_hour_local"))
    platform_watch_hour = max(13.0, float(peak_start) - 0.5) if peak_start is not None else 13.0
    if local_hour >= platform_watch_hour and stable is not None and stable < 0.15 and (short is None or short < 0.25):
        return "platform", "rate_derived_platform"
    if stable is not None and stable <= 0.05 and (short is None or short <= 0.0):
        return "not_realized", "rate_derived_not_realized"
    if positive_short_windows and confirmed_count == 0:
        return "short_window_only", "rate_derived_short_window_only"
    if confirmed_count >= 2 and stable is not None and stable >= 0.35 and nonpositive_confirmed == 0:
        return "sustained_warming", "rate_derived_sustained_warming"
    if confirmed_count >= 1 and stable is not None and stable >= 0.25:
        return "emerging_warming", "rate_derived_emerging_warming"
    if stable is not None and stable >= 0.15 and nonpositive_confirmed == 0:
        return "weak_warming", "rate_derived_weak_warming"
    if stable is not None and abs(stable) < 0.15:
        return "flat", "rate_derived_flat"
    return "mixed", "rate_derived_mixed"


def _trajectory_response_scale(state: str, *, local_hour: float, peak_window: Mapping[str, Any] | None = None) -> float:
    base = {
        "sustained_warming": 1.00,
        "emerging_warming": 0.78,
        "weak_warming": 0.48,
        "midday_recent_confirmation_missing": 0.12,
        "pre_peak_integer_plateau": 0.18,
        "short_window_only": 0.16,
        "mixed": 0.28,
        "flat": 0.06,
        "platform": 0.00,
        "not_realized": 0.00,
        "cooling": 1.00,
        "single_rate_unconfirmed": 0.24,
        "missing": 0.00,
    }.get(str(state or "").strip().lower(), 0.20)
    window = peak_window if isinstance(peak_window, Mapping) else {}
    peak_end = _to_float(window.get("end_hour_local"))
    taper_hour = max(12.5, float(peak_end) - 1.5) if peak_end is not None else 14.0
    if local_hour >= taper_hour and state not in {"cooling"}:
        base *= 0.65
    peak_start = _to_float(window.get("start_hour_local"))
    early_hour = max(9.5, float(peak_start) - 3.5) if peak_start is not None else 10.0
    if local_hour <= early_hour and state in {"short_window_only", "emerging_warming", "weak_warming"}:
        base *= 0.80
    return _clamp(base, 0.0, 1.0)


def _pre_peak_integer_plateau_watch(
    *,
    rolling_summary: Mapping[str, Any],
    local_hour: float,
    peak_window: Mapping[str, Any],
    confirmed_windows: Sequence[str],
    recent_confirmed_windows: Sequence[str],
    background_rate: float | None,
) -> bool:
    if str(rolling_summary.get("state") or "").strip().lower() != "platform":
        return False
    if bool(rolling_summary.get("platform_end_confirmed")):
        return False
    if recent_confirmed_windows:
        return False
    if not confirmed_windows:
        return False
    if background_rate is None or float(background_rate) < 0.25:
        return False
    peak_end = _to_float(peak_window.get("end_hour_local"))
    if peak_end is None:
        return False
    remaining_hours = float(peak_end) - float(local_hour)
    if remaining_hours < PRE_PEAK_INTEGER_PLATEAU_MIN_REMAINING_HOURS:
        return False
    latest_drop = _to_float(rolling_summary.get("latest_drop_from_rolling_max_c"))
    if latest_drop is not None and latest_drop > METAR_PLATFORM_WITHIN_ROLLING_MAX_C + EPS:
        return False
    return True


def build_temperature_trajectory_summary(
    inputs: Mapping[str, Any],
    *,
    local_hour: float,
    fallback_momentum_rate_c_per_hour: float | None = None,
    peak_window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    peak_window_map = dict(peak_window or {})
    rolling_summary = _rolling_temperature_trajectory_summary(inputs)
    rolling_state = str(rolling_summary.get("state") or "")
    rolling_rate = _to_float(rolling_summary.get("rolling_slope_c_per_hour"))
    inputs_for_rates = dict(inputs)
    if rolling_rate is not None:
        inputs_for_rates.setdefault("temperature_trajectory_stable_rate_c_per_hour", rolling_rate)
        inputs_for_rates.setdefault("temperature_momentum_effective_c_per_hour", rolling_rate)
    if rolling_state and rolling_state != "insufficient_points":
        inputs_for_rates.setdefault("temperature_trajectory_realization_state", rolling_state)
    rates, suppressed = _momentum_rates_from_inputs(inputs_for_rates)
    explicit_effective = _first_input_float(
        inputs_for_rates,
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_posterior_effective_c_per_hour",
    )
    explicit_stable = _first_input_float(
        inputs_for_rates,
        "temperature_trajectory_stable_rate_c_per_hour",
        "temperature_momentum_stable_c_per_hour",
    )
    stable_rate = explicit_stable
    if stable_rate is None:
        stable_rate = _weighted_rate(rates, TEMPERATURE_MOMENTUM_CONFIRM_WINDOWS)
    recent_rate = _weighted_rate(rates, TEMPERATURE_MOMENTUM_RECENT_WINDOWS)
    short_rate = _weighted_rate(rates, TEMPERATURE_MOMENTUM_SHORT_WINDOWS)
    background_rate = _weighted_rate(rates, TEMPERATURE_MOMENTUM_BACKGROUND_CONFIRM_WINDOWS)
    effective_rate = explicit_effective
    if effective_rate is None:
        effective_rate = stable_rate if stable_rate is not None else recent_rate
    if effective_rate is None:
        effective_rate = fallback_momentum_rate_c_per_hour
    if effective_rate is not None and abs(float(effective_rate)) > TEMPERATURE_MOMENTUM_RATE_ABS_MAX:
        suppressed["effective"] = float(effective_rate)
        effective_rate = None

    confirmed_windows = [
        window
        for window in TEMPERATURE_MOMENTUM_CONFIRM_WINDOWS
        if rates.get(window) is not None and float(rates[window]) >= 0.25
    ]
    recent_confirmed_windows = [
        window
        for window in TEMPERATURE_MOMENTUM_RECENT_CONFIRM_WINDOWS
        if rates.get(window) is not None and float(rates[window]) >= 0.25
    ]
    positive_short_windows = [
        window
        for window in TEMPERATURE_MOMENTUM_SHORT_WINDOWS
        if rates.get(window) is not None and float(rates[window]) >= 0.35
    ]
    state, state_selection_reason = _trajectory_state_from_rates(
        rates=rates,
        stable_rate=stable_rate,
        recent_rate=recent_rate,
        short_rate=short_rate,
        confirmed_windows=confirmed_windows,
        positive_short_windows=positive_short_windows,
        local_hour=float(local_hour),
        explicit_state=str(inputs_for_rates.get("temperature_trajectory_realization_state") or ""),
        peak_window=peak_window_map,
    )
    downgraded_unconfirmed_warming = False
    if state in {"sustained_warming", "emerging_warming", "weak_warming"} and not confirmed_windows:
        state = "single_rate_unconfirmed"
        downgraded_unconfirmed_warming = True
    if state == "missing" and fallback_momentum_rate_c_per_hour is not None:
        fallback_rate = float(fallback_momentum_rate_c_per_hour)
        if fallback_rate <= -0.20:
            state = "cooling"
        elif fallback_rate >= 0.25:
            state = "single_rate_unconfirmed"
        else:
            state = "flat"
    if (
        float(local_hour)
        >= max(
            TEMPERATURE_MOMENTUM_RECENT_CONFIRM_MIN_LOCAL_HOUR,
            float(_to_float(peak_window_map.get("start_hour_local")) or 13.0)
            - TEMPERATURE_MOMENTUM_RECENT_CONFIRM_LEAD_HOURS,
        )
        and state in {"sustained_warming", "emerging_warming", "weak_warming"}
        and confirmed_windows
        and not recent_confirmed_windows
    ):
        state = "midday_recent_confirmation_missing"
    if _pre_peak_integer_plateau_watch(
        rolling_summary=rolling_summary,
        local_hour=float(local_hour),
        peak_window=peak_window_map,
        confirmed_windows=confirmed_windows,
        recent_confirmed_windows=recent_confirmed_windows,
        background_rate=background_rate,
    ):
        state = "pre_peak_integer_plateau"
        if effective_rate is None or float(effective_rate) <= 0.05:
            effective_rate = background_rate
    response_scale = _trajectory_response_scale(state, local_hour=float(local_hour), peak_window=peak_window_map)
    adjusted_rate: float | None
    if state == "cooling":
        adjusted_rate = min(0.0, float(effective_rate if effective_rate is not None else recent_rate or 0.0))
    elif effective_rate is None:
        adjusted_rate = None
    else:
        adjusted_rate = max(0.0, float(effective_rate)) * response_scale

    gate_state = "confirmed" if state in {"sustained_warming", "emerging_warming", "weak_warming"} and confirmed_windows else "unconfirmed"
    if state == "short_window_only":
        gate_state = "short_window_watch"
    elif state == "midday_recent_confirmation_missing":
        gate_state = "recent_window_watch"
    elif state == "pre_peak_integer_plateau":
        gate_state = "platform_watch"
    elif state in {"cooling", "not_realized", "flat", "platform"}:
        gate_state = "not_warming"
    elif state == "missing":
        gate_state = "missing"
    confidence = {
        "sustained_warming": 0.90,
        "emerging_warming": 0.72,
        "weak_warming": 0.52,
        "midday_recent_confirmation_missing": 0.34,
        "pre_peak_integer_plateau": 0.38,
        "short_window_only": 0.24,
        "single_rate_unconfirmed": 0.28,
        "mixed": 0.34,
        "flat": 0.44,
        "platform": 0.70,
        "not_realized": 0.62,
        "cooling": 0.82,
        "missing": 0.0,
    }.get(state, 0.30)
    na_reasons: list[str] = []
    if not rates and fallback_momentum_rate_c_per_hour is None:
        if rolling_rate is not None and rolling_summary.get("state") != "insufficient_points":
            na_reasons.append("temperature_momentum_single_rate_without_multi_window_confirmation")
        else:
            na_reasons.append("temperature_momentum_windows_missing")
    elif not rates:
        na_reasons.append("temperature_momentum_single_rate_without_multi_window_confirmation")
    if downgraded_unconfirmed_warming and "temperature_momentum_single_rate_without_multi_window_confirmation" not in na_reasons:
        na_reasons.append("temperature_momentum_single_rate_without_multi_window_confirmation")
    if suppressed:
        na_reasons.append("temperature_momentum_untrusted_rate_suppressed")
    if state == "short_window_only":
        na_reasons.append("temperature_momentum_short_window_only_not_posterior_confirmed")
    if state == "midday_recent_confirmation_missing":
        na_reasons.append("temperature_momentum_midday_recent_window_confirmation_missing")
    if state == "pre_peak_integer_plateau":
        na_reasons.append("integer_metar_pre_peak_plateau_without_30m_recent_confirmation")
    if rolling_summary.get("state") == "insufficient_points":
        na_reasons.append("rolling_temperature_observation_points_insufficient")
    return {
        "schema": "temperature_trajectory_summary_v1",
        "role": "temperature_realization_within_metar_strong_likelihood",
        "boundary": "observation_layer_internal_feature_not_independent_likelihood",
        "state": state,
        "state_selection_reason": state_selection_reason,
        "gate_state": gate_state,
        "gate_reason": (
            "multi_window_confirmed"
            if gate_state == "confirmed"
            else "peak_window_requires_30m_recent_confirmation"
            if gate_state == "recent_window_watch"
            else "integer_metar_plateau_before_peak_window_end"
            if gate_state == "platform_watch"
            else "short_window_requires_30m_1h_confirmation"
            if gate_state == "short_window_watch"
            else f"trajectory_state_{state}"
        ),
        "rates_c_per_hour": _round_mapping(rates),
        "suppressed_untrusted_rates_c_per_hour": _round_mapping(suppressed),
        "stable_rate_c_per_hour": _round_float(stable_rate),
        "recent_rate_c_per_hour": _round_float(recent_rate),
        "short_rate_c_per_hour": _round_float(short_rate),
        "background_rate_c_per_hour": _round_float(background_rate),
        "effective_rate_c_per_hour": _round_float(effective_rate),
        "adjusted_rate_c_per_hour": _round_float(adjusted_rate),
        "response_scale": _round_float(response_scale),
        "confidence": _round_float(confidence),
        "rolling_temperature_observation_summary": rolling_summary,
        "confirmed_windows": list(confirmed_windows),
        "recent_confirmed_windows": list(recent_confirmed_windows),
        "positive_short_windows": list(positive_short_windows),
        "local_hour": _round_float(float(local_hour)),
        "predicted_peak_window": peak_window_map,
        "na_reasons": na_reasons,
        "dedupe_policy": "temperature_path_consumed_once_inside_metar_strong_likelihood",
    }


def _contains_weather_token(text: str, tokens: Sequence[str]) -> bool:
    raw = str(text or "").upper()
    if not raw:
        return False
    compact = re.sub(r"[^A-Z+\\-]", " ", raw)
    parts = [part.strip() for part in compact.split() if part.strip()]
    for token in tokens:
        token_text = str(token or "").upper().strip()
        if not token_text:
            continue
        if token_text in parts:
            return True
        if token_text in raw:
            return True
    return False


def _cloud_text_low_layer_bucket(text: Any) -> str:
    raw = str(text or "").upper()
    if "OVC" in raw:
        return "OVC"
    if "BKN" in raw:
        return "BKN"
    if "SCT" in raw:
        return "SCT"
    if "FEW" in raw:
        return "FEW"
    if "CLR" in raw or "SKC" in raw or "NSC" in raw:
        return "CLR"
    return ""


def _angular_change_degrees(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    diff = abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)
    return diff


def _physical_cutoff_evidence(
    inputs: Mapping[str, Any],
    observation_features: Mapping[str, Any],
    temperature_trajectory: Mapping[str, Any],
    *,
    local_hour: float | None,
    peak_window: Mapping[str, Any] | None,
) -> dict[str, Any]:
    points = _input_temperature_points(inputs)
    latest_point = points[-1] if points else {}
    previous_points = points[:-1]
    point_temperatures = [
        float(value)
        for value in (_to_float(point.get("temperature_c")) for point in points)
        if value is not None
    ]
    point_max_c = max(point_temperatures) if point_temperatures else None
    point_max_hours = [
        hour
        for point in points
        if point_max_c is not None
        and _to_float(point.get("temperature_c")) is not None
        and abs(float(_to_float(point.get("temperature_c")) or 0.0) - float(point_max_c)) <= EPS
        and (hour := _local_hour_from_datetime_text(
            point.get("valid_time"),
            station_timezone=DEFAULT_STATION_TIMEZONE,
        )) is not None
    ]
    observed_max_at = _first_input_text(
        inputs,
        "observed_today_max_at",
        "zuuu_metar_raw_amsc_today_max_at",
        "zuuu_metar_raw_awc_today_max_at",
    )
    observed_max_hour = _local_hour_from_datetime_text(
        observed_max_at,
        station_timezone=DEFAULT_STATION_TIMEZONE,
    )
    pre_dawn_carryover_peak = bool(
        (observed_max_hour is not None and 0.0 <= float(observed_max_hour) < 6.0)
        or (point_max_hours and all(0.0 <= float(hour) < 6.0 for hour in point_max_hours))
    )
    rolling = (
        temperature_trajectory.get("rolling_temperature_observation_summary")
        if isinstance(temperature_trajectory.get("rolling_temperature_observation_summary"), Mapping)
        else {}
    )
    weather_cloud_text = " ".join(
        str(value or "")
        for value in (
            observation_features.get("weather_text"),
            observation_features.get("cloud_text"),
            latest_point.get("weather_text"),
            latest_point.get("cloud_text"),
        )
    ).upper()
    cb_observed = _contains_weather_token(weather_cloud_text, ("CB", "VCB", "TCU"))
    thunder_observed = _contains_weather_token(weather_cloud_text, ("TS", "TSRA", "+TSRA", "-TSRA", "VCTS"))
    shower_observed = _contains_weather_token(weather_cloud_text, ("SHRA", "+SHRA", "-SHRA"))
    rain_observed = _contains_weather_token(weather_cloud_text, ("RA", "+RA", "-RA", "DZ", "+DZ", "-DZ"))
    convective_or_rain_cutoff = bool(cb_observed or thunder_observed or shower_observed or rain_observed)

    current_pressure = _to_float(observation_features.get("pressure_hpa"))
    if current_pressure is None:
        current_pressure = _to_float(latest_point.get("pressure_hpa"))
    previous_pressures = [
        value for value in (_to_float(point.get("pressure_hpa")) for point in previous_points[-3:]) if value is not None
    ]
    pressure_jump_hpa = None
    if current_pressure is not None and previous_pressures:
        pressure_jump_hpa = float(current_pressure) - min(float(value) for value in previous_pressures)
    pressure_trend_hpa_per_hour = _first_input_float(
        inputs,
        "pressure_trend_hpa_per_hour",
        "pressure_change_hpa_per_hour",
        "pressure_rise_hpa_per_hour",
    )
    pressure_jump_threshold = max(0.0, _env_float("PM_BAYES_RECOIL_PRESSURE_JUMP_HPA", 1.0))
    pressure_jump_observed = bool(
        (pressure_jump_hpa is not None and pressure_jump_hpa >= pressure_jump_threshold)
        or (pressure_trend_hpa_per_hour is not None and pressure_trend_hpa_per_hour >= pressure_jump_threshold)
    )

    current_wind_speed = _to_float(observation_features.get("wind_speed_mps"))
    if current_wind_speed is None:
        current_wind_speed = _to_float(latest_point.get("wind_speed_mps"))
    current_wind_dir = _to_float(observation_features.get("wind_dir_degrees"))
    if current_wind_dir is None:
        current_wind_dir = _to_float(latest_point.get("wind_dir_degrees"))
    current_gust = _to_float(observation_features.get("wind_gust_mps"))
    if current_gust is None:
        current_gust = _to_float(latest_point.get("wind_gust_mps"))
    previous_wind_speeds = [
        value for value in (_to_float(point.get("wind_speed_mps")) for point in previous_points[-3:]) if value is not None
    ]
    previous_wind_dirs = [
        value for value in (_to_float(point.get("wind_dir_degrees")) for point in previous_points[-3:]) if value is not None
    ]
    wind_speed_jump = None
    if current_wind_speed is not None and previous_wind_speeds:
        wind_speed_jump = float(current_wind_speed) - min(float(value) for value in previous_wind_speeds)
    wind_dir_shift = _angular_change_degrees(current_wind_dir, previous_wind_dirs[-1] if previous_wind_dirs else None)
    wind_shift_input = _first_input_float(inputs, "wind_shift_degrees", "wind_direction_shift_degrees")
    wind_shift = max(
        value for value in (wind_dir_shift, wind_shift_input, 0.0) if value is not None
    )
    gust_threshold = max(0.0, _env_float("PM_BAYES_RECOIL_OUTFLOW_GUST_MPS", 7.0))
    wind_jump_threshold = max(0.0, _env_float("PM_BAYES_RECOIL_OUTFLOW_WIND_JUMP_MPS", 3.0))
    wind_shift_threshold = max(0.0, _env_float("PM_BAYES_RECOIL_OUTFLOW_SHIFT_DEG", 75.0))
    northwest_enhanced = bool(
        current_wind_dir is not None
        and 270.0 <= float(current_wind_dir) <= 360.0
        and current_wind_speed is not None
        and float(current_wind_speed) >= 4.0
    )
    wind_outflow_observed = bool(
        _to_bool(inputs.get("outflow_wind_observed"))
        or (current_gust is not None and current_gust >= gust_threshold)
        or (wind_speed_jump is not None and wind_speed_jump >= wind_jump_threshold and wind_shift >= wind_shift_threshold)
        or (northwest_enhanced and wind_speed_jump is not None and wind_speed_jump >= 2.0)
    )

    current_visibility = _to_float(observation_features.get("visibility_m"))
    if current_visibility is None:
        current_visibility = _to_float(latest_point.get("visibility_m"))
    previous_visibility = [
        value for value in (_to_float(point.get("visibility_m")) for point in previous_points[-3:]) if value is not None
    ]
    visibility_drop_m = None
    if current_visibility is not None and previous_visibility:
        visibility_drop_m = max(float(value) for value in previous_visibility) - float(current_visibility)
    visibility_trend_m_per_hour = _first_input_float(inputs, "visibility_trend_m_per_hour", "visibility_change_m_per_hour")
    visibility_deterioration = bool(
        (current_visibility is not None and current_visibility < 8000.0 and visibility_drop_m is not None and visibility_drop_m >= 1500.0)
        or (visibility_trend_m_per_hour is not None and visibility_trend_m_per_hour <= -2000.0)
    )

    current_low_cloud = _to_float(observation_features.get("low_cloud_cover_pct"))
    if current_low_cloud is None:
        current_low_cloud = _to_float(latest_point.get("low_cloud_cover_pct"))
    previous_low_cloud = [
        value for value in (_to_float(point.get("low_cloud_cover_pct")) for point in previous_points[-3:]) if value is not None
    ]
    low_cloud_jump_pct = None
    if current_low_cloud is not None and previous_low_cloud:
        low_cloud_jump_pct = float(current_low_cloud) - min(float(value) for value in previous_low_cloud)
    latest_cloud_bucket = _cloud_text_low_layer_bucket(
        observation_features.get("cloud_text") or latest_point.get("cloud_text")
    )
    previous_cloud_buckets = [
        _cloud_text_low_layer_bucket(point.get("cloud_text")) for point in previous_points[-3:]
    ]
    cloud_text_worsened = bool(
        latest_cloud_bucket in {"BKN", "OVC"}
        and any(bucket in {"FEW", "SCT", "CLR"} for bucket in previous_cloud_buckets)
    )
    low_cloud_deterioration = bool(
        cloud_text_worsened
        or (current_low_cloud is not None and current_low_cloud >= 75.0 and low_cloud_jump_pct is not None and low_cloud_jump_pct >= 25.0)
    )

    temps = [float(point["temperature_c"]) for point in points if _to_float(point.get("temperature_c")) is not None]
    stair_step_cooling = False
    if len(temps) >= 4:
        last4 = temps[-4:]
        total_drop = last4[0] - last4[-1]
        negative_steps = sum(1 for idx in range(1, len(last4)) if last4[idx] < last4[idx - 1] - EPS)
        rebound_steps = sum(1 for idx in range(1, len(last4)) if last4[idx] > last4[idx - 1] + EPS)
        stair_step_cooling = bool(total_drop >= 3.0 and negative_steps >= 2 and rebound_steps == 0)
    platform_end_confirmed = bool(rolling.get("platform_end_confirmed"))
    magnitude_cooling_confirmed = bool(platform_end_confirmed or stair_step_cooling)

    window = peak_window if isinstance(peak_window, Mapping) else {}
    peak_end = _to_float(window.get("end_hour_local"))
    time_grace_hours = max(0.0, _env_float("PM_BAYES_RECOIL_TRUE_COOLING_TIME_GRACE_HOURS", 0.75))
    rolling_state = str(rolling.get("state") or temperature_trajectory.get("state") or "").strip().lower()
    time_cutoff_confirmed = bool(
        local_hour is not None
        and peak_end is not None
        and float(local_hour) > float(peak_end) + float(time_grace_hours)
        and rolling_state in {"cooling", "platform", "flat"}
    )

    evidence_codes: list[str] = []
    for active, code in (
        (cb_observed, "cb_observed"),
        (thunder_observed, "thunderstorm_observed"),
        (shower_observed, "showery_rain_observed"),
        (rain_observed, "rain_observed"),
        (wind_outflow_observed, "outflow_wind_observed"),
        (pressure_jump_observed, "pressure_jump_observed"),
        (visibility_deterioration, "visibility_deterioration"),
        (low_cloud_deterioration, "low_cloud_deterioration"),
        (platform_end_confirmed, "rolling_drop_ge_2c_two_reports_without_rebound"),
        (stair_step_cooling, "stair_step_cooling"),
        (time_cutoff_confirmed, "peak_window_closed_without_rebound"),
    ):
        if active:
            evidence_codes.append(code)
    raw_physical_cutoff_confirmed = bool(
        convective_or_rain_cutoff
        or wind_outflow_observed
        or pressure_jump_observed
        or visibility_deterioration
        or low_cloud_deterioration
    )
    raw_magnitude_cooling_confirmed = bool(platform_end_confirmed or stair_step_cooling)
    pre_dawn_carryover_phase = bool(
        local_hour is not None and 0.0 <= float(local_hour) < 6.0
    )
    # A temperature maximum just after midnight is a hard calendar-day lower
    # bound, but its subsequent fall is normally decay from the previous solar
    # day's heat reservoir.  Preserve the observed convection/cooling fields,
    # while forbidding them from declaring that today's daytime peak has ended.
    physical_cutoff_confirmed = bool(raw_physical_cutoff_confirmed and not pre_dawn_carryover_phase)
    magnitude_cooling_confirmed = bool(raw_magnitude_cooling_confirmed and not pre_dawn_carryover_phase)
    time_cutoff_confirmed = bool(time_cutoff_confirmed and not pre_dawn_carryover_phase)
    true_cooling_or_cutoff_confirmed = bool(
        physical_cutoff_confirmed
        or magnitude_cooling_confirmed
        or time_cutoff_confirmed
    )
    return {
        "schema": "metar_recoil_physical_cutoff_evidence_v1",
        "source": "amsc_metar_hard_observation_path_inside_metar_strong_likelihood",
        "weather_cloud_text": weather_cloud_text.strip(),
        "cb_observed": cb_observed,
        "thunder_observed": thunder_observed,
        "showery_rain_observed": shower_observed,
        "rain_observed": rain_observed,
        "convective_or_rain_cutoff": convective_or_rain_cutoff,
        "outflow_wind_observed": wind_outflow_observed,
        "pressure_jump_observed": pressure_jump_observed,
        "visibility_deterioration": visibility_deterioration,
        "low_cloud_deterioration": low_cloud_deterioration,
        "rolling_platform_end_confirmed": platform_end_confirmed,
        "stair_step_cooling": stair_step_cooling,
        "time_cutoff_confirmed": time_cutoff_confirmed,
        "physical_cutoff_confirmed": physical_cutoff_confirmed,
        "physical_cutoff_confirmed_raw": raw_physical_cutoff_confirmed,
        "magnitude_cooling_confirmed": magnitude_cooling_confirmed,
        "magnitude_cooling_confirmed_raw": raw_magnitude_cooling_confirmed,
        "true_cooling_or_cutoff_confirmed": true_cooling_or_cutoff_confirmed,
        "pre_dawn_carryover_phase": pre_dawn_carryover_phase,
        "pre_dawn_carryover_peak": pre_dawn_carryover_peak,
        "observed_calendar_day_max_at": observed_max_at,
        "observed_calendar_day_max_hour_local": _round_float(observed_max_hour),
        "observed_calendar_day_max_origin": (
            "previous_solar_day_thermal_carryover_after_midnight"
            if pre_dawn_carryover_peak
            else "current_daytime_or_unspecified"
        ),
        "daytime_peak_cutoff_eligible": not pre_dawn_carryover_phase,
        "pre_dawn_cutoff_suppressed": bool(
            pre_dawn_carryover_phase
            and (raw_physical_cutoff_confirmed or raw_magnitude_cooling_confirmed)
        ),
        "pre_dawn_cutoff_suppression_reason": (
            "post_midnight_cooling_is_previous_solar_day_thermal_carryover_not_current_daytime_peak_cutoff"
            if pre_dawn_carryover_phase
            else ""
        ),
        "evidence_codes": evidence_codes,
        "evidence_count": len(evidence_codes),
        "pressure_jump_hpa": _round_float(pressure_jump_hpa),
        "pressure_jump_threshold_hpa": _round_float(pressure_jump_threshold),
        "wind_speed_jump_mps": _round_float(wind_speed_jump),
        "wind_direction_shift_degrees": _round_float(wind_shift),
        "wind_gust_mps": _round_float(current_gust),
        "visibility_drop_m": _round_float(visibility_drop_m),
        "low_cloud_jump_pct": _round_float(low_cloud_jump_pct),
        "peak_window_end_hour_local": _round_float(peak_end),
        "time_grace_hours": _round_float(time_grace_hours),
        "policy": "recoil_requires_no_physical_cutoff_evidence",
    }


def _platform_recoil_meta(
    inputs: Mapping[str, Any],
    observation_features: Mapping[str, Any],
    temperature_trajectory: Mapping[str, Any],
    *,
    current_temp_c: float | None,
    observed_today_max_c: float | None,
    local_hour: float | None,
    peak_window: Mapping[str, Any] | None,
) -> dict[str, Any]:
    physical_cutoff = _physical_cutoff_evidence(
        inputs,
        observation_features,
        temperature_trajectory,
        local_hour=local_hour,
        peak_window=peak_window,
    )

    def result_for_state(state: str, reason: str, **extra: Any) -> dict[str, Any]:
        out = {
            "active": state == "platform_noise",
            "reason": reason,
            "recoil_state": state,
            "recoil_state_label": RECOIL_STATE_LABELS.get(state, state),
            "recoil_action": RECOIL_ACTIONS.get(state, "unknown"),
            "physical_cutoff_evidence": physical_cutoff,
            "physical_cutoff_confirmed": bool(physical_cutoff.get("physical_cutoff_confirmed")),
            "true_cooling_or_cutoff_confirmed": bool(physical_cutoff.get("true_cooling_or_cutoff_confirmed")),
            "momentum_treatment": (
                "carryover_not_daytime_peak"
                if state == "pre_dawn_carryover"
                else "protect_high_tail"
                if state == "platform_noise"
                else "lower_confidence_without_reversing"
                if state == "pullback_watch"
                else "cooling_or_cutoff_confirmed"
                if state == "confirmed_cooling_cutoff"
                else "not_applicable"
            ),
            "policy": "integer_metar_recoil_three_state_physical_cutoff_inside_metar_strong_likelihood",
        }
        out.update(extra)
        return out

    if current_temp_c is None or observed_today_max_c is None or local_hour is None:
        return result_for_state("unknown", "missing_current_observed_max_or_hour")
    if bool(observation_features.get("observed_today_max_final")):
        return result_for_state("final_lock", "final_observed_today_max_locks_no_recoil")
    rolling = (
        temperature_trajectory.get("rolling_temperature_observation_summary")
        if isinstance(temperature_trajectory.get("rolling_temperature_observation_summary"), Mapping)
        else {}
    )
    source_text = " ".join(
        str(value or "")
        for value in (
            inputs.get("temperature_path_source"),
            inputs.get("temperature_momentum_source"),
            inputs.get("observed_today_max_source"),
            inputs.get("observed_temp_source"),
        )
    ).strip().lower()
    hard_source = bool(
        PRIMARY_METAR_SOURCE_ID.lower() in source_text
        or "amsc" in source_text
        or "awc" in source_text
        or "metar" in source_text
        or isinstance(inputs.get("_source_observation_temperature_points"), list)
    )
    if not hard_source:
        return result_for_state("not_applicable", "hard_metar_source_missing")
    if bool(physical_cutoff.get("pre_dawn_carryover_peak")):
        return result_for_state(
            "pre_dawn_carryover",
            "pre_dawn_carryover_lower_bound_not_daytime_peak_recoil",
            pre_dawn_carryover=True,
            settlement_calendar_day_lower_bound=True,
            daytime_peak_platform_formed=False,
            hour_local=_round_float(float(local_hour)),
        )
    latest_drop = _to_float(rolling.get("latest_drop_from_rolling_max_c"))
    if latest_drop is None:
        latest_drop = max(0.0, float(observed_today_max_c) - float(current_temp_c))
    max_allowed_drop = max(0.0, _env_float("PM_BAYES_PLATFORM_RECOIL_MAX_DROP_C", 1.0))
    window = peak_window if isinstance(peak_window, Mapping) else {}
    peak_end = _to_float(window.get("end_hour_local"))
    grace_hours = max(0.0, _env_float("PM_BAYES_PLATFORM_RECOIL_PEAK_WINDOW_GRACE_HOURS", 0.25))
    trajectory_state = str(
        rolling.get("state")
        or temperature_trajectory.get("state")
        or observation_features.get("temperature_trajectory_state")
        or ""
    ).strip().lower()
    common = {
        "latest_drop_from_high_c": _round_float(float(latest_drop)),
        "max_allowed_drop_c": _round_float(float(max_allowed_drop)),
        "hour_local": _round_float(float(local_hour)),
        "peak_window_end_hour_local": _round_float(float(peak_end)) if peak_end is not None else None,
        "peak_window_grace_hours": _round_float(float(grace_hours)),
        "trajectory_state": trajectory_state or "unknown",
    }
    if bool(physical_cutoff.get("true_cooling_or_cutoff_confirmed")):
        reason = (
            "physical_cutoff_evidence_confirmed"
            if physical_cutoff.get("physical_cutoff_confirmed")
            else "rolling_or_time_cooling_confirmed"
        )
        return result_for_state("confirmed_cooling_cutoff", reason, **common)
    if peak_end is not None and float(local_hour) > float(peak_end) + float(grace_hours):
        return result_for_state("confirmed_cooling_cutoff", "peak_window_already_closed", **common)
    platform_like = trajectory_state in {"", "platform", "flat", "mixed", "weak_warming", "sustained_warming"}
    if not platform_like:
        if float(latest_drop) >= METAR_PLATFORM_END_DROP_C - EPS:
            return result_for_state("pullback_watch", "trajectory_state_not_platform_like_but_cooling_unconfirmed", **common)
        return result_for_state("not_applicable", "trajectory_state_not_platform_like", **common)
    if float(latest_drop) > float(max_allowed_drop):
        return result_for_state(
            "pullback_watch",
            "latest_temperature_below_platform_high_but_not_confirmed_cooling",
            **common,
        )
    residual_c = max(0.0, _env_float("PM_BAYES_PLATFORM_RECOIL_RESIDUAL_C", 0.55))
    residual_floor_c = float(observed_today_max_c) + float(residual_c)
    return result_for_state(
        "platform_noise",
        "hard_metar_near_high_platform_before_peak_window_end",
        source="hard_metar_platform_recoil",
        residual_c=_round_float(float(residual_c)),
        residual_floor_c=_round_float(float(residual_floor_c)),
        **common,
    )


def _features_from_inputs(inputs: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    na_reasons: list[str] = []
    current_temp = _to_float(inputs.get("current_temp_c"))
    dewpoint = _to_float(inputs.get("dewpoint_c"))
    observed_max = _first_input_float(
        inputs,
        "observed_today_max_c",
        "today_max_c",
        "settlement_actual",
        "settlement_actual_c",
        "official_final_result",
        "final_daily_max_c",
    )
    visibility_m = _to_float(inputs.get("visibility_m"))
    if visibility_m is None:
        visibility_text = str(inputs.get("visibility") or "").strip()
        if visibility_text == "9999":
            visibility_m = 9999.0
        else:
            visibility_m = _to_float(visibility_text)
    cloud_cover = _to_float(inputs.get("cloud_cover_pct"))
    low_cloud = _to_float(inputs.get("low_cloud_cover_pct"))
    wind_speed_mps = _to_float(inputs.get("wind_speed_mps"))
    wind_dir_degrees = _first_input_float(inputs, "wind_dir_degrees", "wind_direction_degrees", "wind_direction", "wind_dir")
    wind_gust_mps = _first_input_float(inputs, "wind_gust_mps", "gust_mps", "wind_gust", "windGust")
    pressure_hpa = _first_input_float(inputs, "pressure_hpa", "altimeter_hpa", "pressure")
    dewpoint_trend = _first_input_float(inputs, "dewpoint_trend_c_per_hour", "dewpoint_trend_c")
    momentum = _first_input_float(
        inputs,
        "momentum_rate_c_per_hour",
        "temperature_rate_c_per_hour",
        "temperature_rise_rate_c_per_hour",
        "observed_temp_rise_rate_c_per_hour",
        "metar_temp_rise_rate_c_per_hour",
    )
    if momentum is None:
        momentum_rates, suppressed_momentum_rates = _momentum_rates_from_inputs(inputs)
        if not suppressed_momentum_rates:
            momentum = _weighted_rate(momentum_rates, TEMPERATURE_MOMENTUM_CONFIRM_WINDOWS)
        if momentum is None:
            momentum = _weighted_rate(momentum_rates, TEMPERATURE_MOMENTUM_RECENT_WINDOWS)
    weather_text = str(inputs.get("weather_text") or inputs.get("weather") or "").upper()
    cloud_text = str(inputs.get("cloud_text") or inputs.get("clouds") or "").upper()
    observed_today_max_final = any(
        _to_bool(inputs.get(key))
        for key in (
            "hard_source_final_daily_max",
            "hard_source_settlement_final",
            "observed_today_max_final",
            "observed_today_max_is_final",
            "observed_today_max_is_official",
            "observed_today_max_trusted_final",
            "temperature_observation_final_daily_max",
            "no_more_warming_possible",
        )
    ) or any(
        _to_float(inputs.get(key)) is not None
        for key in (
            "settlement_actual",
            "settlement_actual_c",
            "official_final_result",
            "final_daily_max_c",
        )
    )

    if current_temp is None and observed_max is None:
        na_reasons.append("current_temp_and_observed_today_max_missing")
    if current_temp is None:
        current_temp = observed_max
    if observed_max is None:
        observed_max = current_temp
    if dewpoint is None or current_temp is None:
        spread = _to_float(inputs.get("temp_dewpoint_spread_c"))
        if spread is None:
            na_reasons.append("temp_dewpoint_spread_missing")
            spread = 4.0
    else:
        spread = current_temp - dewpoint
    if cloud_cover is None:
        if "OVC" in cloud_text:
            cloud_cover = 92.0
        elif "BKN" in cloud_text:
            cloud_cover = 75.0
        elif "SCT" in cloud_text:
            cloud_cover = 38.0
        elif "FEW" in cloud_text:
            cloud_cover = 15.0
        elif "CLR" in cloud_text or "SKC" in cloud_text:
            cloud_cover = 5.0
        else:
            cloud_cover = 45.0
            na_reasons.append("cloud_cover_missing_defaulted")
    if low_cloud is None:
        low_cloud = cloud_cover
    if visibility_m is None:
        visibility_m = 9000.0
        na_reasons.append("visibility_missing_defaulted")
    if dewpoint_trend is None:
        dewpoint_trend = 0.0
        na_reasons.append("dewpoint_trend_missing_defaulted")
    if momentum is None:
        momentum = 0.0
        na_reasons.append("momentum_rate_missing_defaulted")

    precip = any(token in weather_text for token in ("RA", "SHRA", "TS", "DZ"))
    convective = any(token in weather_text or token in cloud_text for token in ("TS", "CB", "SHRA"))
    br_fg_hz = any(token in weather_text for token in ("BR", "FG", "HZ"))
    visibility_good = visibility_m >= 9000.0
    visibility_poor = visibility_m < 8000.0 or br_fg_hz
    no_precip = not precip and not convective
    high_dewpoint = bool(dewpoint is not None and dewpoint >= 23.0)

    return (
        {
            "current_temp_c": current_temp,
            "dewpoint_c": dewpoint,
            "observed_today_max_c": observed_max,
            "temp_dewpoint_spread_c": spread,
            "visibility_m": visibility_m,
            "cloud_cover_pct": cloud_cover,
            "low_cloud_cover_pct": low_cloud,
            "wind_speed_mps": wind_speed_mps,
            "wind_dir_degrees": wind_dir_degrees,
            "wind_gust_mps": wind_gust_mps,
            "pressure_hpa": pressure_hpa,
            "dewpoint_trend_c": dewpoint_trend,
            "momentum_rate_c_per_hour": momentum,
            "observed_today_max_final": observed_today_max_final,
            "weather_text": weather_text,
            "cloud_text": cloud_text,
            "visibility_good": visibility_good,
            "visibility_poor": visibility_poor,
            "no_precip_confirmed": no_precip,
            "precip_observed": precip,
            "showery_or_convective": convective,
            "drying": dewpoint_trend < -0.5,
            "moistening": dewpoint_trend > 0.5,
            "high_dewpoint": high_dewpoint,
        },
        na_reasons,
    )


def infer_environment_state_probs(observation_features: Mapping[str, Any]) -> dict[str, float]:
    cloud = _clamp(_to_float(observation_features.get("cloud_cover_pct")) or 45.0, 0.0, 100.0)
    low_cloud = _clamp(_to_float(observation_features.get("low_cloud_cover_pct")) or cloud, 0.0, 100.0)
    clear_score = _clamp(1.0 - cloud / 100.0, 0.0, 1.0)
    low_cloud_score = _clamp(low_cloud / 80.0, 0.0, 1.0)
    visibility_good = 1.0 if observation_features.get("visibility_good") else 0.0
    visibility_poor = 1.0 if observation_features.get("visibility_poor") else 0.0
    no_precip = 1.0 if observation_features.get("no_precip_confirmed") else 0.0
    precip = 1.0 if observation_features.get("precip_observed") else 0.0
    convective = 1.0 if observation_features.get("showery_or_convective") else 0.0
    physical_cutoff = 1.0 if observation_features.get("physical_cutoff_confirmed") else 0.0
    true_cooling_or_cutoff = 1.0 if observation_features.get("true_cooling_or_cutoff_confirmed") else 0.0
    spread = _to_float(observation_features.get("temp_dewpoint_spread_c"))
    if spread is None:
        spread = 4.0
    spread_open = _clamp((spread - 3.0) / 5.0, 0.0, 1.0)
    spread_closed = _clamp((4.0 - spread) / 4.0, 0.0, 1.0)
    drying = 1.0 if observation_features.get("drying") else 0.0
    moistening = 1.0 if observation_features.get("moistening") else 0.0
    high_dewpoint = 1.0 if observation_features.get("high_dewpoint") else 0.0
    scores = {
        "S0_wet_layer_closed": 0.25 + 1.20 * low_cloud_score + 1.20 * spread_closed + 0.70 * visibility_poor + 0.85 * precip + 0.35 * moistening + 0.25 * high_dewpoint - 0.30 * visibility_good,
        "S1_opening": 0.55 + 0.55 * visibility_good + 0.45 * drying + 0.45 * no_precip + 0.35 * (1.0 - abs(clear_score - 0.45)) + 0.20 * spread_open - 0.25 * precip,
        "S2_open_boundary_layer": 0.10 + 1.20 * clear_score + 0.90 * spread_open + 0.65 * visibility_good + 0.40 * no_precip + 0.35 * drying - 0.80 * low_cloud_score - 1.00 * precip,
        "S3_convective_cutoff": -0.20 + 2.00 * convective + 1.15 * precip + 1.40 * physical_cutoff + 0.85 * true_cooling_or_cutoff + 0.45 * visibility_poor + 0.30 * high_dewpoint - 0.25 * no_precip,
    }
    return _softmax(scores)


def remaining_warming_params(*, state: str, local_hour: float, momentum_rate_c_per_hour: float) -> tuple[float, float]:
    params = remaining_warming_params_with_trajectory(
        state=state,
        local_hour=local_hour,
        momentum_rate_c_per_hour=momentum_rate_c_per_hour,
        temperature_trajectory=None,
    )
    return float(params["delta_mu_c"]), float(params["sigma_c"])


def remaining_warming_params_with_trajectory(
    *,
    state: str,
    local_hour: float,
    momentum_rate_c_per_hour: float,
    temperature_trajectory: Mapping[str, Any] | None = None,
    peak_window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    trajectory = temperature_trajectory if isinstance(temperature_trajectory, Mapping) else {}
    window = peak_window if isinstance(peak_window, Mapping) else {}
    trajectory_state = str(trajectory.get("state") or "").strip().lower()
    adjusted_rate = _to_float(trajectory.get("adjusted_rate_c_per_hour"))
    response_scale = _to_float(trajectory.get("response_scale"))
    confidence = _to_float(trajectory.get("confidence"))
    if adjusted_rate is None:
        adjusted_rate = float(momentum_rate_c_per_hour)
    if response_scale is None:
        response_scale = 1.0
    if confidence is None:
        confidence = 0.0 if trajectory_state == "missing" else 0.45

    peak_start = _to_float(window.get("start_hour_local"))
    peak_end = _to_float(window.get("end_hour_local"))
    peak_tail_hour = float(peak_end) if peak_end is not None else 17.5
    remaining_hours = _clamp(peak_tail_hour - local_hour, 0.0, 8.5)
    rate_up = max(0.0, float(adjusted_rate))
    rate_down = max(0.0, -float(adjusted_rate))
    rate_horizon = min(remaining_hours, 3.0)
    if state == "S0_wet_layer_closed":
        mu = 0.15 + 0.23 * remaining_hours + 0.10 * rate_up * rate_horizon - 0.20 * rate_down
        sigma = 0.85
    elif state == "S1_opening":
        mu = 0.45 + 0.42 * remaining_hours + 0.38 * rate_up * rate_horizon - 0.15 * rate_down
        sigma = 1.05
    elif state == "S2_open_boundary_layer":
        mu = 0.75 + 0.58 * remaining_hours + 0.55 * rate_up * rate_horizon - 0.10 * rate_down
        sigma = 1.15
    elif state == "S3_convective_cutoff":
        mu = 0.05 + 0.12 * remaining_hours + 0.08 * rate_up * rate_horizon - 0.35 * rate_down
        sigma = 0.95
    else:
        mu = 0.35 + 0.30 * remaining_hours + 0.25 * rate_up * rate_horizon
        sigma = 1.10
    taper_start = max(12.5, peak_tail_hour - 3.5)
    if local_hour >= taper_start:
        mu *= _clamp((peak_tail_hour - local_hour) / max(0.5, peak_tail_hour - taper_start), 0.05, 1.0)
        sigma = max(0.55, sigma - 0.10)
    elif local_hour <= 10.0:
        sigma += 0.25
    if trajectory_state == "short_window_only":
        mu *= 0.88
        sigma += 0.22
    elif trajectory_state == "midday_recent_confirmation_missing":
        mu *= 0.62
        sigma += 0.18
    elif trajectory_state == "pre_peak_integer_plateau":
        mu *= 0.68
        sigma += 0.12
    elif trajectory_state in {"flat", "not_realized"}:
        mu *= 0.72
        sigma += 0.12
    elif trajectory_state == "platform":
        mu *= 0.45
        sigma = max(0.55, sigma - 0.08)
    elif trajectory_state == "cooling":
        mu *= 0.55
        sigma = max(0.60, sigma - 0.05)
    elif trajectory_state == "mixed":
        mu *= 0.92
        sigma += 0.18
    elif trajectory_state in {"sustained_warming", "emerging_warming"} and confidence >= 0.70:
        sigma = max(0.55, sigma - 0.08)
    cap = 1.0 + 1.15 * remaining_hours
    return {
        "delta_mu_c": _clamp(mu, -0.25, cap),
        "sigma_c": sigma,
        "remaining_hours": remaining_hours,
        "peak_window_start_hour_local": float(peak_start) if peak_start is not None else None,
        "peak_window_end_hour_local": float(peak_end) if peak_end is not None else None,
        "hours_to_peak_window": remaining_hours,
        "momentum_rate_c_per_hour": float(momentum_rate_c_per_hour),
        "trajectory_adjusted_rate_c_per_hour": float(adjusted_rate),
        "trajectory_response_scale": float(response_scale),
        "temperature_trajectory_state": trajectory_state or None,
        "temperature_trajectory_confidence": float(confidence),
    }


def _standard_normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0)))


def _termination_risk_from_remaining_distribution(
    *,
    remaining_rows: Sequence[Mapping[str, Any]],
    observed_today_max_c: float | None,
    current_temperature_c: float | None,
    physical_cutoff_evidence: Mapping[str, Any],
    source_trace: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Read ``P(no material new high)`` from the already-built strong mixture.

    This is deliberately a *derived marginal* of the same state mixture that
    forms the strong likelihood.  It must never be multiplied back into the
    posterior as a second likelihood, otherwise cloud/rain/momentum evidence
    would be counted twice.
    """

    reference = observed_today_max_c
    if reference is None:
        reference = current_temperature_c
    if reference is None or not remaining_rows:
        return {
            "schema": TERMINATION_RISK_SCHEMA_VERSION,
            "state": "not_available",
            "probability_no_material_new_daily_high": None,
            "na_reason": "remaining_warming_distribution_or_reference_temperature_missing",
            "derived_from_same_remaining_distribution_not_second_likelihood": True,
        }
    threshold = float(reference) + TERMINATION_RISK_MATERIAL_WARMING_C
    components: list[dict[str, Any]] = []
    probability = 0.0
    for raw in remaining_rows:
        row = dict(raw) if isinstance(raw, Mapping) else {}
        state_prob = _to_float(row.get("state_prob"))
        peak_mu = _to_float(row.get("peak_mu_c"))
        sigma = _to_float(row.get("sigma_c"))
        if state_prob is None or peak_mu is None or sigma is None or sigma <= 0.0:
            continue
        conditional = _clamp(_standard_normal_cdf((threshold - float(peak_mu)) / float(sigma)), 0.0, 1.0)
        contribution = float(state_prob) * conditional
        probability += contribution
        components.append(
            {
                "state": str(row.get("state") or ""),
                "state_probability": _round_float(state_prob),
                "conditional_termination_probability": _round_float(conditional),
                "mixture_contribution": _round_float(contribution),
            }
        )
    included_report_types = sorted(
        {
                str(
                    (dict(row.get("payload")) if isinstance(row.get("payload"), Mapping) else {}).get("report_type")
                    or row.get("report_type")
                    or ""
                ).strip().upper()
            for row in source_trace
            if isinstance(row, Mapping) and row.get("included") is True
        }
        & {"METAR", "SPECI"}
    )
    cutoff = dict(physical_cutoff_evidence) if isinstance(physical_cutoff_evidence, Mapping) else {}
    return {
        "schema": TERMINATION_RISK_SCHEMA_VERSION,
        "state": "available" if components else "not_available",
        "probability_no_material_new_daily_high": _round_float(_clamp(probability, 0.0, 1.0)) if components else None,
        "event_definition": "no_material_new_daily_high_before_local_day_close",
        "material_new_high_threshold_c": TERMINATION_RISK_MATERIAL_WARMING_C,
        "reference_observed_high_c": _round_float(reference),
        "termination_ceiling_c": _round_float(threshold),
        "components": components,
        "physical_evidence_codes": list(cutoff.get("evidence_codes") or []),
        "physical_cutoff_confirmed": bool(cutoff.get("physical_cutoff_confirmed")),
        "true_cooling_or_cutoff_confirmed": bool(cutoff.get("true_cooling_or_cutoff_confirmed")),
        "included_report_types": included_report_types,
        "speci_accelerated": "SPECI" in included_report_types,
        "derived_from_same_remaining_distribution_not_second_likelihood": True,
        "boundary": {
            "inside_unique_strong_likelihood": True,
            "does_not_change_final_x_definition": True,
            "does_not_create_hard_mask": True,
            "does_not_multiply_posterior_again": True,
        },
    }


def build_metar_strong_likelihood(
    *,
    buckets: Sequence[Any],
    observation_inputs: Mapping[str, Any] | None = None,
    source_observations: Iterable[Any] | None = None,
    as_of: Any = None,
    station_timezone: str = DEFAULT_STATION_TIMEZONE,
    rounding_rule: str = "rounded_degrees",
    event_date: str = "",
    allow_legacy_v31_probability_overlay: bool = False,
) -> dict[str, Any]:
    labels = [str(item.get("bucket") if isinstance(item, Mapping) else item).strip() for item in buckets or []]
    labels = [label for label in labels if label]
    source_rows = list(source_observations or [])
    merged_inputs, source_trace = _merge_observation_inputs(
        observation_inputs,
        source_rows,
        event_date=str(event_date or ""),
        station_timezone=station_timezone,
    )
    features, na_reasons = _features_from_inputs(merged_inputs)
    local_hour = _to_float(merged_inputs.get("local_hour"))
    if local_hour is None:
        local_hour = _station_local_hour(as_of, station_timezone)
    peak_window = _peak_window_from_inputs(merged_inputs, station_timezone=station_timezone)
    temperature_trajectory = build_temperature_trajectory_summary(
        merged_inputs,
        local_hour=float(local_hour),
        fallback_momentum_rate_c_per_hour=_to_float(features.get("momentum_rate_c_per_hour")),
        peak_window=peak_window,
    )
    trajectory_na_reasons = temperature_trajectory.get("na_reasons")
    if isinstance(trajectory_na_reasons, list):
        na_reasons.extend(str(reason) for reason in trajectory_na_reasons if str(reason))
    features.update(
        {
            "temperature_trajectory_state": temperature_trajectory.get("state"),
            "temperature_trajectory_gate_state": temperature_trajectory.get("gate_state"),
            "temperature_trajectory_gate_reason": temperature_trajectory.get("gate_reason"),
            "temperature_trajectory_stable_rate_c_per_hour": temperature_trajectory.get("stable_rate_c_per_hour"),
            "temperature_trajectory_recent_rate_c_per_hour": temperature_trajectory.get("recent_rate_c_per_hour"),
            "temperature_trajectory_short_rate_c_per_hour": temperature_trajectory.get("short_rate_c_per_hour"),
            "temperature_trajectory_background_rate_c_per_hour": temperature_trajectory.get("background_rate_c_per_hour"),
            "temperature_trajectory_effective_rate_c_per_hour": temperature_trajectory.get("effective_rate_c_per_hour"),
            "temperature_trajectory_adjusted_rate_c_per_hour": temperature_trajectory.get("adjusted_rate_c_per_hour"),
            "temperature_trajectory_response_scale": temperature_trajectory.get("response_scale"),
            "temperature_trajectory_confidence": temperature_trajectory.get("confidence"),
            "expected_peak_hour_local": peak_window.get("center_hour_local"),
            "expected_peak_window_start_hour_local": peak_window.get("start_hour_local"),
            "expected_peak_window_end_hour_local": peak_window.get("end_hour_local"),
            "hours_to_peak_window": _round_float(
                _clamp(float(_to_float(peak_window.get("end_hour_local")) or 17.5) - float(local_hour), 0.0, 8.5)
            ),
        }
    )
    current_temp = _to_float(features.get("current_temp_c"))
    observed_max = _to_float(features.get("observed_today_max_c"))
    observed_today_max_final = bool(features.get("observed_today_max_final")) and observed_max is not None
    platform_recoil = _platform_recoil_meta(
        merged_inputs,
        features,
        temperature_trajectory,
        current_temp_c=current_temp,
        observed_today_max_c=observed_max,
        local_hour=float(local_hour) if local_hour is not None else None,
        peak_window=peak_window,
    )
    features.update(
        {
            "platform_recoil_active": bool(platform_recoil.get("active")),
            "platform_recoil_reason": platform_recoil.get("reason"),
            "recoil_state": platform_recoil.get("recoil_state"),
            "recoil_state_label": platform_recoil.get("recoil_state_label"),
            "recoil_action": platform_recoil.get("recoil_action"),
            "recoil_momentum_treatment": platform_recoil.get("momentum_treatment"),
            "physical_cutoff_confirmed": bool(platform_recoil.get("physical_cutoff_confirmed")),
            "true_cooling_or_cutoff_confirmed": bool(platform_recoil.get("true_cooling_or_cutoff_confirmed")),
            "physical_cutoff_evidence_codes": list(
                platform_recoil.get("physical_cutoff_evidence", {}).get("evidence_codes", [])
            )
            if isinstance(platform_recoil.get("physical_cutoff_evidence"), Mapping)
            else [],
            "physical_cutoff_evidence_count": platform_recoil.get("physical_cutoff_evidence", {}).get("evidence_count")
            if isinstance(platform_recoil.get("physical_cutoff_evidence"), Mapping)
            else 0,
            "platform_recoil_residual_c": platform_recoil.get("residual_c"),
            "platform_recoil_residual_floor_c": platform_recoil.get("residual_floor_c"),
            "platform_recoil_latest_drop_from_high_c": platform_recoil.get("latest_drop_from_high_c"),
        }
    )
    # Remaining warming starts from the current air temperature.  The already
    # observed daily maximum is a separate hard floor on the final peak; using
    # max(current, observed_max) as the trajectory base double-counts a prior
    # peak and prevents the platform-recoil floor from doing its intended job.
    base_temp = current_temp if current_temp is not None else observed_max
    state_probs = infer_environment_state_probs(features)
    likelihood = [0.0 for _ in labels]
    remaining_rows: list[dict[str, Any]] = []
    hard_mask = [1.0 for _ in labels]
    if not labels:
        na_reasons.append("bucket_labels_missing")
    if base_temp is None:
        likelihood = [1.0 / len(labels) for _ in labels] if labels else []
    else:
        for state in STATE_ORDER:
            state_prob = state_probs.get(state, 0.0)
            warming_params = remaining_warming_params_with_trajectory(
                state=state,
                local_hour=float(local_hour),
                momentum_rate_c_per_hour=float(features.get("momentum_rate_c_per_hour") or 0.0),
                temperature_trajectory=temperature_trajectory,
                peak_window=peak_window,
            )
            raw_delta_mu = float(warming_params["delta_mu_c"])
            delta_mu = raw_delta_mu
            sigma = float(warming_params["sigma_c"])
            recoil_state = str(platform_recoil.get("recoil_state") or "")
            if recoil_state == "pullback_watch":
                sigma += 0.18
            elif recoil_state == "confirmed_cooling_cutoff" and state != "S3_convective_cutoff":
                delta_mu *= 0.72
                sigma = max(0.55, sigma - 0.05)
            peak_mu = max(float(observed_max if observed_max is not None else base_temp), float(base_temp) + delta_mu)
            recoil_applied = False
            recoil_floor = _to_float(platform_recoil.get("residual_floor_c"))
            if platform_recoil.get("active") and recoil_floor is not None and peak_mu < float(recoil_floor):
                peak_mu = float(recoil_floor)
                delta_mu = max(0.0, float(peak_mu) - float(base_temp))
                recoil_applied = True
            remaining_rows.append(
                {
                    "state": state,
                    "state_prob": _round_float(state_prob),
                    "delta_mu_c": _round_float(delta_mu),
                    "raw_delta_mu_c": _round_float(raw_delta_mu),
                    "sigma_c": _round_float(sigma),
                    "peak_mu_c": _round_float(peak_mu),
                    "platform_recoil_active": bool(platform_recoil.get("active")),
                    "platform_recoil_applied": bool(recoil_applied),
                    "platform_recoil_reason": platform_recoil.get("reason"),
                    "platform_recoil_residual_floor_c": _round_float(recoil_floor),
                    "recoil_state": platform_recoil.get("recoil_state"),
                    "recoil_state_label": platform_recoil.get("recoil_state_label"),
                    "recoil_action": platform_recoil.get("recoil_action"),
                    "recoil_momentum_treatment": platform_recoil.get("momentum_treatment"),
                    "recoil_physical_cutoff_confirmed": bool(platform_recoil.get("physical_cutoff_confirmed")),
                    "recoil_true_cooling_or_cutoff_confirmed": bool(
                        platform_recoil.get("true_cooling_or_cutoff_confirmed")
                    ),
                    "recoil_evidence_codes": list(
                        platform_recoil.get("physical_cutoff_evidence", {}).get("evidence_codes", [])
                    )
                    if isinstance(platform_recoil.get("physical_cutoff_evidence"), Mapping)
                    else [],
                    "momentum_rate_c_per_hour": _round_float(_to_float(warming_params.get("momentum_rate_c_per_hour"))),
                    "trajectory_adjusted_rate_c_per_hour": _round_float(
                        _to_float(warming_params.get("trajectory_adjusted_rate_c_per_hour"))
                    ),
                    "trajectory_response_scale": _round_float(_to_float(warming_params.get("trajectory_response_scale"))),
                    "temperature_trajectory_state": warming_params.get("temperature_trajectory_state"),
                    "temperature_trajectory_confidence": _round_float(
                        _to_float(warming_params.get("temperature_trajectory_confidence"))
                    ),
                    "remaining_hours": _round_float(_to_float(warming_params.get("remaining_hours"))),
                    "peak_window_start_hour_local": _round_float(
                        _to_float(warming_params.get("peak_window_start_hour_local"))
                    ),
                    "peak_window_end_hour_local": _round_float(
                        _to_float(warming_params.get("peak_window_end_hour_local"))
                    ),
                    "hours_to_peak_window": _round_float(_to_float(warming_params.get("hours_to_peak_window"))),
                }
            )
            final_lock_match_found = False
            if observed_today_max_final and observed_max is not None:
                for idx, label in enumerate(labels):
                    bounds = _bucket_bounds(label, rounding_rule=rounding_rule)
                    if bounds is None:
                        continue
                    low, high = bounds
                    if low <= float(observed_max) < high:
                        final_lock_match_found = True
                    else:
                        hard_mask[idx] = 0.0
                if not final_lock_match_found:
                    na_reasons.append("observed_today_max_final_bucket_not_matched")
            for idx, label in enumerate(labels):
                bounds = _bucket_bounds(label, rounding_rule=rounding_rule)
                if bounds is None:
                    likelihood[idx] += state_prob / max(1, len(labels))
                    continue
                low, high = bounds
                if observed_today_max_final and final_lock_match_found:
                    prob = 1.0 if low <= float(observed_max) < high else 0.0
                elif observed_max is not None and high <= observed_max:
                    prob = 0.0
                    hard_mask[idx] = 0.0
                else:
                    prob = _normal_interval_prob(low, high, mu=peak_mu, sigma=sigma)
                likelihood[idx] += state_prob * prob
        likelihood = _normalize(likelihood)

    termination_risk = _termination_risk_from_remaining_distribution(
        remaining_rows=remaining_rows,
        observed_today_max_c=observed_max,
        current_temperature_c=current_temp,
        physical_cutoff_evidence=(
            platform_recoil.get("physical_cutoff_evidence")
            if isinstance(platform_recoil.get("physical_cutoff_evidence"), Mapping)
            else {}
        ),
        source_trace=source_trace,
    )

    hard_mask_reason_parts: list[str] = []
    if observed_max is not None:
        hard_mask_reason_parts.append("observed_today_max_c")
    if current_temp is not None:
        hard_mask_reason_parts.append("current_temperature_c")
    if observed_today_max_final:
        hard_mask_reason_parts.append("official_final_result")
        hard_mask_reason_parts.append("final_daily_max_bucket_lock")
    masked_buckets = [label for label, mask in zip(labels, hard_mask) if mask <= EPS]
    if masked_buckets:
        hard_mask_reason_parts.append("masked_bucket_floor")
    candidate_rows: list[Any] = source_rows
    if not candidate_rows:
        candidate_rows = [
            {
                **dict(point),
                "source_id": str(point.get("source_id") or PRIMARY_METAR_SOURCE_ID),
                "source_class": SOURCE_CLASSES["hard_observation"],
                "fetched_at": str(point.get("fetched_at") or as_of or ""),
                "parsed_at": str(point.get("parsed_at") or as_of or ""),
            }
            for point in (merged_inputs.get("_source_observation_temperature_points") or [])
            if isinstance(point, Mapping)
        ]
    try:
        observation_candidate = build_zuuu_daily_high_candidate(
            buckets=labels,
            source_observations=candidate_rows,
            as_of=as_of,
            event_date=str(event_date or ""),
        )
    except Exception as exc:
        # The candidate is shadow-only; it must never change or block the
        # existing production strong-likelihood result.
        observation_candidate = {
            "schema": "zuuu_daily_high_observation_candidate_v1",
            "candidate_only": True,
            "production_applied": False,
            "error": f"candidate_build_failed:{type(exc).__name__}",
            "guards": _guards(),
        }
    # Compatibility trace only.  The retired v3.1 probability model is not
    # imported, evaluated or allowed to alter the physical strong likelihood.
    v31_multimodel_posterior = {
        "schema": "retired_probability_model_trace_v1",
        "state": "retired_not_consumed",
        "state_reason": "physical_strong_likelihood_uses_only_observation_trajectory_momentum_and_weather_evidence",
        "activation_mode": "retired",
        "applied_to_metar_strong_likelihood": False,
    }
    v31_live_applied = False
    return {
        "schema": SCHEMA_VERSION,
        "runtime_policy_version": RUNTIME_POLICY_VERSION,
        "ok": bool(labels),
        "read_only": True,
        "production_entry_enabled": False,
        "guards": _guards(),
        "event_date": str(event_date or ""),
        "as_of": str(as_of or ""),
        "station_timezone": str(station_timezone or DEFAULT_STATION_TIMEZONE),
        "rounding_rule": str(rounding_rule or ""),
        "metar_strong_likelihood": [
            {"bucket": label, "metar_strong_likelihood": _round_float(value)} for label, value in zip(labels, likelihood)
        ],
        "metar_strong_likelihood_map": _distribution_map(labels, likelihood),
        "state_probs": {state: _round_float(state_probs.get(state, 0.0)) for state in STATE_ORDER},
        "predicted_peak_window": peak_window,
        "remaining_warming_by_state": remaining_rows,
        "platform_recoil_active": bool(platform_recoil.get("active")),
        "platform_recoil_watch": platform_recoil,
        "recoil_state_ssot": {
            "schema": "metar_recoil_three_state_ssot_v1",
            "state": platform_recoil.get("recoil_state"),
            "state_label": platform_recoil.get("recoil_state_label"),
            "action": platform_recoil.get("recoil_action"),
            "reason": platform_recoil.get("reason"),
            "momentum_treatment": platform_recoil.get("momentum_treatment"),
            "platform_recoil_active": bool(platform_recoil.get("active")),
            "physical_cutoff_confirmed": bool(platform_recoil.get("physical_cutoff_confirmed")),
            "true_cooling_or_cutoff_confirmed": bool(platform_recoil.get("true_cooling_or_cutoff_confirmed")),
            "latest_drop_from_high_c": platform_recoil.get("latest_drop_from_high_c"),
            "physical_cutoff_evidence": platform_recoil.get("physical_cutoff_evidence", {}),
            "termination_risk": termination_risk,
            "boundary": "inside_metar_strong_likelihood_not_independent_likelihood",
            "policy": "platform_noise_pullback_watch_confirmed_cooling_cutoff",
        },
        "physical_cutoff_evidence": platform_recoil.get("physical_cutoff_evidence", {}),
        "termination_risk": termination_risk,
        "temperature_trajectory": temperature_trajectory,
        "daily_high_observation_candidate": observation_candidate,
        "metar_strong_likelihood_candidate": observation_candidate.get("metar_strong_likelihood_candidate", {}),
        "v31_multimodel_posterior": v31_multimodel_posterior,
        "hard_constraints": {
            "current_temperature_c": _round_float(current_temp),
            "current_temperature_source": str(merged_inputs.get("observed_temp_source") or ""),
            "observed_today_max_c": _round_float(observed_max),
            "observed_today_max_source": str(merged_inputs.get("observed_today_max_source") or ""),
            "observed_today_max_at": str(merged_inputs.get("observed_today_max_at") or ""),
            "observed_today_max_final": bool(observed_today_max_final),
            "base_temperature_c": _round_float(base_temp),
            "hard_mask": _distribution_map(labels, hard_mask),
            "masked_zero_buckets": masked_buckets,
            "masked_zero_count": len(masked_buckets),
            "hard_lock": bool(observed_today_max_final),
            "official_final_result": _round_float(observed_max) if observed_today_max_final else None,
            "hard_mask_reason": "+".join(dict.fromkeys(hard_mask_reason_parts)) if hard_mask_reason_parts else "none",
        },
        "observation_features": {
            key: (_round_float(value) if isinstance(value, float) else value)
            for key, value in features.items()
        },
        "source_trace": source_trace,
        "dedupe_trace": [
            {
                "legacy_channel": "observed_nowcast",
                "new_location": "hard_constraints + remaining_warming_by_state",
                "policy": "dedupe_with_current_temperature_and_observed_today_max",
            },
            {
                "legacy_channel": "temperature_trajectory",
                "new_location": "temperature_trajectory + remaining_warming_by_state",
                "policy": "single_internal_metar_strong_likelihood_feature_not_independent_same_level_likelihood",
            },
            {
                "legacy_channel": "physical_likelihood",
                "new_location": "state_probs",
                "policy": "cloud_visibility_dewpoint_precip_wind_state_inference_once",
            },
        ] + ([
            {
                "legacy_channel": "retired_remaining_warming_models",
                "new_location": "v31_multimodel_posterior",
                "policy": "v31_only_no_fallback",
                "applied": True,
            }
        ] if v31_live_applied else []),
        "normalization": {
            "bucket_count": len(labels),
            "state_prob_sum": _round_float(sum(float(v) for v in state_probs.values())),
            "metar_strong_likelihood_sum": _round_float(sum(float(v) for v in likelihood)),
        },
        "na_reasons": sorted(set(reason for reason in na_reasons if reason)),
        "boundary": {
            "role": "observation_strong_likelihood",
            "metar_internal_fields_not_independent_likelihoods": True,
            "weak_forecast_sources_excluded": True,
            "diagnostic_current_sources_excluded": True,
            "current_temperature_hard_floor_applied_once": True,
            "temperature_trajectory_consumed_once_inside_strong_likelihood": True,
            "platform_recoil_consumed_once_inside_strong_likelihood": True,
            "recoil_three_state_consumed_once_inside_strong_likelihood": True,
            "physical_cutoff_evidence_consumed_once_inside_strong_likelihood": True,
            "termination_risk_is_same_strong_mixture_marginal": True,
            "daily_high_observation_candidate_shadow_only": True,
            "legacy_v31_probability_overlay_requested": bool(allow_legacy_v31_probability_overlay),
            "legacy_v31_probability_overlay_applied": False,
            "legacy_v31_probability_overlay_retired": True,
            "physical_x_observation_likelihood_excludes_legacy_v31": True,
        },
        "next_boundary": "sequential_remaining_warming_bayes_runtime_no_standalone_production_entry",
    }


def _likelihood_map_from_output(output: Mapping[str, Any]) -> dict[str, Any]:
    likelihood_map = output.get("metar_strong_likelihood_map")
    if isinstance(likelihood_map, Mapping) and likelihood_map:
        return dict(likelihood_map)
    likelihood_rows = output.get("metar_strong_likelihood")
    if isinstance(likelihood_rows, Mapping) and likelihood_rows:
        return dict(likelihood_rows)
    result: dict[str, Any] = {}
    if isinstance(likelihood_rows, list):
        for row in likelihood_rows:
            if not isinstance(row, Mapping):
                continue
            label = str(row.get("bucket") or row.get("outcome") or row.get("label") or "").strip()
            if not label:
                continue
            result[label] = row.get("metar_strong_likelihood")
    return result


def build_metar_strong_likelihood_contract(metar_output: Mapping[str, Any] | None) -> dict[str, Any]:
    output = metar_output if isinstance(metar_output, Mapping) else {}
    issues: list[str] = []
    if output.get("schema") != SCHEMA_VERSION:
        issues.append("metar_strong_likelihood_schema_invalid")

    likelihood_map = _likelihood_map_from_output(output)
    likelihood_sum: float | None
    if not likelihood_map:
        issues.append("metar_strong_likelihood_map_missing")
        likelihood_sum = None
    else:
        likelihood_sum = 0.0
        for value in likelihood_map.values():
            parsed = _to_float(value)
            if parsed is None:
                issues.append("metar_strong_likelihood_contains_non_numeric_probability")
                likelihood_sum = None
                break
            if parsed < -1e-12:
                issues.append("metar_strong_likelihood_contains_negative_probability")
            likelihood_sum += max(0.0, float(parsed))
        if likelihood_sum is not None and abs(float(likelihood_sum) - 1.0) > 1e-6:
            issues.append("metar_strong_likelihood_not_normalized")

    boundary = output.get("boundary") if isinstance(output.get("boundary"), Mapping) else {}
    if boundary.get("role") != "observation_strong_likelihood":
        issues.append("boundary_role_invalid")
    for key in (
        "metar_internal_fields_not_independent_likelihoods",
        "weak_forecast_sources_excluded",
        "diagnostic_current_sources_excluded",
        "current_temperature_hard_floor_applied_once",
        "temperature_trajectory_consumed_once_inside_strong_likelihood",
        "platform_recoil_consumed_once_inside_strong_likelihood",
        "recoil_three_state_consumed_once_inside_strong_likelihood",
        "physical_cutoff_evidence_consumed_once_inside_strong_likelihood",
    ):
        if boundary.get(key) is not True:
            issues.append(f"boundary_{key}_missing")

    state_probs = output.get("state_probs") if isinstance(output.get("state_probs"), Mapping) else {}
    state_prob_sum: float | None = None
    if not state_probs:
        issues.append("state_probs_missing")
    else:
        state_prob_sum = 0.0
        for state in STATE_ORDER:
            parsed = _to_float(state_probs.get(state))
            if parsed is None:
                issues.append(f"state_prob_{state}_missing_or_non_numeric")
            else:
                state_prob_sum += max(0.0, float(parsed))
        if state_prob_sum is not None and abs(float(state_prob_sum) - 1.0) > 1e-6:
            issues.append("state_probs_not_normalized")

    source_trace = output.get("source_trace") if isinstance(output.get("source_trace"), list) else []
    included_sources = [row for row in source_trace if isinstance(row, Mapping) and row.get("included")]
    for row in included_sources:
        if row.get("source_class") != SOURCE_CLASSES["hard_observation"]:
            issues.append("included_source_not_hard_observation")
    for row in source_trace:
        if not isinstance(row, Mapping) or row.get("included"):
            continue
        source_class = str(row.get("source_class") or "")
        if source_class in {
            SOURCE_CLASSES["diagnostic_current"],
            SOURCE_CLASSES["forecast_weak"],
            SOURCE_CLASSES["market"],
            SOURCE_CLASSES["settlement"],
        } and row.get("exclusion_reason") not in {
            # Class exclusion is the normal path.  The event-date check is
            # intentionally evaluated first so stale/foreign observations are
            # rejected by their stronger temporal identity reason instead of
            # being mislabeled as merely non-hard evidence.
            "source_class_not_hard_observation",
            "event_date_mismatch",
        }:
            issues.append("non_hard_source_exclusion_reason_invalid")

    trajectory = output.get("temperature_trajectory") if isinstance(output.get("temperature_trajectory"), Mapping) else {}
    if not trajectory:
        issues.append("temperature_trajectory_missing")
    else:
        if trajectory.get("schema") != "temperature_trajectory_summary_v1":
            issues.append("temperature_trajectory_schema_invalid")
        if trajectory.get("role") != "temperature_realization_within_metar_strong_likelihood":
            issues.append("temperature_trajectory_role_invalid")
        if trajectory.get("boundary") != "observation_layer_internal_feature_not_independent_likelihood":
            issues.append("temperature_trajectory_boundary_invalid")
        if trajectory.get("dedupe_policy") != "temperature_path_consumed_once_inside_metar_strong_likelihood":
            issues.append("temperature_trajectory_dedupe_policy_invalid")
        for key in ("state", "gate_state", "confidence", "adjusted_rate_c_per_hour", "local_hour"):
            if key not in trajectory:
                issues.append(f"temperature_trajectory_{key}_missing")

    hard_constraints = output.get("hard_constraints") if isinstance(output.get("hard_constraints"), Mapping) else {}
    hard_mask = hard_constraints.get("hard_mask") if isinstance(hard_constraints.get("hard_mask"), Mapping) else {}
    if not hard_constraints:
        issues.append("hard_constraints_missing")
    if not hard_mask:
        issues.append("hard_mask_missing")
    else:
        for label in likelihood_map:
            parsed = _to_float(hard_mask.get(label))
            if parsed is None:
                issues.append("hard_mask_bucket_missing")
            elif parsed not in (0.0, 1.0):
                issues.append("hard_mask_value_not_binary")
    if not str(hard_constraints.get("hard_mask_reason") or ""):
        issues.append("hard_mask_reason_missing")

    recoil_ssot = output.get("recoil_state_ssot") if isinstance(output.get("recoil_state_ssot"), Mapping) else {}
    if not recoil_ssot:
        issues.append("recoil_state_ssot_missing")
    else:
        if recoil_ssot.get("schema") != "metar_recoil_three_state_ssot_v1":
            issues.append("recoil_state_ssot_schema_invalid")
        if recoil_ssot.get("state") not in set(RECOIL_STATE_LABELS):
            issues.append("recoil_state_invalid")
        if recoil_ssot.get("boundary") != "inside_metar_strong_likelihood_not_independent_likelihood":
            issues.append("recoil_state_boundary_invalid")
        evidence = recoil_ssot.get("physical_cutoff_evidence")
        if not isinstance(evidence, Mapping) or evidence.get("schema") != "metar_recoil_physical_cutoff_evidence_v1":
            issues.append("physical_cutoff_evidence_schema_invalid")

    base_observation_present = (
        _to_float(hard_constraints.get("current_temperature_c")) is not None
        or _to_float(hard_constraints.get("observed_today_max_c")) is not None
        or bool(included_sources)
    )
    remaining_rows = output.get("remaining_warming_by_state") if isinstance(output.get("remaining_warming_by_state"), list) else []
    if base_observation_present:
        states_seen: set[str] = set()
        if len(remaining_rows) != len(STATE_ORDER):
            issues.append("remaining_warming_by_state_incomplete")
        for row in remaining_rows:
            if not isinstance(row, Mapping):
                issues.append("remaining_warming_by_state_row_invalid")
                continue
            state = str(row.get("state") or "")
            states_seen.add(state)
            for key in ("state_prob", "delta_mu_c", "sigma_c", "peak_mu_c", "remaining_hours"):
                if _to_float(row.get(key)) is None:
                    issues.append(f"remaining_warming_{key}_missing_or_non_numeric")
            for key in ("recoil_state", "recoil_action"):
                if not str(row.get(key) or ""):
                    issues.append(f"remaining_warming_{key}_missing")
        if states_seen and states_seen != set(STATE_ORDER):
            issues.append("remaining_warming_state_set_invalid")
    else:
        na_reasons = output.get("na_reasons") if isinstance(output.get("na_reasons"), list) else []
        if "current_temp_and_observed_today_max_missing" not in {str(reason) for reason in na_reasons}:
            issues.append("missing_observation_na_reason_missing")

    dedupe_trace = output.get("dedupe_trace") if isinstance(output.get("dedupe_trace"), list) else []
    dedupe_by_channel = {
        str(row.get("legacy_channel") or ""): str(row.get("policy") or "")
        for row in dedupe_trace
        if isinstance(row, Mapping)
    }
    expected_dedupe = {
        "observed_nowcast": "dedupe_with_current_temperature_and_observed_today_max",
        "temperature_trajectory": "single_internal_metar_strong_likelihood_feature_not_independent_same_level_likelihood",
        "physical_likelihood": "cloud_visibility_dewpoint_precip_wind_state_inference_once",
    }
    for channel, policy in expected_dedupe.items():
        if dedupe_by_channel.get(channel) != policy:
            issues.append(f"dedupe_{channel}_policy_missing")

    normalization = output.get("normalization") if isinstance(output.get("normalization"), Mapping) else {}
    normalized_state_sum = _to_float(normalization.get("state_prob_sum"))
    normalized_likelihood_sum = _to_float(normalization.get("metar_strong_likelihood_sum"))
    if normalized_state_sum is not None and abs(float(normalized_state_sum) - 1.0) > 1e-6:
        issues.append("normalization_state_prob_sum_invalid")
    if normalized_likelihood_sum is not None and abs(float(normalized_likelihood_sum) - 1.0) > 1e-6:
        issues.append("normalization_metar_strong_likelihood_sum_invalid")

    return {
        "schema": CONTRACT_SCHEMA_VERSION,
        "ok": not issues,
        "state": "ready" if not issues else "blocked",
        "issues": sorted(set(issues)),
        "included_source_count": len(included_sources),
        "excluded_source_count": len(source_trace) - len(included_sources),
        "base_observation_present": bool(base_observation_present),
        "metar_strong_likelihood_sum": _round_float(likelihood_sum),
        "state_prob_sum": _round_float(state_prob_sum),
        "remaining_warming_state_count": len(remaining_rows),
        "trajectory_state": trajectory.get("state"),
        "trajectory_gate_state": trajectory.get("gate_state"),
        "hard_masked_count": hard_constraints.get("masked_zero_count"),
        "hard_mask_reason": hard_constraints.get("hard_mask_reason"),
        "boundary": dict(boundary),
    }


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "STATE_ORDER",
    "build_metar_strong_likelihood",
    "build_metar_strong_likelihood_contract",
    "infer_environment_state_probs",
    "remaining_warming_params",
]
