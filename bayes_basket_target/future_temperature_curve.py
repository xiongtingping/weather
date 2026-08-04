"""Content-addressed future hourly temperature curve bound to one P0/P0-M root."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256


FUTURE_CURVE_SCHEMA = "bayes_basket_root_future_temperature_curve_v1"
NATIVE_FAMILY_CURVE_SCHEMA = (
    "bayes_basket_native_family_temperature_curve_v1"
)
FUTURE_CURVE_FEATURES_SCHEMA = "bayes_basket_future_temperature_curve_features_v1"
MAX_CURVE_POINTS = 72


def _utc(name: str, value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def _sha(name: str, value: Any) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid")
    return result


@dataclass(frozen=True)
class RootFutureTemperatureCurve:
    station_id: str
    event_date: str
    settlement_timezone: str
    root_checkpoint_id: str
    root_probability_sha256: str
    available_at_utc: str
    source_snapshot_sha256: str
    independence_family_ids: tuple[str, ...]
    points: tuple[tuple[str, float], ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RootFutureTemperatureCurve":
        expected = {
            "schema", "station_id", "event_date", "settlement_timezone",
            "root_checkpoint_id", "root_probability_sha256", "available_at_utc",
            "source_snapshot_sha256", "independence_family_ids", "points",
            "content_sha256",
        }
        if set(raw) != expected or raw.get("schema") != FUTURE_CURVE_SCHEMA:
            raise ContractError("future_temperature_curve_schema_invalid")
        families_raw = raw.get("independence_family_ids")
        if not isinstance(families_raw, Sequence) or isinstance(
            families_raw, (str, bytes, bytearray)
        ):
            raise ContractError("future_temperature_curve_families_invalid")
        families = tuple(sorted({str(value or "").strip() for value in families_raw}))
        if len(families) < 2 or any(not value for value in families):
            raise ContractError("future_temperature_curve_independent_families_insufficient")
        points_raw = raw.get("points")
        if (
            not isinstance(points_raw, Sequence)
            or isinstance(points_raw, (str, bytes, bytearray))
            or len(points_raw) < 4
            or len(points_raw) > MAX_CURVE_POINTS
        ):
            raise ContractError("future_temperature_curve_point_count_invalid")
        points: list[tuple[str, float]] = []
        for point in points_raw:
            if not isinstance(point, Mapping) or set(point) != {
                "valid_time_utc", "temperature_c"
            }:
                raise ContractError("future_temperature_curve_point_invalid")
            points.append((
                _stamp(_utc("future_temperature_curve_valid_time", point["valid_time_utc"])),
                _finite("future_temperature_curve_temperature_c", point["temperature_c"]),
            ))
        if points != sorted(points) or len({point[0] for point in points}) != len(points):
            raise ContractError("future_temperature_curve_time_order_invalid")
        times = [_utc("future_temperature_curve_valid_time", point[0]) for point in points]
        if any(
            (right - left).total_seconds() <= 0
            or (right - left).total_seconds() > 6 * 3600
            for left, right in zip(times, times[1:])
        ):
            raise ContractError("future_temperature_curve_gap_invalid")
        timezone_name = str(raw.get("settlement_timezone") or "").strip()
        try:
            zone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ContractError("future_temperature_curve_timezone_invalid") from exc
        event_date = str(raw.get("event_date") or "").strip()
        if any(time.astimezone(zone).date().isoformat() != event_date for time in times):
            raise ContractError("future_temperature_curve_event_date_mismatch")
        body = {key: raw[key] for key in raw if key != "content_sha256"}
        if _sha("future_temperature_curve_content_sha256", raw["content_sha256"]) != content_sha256(body):
            raise ContractError("future_temperature_curve_content_sha256_mismatch")
        available = _stamp(_utc("future_temperature_curve_available_at", raw["available_at_utc"]))
        return cls(
            station_id=str(raw.get("station_id") or "").strip().upper(),
            event_date=event_date,
            settlement_timezone=timezone_name,
            root_checkpoint_id=str(raw.get("root_checkpoint_id") or "").strip(),
            root_probability_sha256=_sha("future_temperature_curve_root_probability", raw["root_probability_sha256"]),
            available_at_utc=available,
            source_snapshot_sha256=_sha("future_temperature_curve_snapshot", raw["source_snapshot_sha256"]),
            independence_family_ids=families,
            points=tuple(points),
            content_sha256=str(raw["content_sha256"]).lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FUTURE_CURVE_SCHEMA,
            "station_id": self.station_id,
            "event_date": self.event_date,
            "settlement_timezone": self.settlement_timezone,
            "root_checkpoint_id": self.root_checkpoint_id,
            "root_probability_sha256": self.root_probability_sha256,
            "available_at_utc": self.available_at_utc,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "independence_family_ids": list(self.independence_family_ids),
            "points": [
                {"valid_time_utc": valid, "temperature_c": temperature}
                for valid, temperature in self.points
            ],
            "content_sha256": self.content_sha256,
        }


def build_root_future_temperature_curve(payload: Mapping[str, Any]) -> RootFutureTemperatureCurve:
    raw = dict(payload)
    points_raw = raw.get("points")
    if not isinstance(points_raw, Sequence) or isinstance(
        points_raw, (str, bytes, bytearray)
    ):
        raise ContractError("future_temperature_curve_point_count_invalid")
    families_raw = raw.get("independence_family_ids")
    if not isinstance(families_raw, Sequence) or isinstance(
        families_raw, (str, bytes, bytearray)
    ):
        raise ContractError("future_temperature_curve_families_invalid")
    body = {
        "schema": FUTURE_CURVE_SCHEMA,
        "station_id": str(raw.get("station_id") or "").strip().upper(),
        "event_date": str(raw.get("event_date") or "").strip(),
        "settlement_timezone": str(raw.get("settlement_timezone") or "").strip(),
        "root_checkpoint_id": str(raw.get("root_checkpoint_id") or "").strip(),
        "root_probability_sha256": str(raw.get("root_probability_sha256") or "").strip().lower(),
        "available_at_utc": _stamp(_utc("future_temperature_curve_available_at", raw.get("available_at_utc"))),
        "source_snapshot_sha256": str(raw.get("source_snapshot_sha256") or "").strip().lower(),
        "independence_family_ids": sorted({str(value or "").strip() for value in families_raw}),
        "points": [
            {
                "valid_time_utc": _stamp(_utc("future_temperature_curve_valid_time", point.get("valid_time_utc"))),
                "temperature_c": _finite("future_temperature_curve_temperature_c", point.get("temperature_c")),
            }
            for point in points_raw
            if isinstance(point, Mapping)
        ],
    }
    return RootFutureTemperatureCurve.from_mapping({
        **body,
        "content_sha256": content_sha256(body),
    })


def build_native_family_temperature_curve(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal one native family mean curve produced by the same field decode."""

    points_raw = payload.get("points")
    if (
        not isinstance(points_raw, Sequence)
        or isinstance(points_raw, (str, bytes, bytearray))
        or len(points_raw) < 4
        or len(points_raw) > MAX_CURVE_POINTS
    ):
        raise ContractError("native_family_temperature_curve_point_count_invalid")
    timezone_name = str(payload.get("settlement_timezone") or "").strip()
    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ContractError("native_family_temperature_curve_timezone_invalid") from exc
    event_date = str(payload.get("event_date") or "").strip()
    points = [
        {
            "valid_time_utc": _stamp(_utc(
                "native_family_temperature_curve_valid_time",
                point.get("valid_time_utc"),
            )),
            "temperature_c": _finite(
                "native_family_temperature_curve_temperature_c",
                point.get("temperature_c"),
            ),
        }
        for point in points_raw
        if isinstance(point, Mapping)
    ]
    if len(points) != len(points_raw):
        raise ContractError("native_family_temperature_curve_point_invalid")
    times = [
        _utc("native_family_temperature_curve_valid_time", point["valid_time_utc"])
        for point in points
    ]
    if (
        points != sorted(points, key=lambda point: point["valid_time_utc"])
        or len({point["valid_time_utc"] for point in points}) != len(points)
        or any(
            (right - left).total_seconds() <= 0
            or (right - left).total_seconds() > 6 * 3600
            for left, right in zip(times, times[1:])
        )
        or any(time.astimezone(zone).date().isoformat() != event_date for time in times)
    ):
        raise ContractError("native_family_temperature_curve_time_scope_invalid")
    body = {
        "schema": NATIVE_FAMILY_CURVE_SCHEMA,
        "station_id": str(payload.get("station_id") or "").strip().upper(),
        "event_date": event_date,
        "settlement_timezone": timezone_name,
        "ensemble_system_id": str(payload.get("ensemble_system_id") or "").strip(),
        "independence_family_id": str(payload.get("independence_family_id") or "").strip(),
        "available_at_utc": _stamp(_utc(
            "native_family_temperature_curve_available_at",
            payload.get("available_at_utc"),
        )),
        "source_field_receipt_set_sha256": _sha(
            "native_family_temperature_curve_receipt_set",
            payload.get("source_field_receipt_set_sha256"),
        ),
        "points": points,
    }
    if any(not body[key] for key in (
        "station_id", "event_date", "ensemble_system_id", "independence_family_id"
    )):
        raise ContractError("native_family_temperature_curve_scope_invalid")
    return {**body, "content_sha256": content_sha256(body)}


def compile_future_curve_features(
    curve: RootFutureTemperatureCurve,
    *,
    as_of_utc: str,
    running_high_c: float,
) -> dict[str, Any]:
    decision = _utc("future_temperature_curve_as_of", as_of_utc)
    if _utc("future_temperature_curve_available_at", curve.available_at_utc) > decision:
        raise ContractError("future_temperature_curve_not_strict_asof")
    future = [
        (_utc("future_temperature_curve_valid_time", valid), temperature)
        for valid, temperature in curve.points
        if _utc("future_temperature_curve_valid_time", valid) > decision
    ]
    if len(future) < 2:
        raise ContractError("future_temperature_curve_future_points_insufficient")
    peak_time, peak_temperature = max(future, key=lambda item: (item[1], -item[0].timestamp()))
    first_time, first_temperature = future[0]
    last_time, last_temperature = future[-1]
    horizon_hours = (last_time - first_time).total_seconds() / 3600.0
    if horizon_hours <= 0.0:
        raise ContractError("future_temperature_curve_horizon_invalid")
    result = {
        "schema": FUTURE_CURVE_FEATURES_SCHEMA,
        "curve_sha256": curve.content_sha256,
        "future_point_count": len(future),
        "predicted_peak_temperature_c": peak_temperature,
        "predicted_peak_time_utc": _stamp(peak_time),
        "minutes_to_predicted_peak": max(0.0, (peak_time - decision).total_seconds() / 60.0),
        "predicted_peak_remaining_warming_c": max(0.0, peak_temperature - running_high_c),
        "next_curve_temperature_c": first_temperature,
        "next_curve_delta_from_high_c": first_temperature - running_high_c,
        "future_curve_slope_c_per_hour": (last_temperature - first_temperature) / horizon_hours,
    }
    result["content_sha256"] = content_sha256(result)
    return result


__all__ = [
    "FUTURE_CURVE_FEATURES_SCHEMA",
    "FUTURE_CURVE_SCHEMA",
    "NATIVE_FAMILY_CURVE_SCHEMA",
    "RootFutureTemperatureCurve",
    "build_root_future_temperature_curve",
    "build_native_family_temperature_curve",
    "compile_future_curve_features",
]
