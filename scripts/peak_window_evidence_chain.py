#!/usr/bin/env python3
"""Read-only second-level evidence recorder for weather peak windows.

The script samples external/current weather surfaces and appends one compact
NDJSON record per sample. It intentionally does not import bot/strategy code,
does not write SQLite, and does not enqueue or signal trading work.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - old Python fallback
    ZoneInfo = None  # type: ignore


DEFAULT_STATION = "ZUUU"
DEFAULT_COUNTRY = "CN"
DEFAULT_EVENT_DATE = "2026-07-03"
DEFAULT_TZ = "Asia/Shanghai"
DEFAULT_WU_URL = "https://www.wunderground.com/weather/cn/chengdu"
AWC_CACHE_URL = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
WEATHER_COM_CURRENT_URL = "https://api.weather.com/v3/wx/observations/current"
WEATHER_COM_HISTORY_URL_PREFIX = "https://api.weather.com/v1/location"

# Existing project code uses this Weather.com/WU public app key as a fallback.
DEFAULT_WEATHER_COM_API_KEY = "6532d6454b8aa370768e63d6ba5a832e"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def fahrenheit_to_c(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round((float(value) - 32.0) * 5.0 / 9.0, 3)


def parse_utc_datetime(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def epoch_to_utc_iso(value: Any) -> str:
    ts = safe_float(value)
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def utc_iso_to_local(value: str, tz_name: str) -> str:
    dt = parse_utc_datetime(value)
    if dt is None or ZoneInfo is None:
        return ""
    try:
        return dt.astimezone(ZoneInfo(tz_name)).isoformat()
    except Exception:
        return ""


def epoch_to_local_iso(value: Any, tz_name: str) -> str:
    utc_iso = epoch_to_utc_iso(value)
    return utc_iso_to_local(utc_iso, tz_name) if utc_iso else ""


def compact_url(url: str) -> str:
    parts = urlsplit(url)
    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in {"apikey", "api_key", "token", "key"}:
            query_items.append((key, "REDACTED"))
        else:
            query_items.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def request_payload(
    url: str,
    *,
    response_kind: str,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 20.0,
) -> Tuple[Optional[Any], Dict[str, Any]]:
    started = datetime.now(timezone.utc)
    monotonic_start = time.perf_counter()
    meta: Dict[str, Any] = {
        "request_started_at": started.isoformat().replace("+00:00", "Z"),
        "url": compact_url(url),
        "response_kind": response_kind,
    }
    try:
        resp = requests.get(url, headers=dict(headers or {}), timeout=timeout)
        duration_ms = int((time.perf_counter() - monotonic_start) * 1000)
        completed = datetime.now(timezone.utc)
        meta.update(
            {
                "ok": 200 <= int(resp.status_code) < 300,
                "status_code": int(resp.status_code),
                "response_date": resp.headers.get("Date") or "",
                "content_type": resp.headers.get("Content-Type") or "",
                "content_length": len(resp.content or b""),
                "request_completed_at": completed.isoformat().replace("+00:00", "Z"),
                "duration_ms": duration_ms,
            }
        )
        if not meta["ok"]:
            meta["error"] = f"http_{resp.status_code}"
            return None, meta
        if response_kind == "json":
            return resp.json(), meta
        if response_kind == "text":
            return resp.text, meta
        return resp.content, meta
    except Exception as exc:
        duration_ms = int((time.perf_counter() - monotonic_start) * 1000)
        completed = datetime.now(timezone.utc)
        meta.update(
            {
                "ok": False,
                "error": f"{exc.__class__.__name__}: {exc}",
                "request_completed_at": completed.isoformat().replace("+00:00", "Z"),
                "duration_ms": duration_ms,
            }
        )
        return None, meta


def weather_com_api_key() -> Tuple[str, str]:
    for env_name in ("PM_WEATHER_COM_API_KEY", "PM_WU_HISTORY_API_KEY"):
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            return value, f"env:{env_name}"
    return DEFAULT_WEATHER_COM_API_KEY, "project_default_public_key"


def extract_wu_api_key(text: str) -> str:
    matches = re.findall(r"apiKey=([A-Za-z0-9]+)", text or "")
    return str(matches[0]) if matches else ""


def strip_html(text: str) -> str:
    plain = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", plain).strip()


def parse_wu_display_html(text: str, tz_name: str) -> Dict[str, Any]:
    plain = strip_html(text)
    access_time = ""
    access_match = re.search(
        r"access_time\s+(?P<access>[^|]{1,120}?\(GMT\s*[+-]?\d+\))\s*\|",
        plain,
        flags=re.I,
    )
    if access_match:
        access_time = access_match.group("access").strip()
    updated_text = ""
    updated_match = re.search(
        r"\bUpdated\s+(?:just now|\d+\s+(?:second|seconds|minute|minutes|hour|hours)\s+ago)",
        plain,
        flags=re.I,
    )
    if updated_match:
        updated_text = updated_match.group(0).strip()
    station_label = ""
    station_match = re.search(
        r"(Chengdu Shuangliu Intl Airport(?: Station)?(?: \| Report)?)",
        plain,
        flags=re.I,
    )
    if station_match:
        station_label = station_match.group(1).strip()
    temp_f: Optional[float] = None
    anchor = plain.find(updated_text) if updated_text else -1
    snippet = plain[anchor : anchor + 1200] if anchor >= 0 else plain[:2000]
    temp_match = re.search(r"(-?\d+(?:\.\d+)?)\s*°\s*F\b", snippet)
    if temp_match:
        temp_f = safe_float(temp_match.group(1))
    return {
        "display_access_time_text": access_time,
        "display_updated_text": updated_text,
        "station_label": station_label,
        "temperature_f": temp_f,
        "temperature_c": fahrenheit_to_c(temp_f),
        "valid_time_local_text": access_time,
        "valid_time_local": "",
        "valid_time_utc": "",
        "html_excerpt": snippet[:320],
        "api_key_found": bool(extract_wu_api_key(text)),
        "timezone": tz_name,
    }


def wu_display_source(wu_url: str, tz_name: str, timeout: float) -> Tuple[Dict[str, Any], str]:
    payload, meta = request_payload(
        wu_url,
        response_kind="text",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    entry: Dict[str, Any] = {
        "source": "wu_display_html",
        "provider": "Wunderground/Weather.com",
        "ok": bool(meta.get("ok")),
        "transport": meta,
    }
    api_key = ""
    if isinstance(payload, str):
        api_key = extract_wu_api_key(payload)
        entry.update(parse_wu_display_html(payload, tz_name))
    else:
        entry["error"] = meta.get("error") or "wu_html_missing"
    return entry, api_key


def weather_current_source(
    *,
    source: str,
    station: str,
    api_key: str,
    api_key_source: str,
    tz_name: str,
    timeout: float,
) -> Dict[str, Any]:
    url = (
        f"{WEATHER_COM_CURRENT_URL}?apiKey={quote(api_key)}"
        f"&language=en-US&format=json&icaoCode={quote(station)}&units=m"
    )
    payload, meta = request_payload(
        url,
        response_kind="json",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    entry: Dict[str, Any] = {
        "source": source,
        "provider": "Weather.com/TWC",
        "station_id": station,
        "api_key_source": api_key_source,
        "ok": bool(meta.get("ok")) and isinstance(payload, Mapping),
        "transport": meta,
    }
    if isinstance(payload, Mapping):
        valid_utc = epoch_to_utc_iso(payload.get("validTimeUtc"))
        entry.update(
            {
                "valid_time_local": str(payload.get("validTimeLocal") or ""),
                "valid_time_utc": valid_utc,
                "valid_time_local_from_utc": utc_iso_to_local(valid_utc, tz_name) if valid_utc else "",
                "temperature_c": safe_float(payload.get("temperature")),
                "feels_like_c": safe_float(payload.get("temperatureFeelsLike")),
                "dewpoint_c": safe_float(payload.get("temperatureDewPoint")),
                "pressure_altimeter_hpa": safe_float(payload.get("pressureAltimeter")),
                "phrase": str(payload.get("wxPhraseLong") or payload.get("wxPhraseMedium") or ""),
            }
        )
    else:
        entry["error"] = meta.get("error") or "json_payload_missing"
    return entry


def observation_from_history_row(row: Mapping[str, Any], tz_name: str) -> Dict[str, Any]:
    valid_utc = epoch_to_utc_iso(row.get("valid_time_gmt"))
    return {
        "valid_time_utc": valid_utc,
        "valid_time_local": epoch_to_local_iso(row.get("valid_time_gmt"), tz_name),
        "temperature_c": safe_float(row.get("temp")),
        "heat_index_c": safe_float(row.get("heat_index")),
        "dewpoint_c": safe_float(row.get("dewPt")),
        "relative_humidity_pct": safe_float(row.get("rh")),
        "phrase": str(row.get("wx_phrase") or ""),
        "expire_time_utc": epoch_to_utc_iso(row.get("expire_time_gmt")),
    }


def weather_history_source(
    *,
    source: str,
    station: str,
    country: str,
    event_date: str,
    api_key: str,
    api_key_source: str,
    tz_name: str,
    timeout: float,
) -> Dict[str, Any]:
    date_key = re.sub(r"[^0-9]", "", event_date)[:8]
    location_id = f"{station}:9:{country}"
    url = (
        f"{WEATHER_COM_HISTORY_URL_PREFIX}/{quote(location_id, safe='')}/observations/historical.json"
        f"?apiKey={quote(api_key)}&units=m&startDate={date_key}&endDate={date_key}"
    )
    payload, meta = request_payload(
        url,
        response_kind="json",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=timeout,
    )
    entry: Dict[str, Any] = {
        "source": source,
        "provider": "Weather.com/TWC historical observations",
        "station_id": station,
        "event_date": event_date,
        "api_key_source": api_key_source,
        "ok": bool(meta.get("ok")) and isinstance(payload, Mapping),
        "transport": meta,
    }
    observations = payload.get("observations") if isinstance(payload, Mapping) else None
    if not isinstance(observations, list):
        entry["error"] = meta.get("error") or "observations_missing"
        return entry
    rows = [row for row in observations if isinstance(row, Mapping)]
    latest = max(rows, key=lambda row: safe_float(row.get("valid_time_gmt")) or -1.0, default=None)
    max_temp = max(rows, key=lambda row: safe_float(row.get("temp")) or -999.0, default=None)
    entry["observation_count"] = len(rows)
    if latest:
        latest_obs = observation_from_history_row(latest, tz_name)
        entry["latest_observation"] = latest_obs
        entry["valid_time_utc"] = latest_obs.get("valid_time_utc", "")
        entry["valid_time_local"] = latest_obs.get("valid_time_local", "")
        entry["temperature_c"] = latest_obs.get("temperature_c")
    if max_temp:
        entry["max_observation"] = observation_from_history_row(max_temp, tz_name)
        entry["today_max_c"] = entry["max_observation"].get("temperature_c")
    return entry


def parse_awc_cache_text(text: str, station: str, tz_name: str) -> Dict[str, Any]:
    reader = csv.DictReader(io.StringIO(text))
    row_count = 0
    selected: Optional[Mapping[str, str]] = None
    for row in reader:
        row_count += 1
        if str(row.get("station_id") or "").strip().upper() == station.upper():
            selected = row
            break
    if selected is None:
        return {
            "ok": False,
            "error": "station_not_found",
            "station_id": station,
            "cache_rows_scanned": row_count,
        }
    valid_utc = str(selected.get("observation_time") or "")
    return {
        "ok": True,
        "station_id": station,
        "cache_rows_scanned": row_count,
        "raw_text": str(selected.get("raw_text") or ""),
        "valid_time_utc": valid_utc,
        "valid_time_local": utc_iso_to_local(valid_utc, tz_name),
        "temperature_c": safe_float(selected.get("temp_c")),
        "dewpoint_c": safe_float(selected.get("dewpoint_c")),
        "wind_dir_degrees": safe_float(selected.get("wind_dir_degrees")),
        "wind_speed_kt": safe_float(selected.get("wind_speed_kt")),
        "altimeter_in_hg": safe_float(selected.get("altim_in_hg")),
        "flight_category": str(selected.get("flight_category") or ""),
        "metar_type": str(selected.get("metar_type") or ""),
    }


def awc_cache_source(station: str, tz_name: str, timeout: float) -> Dict[str, Any]:
    payload, meta = request_payload(AWC_CACHE_URL, response_kind="bytes", timeout=timeout)
    entry: Dict[str, Any] = {
        "source": "awc_cache_metars_csv_gz",
        "provider": "NOAA AviationWeather cache",
        "station_id": station,
        "ok": bool(meta.get("ok")),
        "transport": meta,
    }
    if not isinstance(payload, (bytes, bytearray)):
        entry["ok"] = False
        entry["error"] = meta.get("error") or "cache_payload_missing"
        return entry
    try:
        text = gzip.decompress(bytes(payload)).decode("utf-8", "replace")
    except Exception as exc:
        entry["ok"] = False
        entry["error"] = f"gzip_decode_error:{exc.__class__.__name__}"
        return entry
    parsed = parse_awc_cache_text(text, station, tz_name)
    entry.update(parsed)
    return entry


def sample_once(args: argparse.Namespace) -> Dict[str, Any]:
    sample_started = utc_now_iso()
    station = str(args.station or DEFAULT_STATION).strip().upper()
    country = str(args.country or DEFAULT_COUNTRY).strip().upper()
    tz_name = str(args.timezone or DEFAULT_TZ).strip() or DEFAULT_TZ
    timeout = float(args.timeout_sec)

    env_api_key, env_key_source = weather_com_api_key()
    sources: List[Dict[str, Any]] = []

    wu_display, wu_api_key = wu_display_source(str(args.wu_url), tz_name, timeout)
    sources.append(wu_display)

    sources.append(
        weather_current_source(
            source="twc_current_icao_metric",
            station=station,
            api_key=env_api_key,
            api_key_source=env_key_source,
            tz_name=tz_name,
            timeout=timeout,
        )
    )

    if wu_api_key:
        sources.append(
            weather_current_source(
                source="wu_page_api_current_icao_metric",
                station=station,
                api_key=wu_api_key,
                api_key_source="wunderground_display_extracted",
                tz_name=tz_name,
                timeout=timeout,
            )
        )
        if not args.no_history_api:
            sources.append(
                weather_history_source(
                    source="wu_page_api_current_day_history_metric",
                    station=station,
                    country=country,
                    event_date=str(args.event_date),
                    api_key=wu_api_key,
                    api_key_source="wunderground_display_extracted",
                    tz_name=tz_name,
                    timeout=timeout,
                )
            )
    else:
        sources.append(
            {
                "source": "wu_page_api_current_icao_metric",
                "provider": "Wunderground/Weather.com",
                "station_id": station,
                "ok": False,
                "error": "wunderground_api_key_not_found_in_display_html",
            }
        )

    sources.append(awc_cache_source(station, tz_name, timeout))

    return {
        "schema": "peak_window_evidence_chain_v1",
        "sample_started_at": sample_started,
        "sample_completed_at": utc_now_iso(),
        "station_id": station,
        "country": country,
        "event_date": str(args.event_date),
        "timezone": tz_name,
        "guards": {
            "mode": "read_only_evidence_chain",
            "output_only": "ndjson",
            "writes_facts_sqlite": False,
            "writes_archive_db": False,
            "writes_strategy_signal": False,
            "writes_auto_trade_decisions": False,
            "places_orders": False,
            "history_backfill": False,
            "launchd_install_or_restart": False,
        },
        "sources": sources,
    }


def default_output_path(station: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("data/evidence") / f"peak_window_evidence_chain_{station.lower()}_{stamp}.ndjson"


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", default=DEFAULT_STATION)
    parser.add_argument("--country", default=DEFAULT_COUNTRY)
    parser.add_argument("--event-date", default=DEFAULT_EVENT_DATE)
    parser.add_argument("--timezone", default=DEFAULT_TZ)
    parser.add_argument("--wu-url", default=DEFAULT_WU_URL)
    parser.add_argument("--interval-sec", type=float, default=1.0)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--duration-sec", type=float, default=0.0)
    parser.add_argument("--timeout-sec", type=float, default=20.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--no-history-api", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    station = str(args.station or DEFAULT_STATION).strip().upper()
    iterations = max(1, int(args.iterations or 1))
    if args.duration_sec and float(args.duration_sec) > 0:
        iterations = max(iterations, int(math.ceil(float(args.duration_sec) / max(0.1, float(args.interval_sec)))))
    output_path = Path(args.output) if args.output else default_output_path(station)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not args.quiet:
        print(f"[evidence-chain] output={output_path} iterations={iterations} interval_sec={args.interval_sec}")

    for idx in range(iterations):
        loop_started = time.perf_counter()
        sample = sample_once(args)
        sample["iteration"] = idx + 1
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
        if not args.quiet:
            summary = []
            for source in sample.get("sources", []):
                if not isinstance(source, Mapping):
                    continue
                summary.append(
                    f"{source.get('source')} ok={int(bool(source.get('ok')))} "
                    f"t={source.get('valid_time_local') or source.get('valid_time_local_text') or ''} "
                    f"temp={source.get('temperature_c')}"
                )
            print(f"[evidence-chain] sample={idx + 1} " + " | ".join(summary))
        if idx < iterations - 1:
            sleep_sec = max(0.0, float(args.interval_sec) - (time.perf_counter() - loop_started))
            if sleep_sec > 0:
                time.sleep(sleep_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
