"""Leakage-safe adapter from canonical AMSC events to one v6 emission vector."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .observation_model import ObservationFeatureVector


@dataclass(frozen=True)
class CanonicalEmissionInput:
    event_id: str
    event_valid_at: str
    is_regular_metar: bool
    reported_temperature_c: float
    cloud_suppressed: bool
    precipitation_present: bool
    cb_present: bool
    features: ObservationFeatureVector
    pre_event_history_sha256: str
    input_sha256: str


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


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("parsed_observation")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(_text(event.get("parsed_observation_json")))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _weather_signature(cloud_text: str, weather_text: str) -> tuple[str, bool, bool, bool]:
    cloud = cloud_text.upper()
    weather = weather_text.upper()
    cb = "CB" in cloud or "TCU" in cloud or "TS" in weather
    precipitation = any(code in weather for code in ("RA", "DZ", "SN", "SG", "PL", "GR", "GS"))
    if any(code in cloud for code in ("OVC", "VV")):
        cloud_regime = "overcast_or_obscured"
    elif "BKN" in cloud:
        cloud_regime = "broken"
    elif "SCT" in cloud:
        cloud_regime = "scattered"
    else:
        cloud_regime = "clear_or_few"
    cloud_suppressed = cloud_regime in {"broken", "overcast_or_obscured"}
    weather_regime = "convective" if cb else "precipitation" if precipitation else "none"
    signature = f"cloud={cloud_regime}|weather={weather_regime}|convective={int(cb)}"
    return signature, cloud_suppressed, precipitation, cb


def build_canonical_emission_input(
    *,
    canonical_event: Mapping[str, Any],
    pre_event_history: Sequence[Mapping[str, Any]],
) -> CanonicalEmissionInput:
    event_id = _text(canonical_event.get("canonical_observation_event_id"))
    event_valid_at = _text(canonical_event.get("valid_time"))
    event_time = _time(event_valid_at)
    report_type = _text(canonical_event.get("report_type")).upper()
    payload = _payload(canonical_event)
    temperature = _finite(payload.get("temperature_c"))
    dewpoint = _finite(payload.get("dewpoint_c"))
    if (
        not event_id
        or event_time is None
        or report_type not in {"METAR", "SPECI"}
        or temperature is None
        or not temperature.is_integer()
    ):
        raise ValueError("canonical_emission_event_invalid")
    history_rows = []
    for row in pre_event_history:
        history_id = _text(row.get("canonical_observation_event_id"))
        history_time = _time(row.get("valid_time"))
        history_payload = _payload(row)
        history_temperature = _finite(history_payload.get("temperature_c"))
        if history_id == event_id:
            raise ValueError("canonical_emission_current_event_in_pre_event_history")
        if history_time is None or history_time >= event_time:
            raise ValueError("canonical_emission_history_not_strictly_pre_event")
        if history_temperature is not None:
            history_rows.append((history_time, history_id, history_temperature))
    history_rows.sort()
    current_step_rate = None
    if history_rows:
        previous_time, _previous_id, previous_temperature = history_rows[-1]
        hours = (event_time - previous_time).total_seconds() / 3600.0
        if hours > 0.0:
            current_step_rate = (temperature - previous_temperature) / hours
    depression = temperature - dewpoint if dewpoint is not None else None
    signature, cloud_suppressed, precipitation, cb = _weather_signature(
        _text(payload.get("cloud_text")), _text(payload.get("weather_text"))
    )
    history_identity = [
        {"valid_time": time.isoformat(), "event_id": identity, "temperature_c": value}
        for time, identity, value in history_rows
    ]
    core = {
        "event_id": event_id,
        "event_valid_at": event_valid_at,
        "report_type": report_type,
        "reported_temperature_c": temperature,
        "current_step_rate_c_per_hour": current_step_rate,
        "dewpoint_depression_c": depression,
        "weather_signature": signature,
        "cloud_suppressed": cloud_suppressed,
        "precipitation_present": precipitation,
        "cb_present": cb,
        "pre_event_history_sha256": _sha(history_identity),
    }
    return CanonicalEmissionInput(
        event_id=event_id,
        event_valid_at=event_valid_at,
        is_regular_metar=report_type == "METAR",
        reported_temperature_c=temperature,
        cloud_suppressed=cloud_suppressed,
        precipitation_present=precipitation,
        cb_present=cb,
        features=ObservationFeatureVector(
            current_step_rate_c_per_hour=current_step_rate,
            dewpoint_depression_c=depression,
            weather_signature=signature,
        ),
        pre_event_history_sha256=core["pre_event_history_sha256"],
        input_sha256=_sha(core),
    )


__all__ = ["CanonicalEmissionInput", "build_canonical_emission_input"]
