"""Official NWS hourly-forecast argmax windows for the ASOS live side lane.

The forecast is timing evidence only.  It must never provide a temperature
bucket, probability, P0, or P0-M input.  A tradable bucket remains derived
exclusively from a newly observed ASOS/MADIS station-day high.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests
from zoneinfo import ZoneInfo

from bayes_basket_target.station_registry import station_coordinates


SCHEMA = "asos_nws_hourly_argmax_window_v1"
PROVIDER_ID = "NOAA_NWS_GRIDPOINT_HOURLY"
SOURCE_CLASS = "official_forecast_product"
USER_AGENT = "weather-basket-asos-argmax/1.0 local-production"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
LIVE_STATIONS = frozenset({
    "KATL", "KAUS", "KDAL", "KHOU", "KLAX",
    "KLGA", "KMIA", "KORD", "KSEA", "KSFO",
})
CANONICAL_EVENT_TIMEZONE = "America/New_York"
ARGMAX_SELECTION_RULE = "LATEST_CONTIGUOUS_ARGMAX_CLUSTER_V1"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("argmax_datetime_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def select_latest_contiguous_argmax_periods(
    periods: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return only the latest contiguous cluster from tied argmax periods.

    This is intentionally conservative for execution: an earlier tied peak is
    discarded rather than allowing a trade while the station can still return
    to the same forecast maximum later in the day.  Invalid periods are not
    repaired or guessed; if none remain, the caller fails closed.
    """

    valid: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for item in periods:
        if not isinstance(item, Mapping):
            continue
        try:
            start = _utc(str(item.get("start_utc") or ""))
            end = _utc(str(item.get("end_utc") or ""))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        valid.append((start, end, dict(item)))
    valid.sort(key=lambda item: (item[0], item[1]))
    if not valid:
        return []

    clusters: list[list[tuple[datetime, datetime, dict[str, Any]]]] = []
    cluster_end: datetime | None = None
    for item in valid:
        if cluster_end is None or item[0] > cluster_end:
            clusters.append([item])
            cluster_end = item[1]
            continue
        clusters[-1].append(item)
        cluster_end = max(cluster_end, item[1])

    selected = max(
        clusters,
        key=lambda cluster: (
            max(item[1] for item in cluster),
            min(item[0] for item in cluster),
        ),
    )
    return [item[2] for item in selected]


def _bounded_get_json(
    url: str,
    *,
    timeout: float,
    get: Callable[..., Any] = requests.get,
) -> tuple[dict[str, Any], bytes]:
    response = get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = bytes(response.content or b"")
    if not payload or len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("nws_hourly_response_size_invalid")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("nws_hourly_response_not_object")
    return decoded, payload


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?)?$"
)


def _grid_temperature_as_periods(grid: Mapping[str, Any]) -> dict[str, Any]:
    properties = grid.get("properties")
    temperature = properties.get("temperature") if isinstance(properties, Mapping) else None
    values = temperature.get("values") if isinstance(temperature, Mapping) else None
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ValueError("nws_grid_temperature_values_missing")
    periods: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping) or item.get("value") is None:
            continue
        valid_time = str(item.get("validTime") or "")
        if "/" not in valid_time:
            continue
        start_text, duration_text = valid_time.split("/", 1)
        match = _DURATION_RE.fullmatch(duration_text)
        if match is None:
            continue
        try:
            start = _utc(start_text)
            duration = timedelta(
                days=int(match.group("days") or 0),
                hours=int(match.group("hours") or 0),
                minutes=int(match.group("minutes") or 0),
            )
            value_c = float(item["value"])
        except (TypeError, ValueError):
            continue
        if duration.total_seconds() <= 0:
            continue
        periods.append({
            "startTime": _iso(start),
            "endTime": _iso(start + duration),
            "temperature": value_c,
            "temperatureUnit": "C",
            "forecastProductVariant": "grid_temperature_values",
        })
    if not periods:
        raise ValueError("nws_grid_temperature_periods_missing")
    generated = (
        str(properties.get("updateTime") or properties.get("validTimes") or "").split("/", 1)[0]
        if isinstance(properties, Mapping) else ""
    )
    return {
        "properties": {
            "generatedAt": generated,
            "periods": periods,
            "forecastProductVariant": "grid_temperature_values",
        }
    }


def derive_argmax_window(
    forecast: Mapping[str, Any],
    *,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    fetched_at_utc: str,
    points_receipt_sha256: str,
    forecast_receipt_sha256: str,
    raw_object_ref: str,
    coordinate_registry_sha256: str,
) -> dict[str, Any]:
    """Derive the exact maximum-temperature period envelope for one ET day."""

    properties = forecast.get("properties")
    if not isinstance(properties, Mapping):
        raise ValueError("nws_hourly_properties_missing")
    periods = properties.get("periods")
    if not isinstance(periods, Sequence) or isinstance(periods, (str, bytes)):
        raise ValueError("nws_hourly_periods_missing")
    target_tz = ZoneInfo(str(settlement_timezone))
    product_variant = str(
        properties.get("forecastProductVariant") or "hourly_periods"
    )
    candidates: list[dict[str, Any]] = []
    for item in periods:
        if not isinstance(item, Mapping):
            continue
        try:
            start = _utc(str(item.get("startTime") or ""))
            end = _utc(str(item.get("endTime") or ""))
            temperature = float(item["temperature"])
        except (KeyError, TypeError, ValueError):
            continue
        unit = str(item.get("temperatureUnit") or "").upper()
        if unit not in {"F", "C"} or end <= start:
            continue
        if start.astimezone(target_tz).date().isoformat() != event_date:
            continue
        normalized_c = (temperature - 32.0) * 5.0 / 9.0 if unit == "F" else temperature
        candidates.append({
            "start_utc": _iso(start),
            "end_utc": _iso(end),
            "temperature": temperature,
            "temperature_unit": unit,
            "temperature_c": normalized_c,
            "forecast_product_variant": str(
                item.get("forecastProductVariant") or product_variant
            ),
        })
    if not candidates:
        raise ValueError("nws_hourly_event_date_missing")
    maximum_c = max(item["temperature_c"] for item in candidates)
    all_maxima = [
        item for item in candidates
        if abs(float(item["temperature_c"]) - maximum_c) < 1e-9
    ]
    maxima = select_latest_contiguous_argmax_periods(all_maxima)
    if not maxima:
        raise ValueError("nws_hourly_argmax_periods_invalid")
    for item in maxima:
        item["selection_rule"] = ARGMAX_SELECTION_RULE
        item["candidate_argmax_period_count"] = len(all_maxima)
    window_start = min(_utc(item["start_utc"]) for item in maxima)
    window_end = max(_utc(item["end_utc"]) for item in maxima)
    fetched_at = _utc(fetched_at_utc)
    if fetched_at >= window_start:
        raise ValueError("argmax_window_not_frozen_before_start")
    generated_at = str(properties.get("generatedAt") or properties.get("updateTime") or "")
    generated_utc = _iso(_utc(generated_at))
    fact = {
        "schema": SCHEMA,
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "provider_id": PROVIDER_ID,
        "source_class": SOURCE_CLASS,
        "forecast_generated_at_utc": generated_utc,
        "fetched_at_utc": _iso(fetched_at),
        "argmax_window_start_utc": _iso(window_start),
        "argmax_window_end_utc": _iso(window_end),
        "argmax_temperature_c": round(maximum_c, 6),
        "argmax_periods": maxima,
        "forecast_product_variant": product_variant,
        "points_receipt_sha256": points_receipt_sha256,
        "forecast_receipt_sha256": forecast_receipt_sha256,
        "raw_object_ref": raw_object_ref,
        "coordinate_registry_sha256": coordinate_registry_sha256,
        "timing_only": True,
        "provides_temperature_bucket": False,
        "provides_probability": False,
        "enters_p0": False,
        "enters_p0m": False,
    }
    fact["content_sha256"] = _sha(_canonical(fact))
    fact["window_id"] = f"asosargmax_{fact['content_sha256'][:32]}"
    return fact


def fetch_station_argmax_windows(
    station_id: str,
    scopes: Sequence[tuple[str, str]],
    *,
    fetched_at_utc: str,
    raw_root: Path,
    timeout: float = 10.0,
    get: Callable[..., Any] = requests.get,
) -> list[dict[str, Any]]:
    """Fetch one bounded NWS curve and derive all requested event-day windows."""

    station = str(station_id or "").strip().upper()
    if station not in LIVE_STATIONS:
        raise ValueError("asos_argmax_station_not_allowlisted")
    coordinate = station_coordinates(station)
    points_url = (
        "https://api.weather.gov/points/"
        f"{coordinate['latitude']},{coordinate['longitude']}"
    )
    points, points_raw = _bounded_get_json(points_url, timeout=timeout, get=get)
    points_properties = points.get("properties")
    hourly_url = (
        str(points_properties.get("forecastHourly") or "")
        if isinstance(points_properties, Mapping) else ""
    )
    if not hourly_url.startswith("https://api.weather.gov/"):
        raise ValueError("nws_hourly_url_invalid")
    try:
        forecast, forecast_raw = _bounded_get_json(hourly_url, timeout=timeout, get=get)
    except Exception as hourly_exc:
        grid_url = (
            str(points_properties.get("forecastGridData") or "")
            if isinstance(points_properties, Mapping) else ""
        )
        if not grid_url.startswith("https://api.weather.gov/"):
            raise hourly_exc
        grid, forecast_raw = _bounded_get_json(grid_url, timeout=timeout, get=get)
        forecast = _grid_temperature_as_periods(grid)
    forecast_sha = _sha(forecast_raw)
    raw_root.mkdir(parents=True, exist_ok=True)
    retained = raw_root / f"{forecast_sha}.json"
    if not retained.exists():
        temporary = retained.with_name(f".{retained.name}.tmp.{os.getpid()}")
        temporary.write_bytes(forecast_raw)
        os.replace(temporary, retained)
    results = []
    for event_date, settlement_timezone in scopes:
        results.append(derive_argmax_window(
            forecast,
            station_id=station,
            event_date=str(event_date),
            settlement_timezone=str(settlement_timezone),
            fetched_at_utc=fetched_at_utc,
            points_receipt_sha256=_sha(points_raw),
            forecast_receipt_sha256=forecast_sha,
            raw_object_ref=str(retained),
            coordinate_registry_sha256=str(coordinate["registry_sha256"]),
        ))
    return results


def commit_argmax_windows(
    conn: sqlite3.Connection, windows: Sequence[Mapping[str, Any]]
) -> int:
    inserted = 0
    for item in windows:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO reported_trajectory_asos_argmax_windows(
                 window_id,station_id,event_date,settlement_timezone,provider_id,
                 source_class,forecast_generated_at_utc,fetched_at_utc,
                 argmax_window_start_utc,argmax_window_end_utc,argmax_temperature_c,
                 argmax_periods_json,points_receipt_sha256,forecast_receipt_sha256,
                 raw_object_ref,coordinate_registry_sha256,content_sha256,state,
                 timing_only,provides_temperature_bucket,provides_probability,
                 enters_p0,enters_p0m
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'FROZEN',1,0,0,0,0)""",
            (
                item["window_id"], item["station_id"], item["event_date"],
                item["settlement_timezone"], item["provider_id"], item["source_class"],
                item["forecast_generated_at_utc"], item["fetched_at_utc"],
                item["argmax_window_start_utc"], item["argmax_window_end_utc"],
                item["argmax_temperature_c"],
                json.dumps(item["argmax_periods"], sort_keys=True, separators=(",", ":")),
                item["points_receipt_sha256"], item["forecast_receipt_sha256"],
                item["raw_object_ref"], item["coordinate_registry_sha256"],
                item["content_sha256"],
            ),
        )
        inserted += int(cursor.rowcount or 0)
    return inserted


def materialize_missing_argmax_windows(
    conn: sqlite3.Connection,
    station_ids: Sequence[str],
    *,
    now_utc: datetime | None = None,
    raw_root: Path,
    minimum_retry_seconds: float = 900.0,
    timeout: float = 10.0,
    get: Callable[..., Any] = requests.get,
) -> dict[str, Any]:
    """Freeze missing active station/day windows without touching P0/P0-M."""

    stations = sorted({
        str(value or "").strip().upper() for value in station_ids
        if str(value or "").strip().upper() in LIVE_STATIONS
    })
    if not stations:
        return {"ok": True, "state": "no_live_station", "stations": []}
    placeholders = ",".join("?" for _ in stations)
    rows = conn.execute(
        f"""SELECT DISTINCT r.station_id,r.event_date,r.settlement_timezone
               FROM reported_trajectory_market_registry r
          LEFT JOIN reported_trajectory_asos_argmax_windows w
                 ON w.station_id=r.station_id AND w.event_date=r.event_date
              WHERE r.station_id IN ({placeholders})
                AND r.source_lane='awc' AND r.source_eligible=1
                AND r.market_active=1 AND r.market_accepting_orders=1
                AND w.window_id IS NULL
           ORDER BY r.station_id,r.event_date""",
        stations,
    ).fetchall()
    grouped: dict[str, list[tuple[str, str]]] = {}
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    canonical_today = now.astimezone(ZoneInfo(CANONICAL_EVENT_TIMEZONE)).date()
    for station, event_date, _registry_timezone in rows:
        try:
            scope_date = datetime.strptime(str(event_date), "%Y-%m-%d").date()
        except ValueError:
            continue
        if scope_date < canonical_today:
            continue
        grouped.setdefault(str(station), []).append(
            (str(event_date), CANONICAL_EVENT_TIMEZONE)
        )
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for station, scopes in grouped.items():
        prior = conn.execute(
            "SELECT attempted_at_utc FROM reported_trajectory_asos_argmax_fetch_state WHERE station_id=?",
            (station,),
        ).fetchone()
        if prior is not None:
            age = (now - _utc(str(prior[0]))).total_seconds()
            if age < max(60.0, float(minimum_retry_seconds)):
                results.append({
                    "station_id": station,
                    "state": "rate_limited_cached",
                    "source_age_sec": round(max(0.0, age), 3),
                })
                continue
        attempted_at = _iso(now)
        try:
            windows = fetch_station_argmax_windows(
                station, scopes, fetched_at_utc=attempted_at,
                raw_root=raw_root, timeout=timeout, get=get,
            )
            inserted = commit_argmax_windows(conn, windows)
            conn.execute(
                """INSERT INTO reported_trajectory_asos_argmax_fetch_state(
                     station_id,attempted_at_utc,state,reason,updated_at_utc
                   ) VALUES(?,?,'AVAILABLE',NULL,?)
                   ON CONFLICT(station_id) DO UPDATE SET
                     attempted_at_utc=excluded.attempted_at_utc,
                     state=excluded.state,reason=NULL,updated_at_utc=excluded.updated_at_utc""",
                (station, attempted_at, attempted_at),
            )
            conn.commit()
            results.append({
                "station_id": station,
                "state": "available",
                "frozen_window_count": inserted,
                "scopes": [list(scope) for scope in scopes],
            })
        except Exception as exc:
            conn.rollback()
            reason = f"{type(exc).__name__}:{exc}"
            conn.execute(
                """INSERT INTO reported_trajectory_asos_argmax_fetch_state(
                     station_id,attempted_at_utc,state,reason,updated_at_utc
                   ) VALUES(?,?,'BLOCKED',?,?)
                   ON CONFLICT(station_id) DO UPDATE SET
                     attempted_at_utc=excluded.attempted_at_utc,
                     state=excluded.state,reason=excluded.reason,
                     updated_at_utc=excluded.updated_at_utc""",
                (station, attempted_at, reason, attempted_at),
            )
            conn.commit()
            errors.append(f"{station}:{reason}")
            results.append({"station_id": station, "state": "blocked", "reason": reason})
    return {
        "ok": not errors,
        "state": "available" if results and not errors else "blocked" if errors else "complete_cached",
        "stations": results,
        "errors": errors,
        "timing_only": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def evaluate_observed_high_entry(
    window: Mapping[str, Any],
    *,
    observation_valid_at_utc: str,
    observation_received_at_utc: str,
    forms_new_station_day_high: bool,
) -> dict[str, Any]:
    """Require the observation and its receipt inside one exact argmax period."""

    if not forms_new_station_day_high:
        return {"eligible": False, "reason": "ASOS_NOT_NEW_STATION_DAY_HIGH"}
    if str(window.get("state") or "") != "FROZEN":
        return {"eligible": False, "reason": "ARGMAX_WINDOW_NOT_FROZEN"}
    valid_at = _utc(observation_valid_at_utc)
    received_at = _utc(observation_received_at_utc)
    if valid_at > received_at:
        return {"eligible": False, "reason": "ASOS_OBSERVATION_TIME_INVALID"}
    periods = window.get("argmax_periods")
    if not isinstance(periods, Sequence) or isinstance(periods, (str, bytes)):
        return {"eligible": False, "reason": "ARGMAX_PERIODS_INVALID"}
    periods = select_latest_contiguous_argmax_periods(periods)
    if not periods:
        return {"eligible": False, "reason": "ARGMAX_PERIODS_INVALID"}
    matched: tuple[datetime, datetime] | None = None
    for item in periods:
        if not isinstance(item, Mapping):
            continue
        try:
            start = _utc(str(item.get("start_utc") or ""))
            end = _utc(str(item.get("end_utc") or ""))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        if start <= valid_at < end and start <= received_at < end:
            matched = (start, end)
            break
    if matched is None:
        return {"eligible": False, "reason": "ARGMAX_PERIOD_NOT_ACTIVE"}
    return {
        "eligible": True,
        "reason": "ASOS_NEW_HIGH_RECEIVED_DURING_ARGMAX_PERIOD",
        "argmax_period_start_utc": _iso(matched[0]),
        "argmax_period_end_utc": _iso(matched[1]),
    }


__all__ = [
    "ARGMAX_SELECTION_RULE",
    "CANONICAL_EVENT_TIMEZONE",
    "LIVE_STATIONS",
    "commit_argmax_windows",
    "derive_argmax_window",
    "evaluate_observed_high_entry",
    "fetch_station_argmax_windows",
    "materialize_missing_argmax_windows",
    "select_latest_contiguous_argmax_periods",
]
