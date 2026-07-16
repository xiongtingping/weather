"""Shadow candidate for ZUUU daily-high observation likelihood.

The candidate is deliberately pure and read-only.  It normalizes raw METAR
observations as of a cutoff, interprets integer temperatures as intervals,
infers trajectory and physical-state mixtures, and emits a bucket distribution
without changing the production posterior or execution chain.
"""

from __future__ import annotations

import calendar
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from source_observation_runtime import PRIMARY_METAR_SOURCE_ID, REFERENCE_METAR_SOURCE_IDS, SOURCE_CLASSES, source_defaults


SCHEMA_VERSION = "zuuu_daily_high_observation_candidate_v1"
POLICY_VERSION = "zuuu_daily_high_observation_candidate_shadow_v1"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
METAR_TIME_RE = re.compile(r"\b(?:METAR|SPECI)\s+ZUUU\s+(\d{2})(\d{2})(\d{2})Z\b")
CLOUD_LAYER_RE = re.compile(r"\b(?:FEW|SCT|BKN|OVC|VV)\d{3}(?:CB|TCU)?\b|\b(?:CAVOK|NSC|SKC|CLR)\b")
WEATHER_TOKEN_RE = re.compile(
    r"\b(?:-|\+|VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)?"
    r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)\b"
)
TRAJECTORY_STATES = (
    "sustained_warming",
    "emerging_warming",
    "quantized_plateau_watch",
    "plateau_near_daily_high",
    "reheating_below_daily_high",
    "pullback_watch",
    "confirmed_cooling_cutoff",
    "missing",
)
PHYSICAL_STATES = (
    "S0_wet_layer_closed",
    "S1_opening",
    "S2_open_boundary_layer",
    "S3_convective_cutoff",
)
QUANTIZATION_HALF_WIDTH_C = 0.5
PRIMARY_SOURCE_PRIORITY = {PRIMARY_METAR_SOURCE_ID: 2, **{source_id: 1 for source_id in REFERENCE_METAR_SOURCE_IDS}}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(row.get("payload"))


def _text(row: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _float(row: Mapping[str, Any], payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        for source in (row, payload):
            value = source.get(key)
            if value in (None, ""):
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed):
                return parsed
    return None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _local_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(LOCAL_TZ).replace(microsecond=0).isoformat()


def _round(value: float | None, digits: int = 8) -> float | None:
    return None if value is None else round(float(value), digits)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _softmax(scores: Mapping[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    maximum = max(float(value) for value in scores.values())
    weights = {key: math.exp(float(value) - maximum) for key, value in scores.items()}
    total = sum(weights.values())
    return {key: value / total for key, value in weights.items()} if total else {key: 1.0 / len(weights) for key in weights}


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0)))


def _month_candidates(anchor: datetime) -> list[tuple[int, int]]:
    center = anchor.astimezone(timezone.utc).replace(day=15, hour=0, minute=0, second=0, microsecond=0)
    return sorted(
        {
            (center.year, center.month),
            ((center - timedelta(days=32)).year, (center - timedelta(days=32)).month),
            ((center + timedelta(days=32)).year, (center + timedelta(days=32)).month),
        }
    )


def _raw_metar_valid_time(raw_text: str, anchor: datetime | None) -> datetime | None:
    match = METAR_TIME_RE.search(str(raw_text or ""))
    if not match:
        return None
    reference = anchor or datetime.now(timezone.utc)
    day, hour, minute = (int(part) for part in match.groups())
    candidates: list[datetime] = []
    for year, month in _month_candidates(reference):
        if day <= calendar.monthrange(year, month)[1]:
            candidates.append(datetime(year, month, day, hour, minute, tzinfo=timezone.utc))
    return min(candidates, key=lambda candidate: abs((candidate - reference).total_seconds())) if candidates else None


def _source_id(row: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    source_id = _text(row, payload, "source_id", "source")
    return source_id or "unknown_source"


def _source_class(row: Mapping[str, Any], payload: Mapping[str, Any], source_id: str) -> str:
    explicit = _text(row, payload, "source_class")
    return explicit or str(source_defaults(source_id).get("source_class") or SOURCE_CLASSES["unknown"])


def _availability_time(row: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[datetime | None, str]:
    first_seen = _dt(_text(row, payload, "first_seen_at"))
    arrival_quality = _text(row, payload, "arrival_time_quality")
    if first_seen is not None:
        return first_seen, arrival_quality or "observed_first_seen"
    fetched = _dt(_text(row, payload, "fetched_at", "response_completed_at", "parsed_at"))
    return fetched, arrival_quality or "arrival_time_not_explicit"


def _field_completeness(record: Mapping[str, Any]) -> int:
    return sum(
        value not in (None, "")
        for value in (
            record.get("dewpoint_c"),
            record.get("visibility_m"),
            record.get("cloud_text"),
            record.get("weather_text"),
            record.get("wind_speed_mps"),
            record.get("pressure_hpa"),
        )
    )


def _canonical_record(raw: Any, *, index: int, cutoff: datetime, event_date: str) -> tuple[dict[str, Any] | None, str]:
    if isinstance(raw, Mapping):
        row = dict(raw)
    elif hasattr(raw, "as_dict"):
        row = dict(raw.as_dict(include_payload=True))
    else:
        row = {}
    payload = _payload(row)
    source_id = _source_id(row, payload)
    parser_version = _text(row, payload, "parser_version")
    if parser_version == "posterior_ssot_projection_fact_feature_source_adapter_v1":
        return None, "adapter_derived_row_excluded"
    source_class = _source_class(row, payload, source_id)
    if source_class != SOURCE_CLASSES["hard_observation"]:
        return None, "source_class_not_hard_observation"
    temperature_c = _float(row, payload, "temperature_c", "temp_c", "metar_temperature_c", "current_temp_c", "observed_temp_c")
    if temperature_c is None:
        return None, "temperature_missing"
    raw_text = _text(row, payload, "raw_text", "raw_ref")
    availability, arrival_quality = _availability_time(row, payload)
    valid_from_field = _dt(_text(row, payload, "verified_metar_valid_time", "valid_time", "valid_time_utc", "observation_time"))
    valid_from_raw = _raw_metar_valid_time(raw_text, availability or cutoff)
    verified_valid = valid_from_raw or valid_from_field
    if verified_valid is None:
        return None, "verified_valid_time_missing"
    if verified_valid > cutoff:
        return None, "future_valid_time"
    if availability is not None and availability > cutoff:
        return None, "late_arrival_after_cutoff"
    if event_date and verified_valid.astimezone(LOCAL_TZ).date().isoformat() != str(event_date):
        return None, "station_local_event_date_mismatch"
    flags: list[str] = []
    if valid_from_raw is not None and valid_from_field is not None and abs((valid_from_raw - valid_from_field).total_seconds()) > 1:
        flags.append("valid_time_contract_mismatch")
    if availability is None:
        flags.append("arrival_time_missing")
    if arrival_quality == "backfill_arrival_unknown":
        flags.append("arrival_time_backfill_unknown")
    record = {
        "source_id": source_id,
        "source_class": source_class,
        "station_id": _text(row, payload, "station_id") or "ZUUU",
        "temperature_c": float(temperature_c),
        "temperature_interval_c": [float(temperature_c) - QUANTIZATION_HALF_WIDTH_C, float(temperature_c) + QUANTIZATION_HALF_WIDTH_C],
        "valid_time": _iso(verified_valid),
        "valid_time_local": _local_iso(verified_valid),
        "verified_metar_valid_time": _iso(verified_valid),
        "fetched_at": _text(row, payload, "fetched_at", "response_completed_at"),
        "parsed_at": _text(row, payload, "parsed_at"),
        "first_seen_at": _iso(availability),
        "arrival_time_quality": arrival_quality,
        "source_observation_id": _text(row, payload, "source_observation_id"),
        "raw_sha256": _text(row, payload, "raw_sha256"),
        "raw_object_ref": _text(row, payload, "raw_object_ref"),
        "parser_version": parser_version,
        "dewpoint_c": _float(row, payload, "dewpoint_c", "dewPt"),
        "visibility_m": _float(row, payload, "visibility_m", "visibility"),
        "cloud_cover_pct": _float(row, payload, "cloud_cover_pct", "cloud_pct"),
        "low_cloud_cover_pct": _float(row, payload, "low_cloud_cover_pct"),
        "wind_speed_mps": _float(row, payload, "wind_speed_mps", "wind_speed"),
        "wind_dir_degrees": _float(row, payload, "wind_dir_degrees", "wind_dir_deg", "wind_direction"),
        "wind_gust_mps": _float(row, payload, "wind_gust_mps", "gust_mps"),
        "pressure_hpa": _float(row, payload, "pressure_hpa", "pressure"),
        "weather_text": _text(row, payload, "weather_text", "weather", "raw_text", "raw_ref"),
        "cloud_text": _text(row, payload, "cloud_text", "clouds", "raw_text", "raw_ref"),
        "raw_text": raw_text,
        "quality_flags": flags,
        "_availability": availability.timestamp() if availability else float("-inf"),
        "_source_priority": PRIMARY_SOURCE_PRIORITY.get(source_id, 0),
        "_completeness": 0,
        "_index": index,
    }
    record["_completeness"] = _field_completeness(record)
    return record, ""


def canonical_observation_snapshot(
    *,
    source_observations: Iterable[Any] | None,
    as_of: Any,
    event_date: str,
) -> dict[str, Any]:
    cutoff = _dt(as_of) or datetime.now(timezone.utc)
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for index, raw in enumerate(source_observations or []):
        record, reason = _canonical_record(raw, index=index, cutoff=cutoff, event_date=event_date)
        if record is None:
            excluded.append({"index": index, "reason": reason})
        else:
            candidates.append(record)
    by_time: dict[str, list[dict[str, Any]]] = {}
    for record in candidates:
        by_time.setdefault(str(record["verified_metar_valid_time"]), []).append(record)
    selected: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for valid_time, records in by_time.items():
        ranked = sorted(
            records,
            key=lambda record: (
                int(record["_source_priority"]),
                float(record["_availability"]),
                int(record["_completeness"]),
                int(record["_index"]),
            ),
            reverse=True,
        )
        selected.append(ranked[0])
        superseded.extend(
            {
                "source_observation_id": row.get("source_observation_id"),
                "source_id": row.get("source_id"),
                "verified_metar_valid_time": valid_time,
                "reason": "superseded_by_primary_or_later_correction",
            }
            for row in ranked[1:]
        )
    selected.sort(key=lambda record: str(record["verified_metar_valid_time"]))
    flags = sorted({flag for record in selected for flag in record.get("quality_flags", [])})
    points = [{key: value for key, value in record.items() if not key.startswith("_")} for record in selected]
    return {
        "schema": "canonical_observation_snapshot_v1",
        "as_of": _iso(cutoff),
        "station_id": "ZUUU",
        "event_date": str(event_date or ""),
        "selected_points": points,
        "selected_source_observation_ids": [str(point.get("source_observation_id") or "") for point in points if point.get("source_observation_id")],
        "superseded_observations": superseded,
        "excluded_observations": excluded,
        "distinct_valid_time_count": len(points),
        "latest_valid_time": str(points[-1].get("verified_metar_valid_time") or "") if points else "",
        "latest_fetched_at": str(points[-1].get("fetched_at") or "") if points else "",
        "quality_flags": flags,
        "na_reasons": ["canonical_observation_missing"] if not points else [],
    }


def _phase(local_hour: float) -> tuple[str, float]:
    if local_hour < 9.0:
        return "opening", 6.0
    if local_hour < 13.0:
        return "morning_warming", 4.0
    if local_hour < 14.0:
        return "pre_peak", 2.5
    if local_hour < 18.0:
        return "broad_peak", 1.25
    return "post_peak", 0.5


def _slope(points: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    if len(points) < 2:
        return {"mean": None, "low": None, "high": None, "span_hours": None}
    times = [_dt(point.get("verified_metar_valid_time")) for point in points]
    if any(value is None for value in times):
        return {"mean": None, "low": None, "high": None, "span_hours": None}
    timestamps = [value.timestamp() / 3600.0 for value in times if value is not None]
    temperatures = [float(point["temperature_c"]) for point in points]
    center = sum(timestamps) / len(timestamps)
    denominator = sum((value - center) ** 2 for value in timestamps)
    if denominator <= 1e-9:
        return {"mean": None, "low": None, "high": None, "span_hours": None}
    mean_temp = sum(temperatures) / len(temperatures)
    estimate = sum((x - center) * (y - mean_temp) for x, y in zip(timestamps, temperatures)) / denominator
    span = max(timestamps) - min(timestamps)
    interval_error = max(0.10, (2.0 * QUANTIZATION_HALF_WIDTH_C) / max(span, 0.25))
    return {
        "mean": estimate,
        "low": estimate - interval_error,
        "high": estimate + interval_error,
        "span_hours": span,
    }


def _prob_positive(mean: float | None, low: float | None, high: float | None) -> float:
    if mean is None or low is None or high is None:
        return 0.5
    half_width = max((float(high) - float(low)) / 2.0, 0.10)
    return _clamp(0.5 + float(mean) / (2.0 * half_width), 0.02, 0.98)


def _trajectory_posterior(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    points = list(snapshot.get("selected_points") or [])
    if not points:
        return {
            "schema": "temperature_trajectory_posterior_v1",
            "state": "missing",
            "state_probs": {state: (1.0 if state == "missing" else 0.0) for state in TRAJECTORY_STATES},
            "gate_state": "missing",
            "local_phase": "unknown",
            "na_reasons": ["temperature_points_missing"],
        }
    latest_dt = _dt(points[-1].get("verified_metar_valid_time"))
    assert latest_dt is not None
    local_hour = latest_dt.astimezone(LOCAL_TZ).hour + latest_dt.astimezone(LOCAL_TZ).minute / 60.0
    phase, phase_horizon = _phase(local_hour)
    recent = [point for point in points if (latest_dt - (_dt(point.get("verified_metar_valid_time")) or latest_dt)).total_seconds() <= 90 * 60]
    background = [
        point
        for point in points
        if 90 * 60 < (latest_dt - (_dt(point.get("verified_metar_valid_time")) or latest_dt)).total_seconds() <= 4 * 3600
    ]
    recent_slope = _slope(recent)
    background_slope = _slope(background)
    recent_mean = recent_slope["mean"]
    background_mean = background_slope["mean"]
    warming_probability = _prob_positive(recent_mean, recent_slope["low"], recent_slope["high"])
    cooling_probability = 1.0 - warming_probability
    acceleration = float(recent_mean) - float(background_mean) if recent_mean is not None and background_mean is not None else None
    temperatures = [float(point["temperature_c"]) for point in points]
    observed_max = max(temperatures)
    current = temperatures[-1]
    daily_gap = observed_max - current
    consecutive_negative_steps = 0
    for previous, current_value in zip(reversed(temperatures[:-1]), reversed(temperatures[1:])):
        if current_value < previous - QUANTIZATION_HALF_WIDTH_C:
            consecutive_negative_steps += 1
        else:
            break
    sustained_cooling = consecutive_negative_steps >= 2 and cooling_probability > 0.60
    flatness = 1.0 - abs(warming_probability - 0.5) * 2.0
    near_peak = 1.0 if phase in {"broad_peak", "post_peak"} else 0.0
    scores = {
        "sustained_warming": 2.0 * warming_probability + (0.25 if acceleration is not None and acceleration > 0 else 0.0),
        "emerging_warming": 1.1 * warming_probability + (0.35 if acceleration is not None and acceleration > 0 else 0.0),
        "quantized_plateau_watch": 1.15 * flatness + (0.55 if phase in {"opening", "morning_warming", "pre_peak"} else 0.0),
        "plateau_near_daily_high": 1.15 * flatness + 0.85 * near_peak + (0.25 if daily_gap <= QUANTIZATION_HALF_WIDTH_C else 0.0),
        "reheating_below_daily_high": 1.5 * warming_probability + (0.75 if daily_gap > QUANTIZATION_HALF_WIDTH_C else -0.25),
        "pullback_watch": 0.85 * cooling_probability + (0.85 if daily_gap > QUANTIZATION_HALF_WIDTH_C else 0.0),
        "confirmed_cooling_cutoff": (1.8 if sustained_cooling else -1.0) + 0.55 * near_peak,
        "missing": -1.2,
    }
    if len(points) < 3:
        scores["missing"] = 1.2
        scores["quantized_plateau_watch"] += 0.35
    state_probs = _softmax(scores)
    state = max(state_probs, key=state_probs.get)
    rolling_gap = max(temperatures[-5:]) - temperatures[-1]
    max_gap_minutes = 0.0
    for prior, later in zip(points, points[1:]):
        prior_dt = _dt(prior.get("verified_metar_valid_time"))
        later_dt = _dt(later.get("verified_metar_valid_time"))
        if prior_dt and later_dt:
            max_gap_minutes = max(max_gap_minutes, (later_dt - prior_dt).total_seconds() / 60.0)
    na_reasons: list[str] = []
    if len(recent) < 2:
        na_reasons.append("recent_slope_insufficient_points")
    if len(background) < 2:
        na_reasons.append("background_slope_insufficient_points")
    if max_gap_minutes > 90.0:
        na_reasons.append("trajectory_gap_widens_uncertainty")
    return {
        "schema": "temperature_trajectory_posterior_v1",
        "state": state,
        "state_probs": {key: _round(value) for key, value in state_probs.items()},
        "gate_state": "watch" if state in {"pullback_watch", "quantized_plateau_watch"} else state,
        "local_phase": phase,
        "local_hour": _round(local_hour),
        "phase_horizon_hours": _round(phase_horizon),
        "latent_current_temp_mean_c": _round(current),
        "latent_current_temp_interval_c": [_round(current - QUANTIZATION_HALF_WIDTH_C), _round(current + QUANTIZATION_HALF_WIDTH_C)],
        "recent_slope_mean_c_per_hour": _round(recent_mean),
        "recent_slope_interval_c_per_hour": [_round(recent_slope["low"]), _round(recent_slope["high"])],
        "background_slope_mean_c_per_hour": _round(background_mean),
        "background_slope_interval_c_per_hour": [_round(background_slope["low"]), _round(background_slope["high"])],
        "acceleration_mean_c_per_hour2": _round(acceleration),
        "warming_probability": _round(warming_probability),
        "cooling_probability": _round(cooling_probability),
        "rolling_max_gap_c": _round(rolling_gap),
        "daily_max_gap_c": _round(daily_gap),
        "observed_today_max_c": _round(observed_max),
        "effective_observation_count": len(points),
        "max_gap_minutes": _round(max_gap_minutes),
        "state_transition_trace": {
            "single_report_direct_cutoff_forbidden": True,
            "sustained_negative_step_count": consecutive_negative_steps,
            "sustained_cooling_without_physical_confirmation": sustained_cooling,
            "allowed_recovery_state": "reheating_below_daily_high",
        },
        "confidence": _round(_clamp((min(len(points), 8) / 8.0) * (1.0 - min(max_gap_minutes / 360.0, 0.55)), 0.05, 0.95)),
        "na_reasons": na_reasons,
    }


def _weather_tokens(text: str) -> list[str]:
    return [match.group(0).upper() for match in WEATHER_TOKEN_RE.finditer(str(text or "").upper())]


def _cloud_tokens(text: str) -> list[str]:
    return [match.group(0).upper() for match in CLOUD_LAYER_RE.finditer(str(text or "").upper())]


def _angular_difference(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return abs((float(left) - float(right) + 180.0) % 360.0 - 180.0)


def _physical_state(snapshot: Mapping[str, Any], trajectory: Mapping[str, Any]) -> dict[str, Any]:
    points = list(snapshot.get("selected_points") or [])
    latest = points[-1] if points else {}
    prior = points[-4:-1]
    weather_text = " ".join(str(point.get("weather_text") or "") for point in points[-3:]).upper()
    cloud_text = " ".join(str(point.get("cloud_text") or "") for point in points[-3:]).upper()
    weather_tokens = _weather_tokens(weather_text)
    clouds = _cloud_tokens(cloud_text)
    direct_station = any(token.replace("+", "").replace("-", "") in {"RA", "SHRA", "TSRA", "DZ"} for token in weather_tokens)
    vicinity = any(token.startswith("VC") for token in weather_tokens) or "VCTS" in weather_text
    convective = any("TS" in token for token in weather_tokens) or any(token.endswith("CB") or token.endswith("TCU") for token in clouds)
    direct_count = sum(
        1
        for point in points
        if any(token.replace("+", "").replace("-", "") in {"RA", "SHRA", "TSRA", "DZ"} for token in _weather_tokens(str(point.get("weather_text") or "")))
    )
    latest_low_cloud = latest.get("low_cloud_cover_pct")
    prior_low_cloud = [point.get("low_cloud_cover_pct") for point in prior if point.get("low_cloud_cover_pct") is not None]
    low_cloud_deterioration = (
        latest_low_cloud is not None
        and prior_low_cloud
        and float(latest_low_cloud) - min(float(value) for value in prior_low_cloud) >= 25.0
    )
    latest_visibility = latest.get("visibility_m")
    prior_visibility = [point.get("visibility_m") for point in prior if point.get("visibility_m") is not None]
    visibility_deterioration = (
        latest_visibility is not None
        and prior_visibility
        and float(latest_visibility) < min(float(value) for value in prior_visibility) - 1500.0
    )
    latest_pressure = latest.get("pressure_hpa")
    prior_pressure = [point.get("pressure_hpa") for point in prior if point.get("pressure_hpa") is not None]
    robust_pressure_rise = (
        latest_pressure is not None
        and len(prior_pressure) >= 2
        and float(latest_pressure) > min(float(value) for value in prior_pressure) + 1.2
    )
    latest_wind = latest.get("wind_dir_degrees")
    # Compare against the earliest independent recent wind, not merely the
    # immediately previous report; the latter can hide a progressive shift.
    prior_wind = next((point.get("wind_dir_degrees") for point in prior if point.get("wind_dir_degrees") is not None), None)
    wind_shift = (_angular_difference(_number(latest_wind), _number(prior_wind)) or 0.0) >= 55.0
    recent_cooling = bool(trajectory.get("state_transition_trace", {}).get("sustained_cooling_without_physical_confirmation"))
    phase = str(trajectory.get("local_phase") or "unknown")
    phase_late = phase in {"broad_peak", "post_peak"}
    dynamic_support = any((wind_shift, robust_pressure_rise, visibility_deterioration, low_cloud_deterioration))
    direct_weather_cutoff = bool(direct_station and (recent_cooling or (direct_count >= 2 and phase_late)))
    cooling_dynamic_cutoff = bool(recent_cooling and dynamic_support)
    confirmed_cutoff = bool(direct_weather_cutoff or cooling_dynamic_cutoff)
    clear_signal = bool(any(token in {"CAVOK", "NSC", "SKC", "CLR"} for token in clouds))
    low_cloud_signal = any(token.startswith(("BKN", "OVC", "VV")) for token in clouds)
    warming_probability = float(trajectory.get("warming_probability") or 0.5)
    recovery = bool(not direct_station and direct_count > 0 and warming_probability > 0.55) or bool(clear_signal and warming_probability > 0.60)
    scores = {
        "S0_wet_layer_closed": 0.2 + (0.9 if low_cloud_signal else 0.0) + (0.7 if direct_station else 0.0) + (0.25 if visibility_deterioration else 0.0),
        "S1_opening": 0.35 + (0.7 if recovery else 0.0) + (0.3 if clear_signal else 0.0),
        "S2_open_boundary_layer": 0.25 + (0.8 if clear_signal else 0.0) + 0.45 * warming_probability - (0.45 if direct_station else 0.0),
        "S3_convective_cutoff": -0.55 + (1.25 if direct_station else 0.0) + (0.8 if convective else 0.0) + (0.9 if confirmed_cutoff else 0.0) + (0.45 if recent_cooling else 0.0),
    }
    probs = _softmax(scores)
    return {
        "schema": "physical_environment_state_posterior_v1",
        "state_probs": {key: _round(value) for key, value in probs.items()},
        "dominant_state": max(probs, key=probs.get),
        "physical_cutoff_probability": _round(probs["S3_convective_cutoff"]),
        "physical_cutoff_confirmed": confirmed_cutoff,
        "rebound_probability": _round(probs["S1_opening"]),
        "precipitation_event_state": "active" if direct_station else "vicinity" if vicinity else "none_observed" if weather_text else "unknown",
        "direct_weather_location_scope": "station" if direct_station else "vicinity" if vicinity else "unknown" if not weather_text else "none",
        "direct_weather_intensity_class": "convective" if convective else "active" if direct_station else "vicinity" if vicinity else "unknown",
        "thermal_response_state": "sustained_cooling" if recent_cooling else "warming" if warming_probability > 0.55 else "flat_or_uncertain",
        "cause_group_probs": {
            "radiative_boundary": _round((1.0 if low_cloud_signal else 0.0) + (0.35 if clear_signal else 0.0)),
            "direct_weather": _round(1.0 if direct_station else 0.45 if vicinity else 0.0),
            "cold_pool_dynamics": _round(sum((recent_cooling, wind_shift, robust_pressure_rise, visibility_deterioration, low_cloud_deterioration)) / 5.0),
            "recovery": _round(1.0 if recovery else 0.0),
        },
        "evidence_consumption_trace": {
            "radiative_boundary_consumed_once": True,
            "direct_weather_consumed_once": True,
            "cold_pool_dynamics_consumed_once": True,
            "recovery_consumed_once": True,
            "direct_weather_alone_cannot_confirm_cutoff": True,
        },
        "evidence_codes": [
            code
            for code, enabled in (
                ("station_precipitation_or_convection", direct_station),
                ("vicinity_weather_only", vicinity and not direct_station),
                ("convective_token", convective),
                ("sustained_temperature_cooling", recent_cooling),
                ("wind_shift", wind_shift),
                ("robust_pressure_rise", robust_pressure_rise),
                ("visibility_deterioration", visibility_deterioration),
                ("low_cloud_deterioration", low_cloud_deterioration),
                ("recovery_signal", recovery),
            )
            if enabled
        ],
        "na_reasons": [reason for reason, missing in (("weather_token_missing", not weather_text), ("cloud_token_missing", not cloud_text)) if missing],
    }


def _bucket_bounds(label: str) -> tuple[float, float] | None:
    text = str(label or "").strip().replace("–", "-")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?C", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    lower = text.lower()
    if "or below" in lower or "or less" in lower:
        return -math.inf, value + 0.5
    if "or higher" in lower or "or above" in lower or "+" in lower:
        return value - 0.5, math.inf
    return value - 0.5, value + 0.5


def _positive_delta_distribution(mu: float, sigma: float, zero_mass: float) -> list[tuple[float, float]]:
    zero_mass = _clamp(zero_mass, 0.02, 0.93)
    values = [round(index * 0.1, 1) for index in range(1, 101)]
    weights = [math.exp(-0.5 * ((value - mu) / max(sigma, 0.20)) ** 2) for value in values]
    total = sum(weights)
    return [(0.0, zero_mass)] + [(value, (1.0 - zero_mass) * weight / total) for value, weight in zip(values, weights)]


def _quantile(distribution: Sequence[tuple[float, float]], probability: float) -> float:
    cumulative = 0.0
    for value, mass in distribution:
        cumulative += mass
        if cumulative + 1e-12 >= probability:
            return value
    return distribution[-1][0] if distribution else 0.0


def _remaining_distribution(
    trajectory: Mapping[str, Any],
    physical: Mapping[str, Any],
    buckets: Sequence[str],
) -> dict[str, Any]:
    observed_max = float(trajectory.get("observed_today_max_c") or trajectory.get("latent_current_temp_mean_c") or 0.0)
    horizon = float(trajectory.get("phase_horizon_hours") or 0.5)
    phase = str(trajectory.get("local_phase") or "post_peak")
    warming = float(trajectory.get("warming_probability") or 0.5)
    cooling = float(trajectory.get("cooling_probability") or 0.5)
    confidence = float(trajectory.get("confidence") or 0.05)
    base_mu = _clamp(0.20 + 0.65 * horizon + 0.75 * (warming - cooling) * min(horizon, 2.0), 0.12, 6.5)
    base_zero = {"opening": 0.06, "morning_warming": 0.12, "pre_peak": 0.20, "broad_peak": 0.36, "post_peak": 0.62}.get(phase, 0.42)
    state_adjustments = {
        "S0_wet_layer_closed": (-0.45, 0.12),
        "S1_opening": (-0.08, 0.02),
        "S2_open_boundary_layer": (0.38, -0.08),
        "S3_convective_cutoff": (-0.72, 0.23),
    }
    combined: dict[float, float] = {}
    rows: list[dict[str, Any]] = []
    for state in PHYSICAL_STATES:
        state_prob = float(physical.get("state_probs", {}).get(state) or 0.0)
        adjustment, zero_adjustment = state_adjustments[state]
        mu = max(0.10, base_mu + adjustment)
        zero_mass = _clamp(base_zero + zero_adjustment + 0.15 * cooling - 0.10 * warming, 0.03, 0.90)
        sigma = 0.75 + 0.65 * (1.0 - confidence)
        if state == "S3_convective_cutoff" and not bool(physical.get("physical_cutoff_confirmed")):
            # A direct weather token alone raises S3 but must not erase all
            # future warming paths before its thermal response is observed.
            zero_mass = min(zero_mass, 0.66)
            mu = max(mu, 0.22)
        distribution = _positive_delta_distribution(mu, sigma, zero_mass)
        for delta, mass in distribution:
            combined[delta] = combined.get(delta, 0.0) + state_prob * mass
        rows.append(
            {
                "physical_state": state,
                "state_prob": _round(state_prob),
                "delta_mu_c": _round(mu),
                "sigma_c": _round(sigma),
                "zero_mass": _round(zero_mass),
            }
        )
    total = sum(combined.values())
    distribution = sorted((delta, mass / total) for delta, mass in combined.items()) if total else [(0.0, 1.0)]
    bucket_mass = {str(bucket): 0.0 for bucket in buckets}
    hard_floor = {str(bucket): 1.0 for bucket in buckets}
    for bucket in buckets:
        bounds = _bucket_bounds(bucket)
        if bounds is None:
            continue
        low, high = bounds
        if high <= observed_max:
            hard_floor[str(bucket)] = 0.0
        for delta, mass in distribution:
            value = observed_max + delta
            if low <= value < high:
                bucket_mass[str(bucket)] += mass
    normalized_total = sum(bucket_mass.values())
    if normalized_total > 0:
        bucket_mass = {bucket: mass / normalized_total for bucket, mass in bucket_mass.items()}
    elif bucket_mass:
        allowed = [bucket for bucket, mask in hard_floor.items() if mask > 0]
        bucket_mass = {bucket: (1.0 / len(allowed) if bucket in allowed else 0.0) for bucket in bucket_mass} if allowed else bucket_mass
    return {
        "schema": "remaining_daily_high_distribution_v1",
        "observed_today_max_c": _round(observed_max),
        "remaining_increment_zero_mass": _round(dict(distribution).get(0.0, 0.0)),
        "remaining_increment_positive_quantiles_c": {"p50": _round(_quantile(distribution, 0.50)), "p90": _round(_quantile(distribution, 0.90))},
        "remaining_increment_by_physical_state": rows,
        "bucket_probabilities": {bucket: _round(mass) for bucket, mass in bucket_mass.items()},
        "hard_floor_applied": True,
        "hard_floor": hard_floor,
        "phase": phase,
        "candidate_only": True,
        "na_reasons": [],
    }


def build_zuuu_daily_high_candidate(
    *,
    buckets: Sequence[str],
    source_observations: Iterable[Any] | None,
    as_of: Any,
    event_date: str,
) -> dict[str, Any]:
    """Build the shadow observation candidate without changing production likelihoods."""

    snapshot = canonical_observation_snapshot(source_observations=source_observations, as_of=as_of, event_date=event_date)
    trajectory = _trajectory_posterior(snapshot)
    physical = _physical_state(snapshot, trajectory)
    remaining = _remaining_distribution(trajectory, physical, buckets)
    likelihood = dict(remaining.get("bucket_probabilities") or {})
    return {
        "schema": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "candidate_only": True,
        "production_applied": False,
        "event_date": str(event_date or ""),
        "as_of": str(as_of or ""),
        "canonical_observation_snapshot": snapshot,
        "temperature_trajectory_posterior": trajectory,
        "physical_environment_state_posterior": physical,
        "remaining_daily_high_distribution": remaining,
        "metar_strong_likelihood_candidate": {
            "schema": "metar_strong_likelihood_candidate_v1",
            "likelihood_map": likelihood,
            "likelihood_sum": _round(sum(float(value) for value in likelihood.values())),
            "time_conditioning_trace": {"local_phase_consumed_once_in_remaining_path": True},
            "physical_cause_consumption_trace": physical.get("evidence_consumption_trace", {}),
        },
        "source_observation_ids": snapshot.get("selected_source_observation_ids", []),
        "na_reasons": sorted(
            set(
                [*snapshot.get("na_reasons", []), *trajectory.get("na_reasons", []), *physical.get("na_reasons", []), *remaining.get("na_reasons", [])]
            )
        ),
        "guards": {
            "writes_archive_db": False,
            "writes_facts_sqlite": False,
            "writes_posterior": False,
            "places_orders": False,
            "historical_backfill": False,
            "changes_parameters": False,
        },
    }


def build_zuuu_daily_high_candidate_contract(candidate: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the shadow-candidate boundary without applying its result."""

    value = candidate if isinstance(candidate, Mapping) else {}
    issues: list[str] = []
    if value.get("schema") != SCHEMA_VERSION:
        issues.append("candidate_schema_invalid")
    if value.get("policy_version") != POLICY_VERSION:
        issues.append("candidate_policy_version_invalid")
    if value.get("candidate_only") is not True:
        issues.append("candidate_only_guard_missing")
    if value.get("production_applied") is not False:
        issues.append("production_applied_guard_invalid")

    guards = _as_mapping(value.get("guards"))
    for key in (
        "writes_archive_db",
        "writes_facts_sqlite",
        "writes_posterior",
        "places_orders",
        "historical_backfill",
        "changes_parameters",
    ):
        if guards.get(key) is not False:
            issues.append(f"guard_{key}_invalid")

    likelihood_row = _as_mapping(value.get("metar_strong_likelihood_candidate"))
    likelihood_map = _as_mapping(likelihood_row.get("likelihood_map"))
    likelihood_sum: float | None = 0.0
    if likelihood_row.get("schema") != "metar_strong_likelihood_candidate_v1":
        issues.append("candidate_likelihood_schema_invalid")
    if not likelihood_map:
        issues.append("candidate_likelihood_map_missing")
        likelihood_sum = None
    else:
        for probability in likelihood_map.values():
            parsed = _number(probability)
            if parsed is None:
                issues.append("candidate_likelihood_non_numeric")
                likelihood_sum = None
                break
            if parsed < -1e-12:
                issues.append("candidate_likelihood_negative")
            likelihood_sum += max(0.0, parsed)
        if likelihood_sum is not None and abs(likelihood_sum - 1.0) > 1e-6:
            issues.append("candidate_likelihood_not_normalized")
    if _as_mapping(likelihood_row.get("time_conditioning_trace")).get("local_phase_consumed_once_in_remaining_path") is not True:
        issues.append("local_phase_single_consumption_guard_missing")

    trajectory = _as_mapping(value.get("temperature_trajectory_posterior"))
    state_probs = _as_mapping(trajectory.get("state_probs"))
    trajectory_prob_sum: float | None = 0.0
    if not state_probs:
        issues.append("trajectory_state_probs_missing")
        trajectory_prob_sum = None
    else:
        for state in TRAJECTORY_STATES:
            parsed = _number(state_probs.get(state))
            if parsed is None:
                issues.append(f"trajectory_state_prob_{state}_missing")
                trajectory_prob_sum = None
                continue
            if parsed < -1e-12:
                issues.append("trajectory_state_prob_negative")
            if trajectory_prob_sum is not None:
                trajectory_prob_sum += max(0.0, parsed)
        if trajectory_prob_sum is not None and abs(trajectory_prob_sum - 1.0) > 1e-6:
            issues.append("trajectory_state_probs_not_normalized")
    transition = _as_mapping(trajectory.get("state_transition_trace"))
    if transition.get("single_report_direct_cutoff_forbidden") is not True:
        issues.append("single_report_cutoff_guard_missing")

    physical = _as_mapping(value.get("physical_environment_state_posterior"))
    physical_probs = _as_mapping(physical.get("state_probs"))
    physical_prob_sum: float | None = 0.0
    if not physical_probs:
        issues.append("physical_state_probs_missing")
        physical_prob_sum = None
    else:
        for state in PHYSICAL_STATES:
            parsed = _number(physical_probs.get(state))
            if parsed is None:
                issues.append(f"physical_state_prob_{state}_missing")
                physical_prob_sum = None
                continue
            if parsed < -1e-12:
                issues.append("physical_state_prob_negative")
            if physical_prob_sum is not None:
                physical_prob_sum += max(0.0, parsed)
        if physical_prob_sum is not None and abs(physical_prob_sum - 1.0) > 1e-6:
            issues.append("physical_state_probs_not_normalized")
    consumption = _as_mapping(physical.get("evidence_consumption_trace"))
    if consumption.get("direct_weather_alone_cannot_confirm_cutoff") is not True:
        issues.append("direct_weather_combination_guard_missing")

    remaining = _as_mapping(value.get("remaining_daily_high_distribution"))
    if remaining.get("hard_floor_applied") is not True:
        issues.append("remaining_hard_floor_guard_missing")
    remaining_map = _as_mapping(remaining.get("bucket_probabilities"))
    remaining_sum: float | None = 0.0
    if not remaining_map:
        issues.append("remaining_bucket_probabilities_missing")
        remaining_sum = None
    else:
        for probability in remaining_map.values():
            parsed = _number(probability)
            if parsed is None:
                issues.append("remaining_bucket_probability_non_numeric")
                remaining_sum = None
                break
            if remaining_sum is not None:
                remaining_sum += max(0.0, parsed)
        if remaining_sum is not None and abs(remaining_sum - 1.0) > 1e-6:
            issues.append("remaining_bucket_probabilities_not_normalized")

    return {
        "schema": "zuuu_daily_high_observation_candidate_contract_v1",
        "ok": not issues,
        "state": "ready" if not issues else "blocked",
        "issues": sorted(set(issues)),
        "candidate_likelihood_sum": _round(likelihood_sum),
        "trajectory_state_prob_sum": _round(trajectory_prob_sum),
        "physical_state_prob_sum": _round(physical_prob_sum),
        "remaining_bucket_probability_sum": _round(remaining_sum),
        "candidate_only": value.get("candidate_only") is True,
        "production_applied": value.get("production_applied") is True,
    }
