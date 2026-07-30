"""Display-only optional forecast adapters for the copy-follow report.

This module is intentionally outside the copy-follow selection and settlement
logic.  Open-Meteo is an external comparison source: failures are represented
as unavailable data and never authorize, block, resize, or reprioritize an
order.
"""

from __future__ import annotations

from datetime import date
import json
import math
import ssl
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


OPEN_METEO_SOURCE_ID = "open_meteo_daily_temperature_2m_max"
OPEN_METEO_HOST = "api.open-meteo.com"


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _default_opener(request: Request, *, timeout: float):
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        context = ssl.create_default_context()
    return urlopen(request, timeout=timeout, context=context)


def _safe_api_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme != "https" or parsed.hostname != OPEN_METEO_HOST:
        return ""
    return urlunsplit(("https", OPEN_METEO_HOST, parsed.path or "/v1/forecast", "", ""))


def fetch_open_meteo_daily_peak(
    *,
    latitude: Any,
    longitude: Any,
    event_date: str,
    timezone_name: str,
    models: str | None = None,
    api_url: str = "https://api.open-meteo.com/v1/forecast",
    timeout_sec: float = 4.0,
    opener: Callable[..., Any] = _default_opener,
) -> dict[str, Any]:
    """Fetch one provider/model daily maximum for display-only comparison."""

    day = str(event_date or "").strip()[:10]
    try:
        date.fromisoformat(day)
    except ValueError:
        return {"source_id": OPEN_METEO_SOURCE_ID, "state": "unavailable", "reason": "event_date_invalid", "peak_c": None, "rows": []}
    lat, lon = _finite(latitude), _finite(longitude)
    if lat is None or lon is None or not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        return {"source_id": OPEN_METEO_SOURCE_ID, "state": "unavailable", "reason": "coordinates_invalid", "peak_c": None, "rows": []}
    safe_url = _safe_api_url(api_url)
    if not safe_url:
        return {"source_id": OPEN_METEO_SOURCE_ID, "state": "unavailable", "reason": "api_host_not_allowlisted", "peak_c": None, "rows": []}
    query_params = {
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "daily": "temperature_2m_max",
        "temperature_unit": "celsius",
        "timezone": str(timezone_name or "UTC"),
        "start_date": day,
        "end_date": day,
    }
    model_text = str(models or "").strip()
    if model_text:
        query_params["models"] = model_text
    query = urlencode(query_params)
    request_url = f"{safe_url}?{query}"
    request = Request(
        request_url,
        headers={"Accept": "application/json", "User-Agent": "weather-copy-follow-open-meteo/1"},
    )
    last_exc: Exception | None = None
    payload: Any = None
    for _attempt in range(2):
        try:
            with opener(request, timeout=float(timeout_sec)) as response:
                payload = json.loads(response.read().decode("utf-8"))
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        return {
            "source_id": OPEN_METEO_SOURCE_ID,
            "state": "unavailable",
            "reason": f"request_failed:{type(last_exc).__name__}",
            "peak_c": None,
            "rows": [],
            "request_url": request_url,
        }
    daily = _mapping(_mapping(payload).get("daily"))
    dates = daily.get("time") if isinstance(daily.get("time"), list) else []
    peaks = daily.get("temperature_2m_max") if isinstance(daily.get("temperature_2m_max"), list) else []
    rows = []
    for item_day, item_peak in zip(dates, peaks):
        value = _finite(item_peak)
        if str(item_day)[:10] == day and value is not None:
            rows.append({"event_date": day, "peak_c": value})
    if not rows:
        return {
            "source_id": OPEN_METEO_SOURCE_ID,
            "state": "unavailable",
            "reason": "daily_peak_missing",
            "peak_c": None,
            "rows": [],
            "request_url": request_url,
        }
    return {
        "source_id": OPEN_METEO_SOURCE_ID,
        "state": "available",
        "peak_c": rows[0]["peak_c"],
        "rows": rows,
        "request_url": request_url,
        "timezone": str(timezone_name or "UTC"),
        "model": model_text or "best_match",
        "display_only": True,
    }


__all__ = ["OPEN_METEO_SOURCE_ID", "fetch_open_meteo_daily_peak"]
