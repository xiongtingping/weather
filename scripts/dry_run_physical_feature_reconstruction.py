#!/usr/bin/env python3
"""Read-only dry-run for historical physical-likelihood feature reconstruction coverage."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
WEATHER_EVENTS_CACHE_PATHS = (
    ROOT / "data" / "weather_events_cache.json",
    ROOT / "data" / "weather_events_cache.good.json",
)
OURAIRPORTS_CACHE = ROOT / "data" / "ourairports_airports.csv"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_physical_likelihood import (  # noqa: E402
    _derived_feature_group_status,
    _extract_likelihood_features,
    _feature_quality,
    _safe_json,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
PHYSICAL_BACKTEST_SNAPSHOT_CONFIRM_PHRASE = "APPLY_PHYSICAL_BACKTEST_SNAPSHOTS"
_OURAIRPORTS_COORD_INDEX: dict[str, tuple[float, float]] = {}


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _counter_inc(counter: dict[str, int], key: str, amount: int = 1) -> None:
    counter[key] = counter.get(key, 0) + int(amount)


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if "T" in text:
        return text.split("T", 1)[0]
    if " " in text:
        return text.split(" ", 1)[0]
    return text


def _parse_high_temp_slug_date(slug: Any) -> str:
    text = str(slug or "").strip().lower()
    match = re.search(
        r"-on-(january|february|march|april|may|june|july|august|september|october|november|december)-(\d{1,2})(?:-(\d{4}))?$",
        text,
    )
    if not match:
        return ""
    month_map = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    year = int(match.group(3) or datetime.now(timezone.utc).year)
    month = month_map.get(match.group(1))
    day = int(match.group(2))
    if month is None:
        return ""
    try:
        return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
    except ValueError:
        return ""


def _event_stub_date(stub: Mapping[str, Any]) -> str:
    for key in ("event_date", "eventDate", "endDate", "end_date", "closedTime"):
        date_key = _normalize_date(stub.get(key))
        if date_key:
            return date_key
    return _parse_high_temp_slug_date(stub.get("slug"))


def _extract_resolution_source_from_text(text: Any) -> str:
    urls = re.findall(r"https?://[^\s<>'\"]+", str(text or ""), flags=re.I)
    preferred: list[str] = []
    fallback: list[str] = []
    for raw_url in urls:
        url = raw_url.strip().strip("'\"").rstrip(").,;]")
        low = url.lower()
        if "wunderground.com/history/daily/" in low:
            preferred.append(url)
        elif "weather.gov/wrh/timeseries" in low or "weather.gov" in low or "noaa.gov" in low:
            fallback.append(url)
    return (preferred or fallback or [""])[0]


def _parse_station_from_resolution_source(source: Any) -> str:
    text = str(source or "").strip()
    if not text:
        return ""
    parsed = urllib.parse.urlparse(text)
    query_pairs = dict(urllib.parse.parse_qsl(parsed.query or "", keep_blank_values=True))
    for key in ("station", "stationid", "station_id", "stationId", "site", "stid", "icao", "id"):
        raw = str(query_pairs.get(key) or "").strip().upper()
        station = re.sub(r"[^A-Z0-9]", "", raw)
        if re.match(r"^[A-Z0-9]{3,6}$", station):
            return station
    path = parsed.path or ""
    if "wunderground.com" in parsed.netloc.lower() and re.search(r"/(?:history/daily|weather|hourly|dashboard/pws)/.+", path):
        parts = [part for part in path.split("/") if part]
        if parts:
            station = re.sub(r"[^A-Z0-9]", "", parts[-1].upper())
            if re.match(r"^[A-Z0-9]{3,6}$", station):
                return station
    if parsed.netloc.lower().endswith(("weather.gov", "noaa.gov")):
        ignored = {"HTTPS", "HTTP", "NOAA", "NWS", "WEATHER", "GOV", "WWW", "TIMESERIES"}
        for candidate in re.findall(r"\b[A-Z0-9]{3,6}\b", text.upper()):
            if candidate not in ignored:
                return candidate
    return ""


def _resolution_source_kind(source: Any) -> str:
    text = str(source or "").strip().lower()
    parsed = urllib.parse.urlparse(text)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if "wunderground.com" in host and "/history/daily/" in path:
        return "wunderground_history"
    if host == "weather.gov.hk" or host.endswith(".weather.gov.hk"):
        if "/cis/climat" in path:
            return "hko_daily_extract"
    if host.endswith("weather.gov") or host.endswith("noaa.gov"):
        return "noaa_weather_gov"
    if text:
        return "other"
    return "missing"


def _wunderground_country_code(source: Any) -> str:
    match = re.search(r"/history/daily/([a-z]{2})/", str(source or ""), flags=re.I)
    return (match.group(1).upper() if match else "US")


def _weather_com_observation_temp_f(obs: Mapping[str, Any]) -> float | None:
    imperial = obs.get("imperial")
    if isinstance(imperial, Mapping):
        for key in ("temp", "temperature", "tempHigh"):
            val = _to_float(imperial.get(key))
            if val is not None:
                return float(val)
    for key in ("max_temp", "tempHigh", "temperatureHigh"):
        val = _to_float(obs.get(key))
        if val is not None:
            return float(val)
    for key in ("temp", "tempi", "temperature"):
        val = _to_float(obs.get(key))
        if val is not None:
            return float(val)
    metric = obs.get("metric")
    if isinstance(metric, Mapping):
        val = _to_float(metric.get("temp") or metric.get("temperature"))
        if val is not None:
            return float(val) * 9.0 / 5.0 + 32.0
    return None


def _weather_com_observation_time_key(obs: Mapping[str, Any]) -> tuple[int, str]:
    epoch = _to_float(obs.get("valid_time_gmt") or obs.get("expire_time_gmt"))
    if epoch is not None:
        return int(epoch), ""
    return 0, str(obs.get("obsTimeLocal") or obs.get("obs_time_local") or obs.get("obsTimeUtc") or "")


def _weather_com_observation_hour_local(obs: Mapping[str, Any]) -> float | None:
    text = str(obs.get("obsTimeLocal") or obs.get("obs_time_local") or "").strip()
    match = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", text)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        second = int(match.group(3) or 0)
        return float(hour + minute / 60.0 + second / 3600.0)
    epoch = _to_float(obs.get("valid_time_gmt"))
    if epoch is not None:
        return float(datetime.fromtimestamp(float(epoch), tz=timezone.utc).hour)
    return None


def _weather_com_temperature_features(observations: Sequence[Any]) -> dict[str, Any]:
    pairs: list[tuple[tuple[int, str], Mapping[str, Any], float]] = []
    for obs in observations:
        if not isinstance(obs, Mapping):
            continue
        temp_f = _weather_com_observation_temp_f(obs)
        if temp_f is None:
            continue
        pairs.append((_weather_com_observation_time_key(obs), obs, float(temp_f)))
    if not pairs:
        return {}
    pairs.sort(key=lambda item: item[0])
    peak_idx = max(range(len(pairs)), key=lambda idx: pairs[idx][2])
    peak_obs = pairs[peak_idx][1]
    peak_f = float(pairs[peak_idx][2])
    peak_c = (peak_f - 32.0) * 5.0 / 9.0
    features: dict[str, Any] = {
        "observed_temp": float(peak_c),
        "observed_today_max_c": float(peak_c),
        "observed_today_max_source": "weather_com_history_api",
        "observed_today_max_trusted": True,
        "temperature_observation_final_daily_max": True,
        "temperature_unit": "C",
        "weather_com_observation_count": len(pairs),
    }
    hour = _weather_com_observation_hour_local(peak_obs)
    if hour is not None:
        features["observation_hour_local"] = float(hour)
    if peak_idx >= 1:
        prev_f = float(pairs[peak_idx - 1][2])
        features["temperature_rise_rate_c_per_hour"] = float((peak_f - prev_f) * 5.0 / 9.0)
    return features


def _fetch_wu_official_settlement_preview(
    resolution_source: Any,
    event_date: str,
    market_unit: str,
    *,
    timeout: float,
    retries: int = 2,
) -> dict[str, Any]:
    source = str(resolution_source or "").strip()
    station_id = _parse_station_from_resolution_source(source)
    if _resolution_source_kind(source) != "wunderground_history":
        return {"status": "not_applicable", "reason": "source_not_wunderground_history"}
    if not station_id:
        return {"status": "not_applicable", "reason": "station_id_missing"}
    date_key = str(event_date or "").replace("-", "")[:8]
    if len(date_key) != 8:
        return {"status": "not_applicable", "reason": "event_date_invalid"}
    country = _wunderground_country_code(source)
    api_key = "6532d6454b8aa370768e63d6ba5a832e"
    location_id = f"{station_id}:9:{country}"
    url = (
        "https://api.weather.com/v1/location/"
        f"{urllib.parse.quote(location_id, safe='')}/observations/historical.json"
        f"?apiKey={api_key}&units=e&startDate={date_key}&endDate={date_key}"
    )
    last_error = ""
    max_attempts = max(1, int(retries or 1))
    for _attempt in range(max_attempts):
        try:
            proc = subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-L",
                    "-k",
                    "--max-time",
                    str(max(1, int(timeout))),
                    url,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception as exc:
            last_error = f"subprocess_error:{exc.__class__.__name__}"
            continue
        if proc.returncode != 0:
            last_error = f"curl_exit_{proc.returncode}"
            continue
        raw = (proc.stdout or "").strip()
        if not raw or not raw.startswith("{"):
            last_error = "payload_not_json"
            continue
        try:
            payload = json.loads(raw)
        except Exception as exc:
            last_error = f"json_error:{exc.__class__.__name__}"
            continue
        observations = payload.get("observations") if isinstance(payload, Mapping) else None
        if not isinstance(observations, list):
            last_error = "observations_missing"
            continue
        temps_f = [
            temp
            for obs in observations
            if isinstance(obs, Mapping)
            for temp in [_weather_com_observation_temp_f(obs)]
            if temp is not None
        ]
        if not temps_f:
            last_error = "temperatures_missing"
            continue
        value_f = max(float(temp) for temp in temps_f)
        temperature_features = _weather_com_temperature_features(observations)
        value = value_f
        unit = "F"
        if str(market_unit or "").strip().upper() == "C":
            value = (value_f - 32.0) * 5.0 / 9.0
            unit = "C"
        return {
            "status": "ok",
            "value": float(value),
            "unit": unit,
            "source": "weather_com_history_api",
            "station_id": station_id,
            "location_id": location_id,
            "observation_count": len(observations),
            "temperature_features": temperature_features,
        }
    return {
        "status": "error",
        "error": last_error or "unknown",
        "source": "weather_com_history_api",
        "station_id": station_id,
        "location_id": location_id,
    }


def _question_bucket_label(question: Any) -> str:
    text = str(question or "").strip()
    if not text:
        return ""
    match = re.search(r"\bbe\s+(.+?)\s+on\s+", text, flags=re.I)
    if match:
        return match.group(1).strip().rstrip("?")
    return ""


def _candidate_outcome_labels(event: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    markets = event.get("markets")
    if isinstance(markets, Sequence) and not isinstance(markets, (str, bytes, bytearray)):
        for market in markets:
            if not isinstance(market, Mapping):
                continue
            label = _question_bucket_label(market.get("question"))
            if label and label not in labels:
                labels.append(label)
    return labels


def _candidate_yes_prices(event: Mapping[str, Any]) -> list[float]:
    prices: list[float] = []
    markets = event.get("markets")
    if not isinstance(markets, Sequence) or isinstance(markets, (str, bytes, bytearray)):
        return prices
    for market in markets:
        if not isinstance(market, Mapping):
            continue
        raw_prices = _safe_json(market.get("outcomePrices"))
        if isinstance(raw_prices, list) and raw_prices:
            val = _to_float(raw_prices[0])
            if val is not None:
                prices.append(float(val))
                continue
        val = _to_float(market.get("bestAsk")) or _to_float(market.get("lastTradePrice"))
        if val is not None:
            prices.append(float(val))
    return prices


def _market_unit_from_text(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values)
    c_hits = len(re.findall(r"°\s*C|\bCelsius\b|\bdegrees Celsius\b", text, flags=re.I))
    f_hits = len(re.findall(r"°\s*F|\bFahrenheit\b|\bdegrees Fahrenheit\b", text, flags=re.I))
    if c_hits > f_hits:
        return "C"
    if f_hits > c_hits:
        return "F"
    if c_hits:
        return "C"
    if f_hits:
        return "F"
    return ""


def _load_ourairports_coords() -> dict[str, tuple[float, float]]:
    if _OURAIRPORTS_COORD_INDEX:
        return _OURAIRPORTS_COORD_INDEX
    if not OURAIRPORTS_CACHE.exists():
        return _OURAIRPORTS_COORD_INDEX
    try:
        with OURAIRPORTS_CACHE.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                lat = _to_float(row.get("latitude_deg"))
                lon = _to_float(row.get("longitude_deg"))
                if lat is None or lon is None:
                    continue
                for key in ("ident", "icao_code", "gps_code", "iata_code"):
                    station = str(row.get(key) or "").strip().upper()
                    if station and station not in _OURAIRPORTS_COORD_INDEX:
                        _OURAIRPORTS_COORD_INDEX[station] = (float(lat), float(lon))
    except Exception:
        return _OURAIRPORTS_COORD_INDEX
    return _OURAIRPORTS_COORD_INDEX


def _station_coords(station_id: Any) -> tuple[float | None, float | None, str]:
    station = str(station_id or "").strip().upper()
    if not station:
        return None, None, "station_id_missing"
    coords = _load_ourairports_coords().get(station)
    if coords:
        return float(coords[0]), float(coords[1]), "ourairports"
    return None, None, "station_not_found_in_ourairports"


def _is_high_temp_slug(slug: Any) -> bool:
    return str(slug or "").strip().lower().startswith("highest-temperature-in-")


def _is_us_coordinate(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    return 18.0 <= float(lat) <= 72.0 and -180.0 <= float(lon) <= -60.0


def _is_us_station(station_id: Any) -> bool:
    text = str(station_id or "").strip().upper()
    return len(text) == 4 and text.startswith("K")


def _finite_values(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    out: list[float] = []
    for value in values:
        val = _to_float(value)
        if val is not None:
            out.append(float(val))
    return out


def _mean(values: Sequence[float]) -> float | None:
    vals = [float(value) for value in values if math.isfinite(float(value))]
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _dominant_wind_direction(directions: Sequence[float], speeds: Sequence[float]) -> float | None:
    pairs = [
        (float(direction) % 360.0, max(0.0, float(speed)))
        for direction, speed in zip(directions, speeds)
        if math.isfinite(float(direction)) and math.isfinite(float(speed))
    ]
    if not pairs:
        return None
    x = sum(math.sin(math.radians(direction)) * max(speed, 0.1) for direction, speed in pairs)
    y = sum(math.cos(math.radians(direction)) * max(speed, 0.1) for direction, speed in pairs)
    if abs(x) <= 1e-12 and abs(y) <= 1e-12:
        return None
    return float((math.degrees(math.atan2(x, y)) + 360.0) % 360.0)


def _features_from_open_meteo_url(
    base_url: str,
    source_name: str,
    lat: float,
    lon: float,
    event_date: str,
    tz: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    hourly_vars = [
        "temperature_2m",
        "dew_point_2m",
        "cloud_cover",
        "cloud_cover_low",
        "cloud_cover_mid",
        "cloud_cover_high",
        "shortwave_radiation",
        "precipitation",
        "rain",
        "wind_speed_10m",
        "wind_direction_10m",
        "surface_pressure",
    ]
    params = {
        "latitude": f"{float(lat):.5f}",
        "longitude": f"{float(lon):.5f}",
        "hourly": ",".join(hourly_vars),
        "daily": "shortwave_radiation_sum",
        "start_date": event_date,
        "end_date": event_date,
        "timezone": tz or "UTC",
        "temperature_unit": "celsius",
        "wind_speed_unit": "ms",
    }
    url = str(base_url).rstrip("?") + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "pm-weather-dry-run/1.0"})
    with urllib.request.urlopen(req, timeout=max(1.0, float(timeout))) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    hourly = payload.get("hourly") if isinstance(payload, Mapping) else {}
    daily = payload.get("daily") if isinstance(payload, Mapping) else {}
    if not isinstance(hourly, Mapping):
        hourly = {}
    if not isinstance(daily, Mapping):
        daily = {}
    temps = _finite_values(hourly.get("temperature_2m"))
    dewpoints = _finite_values(hourly.get("dew_point_2m"))
    clouds = _finite_values(hourly.get("cloud_cover"))
    low_clouds = _finite_values(hourly.get("cloud_cover_low"))
    mid_clouds = _finite_values(hourly.get("cloud_cover_mid"))
    high_clouds = _finite_values(hourly.get("cloud_cover_high"))
    radiation = _finite_values(hourly.get("shortwave_radiation"))
    precip = _finite_values(hourly.get("precipitation"))
    rain = _finite_values(hourly.get("rain"))
    wind_speed = _finite_values(hourly.get("wind_speed_10m"))
    wind_dir = _finite_values(hourly.get("wind_direction_10m"))
    pressure = _finite_values(hourly.get("surface_pressure"))
    daylight_idx = [idx for idx, val in enumerate(radiation) if val > 10.0]
    features: dict[str, Any] = {
        "source": source_name,
        "unit": "C",
        "temperature_unit": "C",
    }
    if temps:
        features["observed_today_max_c"] = max(temps)
        features["observed_today_max_source"] = source_name
        peak_idx = max(range(len(temps)), key=lambda idx: temps[idx])
        features["observed_temp"] = temps[peak_idx]
        features["observation_hour_local"] = float(peak_idx)
        if peak_idx >= 1:
            features["temperature_rise_rate_c_per_hour"] = float(temps[peak_idx] - temps[peak_idx - 1])
        momentum_rates: dict[int, float] = {}
        for horizon in (1, 2, 3):
            if peak_idx >= horizon:
                delta_c = float(temps[peak_idx] - temps[peak_idx - horizon])
                rate = delta_c / float(horizon)
                momentum_rates[horizon] = rate
                features[f"temperature_momentum_{horizon}h_c_per_hour"] = rate
                features[f"temperature_momentum_{horizon}h_delta_c"] = delta_c
                features[f"temperature_momentum_{horizon}h_actual_hours"] = float(horizon)
        if momentum_rates:
            weights = {1: 0.50, 2: 0.30, 3: 0.20}
            denom = sum(weights[h] for h in momentum_rates)
            effective = sum(momentum_rates[h] * weights[h] for h in momentum_rates) / max(1e-6, denom)
            features["temperature_momentum_effective_c_per_hour"] = effective
            features["temperature_momentum_available_horizons"] = ",".join(f"{h}h" for h in sorted(momentum_rates))
            features["temperature_momentum_sustained_warming"] = bool(
                len(momentum_rates) >= 2
                and momentum_rates.get(1, 0.0) >= 0.35
                and sum(1 for val in momentum_rates.values() if val >= 0.25) >= 2
                and effective >= 0.25
            )
            features["temperature_momentum_realization_state"] = (
                "sustained_warming"
                if features["temperature_momentum_sustained_warming"]
                else ("not_realized" if momentum_rates.get(1, effective) <= 0.0 and effective <= 0.10 else "mixed")
            )
    if dewpoints:
        features["dewpoint_c"] = max(dewpoints)
    cloud_vals = [clouds[idx] for idx in daylight_idx if idx < len(clouds)] if daylight_idx else clouds
    low_vals = [low_clouds[idx] for idx in daylight_idx if idx < len(low_clouds)] if daylight_idx else low_clouds
    mid_vals = [mid_clouds[idx] for idx in daylight_idx if idx < len(mid_clouds)] if daylight_idx else mid_clouds
    high_vals = [high_clouds[idx] for idx in daylight_idx if idx < len(high_clouds)] if daylight_idx else high_clouds
    cloud_mean = _mean(cloud_vals)
    low_mean = _mean(low_vals)
    mid_mean = _mean(mid_vals)
    high_mean = _mean(high_vals)
    if cloud_mean is not None:
        features["cloud_cover_pct"] = cloud_mean
    if low_mean is not None:
        features["cloud_cover_low_pct"] = low_mean
    if mid_mean is not None:
        features["cloud_cover_mid_pct"] = mid_mean
    if high_mean is not None:
        features["cloud_cover_high_pct"] = high_mean
    radiation_vals = [radiation[idx] for idx in daylight_idx] if daylight_idx else radiation
    radiation_mean = _mean(radiation_vals)
    if radiation_mean is not None:
        features["solar_radiation_observed"] = radiation_mean
        features["shortwave_radiation_observed"] = radiation_mean
    shortwave_sum = _finite_values(daily.get("shortwave_radiation_sum"))
    if shortwave_sum:
        features["shortwave_radiation_sum"] = shortwave_sum[0]
    precip_candidates = [value for value in precip + rain if math.isfinite(float(value))]
    if precip_candidates:
        rate = max(0.0, float(max(precip_candidates)))
        features["observed_precip_rate_mm_per_h"] = rate
        features["precipitation_rate_mm_per_h"] = rate
        features["surface_wetness_proxy"] = max(0.0, min(1.0, (sum(max(0.0, v) for v in precip) / 5.0)))
        if rate > 0:
            features["is_raining_observed"] = True
    if wind_speed:
        features["wind_speed_mps"] = max(wind_speed)
    wind = _dominant_wind_direction(wind_dir, wind_speed)
    if wind is not None:
        features["wind_direction_deg"] = wind
    if pressure:
        features["pressure_hpa"] = pressure[-1]
    if len(pressure) >= 4:
        features["pressure_trend_hpa_3h"] = float(pressure[-1] - pressure[-4])
    elif len(pressure) >= 2:
        features["pressure_trend_hpa_3h"] = float(pressure[-1] - pressure[0])
    return features


def _features_from_open_meteo_archive(
    lat: float,
    lon: float,
    event_date: str,
    tz: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    return _features_from_open_meteo_url(
        OPEN_METEO_ARCHIVE_URL,
        "open_meteo_archive_dry_run",
        lat,
        lon,
        event_date,
        tz,
        timeout=timeout,
    )


def _features_from_open_meteo_historical_forecast(
    lat: float,
    lon: float,
    event_date: str,
    tz: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    return _features_from_open_meteo_url(
        OPEN_METEO_HISTORICAL_FORECAST_URL,
        "open_meteo_historical_forecast_dry_run",
        lat,
        lon,
        event_date,
        tz,
        timeout=timeout,
    )


def _event_rows(conn: sqlite3.Connection, start_date: str, end_date: str, max_events: int) -> list[sqlite3.Row]:
    sql = """
        WITH latest AS (
          SELECT slug, event_date, MAX(id) AS max_id
          FROM event_runs
          WHERE market_var = 'temperature_2m_max'
            AND event_date >= ?
            AND event_date <= ?
            AND settlement_actual IS NOT NULL
            AND COALESCE(market_closed, 0) = 1
          GROUP BY slug, event_date
        )
        SELECT e.*
        FROM latest l
        JOIN event_runs e ON e.id = l.max_id
        ORDER BY e.event_date, e.slug
    """
    rows = conn.execute(sql, (start_date, end_date)).fetchall()
    if max_events > 0:
        return list(rows[:max_events])
    return list(rows)


def _candidate_key(slug: Any, event_date: Any) -> tuple[str, str] | None:
    slug_text = str(slug or "").strip()
    date_key = _normalize_date(event_date) or _parse_high_temp_slug_date(slug_text)
    if not slug_text or not date_key or not _is_high_temp_slug(slug_text):
        return None
    return slug_text, date_key


def _read_weather_cache_candidates(start_date: str, end_date: str) -> dict[tuple[str, str], dict[str, Any]]:
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for path in WEATHER_EVENTS_CACHE_PATHS:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        events = payload.get("events") if isinstance(payload, Mapping) else payload
        if not isinstance(events, list):
            continue
        for item in events:
            if not isinstance(item, Mapping):
                continue
            key = _candidate_key(item.get("slug"), _event_stub_date(item))
            if key is None:
                continue
            if key[1] < start_date or key[1] > end_date:
                continue
            resolution_source = str(item.get("resolutionSource") or item.get("resolution_source") or "").strip()
            if not resolution_source:
                resolution_source = _extract_resolution_source_from_text(item.get("description"))
            outcome_labels = _candidate_outcome_labels(item)
            yes_prices = _candidate_yes_prices(item)
            market_unit = _market_unit_from_text(
                item.get("description"),
                item.get("title"),
                " ".join(outcome_labels),
            )
            station_id = _parse_station_from_resolution_source(resolution_source)
            candidates.setdefault(
                key,
                {
                    "slug": key[0],
                    "event_date": key[1],
                    "title": item.get("title") or "",
                    "description": item.get("description") or "",
                    "resolution_source": resolution_source,
                    "station_id": station_id,
                    "market_var": "temperature_2m_max",
                    "market_unit": market_unit,
                    "outcome_labels": outcome_labels,
                    "outcome_count": len(outcome_labels),
                    "yes_prices": yes_prices,
                    "yes_price_count": len(yes_prices),
                    "closed": bool(item.get("closed")),
                    "active": bool(item.get("active")),
                    "source": path.name,
                },
            )
    return candidates


def _read_discovery_candidates(
    conn: sqlite3.Connection,
    start_date: str,
    end_date: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(weather_market_discovery)").fetchall()
            if len(row) > 1
        }
        details_expr = "details_json" if "details_json" in cols else "NULL AS details_json"
        rows = conn.execute(
            f"""
            SELECT slug, event_date, title, status, token_count, {details_expr}
            FROM weather_market_discovery
            WHERE lower(slug) LIKE 'highest-temperature-in-%'
              AND event_date >= ?
              AND event_date <= ?
            ORDER BY event_date, slug
            """,
            (start_date, end_date),
        ).fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        key = _candidate_key(row["slug"], row["event_date"])
        if key is None:
            continue
        details = _safe_json(row["details_json"])
        if not isinstance(details, Mapping):
            details = {}
        out[key] = {
            "slug": key[0],
            "event_date": key[1],
            "title": row["title"],
            "status": row["status"],
            "token_count": row["token_count"],
            "closed": bool(details.get("closed")),
            "active": bool(details.get("active")),
            "details_source": details.get("source") or "",
            "source": "weather_market_discovery",
        }
    return out


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _merge_candidate_detail(entry: dict[str, Any], item: Mapping[str, Any]) -> None:
    for key, value in item.items():
        if key in {"source", "yes_prices"}:
            continue
        if value in (None, "", []):
            continue
        if key not in entry or entry.get(key) in (None, "", []):
            entry[key] = value
    if isinstance(item.get("outcome_labels"), list):
        labels = list(entry.get("outcome_labels") or [])
        for label in item.get("outcome_labels") or []:
            if label and label not in labels:
                labels.append(label)
        entry["outcome_labels"] = labels
        entry["outcome_count"] = len(labels)
    if isinstance(item.get("yes_prices"), list):
        prices = list(entry.get("yes_prices") or [])
        for price in item.get("yes_prices") or []:
            val = _to_float(price)
            if val is not None:
                prices.append(float(val))
        entry["yes_prices"] = prices
        entry["yes_price_count"] = len(prices)


def _combined_candidate_records(
    conn: sqlite3.Connection | None,
    start_date: str,
    end_date: str,
    event_rows: Sequence[sqlite3.Row],
) -> dict[tuple[str, str], dict[str, Any]]:
    event_run_keys: dict[tuple[str, str], dict[str, Any]] = {}
    for row in event_rows:
        key = _candidate_key(_row_get(row, "slug"), _row_get(row, "event_date"))
        if key is not None:
            payload = _safe_json(_row_get(row, "payload_json"))
            payload_rows = payload.get("rows") if isinstance(payload, Mapping) else []
            outcome_labels = []
            if isinstance(payload_rows, list):
                for item in payload_rows:
                    if isinstance(item, Mapping):
                        label = str(item.get("outcome") or item.get("label") or "").strip()
                        if label and label not in outcome_labels:
                            outcome_labels.append(label)
            q_values: list[float] = []
            if isinstance(payload_rows, list):
                for item in payload_rows:
                    if not isinstance(item, Mapping):
                        continue
                    val = (
                        _to_float(item.get("q_raw_static"))
                        or _to_float(item.get("q_raw"))
                        or _to_float(item.get("q_round"))
                        or _to_float(item.get("q_bayes_posterior"))
                    )
                    if val is not None:
                        q_values.append(float(val))
            event_run_keys[key] = {
                "slug": key[0],
                "event_date": key[1],
                "title": _row_get(row, "title", ""),
                "city": _row_get(row, "city", ""),
                "lat": _row_get(row, "lat"),
                "lon": _row_get(row, "lon"),
                "tz": _row_get(row, "tz", ""),
                "station_id": _row_get(row, "station_id", ""),
                "resolution_source": _row_get(row, "resolution_source", ""),
                "market_var": _row_get(row, "market_var", ""),
                "market_unit": _row_get(row, "market_unit", ""),
                "settlement_actual": _row_get(row, "settlement_actual"),
                "outcome_labels": outcome_labels,
                "outcome_count": len(outcome_labels),
                "yes_prices": q_values,
                "yes_price_count": len(q_values),
                "source": "event_runs",
            }
    discovery = _read_discovery_candidates(conn, start_date, end_date) if conn is not None else {}
    cache = _read_weather_cache_candidates(start_date, end_date)
    combined: dict[tuple[str, str], dict[str, Any]] = {}
    for source_name, source_map in (
        ("event_runs", event_run_keys),
        ("weather_market_discovery", discovery),
        ("weather_events_cache", cache),
    ):
        for key, item in source_map.items():
            entry = combined.setdefault(
                key,
                {
                    "slug": key[0],
                    "event_date": key[1],
                    "sources": [],
                    "title": item.get("title") or "",
                },
            )
            if source_name not in entry["sources"]:
                entry["sources"].append(source_name)
            _merge_candidate_detail(entry, item)
    return combined


def _candidate_source_summary(
    conn: sqlite3.Connection | None,
    start_date: str,
    end_date: str,
    event_rows: Sequence[sqlite3.Row],
) -> dict[str, Any]:
    combined = _combined_candidate_records(conn, start_date, end_date, event_rows)
    event_run_keys = {
        key for key, item in combined.items() if "event_runs" in (item.get("sources") or [])
    }
    discovery_keys = {
        key for key, item in combined.items() if "weather_market_discovery" in (item.get("sources") or [])
    }
    cache_keys = {
        key for key, item in combined.items() if "weather_events_cache" in (item.get("sources") or [])
    }
    not_in_event_runs = [item for key, item in sorted(combined.items(), key=lambda kv: (kv[0][1], kv[0][0])) if key not in event_run_keys]
    by_date: dict[str, int] = {}
    for _slug, event_date in combined:
        _counter_inc(by_date, event_date)

    def _sample(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "slug": item.get("slug") or "",
            "event_date": item.get("event_date") or "",
            "title": item.get("title") or "",
            "sources": list(item.get("sources") or []),
            "status": item.get("status") or "",
            "active": item.get("active") if "active" in item else None,
            "closed": item.get("closed") if "closed" in item else None,
            "station_id": item.get("station_id") or "",
            "market_unit": item.get("market_unit") or "",
            "outcome_count": int(item.get("outcome_count") or 0),
            "yes_price_count": int(item.get("yes_price_count") or 0),
            "resolution_source": item.get("resolution_source") or "",
        }

    return {
        "event_runs_settled_high_temp_days": len(event_run_keys),
        "weather_market_discovery_high_temp_days": len(discovery_keys),
        "weather_events_cache_high_temp_days": len(cache_keys),
        "combined_high_temp_candidate_days": len(combined),
        "combined_candidates_not_in_event_runs": len(not_in_event_runs),
        "combined_candidates_by_date": dict(sorted(by_date.items())),
        "sample_candidates_not_in_event_runs": [_sample(item) for item in not_in_event_runs[:20]],
    }


def _merge_without_overwrite(base: dict[str, Any], addition: Mapping[str, Any]) -> None:
    for key, value in addition.items():
        if key in {"source", "unit", "market_unit", "market_var"}:
            continue
        if key not in base or base.get(key) in (None, "", []):
            base[str(key)] = value


def _merge_stronger_temperature_trajectory(base: dict[str, Any], addition: Mapping[str, Any]) -> None:
    allowed = {
        "observed_temp",
        "observed_today_max_c",
        "observed_today_max_source",
        "observed_today_max_trusted",
        "temperature_observation_final_daily_max",
        "temperature_rise_rate_c_per_hour",
        "temperature_momentum_1h_c_per_hour",
        "temperature_momentum_2h_c_per_hour",
        "temperature_momentum_3h_c_per_hour",
        "temperature_momentum_effective_c_per_hour",
        "temperature_momentum_realization_state",
        "temperature_momentum_sustained_warming",
        "temperature_momentum_available_horizons",
        "temperature_unit",
        "observation_hour_local",
        "weather_com_observation_count",
    }
    for key in allowed:
        if key in addition and addition.get(key) not in (None, "", []):
            base[key] = addition.get(key)


def _candidate_projection_record(
    record: Mapping[str, Any],
    *,
    fetch_open_meteo_archive: bool,
    fetch_open_meteo_historical_forecast_fallback: bool = True,
    fetch_settlement_preview: bool = False,
    timeout: float,
) -> dict[str, Any]:
    slug = str(record.get("slug") or "").strip()
    event_date = _normalize_date(record.get("event_date"))
    title = str(record.get("title") or "").strip()
    resolution_source = str(record.get("resolution_source") or "").strip()
    if not resolution_source:
        resolution_source = _extract_resolution_source_from_text(record.get("description"))
    station_id = str(record.get("station_id") or "").strip().upper()
    if not station_id:
        station_id = _parse_station_from_resolution_source(resolution_source)
    lat = _to_float(record.get("lat"))
    lon = _to_float(record.get("lon"))
    coord_source = str(record.get("coord_source") or "").strip()
    if (lat is None or lon is None) and station_id:
        lat, lon, coord_source = _station_coords(station_id)
    tz = str(record.get("tz") or "").strip() or "auto"
    outcome_labels = [
        str(label).strip()
        for label in (record.get("outcome_labels") or [])
        if str(label or "").strip()
    ]
    market_unit = str(record.get("market_unit") or "").strip()
    if not market_unit:
        market_unit = _market_unit_from_text(
            record.get("description"),
            title,
            " ".join(outcome_labels),
        )
    market_var = str(record.get("market_var") or "").strip() or "temperature_2m_max"
    yes_prices = [
        float(val)
        for val in (record.get("yes_prices") or [])
        if _to_float(val) is not None
    ]
    yes_price_count = int(record.get("yes_price_count") or len(yes_prices))
    probability_snapshot_ready = bool(
        len(outcome_labels) >= 2
        and len(yes_prices) >= 2
        and len(yes_prices) == len(outcome_labels)
    )
    settlement_actual = _to_float(record.get("settlement_actual"))
    settlement_actual_ready = settlement_actual is not None
    source_low = resolution_source.lower()
    source_kind = _resolution_source_kind(resolution_source)
    settlement_source_ready = bool(
        resolution_source
        and station_id
        and (
            "wunderground.com/history/daily/" in source_low
            or "weather.gov/wrh/timeseries" in source_low
            or "noaa.gov" in source_low
        )
    )
    metadata_ready = bool(
        slug
        and event_date
        and market_var == "temperature_2m_max"
        and market_unit
        and len(outcome_labels) >= 2
        and settlement_source_ready
    )
    feature_reconstruction_ready = bool(lat is not None and lon is not None and event_date)
    missing_reasons: list[str] = []
    if not resolution_source:
        missing_reasons.append("resolution_source_missing")
    if not station_id:
        missing_reasons.append("station_id_missing")
    if not market_unit:
        missing_reasons.append("market_unit_missing")
    if len(outcome_labels) < 2:
        missing_reasons.append("outcomes_missing")
    if not settlement_source_ready:
        missing_reasons.append("settlement_source_not_ready")
    if lat is None or lon is None:
        missing_reasons.append(coord_source or "coords_missing")
    backtest_missing_reasons: list[str] = []
    if not probability_snapshot_ready:
        backtest_missing_reasons.append("probability_snapshot_missing")
    if not settlement_actual_ready:
        backtest_missing_reasons.append("settlement_actual_missing")
    settlement_preview: dict[str, Any] = {
        "status": "existing" if settlement_actual_ready else "not_fetched",
    }
    settlement_preview_ready = False
    settlement_preview_actual = None
    if fetch_settlement_preview and not settlement_actual_ready:
        settlement_preview = _fetch_wu_official_settlement_preview(
            resolution_source,
            event_date,
            market_unit,
            timeout=timeout,
        )
        settlement_preview_actual = _to_float(settlement_preview.get("value"))
        settlement_preview_ready = bool(
            settlement_preview.get("status") == "ok" and settlement_preview_actual is not None
        )
    reconstructed: dict[str, Any] = {}
    source_attempts: dict[str, dict[str, Any]] = {}
    if fetch_open_meteo_archive and feature_reconstruction_ready:
        try:
            features = _features_from_open_meteo_archive(
                float(lat),
                float(lon),
                event_date,
                tz,
                timeout=timeout,
            )
            _merge_without_overwrite(reconstructed, features)
            source_attempts["open_meteo_archive"] = {
                "status": "ok",
                "feature_count": len(features),
            }
        except Exception as exc:
            source_attempts["open_meteo_archive"] = {
                "status": "error",
                "error": exc.__class__.__name__,
            }
            if fetch_open_meteo_historical_forecast_fallback:
                try:
                    features = _features_from_open_meteo_historical_forecast(
                        float(lat),
                        float(lon),
                        event_date,
                        tz,
                        timeout=timeout,
                    )
                    _merge_without_overwrite(reconstructed, features)
                    source_attempts["open_meteo_historical_forecast"] = {
                        "status": "ok",
                        "feature_count": len(features),
                    }
                except Exception as fallback_exc:
                    source_attempts["open_meteo_historical_forecast"] = {
                        "status": "error",
                        "error": fallback_exc.__class__.__name__,
                    }
    else:
        source_attempts["open_meteo_archive"] = {
            "status": "not_fetched" if feature_reconstruction_ready else "not_applicable",
        }
    if isinstance(settlement_preview.get("temperature_features"), Mapping):
        _merge_stronger_temperature_trajectory(
            reconstructed,
            settlement_preview.get("temperature_features") or {},
        )
        source_attempts["weather_com_history_temperature"] = {
            "status": "ok",
            "feature_count": len(settlement_preview.get("temperature_features") or {}),
        }
    groups = _derived_feature_group_status(reconstructed)
    quality = _feature_quality(reconstructed)
    feature_keys = sorted(str(key) for key in reconstructed.keys())
    feature_digest = _stable_digest(
        {
            key: reconstructed.get(key)
            for key in feature_keys
            if key not in {"raw_response", "source_url"}
        }
    ) if reconstructed else ""
    missing_groups = [
        group for group, status in groups.items() if not bool(status.get("available"))
    ]
    mrms_applicable = bool(lat is not None and lon is not None and _is_us_coordinate(lat, lon))
    nws_applicable = bool(mrms_applicable and (not station_id or _is_us_station(station_id)))
    return {
        "slug": slug,
        "event_date": event_date,
        "title": title,
        "sources": list(record.get("sources") or []),
        "station_id": station_id,
        "resolution_source": resolution_source,
        "resolution_source_kind": source_kind,
        "market_unit": market_unit,
        "market_var": market_var,
        "outcome_labels": outcome_labels,
        "outcome_count": len(outcome_labels),
        "yes_prices": yes_prices,
        "yes_price_count": yes_price_count,
        "yes_price_digest": _stable_digest([round(float(val), 8) for val in yes_prices]) if yes_prices else "",
        "probability_snapshot_ready": probability_snapshot_ready,
        "settlement_actual_ready": settlement_actual_ready,
        "settlement_preview_ready": settlement_preview_ready,
        "settlement_preview_actual": settlement_preview_actual,
        "settlement_preview": settlement_preview,
        "lat": lat,
        "lon": lon,
        "tz": tz,
        "coord_source": coord_source,
        "metadata_ready": metadata_ready,
        "settlement_source_ready": settlement_source_ready,
        "feature_reconstruction_ready": feature_reconstruction_ready,
        "strict_radar_qpe_applicable": mrms_applicable,
        "nws_applicable": nws_applicable,
        "open_meteo_archive_applicable": feature_reconstruction_ready,
        "projected_feature_quality": quality,
        "projected_complete": quality == "complete",
        "feature_key_count": len(feature_keys),
        "feature_keys": feature_keys,
        "feature_digest": feature_digest,
        "weather_likelihood_features": reconstructed,
        "weather_likelihood_feature_groups": groups,
        "backtest_row_ready": bool(
            quality == "complete"
            and metadata_ready
            and probability_snapshot_ready
            and settlement_actual_ready
        ),
        "backtest_row_ready_preview": bool(
            quality == "complete"
            and metadata_ready
            and probability_snapshot_ready
            and (settlement_actual_ready or settlement_preview_ready)
        ),
        "missing_reasons": missing_reasons,
        "backtest_missing_reasons": backtest_missing_reasons,
        "missing_feature_groups": missing_groups,
        "source_attempts": source_attempts,
    }


def _candidate_projection_summary(
    records: Sequence[Mapping[str, Any]],
    *,
    fetch_open_meteo_archive: bool,
    fetch_open_meteo_historical_forecast_fallback: bool = True,
    fetch_settlement_preview: bool = False,
    max_settlement_fetches: int = 0,
    timeout: float,
    max_candidates: int,
    workers: int = 1,
    emit_apply_plan: bool = False,
    apply_plan_max_rows: int = 50,
    apply_plan_include_payload: bool = False,
) -> dict[str, Any]:
    selected = list(records)
    if max_candidates > 0:
        selected = selected[: int(max_candidates)]
    worker_count = max(1, min(int(workers or 1), 16))
    settlement_fetch_limit = max(0, int(max_settlement_fetches or 0))

    def _project_indexed(index_record: tuple[int, Mapping[str, Any]]) -> dict[str, Any]:
        idx, record = index_record
        allow_settlement_fetch = bool(
            fetch_settlement_preview
            and (settlement_fetch_limit <= 0 or idx < settlement_fetch_limit)
        )
        return _candidate_projection_record(
            record,
            fetch_open_meteo_archive=fetch_open_meteo_archive,
            fetch_open_meteo_historical_forecast_fallback=fetch_open_meteo_historical_forecast_fallback,
            fetch_settlement_preview=allow_settlement_fetch,
            timeout=timeout,
        )

    if (fetch_open_meteo_archive or fetch_settlement_preview) and worker_count > 1 and len(selected) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            projections = list(executor.map(_project_indexed, list(enumerate(selected))))
    else:
        projections = [_project_indexed(item) for item in enumerate(selected)]
    counts: dict[str, int] = {
        "candidate_records_scanned": len(projections),
        "metadata_ready": sum(1 for item in projections if item.get("metadata_ready")),
        "settlement_source_ready": sum(1 for item in projections if item.get("settlement_source_ready")),
        "feature_reconstruction_ready": sum(1 for item in projections if item.get("feature_reconstruction_ready")),
        "probability_snapshot_ready": sum(1 for item in projections if item.get("probability_snapshot_ready")),
        "settlement_actual_ready": sum(1 for item in projections if item.get("settlement_actual_ready")),
        "settlement_preview_ready": sum(1 for item in projections if item.get("settlement_preview_ready")),
        "strict_radar_qpe_applicable": sum(1 for item in projections if item.get("strict_radar_qpe_applicable")),
        "nws_applicable": sum(1 for item in projections if item.get("nws_applicable")),
        "projected_complete": sum(1 for item in projections if item.get("projected_complete")),
        "complete_feature_probability_ready": sum(
            1
            for item in projections
            if item.get("projected_complete") and item.get("probability_snapshot_ready")
        ),
        "backtest_row_ready": sum(1 for item in projections if item.get("backtest_row_ready")),
        "backtest_row_ready_preview": sum(1 for item in projections if item.get("backtest_row_ready_preview")),
    }
    quality_counts: dict[str, int] = {}
    missing_reason_counts: dict[str, int] = {}
    backtest_missing_reason_counts: dict[str, int] = {}
    missing_group_counts: dict[str, int] = {}
    source_status_counts: dict[str, int] = {}
    source_error_counts: dict[str, int] = {}
    resolution_source_kind_counts: dict[str, int] = {}
    settlement_preview_status_counts: dict[str, int] = {}
    settlement_preview_error_counts: dict[str, int] = {}
    for item in projections:
        _counter_inc(quality_counts, str(item.get("projected_feature_quality") or "missing"))
        _counter_inc(resolution_source_kind_counts, str(item.get("resolution_source_kind") or "missing"))
        preview = item.get("settlement_preview")
        if isinstance(preview, Mapping):
            status = str(preview.get("status") or "unknown")
            _counter_inc(settlement_preview_status_counts, status)
            if status == "error":
                _counter_inc(settlement_preview_error_counts, str(preview.get("error") or "unknown"))
        for reason in item.get("missing_reasons") or []:
            _counter_inc(missing_reason_counts, str(reason))
        for reason in item.get("backtest_missing_reasons") or []:
            _counter_inc(backtest_missing_reason_counts, str(reason))
        for group in item.get("missing_feature_groups") or []:
            _counter_inc(missing_group_counts, str(group))
        attempts = item.get("source_attempts") or {}
        if isinstance(attempts, Mapping):
            for source, detail in attempts.items():
                status = detail.get("status") if isinstance(detail, Mapping) else "unknown"
                _counter_inc(source_status_counts, f"{source}:{status}")
                if status == "error" and isinstance(detail, Mapping):
                    _counter_inc(source_error_counts, f"{source}:{detail.get('error') or 'unknown'}")
    apply_plan = (
        _candidate_physical_backtest_apply_plan(
            projections,
            max_rows=int(apply_plan_max_rows),
            include_payload=bool(apply_plan_include_payload),
        )
        if emit_apply_plan
        else {}
    )

    def _sample_projection(item: Mapping[str, Any]) -> dict[str, Any]:
        omitted = {
            "weather_likelihood_features",
            "weather_likelihood_feature_groups",
            "outcome_labels",
            "yes_prices",
        }
        return {str(key): value for key, value in item.items() if key not in omitted}

    return {
        "counts": counts,
        "quality_counts": dict(sorted(quality_counts.items())),
        "missing_reason_counts": dict(sorted(missing_reason_counts.items())),
        "backtest_missing_reason_counts": dict(sorted(backtest_missing_reason_counts.items())),
        "missing_group_counts": dict(sorted(missing_group_counts.items())),
        "source_status_counts": dict(sorted(source_status_counts.items())),
        "source_error_counts": dict(sorted(source_error_counts.items())),
        "resolution_source_kind_counts": dict(sorted(resolution_source_kind_counts.items())),
        "settlement_preview_status_counts": dict(sorted(settlement_preview_status_counts.items())),
        "settlement_preview_error_counts": dict(sorted(settlement_preview_error_counts.items())),
        "physical_backtest_apply_plan": apply_plan,
        "sample_candidates": [_sample_projection(item) for item in projections[:10]],
    }


def _candidate_physical_backtest_apply_plan(
    projections: Sequence[Mapping[str, Any]],
    *,
    max_rows: int,
    include_payload: bool = False,
) -> dict[str, Any]:
    ready = [
        item
        for item in projections
        if bool(item.get("backtest_row_ready_preview"))
    ]
    existing_actual = [item for item in ready if bool(item.get("settlement_actual_ready"))]
    preview_actual = [item for item in ready if not bool(item.get("settlement_actual_ready")) and bool(item.get("settlement_preview_ready"))]
    source_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    for item in ready:
        _counter_inc(source_counts, str(item.get("resolution_source_kind") or "missing"))
        action = (
            "append_feature_snapshot_with_existing_actual"
            if item.get("settlement_actual_ready")
            else "append_candidate_actual_and_feature_snapshot"
        )
        _counter_inc(action_counts, action)

    def _snapshot_payload(item: Mapping[str, Any]) -> dict[str, Any]:
        labels = [str(label) for label in (item.get("outcome_labels") or [])]
        prices = [
            float(price)
            for price in (item.get("yes_prices") or [])
            if _to_float(price) is not None
        ]
        rows = [
            {
                "outcome": label,
                "q_raw_static": float(price),
                "q_raw": float(price),
                "q_round": float(price),
                "q_bayes_posterior": float(price),
            }
            for label, price in zip(labels, prices)
        ]
        features = (
            dict(item.get("weather_likelihood_features"))
            if isinstance(item.get("weather_likelihood_features"), Mapping)
            else {}
        )
        groups = (
            dict(item.get("weather_likelihood_feature_groups"))
            if isinstance(item.get("weather_likelihood_feature_groups"), Mapping)
            else {}
        )
        return {
            "market_unit": item.get("market_unit") or "",
            "market_var": item.get("market_var") or "temperature_2m_max",
            "weather_likelihood_features": features,
            "weather_likelihood_feature_groups": groups,
            "weather_likelihood_feature_quality": item.get("projected_feature_quality") or "",
            "weather_likelihood_feature_digest": item.get("feature_digest") or "",
            "physical_backtest_snapshot_source": "dry_run_physical_feature_reconstruction",
            "rows": rows,
        }

    def _plan_row(item: Mapping[str, Any]) -> dict[str, Any]:
        action = (
            "append_feature_snapshot_with_existing_actual"
            if item.get("settlement_actual_ready")
            else "append_candidate_actual_and_feature_snapshot"
        )
        preview = item.get("settlement_preview") if isinstance(item.get("settlement_preview"), Mapping) else {}
        basis = {
            "slug": item.get("slug") or "",
            "event_date": item.get("event_date") or "",
            "action": action,
            "actual": item.get("settlement_preview_actual") if not item.get("settlement_actual_ready") else "existing_db_actual",
            "feature_digest": item.get("feature_digest") or "",
            "yes_price_digest": item.get("yes_price_digest") or "",
        }
        row = {
            "slug": item.get("slug") or "",
            "event_date": item.get("event_date") or "",
            "title": item.get("title") or "",
            "sources": list(item.get("sources") or []),
            "action": action,
            "target_table": "event_runs",
            "would_write": False,
            "would_append_row_after_confirmation": True,
            "settlement_actual_source": "existing_db" if item.get("settlement_actual_ready") else preview.get("source") or "",
            "candidate_settlement_actual": item.get("settlement_preview_actual"),
            "settlement_unit": preview.get("unit") or item.get("market_unit") or "",
            "station_id": item.get("station_id") or "",
            "lat": item.get("lat"),
            "lon": item.get("lon"),
            "tz": item.get("tz") or "",
            "resolution_source_kind": item.get("resolution_source_kind") or "",
            "resolution_source": item.get("resolution_source") or "",
            "market_var": item.get("market_var") or "temperature_2m_max",
            "market_unit": item.get("market_unit") or "",
            "outcome_count": int(item.get("outcome_count") or 0),
            "yes_price_count": int(item.get("yes_price_count") or 0),
            "yes_price_digest": item.get("yes_price_digest") or "",
            "feature_quality": item.get("projected_feature_quality") or "",
            "feature_key_count": int(item.get("feature_key_count") or 0),
            "feature_digest": item.get("feature_digest") or "",
            "review_checksum": _stable_digest(basis),
        }
        if include_payload:
            payload = _snapshot_payload(item)
            row["snapshot_payload"] = payload
            row["snapshot_payload_digest"] = _stable_digest(payload)
        return row

    row_limit = int(max_rows or 0)
    selected_rows = ready if row_limit <= 0 else ready[: max(0, row_limit)]
    rows = [_plan_row(item) for item in selected_rows]
    blockers: list[str] = []
    if len(ready) < 100:
        blockers.append(f"apply_plan_ready_rows_below_100:{len(ready)}<100")
    return {
        "ok": not blockers,
        "read_only": True,
        "writes_performed": False,
        "apply_allowed": False,
        "requires_confirmation": True,
        "required_confirm_apply": PHYSICAL_BACKTEST_SNAPSHOT_CONFIRM_PHRASE,
        "write_policy": "append_only_event_runs_candidate_settlement_and_physical_feature_snapshot_rows",
        "target_table": "event_runs",
        "candidate_count": len(projections),
        "ready_row_count": len(ready),
        "existing_actual_ready_rows": len(existing_actual),
        "preview_actual_ready_rows": len(preview_actual),
        "would_append_event_run_rows_after_confirmation": len(ready),
        "projected_formal_complete_feature_rows_after_apply": len(ready),
        "source_kind_counts": dict(sorted(source_counts.items())),
        "action_counts": dict(sorted(action_counts.items())),
        "row_sample_count": len(rows),
        "rows_include_payload": bool(include_payload),
        "rows": rows,
        "blockers": blockers,
        "safety_constraints": [
            "no_write_without_explicit_confirmation",
            "append_only_event_runs_rows",
            "do_not_overwrite_existing_settlement_actual",
            "preserve_existing_payloads_and_stronger_sources",
            "rerun_backtest_physical_likelihood_after_confirmed_apply",
        ],
        "next_action": (
            "review_impact_then_build_confirmed_apply_tool"
            if not blockers
            else "collect_more_preview_ready_rows_before_apply"
        ),
    }


def _analyze_event(
    row: sqlite3.Row,
    *,
    fetch_open_meteo_archive: bool,
    fetch_open_meteo_historical_forecast_fallback: bool = True,
    timeout: float,
) -> dict[str, Any]:
    payload = _safe_json(row["payload_json"])
    payload_rows = payload.get("rows") if isinstance(payload, Mapping) else []
    if not isinstance(payload_rows, list):
        payload_rows = []
    base_features = _extract_likelihood_features(payload if isinstance(payload, Mapping) else {}, payload_rows)
    reconstructed = dict(base_features)
    lat = _to_float(row["lat"])
    lon = _to_float(row["lon"])
    tz = str(row["tz"] or "").strip() or "UTC"
    event_date = _normalize_date(row["event_date"])
    station_id = str(row["station_id"] or "").strip().upper()
    source_attempts: dict[str, dict[str, Any]] = {}
    open_meteo_applicable = lat is not None and lon is not None and bool(event_date)
    if fetch_open_meteo_archive and open_meteo_applicable:
        try:
            features = _features_from_open_meteo_archive(float(lat), float(lon), event_date, tz, timeout=timeout)
            _merge_without_overwrite(reconstructed, features)
            source_attempts["open_meteo_archive"] = {
                "status": "ok",
                "feature_count": len(features),
            }
        except Exception as exc:
            source_attempts["open_meteo_archive"] = {
                "status": "error",
                "error": exc.__class__.__name__,
            }
            if fetch_open_meteo_historical_forecast_fallback:
                try:
                    features = _features_from_open_meteo_historical_forecast(
                        float(lat),
                        float(lon),
                        event_date,
                        tz,
                        timeout=timeout,
                    )
                    _merge_without_overwrite(reconstructed, features)
                    source_attempts["open_meteo_historical_forecast"] = {
                        "status": "ok",
                        "feature_count": len(features),
                    }
                except Exception as fallback_exc:
                    source_attempts["open_meteo_historical_forecast"] = {
                        "status": "error",
                        "error": fallback_exc.__class__.__name__,
                    }
    else:
        source_attempts["open_meteo_archive"] = {
            "status": "not_fetched" if open_meteo_applicable else "not_applicable",
        }
    mrms_applicable = bool(lat is not None and lon is not None and _is_us_coordinate(lat, lon))
    nws_applicable = bool(mrms_applicable and (not station_id or _is_us_station(station_id)))
    metar_applicable = bool(station_id)
    groups = _derived_feature_group_status(reconstructed)
    quality = _feature_quality(reconstructed)
    missing_groups = [
        group for group, status in groups.items() if not bool(status.get("available"))
    ]
    return {
        "slug": row["slug"],
        "event_date": event_date,
        "city": row["city"],
        "station_id": station_id,
        "lat": lat,
        "lon": lon,
        "tz": tz,
        "local_payload_feature_quality": _feature_quality(base_features),
        "projected_feature_quality": quality,
        "local_feature_count": len(base_features),
        "projected_feature_count": len(reconstructed),
        "projected_complete": quality == "complete",
        "strict_radar_qpe_applicable": mrms_applicable,
        "nws_applicable": nws_applicable,
        "metar_applicable": metar_applicable,
        "open_meteo_archive_applicable": open_meteo_applicable,
        "missing_feature_groups": missing_groups,
        "source_attempts": source_attempts,
        "present_feature_groups": {
            group: status.get("present_keys", []) for group, status in groups.items() if status.get("available")
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--max-events", type=int, default=0)
    ap.add_argument("--analyze-candidates", action="store_true")
    ap.add_argument("--max-candidates", type=int, default=0)
    ap.add_argument("--candidate-fetch-workers", type=int, default=1)
    ap.add_argument("--emit-apply-plan", action="store_true")
    ap.add_argument("--apply-plan-max-rows", type=int, default=50)
    ap.add_argument("--apply-plan-include-payload", action="store_true")
    ap.add_argument("--fetch-open-meteo-archive", action="store_true")
    ap.add_argument(
        "--no-open-meteo-historical-forecast-fallback",
        action="store_true",
        help="Disable read-only historical-forecast fallback after Open-Meteo archive errors.",
    )
    ap.add_argument(
        "--fetch-settlement-preview",
        action="store_true",
        help="Read-only preview of official WU settlement_actual candidates; never writes to the DB.",
    )
    ap.add_argument("--max-settlement-fetches", type=int, default=0)
    ap.add_argument("--timeout-sec", type=float, default=20.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    tz = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone(timedelta(hours=-5))
    today_et = datetime.now(timezone.utc).astimezone(tz).date()
    start_date = (today_et - timedelta(days=max(1, int(args.days)) - 1)).isoformat()
    end_date = today_et.isoformat()
    db = Path(args.db)
    blockers: list[str] = []
    events: list[dict[str, Any]] = []
    raw_count = 0
    candidate_sources: dict[str, Any] = {}
    candidate_projection: dict[str, Any] = {}
    if not db.exists():
        blockers.append("archive_db_missing")
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            rows = _event_rows(conn, start_date, end_date, int(args.max_events))
            raw_count = len(rows)
            candidate_sources = _candidate_source_summary(conn, start_date, end_date, rows)
            if args.analyze_candidates:
                combined_records = _combined_candidate_records(conn, start_date, end_date, rows)
                candidate_records = [
                    item
                    for _key, item in sorted(
                        combined_records.items(),
                        key=lambda kv: (kv[0][1], kv[0][0]),
                    )
                ]
                candidate_projection = _candidate_projection_summary(
                    candidate_records,
                    fetch_open_meteo_archive=bool(args.fetch_open_meteo_archive),
                    fetch_open_meteo_historical_forecast_fallback=not bool(
                        args.no_open_meteo_historical_forecast_fallback
                    ),
                    fetch_settlement_preview=bool(args.fetch_settlement_preview),
                    max_settlement_fetches=int(args.max_settlement_fetches),
                    timeout=float(args.timeout_sec),
                    max_candidates=int(args.max_candidates),
                    workers=int(args.candidate_fetch_workers),
                    emit_apply_plan=bool(args.emit_apply_plan),
                    apply_plan_max_rows=int(args.apply_plan_max_rows),
                    apply_plan_include_payload=bool(args.apply_plan_include_payload),
                )
            for row in rows:
                events.append(
                    _analyze_event(
                        row,
                        fetch_open_meteo_archive=bool(args.fetch_open_meteo_archive),
                        fetch_open_meteo_historical_forecast_fallback=not bool(
                            args.no_open_meteo_historical_forecast_fallback
                        ),
                        timeout=float(args.timeout_sec),
                    )
                )
        finally:
            conn.close()
    quality_counts: dict[str, int] = {}
    local_quality_counts: dict[str, int] = {}
    missing_group_counts: dict[str, int] = {}
    source_applicability_counts: dict[str, int] = {}
    source_status_counts: dict[str, int] = {}
    for event in events:
        _counter_inc(quality_counts, str(event.get("projected_feature_quality") or "missing"))
        _counter_inc(local_quality_counts, str(event.get("local_payload_feature_quality") or "missing"))
        for key in (
            "strict_radar_qpe_applicable",
            "nws_applicable",
            "metar_applicable",
            "open_meteo_archive_applicable",
        ):
            if event.get(key):
                _counter_inc(source_applicability_counts, key)
        for group in event.get("missing_feature_groups") or []:
            _counter_inc(missing_group_counts, str(group))
        attempts = event.get("source_attempts") or {}
        if isinstance(attempts, Mapping):
            for source, detail in attempts.items():
                status = detail.get("status") if isinstance(detail, Mapping) else "unknown"
                _counter_inc(source_status_counts, f"{source}:{status}")
    complete_count = quality_counts.get("complete", 0)
    if len(events) < 100:
        blockers.append(f"candidate_event_days_below_100:{len(events)}<100")
    combined_candidates = int(candidate_sources.get("combined_high_temp_candidate_days") or 0)
    if combined_candidates < 100:
        blockers.append(f"combined_discovered_candidate_days_below_100:{combined_candidates}<100")
    if complete_count < 100:
        blockers.append(f"projected_complete_feature_days_below_100:{complete_count}<100")
    if source_applicability_counts.get("strict_radar_qpe_applicable", 0) <= 0:
        blockers.append("strict_radar_qpe_not_applicable_to_current_window")
    if args.analyze_candidates:
        projection_counts = candidate_projection.get("counts") if isinstance(candidate_projection, Mapping) else {}
        if isinstance(projection_counts, Mapping):
            metadata_ready = int(projection_counts.get("metadata_ready") or 0)
            feature_ready = int(projection_counts.get("feature_reconstruction_ready") or 0)
            if metadata_ready < 100:
                blockers.append(f"candidate_settlement_metadata_ready_below_100:{metadata_ready}<100")
            if feature_ready < 100:
                blockers.append(f"candidate_feature_reconstruction_ready_below_100:{feature_ready}<100")
            if args.fetch_open_meteo_archive:
                projected_complete = int(projection_counts.get("projected_complete") or 0)
                if projected_complete < 100:
                    blockers.append(f"candidate_projected_complete_feature_days_below_100:{projected_complete}<100")
                backtest_ready = int(projection_counts.get("backtest_row_ready") or 0)
                if backtest_ready < 100:
                    blockers.append(f"candidate_backtest_row_ready_below_100:{backtest_ready}<100")
            if args.fetch_settlement_preview:
                backtest_ready_preview = int(projection_counts.get("backtest_row_ready_preview") or 0)
                if backtest_ready_preview < 100:
                    blockers.append(f"candidate_backtest_row_ready_preview_below_100:{backtest_ready_preview}<100")
    report = {
        "ok": not blockers,
        "mode": "dry_run_read_only",
        "writes_performed": False,
        "db": str(db),
        "window": {"start_date_et": start_date, "end_date_et": end_date, "days": int(args.days)},
        "options": {
            "max_events": int(args.max_events),
            "analyze_candidates": bool(args.analyze_candidates),
            "max_candidates": int(args.max_candidates),
            "candidate_fetch_workers": int(args.candidate_fetch_workers),
            "emit_apply_plan": bool(args.emit_apply_plan),
            "apply_plan_max_rows": int(args.apply_plan_max_rows),
            "apply_plan_include_payload": bool(args.apply_plan_include_payload),
            "fetch_open_meteo_archive": bool(args.fetch_open_meteo_archive),
            "open_meteo_historical_forecast_fallback": not bool(
                args.no_open_meteo_historical_forecast_fallback
            ),
            "fetch_settlement_preview": bool(args.fetch_settlement_preview),
            "max_settlement_fetches": int(args.max_settlement_fetches),
        },
        "events_scanned": len(events),
        "query_rows_scanned": raw_count,
        "quality_counts": quality_counts,
        "local_payload_quality_counts": local_quality_counts,
        "candidate_sources": candidate_sources,
        "candidate_projection": candidate_projection,
        "source_applicability_counts": source_applicability_counts,
        "source_status_counts": source_status_counts,
        "missing_group_counts": dict(sorted(missing_group_counts.items())),
        "blockers": blockers,
        "sample_events": events[:10],
    }
    if str(args.out or "").strip():
        out_path = Path(str(args.out).strip())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
