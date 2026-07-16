"""Derive an immutable weak-model context from forecast-only as-of evidence.

The context is intentionally small: calendar season, a forecast-declared
weather regime, and the decision's local lead/time bucket.  It never inspects
METAR/SPECI, the later physical-X label, market state, or trading state.  When
providers do not expose a normalised forecast regime, it records the explicit
``unknown_forecast_regime`` category rather than inventing one from reality.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Mapping, Sequence

from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE


SCHEMA_VERSION = "forecast_weak_context_capture_v1"
IDENTITY_KIND_STATION_DAY = "station_day"
IDENTITY_KIND_MARKET_TARGET = "market_target"
_REGIMES = frozenset(
    {
        "clear_dry",
        "cloudy_stable",
        "wet_precipitation",
        "convection_or_showers",
        "fog_low_cloud",
        "unknown_forecast_regime",
        "mixed_forecast_regime",
    }
)
_FORBIDDEN_TOKENS = ("market", "price", "order", "position", "pnl", "edge", "lcb", "metar", "observation", "current_")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _forbidden(value: Any, *, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = _text(key).lower()
            child_path = f"{path}.{name}" if path else name
            if any(token in name for token in _FORBIDDEN_TOKENS):
                hits.append(child_path)
            hits.extend(_forbidden(child, path=child_path))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            hits.extend(_forbidden(child, path=f"{path}[{index}]"))
    return hits


def _season(month: int) -> str:
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def _lead_bucket(*, local_as_of: datetime, event_local_day: datetime) -> str:
    hours = (local_as_of - event_local_day).total_seconds() / 3600.0
    if hours < -24.0:
        return "d_minus_2_or_earlier"
    if hours < 0.0:
        return "d_minus_1"
    if hours < 6.0:
        return "d0_0000_0559"
    if hours < 9.0:
        return "d0_0600_0859"
    if hours < 12.0:
        return "d0_0900_1159"
    if hours < 15.0:
        return "d0_1200_1459"
    return "d0_1500_or_later"


def build_forecast_weak_context_capture(
    *,
    event_date: str,
    decision_as_of: str,
    strict_forecast_observations: Sequence[Mapping[str, Any]],
    target_identity: str = "",
    station_id: str = "ZUUU",
    target_timezone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Return a one-cutoff forecast-only context capture, or fail closed."""

    target = _text(target_identity)
    day = _text(event_date)[:10]
    station = _text(station_id).upper()
    cutoff = _time(decision_as_of)
    issues: list[str] = []
    try:
        zone = ZoneInfo(_text(target_timezone) or "Asia/Shanghai")
    except Exception:
        zone = None
        issues.append("forecast_context_target_timezone_invalid")
    if not day or not station or cutoff is None or zone is None:
        issues.append("forecast_context_target_event_date_or_decision_asof_missing_or_invalid")
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": sorted(set(issues)), "read_only": True}
    try:
        event_local_day = datetime.fromisoformat(day).replace(tzinfo=zone)
    except ValueError:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": ["forecast_context_event_date_invalid"], "read_only": True}
    rows: list[dict[str, Any]] = []
    for value in strict_forecast_observations:
        row = _mapping(value)
        payload = _mapping(row.get("payload"))
        source_id = _text(row.get("source_id"))
        available = _time(row.get("fetched_at"))
        if _text(row.get("event_date"))[:10] != day:
            continue
        if available is None or available > cutoff:
            continue
        if source_id not in SOURCE_UNIVERSE or _text(row.get("source_class")) != "forecast_weak_source":
            issues.append("forecast_context_forecast_source_invalid")
            continue
        if payload.get("evidence_tier") != "A_live_observed_transport" or payload.get("response_headers_observed") is not True:
            issues.append("forecast_context_forecast_not_tier_a")
            continue
        surface = {
            "source_id": source_id,
            "source_event_id": _text(row.get("source_event_id")),
            "forecast_peak_c": row.get("forecast_peak_c"),
            "forecast_weather_regime": payload.get("forecast_weather_regime"),
        }
        if _forbidden(surface):
            issues.append("forecast_context_forecast_contains_forbidden_input")
            continue
        if not surface["source_event_id"] or not _text(row.get("raw_sha256")):
            issues.append("forecast_context_forecast_lineage_missing")
            continue
        rows.append(
            {
                "source_id": source_id,
                "source_event_id": surface["source_event_id"],
                "raw_sha256": _text(row.get("raw_sha256")),
                "available_at": available.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
                "forecast_weather_regime": _text(payload.get("forecast_weather_regime")).lower(),
            }
        )
    if not rows:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set([*issues, "forecast_context_no_tier_a_forecast_visible_at_cutoff"])),
            "read_only": True,
        }
    regimes = {row["forecast_weather_regime"] for row in rows if row["forecast_weather_regime"]}
    if any(regime not in _REGIMES - {"unknown_forecast_regime", "mixed_forecast_regime"} for regime in regimes):
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["forecast_context_weather_regime_not_normalized"],
            "read_only": True,
        }
    regime = "unknown_forecast_regime" if not regimes else next(iter(regimes)) if len(regimes) == 1 else "mixed_forecast_regime"
    local_cutoff = cutoff.astimezone(zone)
    raw_identity = sorted(rows, key=lambda row: (row["source_id"], row["available_at"], row["source_event_id"]))
    context = {
        "season": _season(event_local_day.month),
        "weather_regime": regime,
        "lead_bucket": _lead_bucket(local_as_of=local_cutoff, event_local_day=event_local_day),
    }
    # Physical-X learning is station-day based.  A market target may annotate
    # a capture when one exists, but it is never a prerequisite for forecast
    # calibration samples or for their final-X labels.
    identity_kind = IDENTITY_KIND_MARKET_TARGET if target else IDENTITY_KIND_STATION_DAY
    training_identity = target if target else f"station_day:{station}:{day}"
    core = {
        "schema": SCHEMA_VERSION,
        "identity_kind": identity_kind,
        "training_identity": training_identity,
        "station_id": station,
        "event_date": day,
        "decision_as_of": cutoff.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
        "context_available_at": max(row["available_at"] for row in raw_identity),
        "context_source_class": "forecast_weak_context",
        "forecast_context": context,
        "forecast_raw_lineage": raw_identity,
        "context_raw_sha256": _sha(raw_identity),
        "target_timezone": str(zone),
    }
    if target:
        core["target_identity"] = target
    return {
        **core,
        "context_capture_id": f"fwctx_{_sha(core)[:24]}",
        "ok": True,
        "state": "available",
        "read_only": True,
        "boundary": {
            "forecast_only": True,
            "station_day_training_identity_without_market_target": identity_kind == IDENTITY_KIND_STATION_DAY,
            "d0_observation_not_consumed": True,
            "final_label_not_consumed": True,
            "market_or_trading_not_consumed": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = [
    "IDENTITY_KIND_MARKET_TARGET",
    "IDENTITY_KIND_STATION_DAY",
    "SCHEMA_VERSION",
    "build_forecast_weak_context_capture",
]
