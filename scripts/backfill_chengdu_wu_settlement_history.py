#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import sys
import time
import json
import requests
import subprocess
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm  # noqa: E402

DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_REPORT = ROOT / "data" / "reports" / "backfill_chengdu_settlement_history_latest.json"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
DEFAULT_DAYS = 365
DEFAULT_STATION_ID = "ZUUU"
DEFAULT_CITY = "chengdu"
DEFAULT_WU_BASE_URL = "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU"
DEFAULT_SCRAPER_MODE = os.getenv("PM_CHENGDU_WU_SCRAPER_MODE", "auto")
DEFAULT_BACKFILL_MODE = os.getenv("PM_CHENGDU_WU_BACKFILL_MODE", "event_runs")
APPLY_CONFIRM_PHRASE = "APPLY_CHENGDU_HISTORY_SETTLEMENTS"
SCRAPER_MODE_OPTIONS = {"auto", "requests", "playwright", "selenium", "beautifulsoup", "apify"}
BACKFILL_MODE_OPTIONS = {"event_runs", "event-runs", "daily_temps", "daily", "history_only", "history-only"}
_STATION_COORD_CACHE: Dict[str, Tuple[float, float]] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_city(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _to_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _today_et() -> str:
    try:
        return datetime.now(swm.ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return date.today().isoformat()


def _parse_iso_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _coerce_run_date_range(*, start_date: Optional[str], end_date: Optional[str], days: int) -> Tuple[str, str]:
    if not end_date:
        end_date = _today_et()
    end_dt = _parse_iso_date(end_date) or date.today()
    if days and days > 0:
        start_dt = end_dt - timedelta(days=days - 1)
    else:
        start_dt = end_dt
    if start_date:
        start_from_arg = _parse_iso_date(start_date)
        if start_from_arg:
            start_dt = start_from_arg
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
    return start_dt.isoformat(), end_dt.isoformat()


def _earliest_event_date(
    db_path: Path,
    *,
    city: str,
    station_id: str,
    market_scope: str,
) -> Optional[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        scope_filter = _market_scope_filter(market_scope)
        city_scope, city_params = _city_filter(city=city, station_id=station_id)
        if not city_scope:
            return None

        sql = f"SELECT MIN(event_date) FROM event_runs WHERE 1=1 {scope_filter} {city_scope}"
        row = conn.execute(sql, city_params).fetchone()
        if row and row[0]:
            return str(row[0])
        return None
    finally:
        conn.close()


def _normalize_url(base: str) -> str:
    if not base:
        return ""
    base = base.strip().rstrip("/")
    if "/date/" in base:
        base = base.split("/date/")[0]
    return base


def _wu_history_date_url(base: str, event_date: str) -> str:
    base = _normalize_url(base)
    if not base or not event_date:
        return ""
    # WU station weather pages use /cn/chengdu/ZUUU, but historical daily date
    # pages are served under /cn/chengdu/date/YYYY-MM-DD.
    if re.search(r"/history/daily/cn/chengdu/ZUUU$", base, flags=re.I):
        base = re.sub(r"/ZUUU$", "", base, flags=re.I)
    return f"{base}/date/{event_date}"


def _normalize_scraper_mode(value: str) -> str:
    if not value:
        return "auto"
    mode = str(value).strip().lower()
    if mode in SCRAPER_MODE_OPTIONS:
        return mode
    if mode in {"bs", "beautiful_soup", "bs4"}:
        return "beautifulsoup"
    if mode in {"pw", "pwb", "wd", "webkit", "chrome"}:
        return "playwright"
    if mode in {"selen", "sel", "selenium"}:
        return "selenium"
    if mode in {"ap", "apify_api", "apify"}:
        return "apify"
    return "auto"


def _normalize_backfill_mode(value: str) -> str:
    if not value:
        return "event_runs"
    mode = str(value).strip().lower().replace("_", "-")
    if mode in {"event-runs", "eventruns"}:
        return "event_runs"
    if mode in {"history-only", "historyonly", "daily", "daily-temps", "daily_temps"}:
        return "daily_temps"
    return "event_runs"


def _market_scope_filter(scope: str) -> str:
    scope_key = str(scope or "").strip().lower()
    if scope_key in {"", "all", "all_markets"}:
        return ""
    if scope_key not in {"highest_temperature", "highest-temperature", "highest-temp", "temperature_2m_max"}:
        return ""
    return (
        "AND ("
        " lower(slug) LIKE 'highest-temperature-in-%'"
        " OR lower(slug) LIKE 'highest-temperature-%'"
        " OR lower(title) LIKE '%highest temperature%'"
        " )"
        " AND lower(slug) NOT LIKE 'lowest-temperature-in-%'"
        " AND lower(title) NOT LIKE '%lowest temperature%'"
    )


def _city_filter(city: str, station_id: str) -> Tuple[str, List[str]]:
    city_norm = _normalize_city(city)
    filters: List[str] = []
    params: List[str] = []
    if city_norm:
        like = f"%{city_norm}%"
        filters.append("(lower(city) LIKE ? OR lower(slug) LIKE ? OR lower(title) LIKE ?)")
        params.extend([like, like, like])
    if station_id:
        filters.append("upper(coalesce(station_id, '')) = upper(?)")
        params.append(station_id)
    if not filters:
        return "", []
    return f"AND ({' OR '.join(filters)})", params


def _collect_candidate_rows(
    db_path: Path,
    *,
    city: str,
    station_id: str,
    market_scope: str,
    start_date: str,
    end_date: str,
) -> List[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        scope_filter = _market_scope_filter(market_scope)
        city_scope, city_params = _city_filter(city=city, station_id=station_id)

        sql = f"""
        WITH ranked AS (
          SELECT
            id, run_date, event_date, slug, title, city, station_id, market_var,
            market_unit, resolution_source, market_closed, event_end_utc, settlement_actual,
            payload_json, created_at
          FROM (
            SELECT
              e.id, e.run_date, e.event_date, e.slug, e.title, e.city, e.station_id,
              e.market_var, e.market_unit, e.resolution_source, e.market_closed, e.event_end_utc,
              e.settlement_actual, e.payload_json, e.created_at,
              ROW_NUMBER() OVER (
                PARTITION BY e.slug, e.event_date
                ORDER BY datetime(e.created_at) DESC, e.id DESC
              ) AS rn
            FROM event_runs e
            WHERE e.event_date >= ?
              AND e.event_date <= ?
              AND e.market_var = 'temperature_2m_max'
              {scope_filter}
              {city_scope}
          )
          WHERE rn = 1
        )
        SELECT *
        FROM ranked
        ORDER BY event_date DESC, slug ASC
        """
        params = [start_date, end_date] + city_params
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _collect_daily_history_candidates(
    db_path: Path,
    *,
    city: str,
    station_id: str,
    market_scope: str,
    start_date: str,
    end_date: str,
    wu_base_url: str,
) -> List[Dict[str, Any]]:
    city_norm = _normalize_city(city)
    station_norm = str(station_id or "").upper()
    source = _normalize_url(wu_base_url)
    if not source and station_norm:
        source = f"https://www.wunderground.com/history/daily/{city_norm.replace(' ', '-')}/{station_norm}"

    start_obj = _parse_iso_date(start_date)
    end_obj = _parse_iso_date(end_date)
    if not start_obj or not end_obj:
        return []
    if start_obj > end_obj:
        start_obj, end_obj = end_obj, start_obj

    out: List[Dict[str, Any]] = []
    canonical_city = city.strip() or city_norm.replace("-", " ").title() or "chengdu"
    canonical_station = station_norm

    event_day = start_obj
    while event_day <= end_obj:
        event_day_str = event_day.isoformat()
        out.append(
            {
                "run_date": event_day_str,
                "event_date": event_day_str,
                "slug": f"historical-{canonical_city.replace(' ', '-')}-{canonical_station.lower() or 'unknown'}-{event_day_str}",
                "title": f"{canonical_city.title()} Highest Temperature {event_day_str}",
                "city": canonical_city,
                "station_id": canonical_station,
                "market_var": "temperature_2m_max",
                "market_unit": "C",
                "resolution_source": source,
                "market_closed": True,
                "event_end_utc": f"{event_day_str}T23:59:59Z",
            }
        )
        event_day = event_day + timedelta(days=1)

    # In historical-only mode we intentionally cover every day in the window,
    # even if Polymarket market rows are not present.
    return out


def _row_event_date(row: Mapping[str, Any]) -> str:
    return str(row.get("event_date") or "").split("T", 1)[0]


def _is_closed(row: Mapping[str, Any], *, now_utc: datetime, today_et: str) -> bool:
    closed = str(row.get("market_closed") or "").strip().lower() in {"1", "true", "yes", "on"}
    if closed:
        return True

    event_end = str(row.get("event_end_utc") or "").strip()
    if event_end:
        try:
            ended_at = datetime.fromisoformat(event_end.replace("Z", "+00:00"))
            if ended_at.tzinfo is None:
                ended_at = ended_at.replace(tzinfo=timezone.utc)
            if ended_at <= now_utc:
                return True
        except Exception:
            pass

    event_day = _row_event_date(row)
    return bool(event_day and event_day < today_et)


def _build_resolution_source(
    row: Mapping[str, Any],
    *,
    station_id: str,
    city: str,
    fallback_base: str,
) -> str:
    explicit = str(row.get("resolution_source") or "").strip()
    if explicit:
        return _normalize_url(explicit)

    if station_id and fallback_base:
        return _normalize_url(fallback_base)

    city_norm = _normalize_city(city)
    if city_norm and station_id:
        return f"https://www.wunderground.com/history/daily/{city_norm.replace(' ', '-')}/{station_id}"
    return ""


def _resolution_source_ready(source: str, station_id: str) -> Tuple[bool, str]:
    if not source:
        return False, "resolution_source_missing"
    if "wunderground.com/history/daily/" not in source.lower():
        return False, "resolution_source_not_wunderground_history"
    profile = swm.weather_resolution_source_profile(source, station_id=station_id)
    if profile.get("source_kind") != "wunderground_history":
        return False, str(profile.get("source_na_reason") or "resolution_source_wrong_kind")
    return True, "ok"


def _find_index_by_time_key(values: Sequence[Any], event_date: str) -> Optional[int]:
    target = str(event_date or "")
    if not target:
        return None
    for idx, item in enumerate(values):
        if not isinstance(item, str):
            continue
        if item.startswith(target):
            return idx
        if item[:10] == target:
            return idx
        if item.count("-") >= 2:
            maybe = item.replace(" ", "T")[:10]
            if maybe == target:
                return idx
    return None


def _extract_numeric_list(value: Any) -> Optional[List[float]]:
    if not isinstance(value, list):
        return None
    out: List[float] = []
    for item in value:
        num = _to_float(item)
        if num is not None:
            out.append(num)
    return out if out else None


def _walk_for_wu_nodes(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        max_keys = {"calendarDayTemperatureMax", "temperatureMax", "maxTemperature"}
        min_keys = {"calendarDayTemperatureMin", "temperatureMin", "minTemperature"}
        has_max = [k for k in max_keys if isinstance(node.get(k), list)]
        has_min = [k for k in min_keys if isinstance(node.get(k), list)]
        if has_max and has_min:
            yield {
                "max_key": has_max[0],
                "min_key": has_min[0],
                "max_values": _extract_numeric_list(node.get(has_max[0], [])),
                "min_values": _extract_numeric_list(node.get(has_min[0], [])),
                "valid_time_local": node.get("validTimeLocal")
                if isinstance(node.get("validTimeLocal"), list)
                else None,
                "valid_time_utc": _extract_numeric_list(node.get("validTimeUtc", []))
                if isinstance(node.get("validTimeUtc"), list)
                else None,
                "calendar_day": node.get("calendarDay")
                if isinstance(node.get("calendarDay"), list)
                else None,
            }
        for key, value in node.items():
            for child in _walk_for_wu_nodes(value):
                yield child
    elif isinstance(node, list):
        for item in node:
            for child in _walk_for_wu_nodes(item):
                yield child


def _extract_max_from_json_text(html: str, event_date: str, market_unit: Optional[str]) -> Optional[float]:
    for data in _iter_wu_json_payloads(html):
        for payload in _walk_for_wu_nodes(data):
            max_values: Optional[List[float]] = payload.get("max_values")  # type: ignore[assignment]
            if not max_values:
                continue

            idx: Optional[int] = None
            for label in ("valid_time_local", "calendar_day"):
                value_seq = payload.get(label)
                if not isinstance(value_seq, list):
                    continue
                idx = _find_index_by_time_key(value_seq, event_date)
                if idx is not None:
                    break

            if idx is None:
                valid_utc = payload.get("valid_time_utc")
                if isinstance(valid_utc, list):
                    target_ts = datetime.fromisoformat(event_date).replace(tzinfo=timezone.utc).timestamp()
                    for j, item in enumerate(valid_utc):
                        ts = _to_float(item)
                        if ts is None:
                            continue
                        if int(ts) >= int(target_ts) - 43200 and int(ts) <= int(target_ts) + 43200:
                                idx = j
                                break
            if idx is None and len(max_values) == 1:
                idx = 0
            if idx is None:
                continue
            if idx >= len(max_values):
                continue
            value = max_values[idx]
            if value is None or not math.isfinite(value):
                continue
            if market_unit and str(market_unit).upper() == "C" and value > 60:
                value = (value - 32.0) * 5.0 / 9.0
            return float(value)
    return None


def _fetch_html_with_requests(url: str, timeout: int) -> Tuple[Optional[str], Dict[str, Any]]:
    try:
        resp = swm.requests_get(url, timeout=timeout, bypass_proxy=True, retries=2)
    except Exception as exc:
        return None, {"ok": False, "error": f"request_error:{type(exc).__name__}"}
    if resp.status_code != 200:
        return None, {"ok": False, "error": f"http_{resp.status_code}"}
    return resp.text, {"ok": True, "status_code": resp.status_code, "method": "requests"}


def _fetch_html_with_selenium(url: str, timeout: int) -> Tuple[Optional[str], Dict[str, Any]]:
    try:
        from selenium import webdriver  # type: ignore[import-not-found]
        from selenium.webdriver.chrome.options import Options  # type: ignore[import-not-found]
    except Exception as exc:
        return None, {"ok": False, "error": f"selenium_import_error:{type(exc).__name__}"}

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    try:
        driver = webdriver.Chrome(options=options)
        try:
            driver.set_page_load_timeout(max(10, timeout))
            driver.get(url)
            time.sleep(2.0)
            html = driver.page_source
            if not html:
                return None, {"ok": False, "error": "selenium_empty_page_source"}
            return html, {"ok": True, "method": "selenium"}
        finally:
            driver.quit()
    except Exception as exc:
        return None, {"ok": False, "error": f"selenium_error:{type(exc).__name__}"}


def _fetch_wu_with_playwright_metrics(
    source: str,
    event_date: str,
    market_unit: Optional[str],
) -> Tuple[Optional[float], Dict[str, Any]]:
    try:
        metrics = swm.fetch_wunderground_history_metrics(source, event_date, "max", market_unit)
    except Exception as exc:
        return None, {"ok": False, "error": f"playwright_metric_error:{type(exc).__name__}"}
    value = metrics.get("value") if isinstance(metrics, Mapping) else None
    if isinstance(value, (int, float)):
        meta = dict(metrics)
        meta["ok"] = True
        meta["method"] = str(meta.get("method") or "wunderground_history_metrics")
        return float(value), meta
    meta = dict(metrics) if isinstance(metrics, Mapping) else {}
    meta["ok"] = False
    meta["error"] = str(meta.get("error") or "playwright_metric_missing")
    meta["method"] = str(meta.get("method") or "wunderground_history_metrics")
    return None, meta


def _fetch_with_apify(
    source: str,
    event_date: str,
    timeout: int,
) -> Tuple[Optional[str], Dict[str, Any]]:
    token = os.environ.get("PM_CHENGDU_WU_APIFY_TOKEN") or os.environ.get("APIFY_TOKEN")
    if not token:
        return None, {"ok": False, "error": "apify_token_missing"}

    actor_id = os.environ.get("PM_CHENGDU_WU_APIFY_ACTOR", "apify~website-content-crawler").strip()
    if not actor_id:
        return None, {"ok": False, "error": "apify_actor_missing"}

    target_url = _wu_history_date_url(source, event_date)
    raw_input = os.environ.get("PM_CHENGDU_WU_APIFY_ACTOR_INPUT")
    if raw_input:
        try:
            actor_input = json.loads(raw_input)
        except Exception:
            actor_input = {}
            actor_error = "apify_actor_input_invalid_json"
        else:
            actor_error = None
    else:
        actor_input = {}
        actor_error = None

    if actor_error is not None:
        return None, {"ok": False, "error": actor_error}
    if not isinstance(actor_input, Mapping):
        actor_input = {}

    if "startUrls" not in actor_input:
        actor_input["startUrls"] = [{"url": target_url}]

    try:
        run_resp = requests.post(
            f"https://api.apify.com/v2/acts/{actor_id}/runs?token={token}",
            json=actor_input,
            timeout=max(1, int(timeout)),
        )
        if run_resp.status_code >= 400:
            return None, {"ok": False, "error": f"apify_start_http_{run_resp.status_code}"}
        run_payload = run_resp.json()
        run_id = str((run_payload.get("data") or {}).get("id") or "").strip()
        if not run_id:
            return None, {"ok": False, "error": "apify_start_run_id_missing"}

        status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}"
        items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?token={token}&format=json&limit=1"
        for _ in range(20):
            status_payload = requests.get(status_url, timeout=max(1, int(timeout))).json()
            status_data = status_payload.get("data") if isinstance(status_payload, Mapping) else None
            state = str(status_data.get("status") if isinstance(status_data, Mapping) else "").strip().lower() if isinstance(status_data, Mapping) else ""
            if state in {"succeeded", "failed", "aborted", "timed-out"}:
                break
            time.sleep(1.0)

        if state != "succeeded":
            return None, {"ok": False, "error": f"apify_run_not_succeeded:{state}"}

        items = requests.get(items_url, timeout=max(1, int(timeout))).json()
        if not isinstance(items, list):
            return None, {"ok": False, "error": "apify_items_not_list"}
        if not items:
            return None, {"ok": False, "error": "apify_items_empty"}

        item = items[0] if isinstance(items, list) else {}
        if not isinstance(item, Mapping):
            return None, {"ok": False, "error": "apify_item_not_mapping"}
        html = (
            str(item.get("html") or item.get("body") or item.get("text") or "")
            or str(item.get("response", {}).get("text") or "")
        )
        if html.strip():
            return html, {"ok": True, "source": "apify", "run_id": run_id}
        return None, {"ok": False, "error": "apify_no_html"}
    except Exception as exc:
        return None, {"ok": False, "error": f"apify_error:{type(exc).__name__}"}


def _iter_wu_json_payloads(html: str) -> Iterable[Dict[str, Any]]:
    if not isinstance(html, str):
        return []

    payloads: List[Dict[str, Any]] = []

    def try_load(raw: str) -> None:
        if not raw:
            return
        raw = raw.strip()
        if not raw:
            return
        if raw.startswith("<") and raw.endswith(">"):
            return
        try:
            payload = json.loads(raw)
        except Exception:
            return
        if isinstance(payload, dict):
            payloads.append(payload)

    try:
        from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    except Exception:
        BeautifulSoup = None  # type: ignore[assignment]

    if BeautifulSoup is not None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            tag = soup.find("script", attrs={"id": "__NEXT_DATA__"})
            if tag is not None:
                text = tag.get_text() if hasattr(tag, "get_text") else str(tag.string or "")
                try_load(str(text))
            for tag in soup.find_all("script"):
                raw = str(tag.string or "") if tag is not None else ""
                if not raw:
                    continue
                text = raw.strip()
                if not text.startswith("{") or not text.endswith("}"):
                    continue
                try_load(text)
        except Exception:
            pass

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
    for script in scripts:
        txt = script.strip()
        if not txt or txt[0] != "{" or txt[-1] != "}":
            continue
        try_load(txt)

    for payload in payloads:
        yield payload


def _fetch_open_meteo_archive_via_curl(
    station_id: str,
    event_date: str,
    market_unit: Optional[str],
    timeout: int,
) -> Optional[float]:
    if not station_id or not event_date:
        return None

    coords: Optional[Tuple[float, float]] = _STATION_COORD_CACHE.get(station_id)
    if coords is None:
        coords = swm.fetch_ourairports_station_coords(station_id)
        if coords is None:
            coords = swm.fetch_override_station_coords(station_id)
        if coords is not None:
            _STATION_COORD_CACHE[station_id] = coords

    if coords is None:
        return None
    lat, lon = coords
    api_url = "https://archive-api.open-meteo.com/v1/archive"
    cmd = [
        "curl",
        "-sS",
        "-k",
        "--max-time",
        str(max(1, int(timeout))),
        f"{api_url}?latitude={lat}&longitude={lon}&daily=temperature_2m_max&start_date={event_date}&end_date={event_date}&timezone=UTC",
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    payload_text = (proc.stdout or "").strip()
    if not payload_text or payload_text.startswith("<"):
        return None

    try:
        payload = json.loads(payload_text)
    except Exception:
        return None
    daily = payload.get("daily") if isinstance(payload, dict) else None
    vals = None
    if isinstance(daily, dict):
        vals = daily.get("temperature_2m_max")
    if not isinstance(vals, list) or not vals:
        return None
    value = _to_float(vals[0])
    if value is None:
        return None
    if market_unit and str(market_unit).upper() == "F":
        return value * 9.0 / 5.0 + 32.0
    return value


def _fetch_open_meteo_archive_range_via_curl(
    station_id: str,
    start_date: str,
    end_date: str,
    market_unit: Optional[str],
    timeout: int,
) -> Dict[str, float]:
    if not station_id or not start_date or not end_date:
        return {}

    coords: Optional[Tuple[float, float]] = _STATION_COORD_CACHE.get(station_id)
    if coords is None:
        coords = swm.fetch_ourairports_station_coords(station_id)
        if coords is None:
            coords = swm.fetch_override_station_coords(station_id)
        if coords is not None:
            _STATION_COORD_CACHE[station_id] = coords

    if coords is None:
        return {}

    lat, lon = coords
    api_url = "https://archive-api.open-meteo.com/v1/archive"
    cmd = [
        "curl",
        "-sS",
        "-k",
        "--max-time",
        str(max(1, int(timeout))),
        (
            f"{api_url}?latitude={lat}&longitude={lon}"
            f"&daily=temperature_2m_max&start_date={start_date}&end_date={end_date}&timezone=UTC"
        ),
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}

    payload_text = (proc.stdout or "").strip()
    if not payload_text or payload_text.startswith("<"):
        return {}

    try:
        payload = json.loads(payload_text)
    except Exception:
        return {}
    daily = payload.get("daily") if isinstance(payload, Mapping) else None
    if not isinstance(daily, Mapping):
        return {}
    dates = daily.get("time")
    values = daily.get("temperature_2m_max")
    if not isinstance(dates, list) or not isinstance(values, list):
        return {}

    out: Dict[str, float] = {}
    limit = min(len(dates), len(values))
    for idx in range(limit):
        event_date = str(dates[idx]).strip()
        value = _to_float(values[idx])
        if value is None:
            continue
        if market_unit and str(market_unit).upper() == "F":
            value = value * 9.0 / 5.0 + 32.0
        if event_date:
            out[event_date] = float(value)
    return out


def _collect_open_meteo_range_values(
    station_id: str,
    rows: Sequence[Mapping[str, Any]],
    market_unit: Optional[str],
    timeout: int,
) -> Dict[str, float]:
    if not rows:
        return {}
    dates: List[str] = sorted({str(r.get("event_date") or "").strip() for r in rows if str(r.get("event_date") or "").strip()})
    if not dates:
        return {}
    return _fetch_open_meteo_archive_range_via_curl(
        station_id=station_id,
        start_date=dates[0],
        end_date=dates[-1],
        market_unit=market_unit,
        timeout=timeout,
    )


def _fetch_wu_history_api_range_values(
    station_id: str,
    rows: Sequence[Mapping[str, Any]],
    market_unit: Optional[str],
    timeout: int,
) -> Dict[str, Dict[str, Any]]:
    if not rows:
        return {}
    dates = sorted({str(r.get("event_date") or "").strip() for r in rows if str(r.get("event_date") or "").strip()})
    if not dates:
        return {}
    station = str(station_id or "ZUUU").strip().upper() or "ZUUU"
    location_id = f"{station}:9:CN"
    api_key = str(
        os.environ.get("PM_WU_HISTORY_API_KEY")
        or os.environ.get("PM_WEATHER_COM_API_KEY")
        or "6532d6454b8aa370768e63d6ba5a832e"
    ).strip()
    if not api_key:
        return {}

    def as_date(text: str) -> date:
        return date.fromisoformat(text)

    start = as_date(dates[0])
    end = as_date(dates[-1])
    out: Dict[str, Dict[str, Any]] = {}
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=30))
        start_key = chunk_start.strftime("%Y%m%d")
        end_key = chunk_end.strftime("%Y%m%d")
        url = (
            "https://api.weather.com/v1/location/"
            f"{requests.utils.quote(location_id, safe='')}/observations/historical.json"
            f"?apiKey={api_key}&units=e&startDate={start_key}&endDate={end_key}"
        )
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": f"{DEFAULT_WU_BASE_URL.rsplit('/ZUUU', 1)[0]}/date/{chunk_start.isoformat()}",
                },
                timeout=max(1, int(timeout)),
            )
            if resp.status_code != 200:
                chunk_start = chunk_end + timedelta(days=1)
                continue
            payload = resp.json()
        except Exception:
            chunk_start = chunk_end + timedelta(days=1)
            continue
        observations = payload.get("observations") if isinstance(payload, Mapping) else None
        if not isinstance(observations, list):
            chunk_start = chunk_end + timedelta(days=1)
            continue
        by_date: Dict[str, List[float]] = {}
        for obs in observations:
            if not isinstance(obs, Mapping):
                continue
            ts = _to_float(obs.get("valid_time_gmt"))
            temp_f = _to_float(obs.get("temp"))
            if ts is None or temp_f is None:
                continue
            # ZUUU/Chengdu WU history days are local China Standard Time days.
            local_day = (datetime.fromtimestamp(float(ts), timezone.utc) + timedelta(hours=8)).date().isoformat()
            if local_day < chunk_start.isoformat() or local_day > chunk_end.isoformat():
                continue
            by_date.setdefault(local_day, []).append(float(temp_f))
        for event_date, temps_f in by_date.items():
            if not temps_f:
                continue
            high_f = max(temps_f)
            value = (high_f - 32.0) * 5.0 / 9.0 if str(market_unit or "C").upper() == "C" else high_f
            out[event_date] = {
                "value": float(value),
                "meta": {
                    "ok": True,
                    "method": "weather_com_history_api_range",
                    "source_url": url,
                    "location_id": location_id,
                    "observation_count_for_day": len(temps_f),
                    "temperature_max_f": float(high_f),
                },
            }
        chunk_start = chunk_end + timedelta(days=1)
    return out


def _fetch_wu_settlement_actual(
    source: str,
    event_date: str,
    station_id: str,
    market_unit: Optional[str],
    timeout: int,
    scraper_mode: str,
    *,
    allow_open_meteo_fallback: bool = True,
) -> Tuple[Optional[float], Dict[str, Any]]:
    base = _normalize_url(source)
    if not base or not event_date:
        return None, {"error": "missing_source_or_event_date"}
    url = _wu_history_date_url(base, event_date)
    mode = _normalize_scraper_mode(scraper_mode)
    last_meta: Dict[str, Any] = {}

    # Historical backfill can use Open-Meteo as a fast fallback, but strict WU
    # evidence must only come from WU history pages/helpers.
    if allow_open_meteo_fallback:
        value = _fetch_open_meteo_archive_via_curl(station_id, event_date, market_unit, timeout)
        if value is not None:
            return value, {"ok": True, "source_url": url, "method": "open-meteo-archive-curl"}

    if mode in {"auto", "requests", "beautifulsoup"}:
        html, meta = _fetch_html_with_requests(url, timeout=timeout)
        if html is not None:
            value = _extract_max_from_json_text(html, event_date, market_unit)
            if value is not None:
                return value, {"ok": True, "source_url": url, "method": f"wu_html_direct_{meta.get('method', 'requests')}"}
        last_meta = dict(meta)

    if mode in {"auto", "playwright"}:
        value, meta = _fetch_wu_with_playwright_metrics(base, event_date, market_unit)
        if value is not None:
            return value, {"ok": True, "source_url": url, "method": meta.get("method", "wunderground_history_metrics")}
        last_meta = dict(meta)

    if mode in {"auto", "selenium"}:
        html, meta = _fetch_html_with_selenium(url, timeout=timeout)
        if html is not None:
            value = _extract_max_from_json_text(html, event_date, market_unit)
            if value is not None:
                return value, {"ok": True, "source_url": url, "method": meta.get("method", "wunderground_html_selenium")}
            # keep trying legacy helper if parse failed
        last_meta = dict(meta)

    if mode in {"auto", "apify"}:
        html, meta = _fetch_with_apify(base, event_date, timeout)
        if html is not None:
            value = _extract_max_from_json_text(html, event_date, market_unit)
            if value is not None:
                return value, {"ok": True, "source_url": url, "method": f"wu_html_apify"}
            # In auto mode, fallback to legacy helper if apify html has no usable payload.
            if mode == "auto":
                pass
            else:
                return None, {
                    "ok": False,
                    "error": "apify_parse_failed",
                    "source": str(meta.get("source") or ""),
                }
            # keep trying legacy helper if parse failed
        # In auto mode keep trying legacy fetch when apify is unavailable.
        if mode == "apify" and meta.get("error"):
            return None, {"ok": False, "error": f"apify_{meta.get('error')}"}
        last_meta = dict(meta)

    # Legacy fallback via existing shared helper may use Playwright. Only use it
    # when the selected scraper mode allows a browser-style fallback.
    if mode == "auto":
        legacy = swm.fetch_settlement_actual(base, event_date, "temperature_2m_max", market_unit, station_id=station_id)
        if legacy is not None:
            return legacy, {"ok": True, "source_url": url, "method": "legacy_helper"}

    if last_meta:
        out = dict(last_meta)
        out.setdefault("ok", False)
        out.setdefault("error", "no_temperature_payload_found")
        out.setdefault("source_url", url)
        return None, out
    return None, {"ok": False, "error": "no_temperature_payload_found", "source_url": url}


def _existing_settlement_actual(conn: sqlite3.Connection, *, slug: str, event_date: str) -> Optional[float]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT settlement_actual
        FROM event_runs
        WHERE slug = ?
          AND event_date = ?
          AND settlement_actual IS NOT NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 20
        """,
        (slug, event_date),
    )
    for row in cur.fetchall():
        value = _to_float(row[0])
        if value is not None:
            return value
    return None


def _existing_settlement_actual_daily(
    conn: sqlite3.Connection,
    *,
    city: str,
    station_id: str,
    event_date: str,
    market_var: str,
    market_unit: str,
    require_wu_history: bool = False,
) -> Optional[float]:
    city_like = f"%{_normalize_city(city)}%"
    source_filter = ""
    if require_wu_history:
        source_filter = "AND lower(coalesce(resolution_source, '')) LIKE '%wunderground.com/history/daily/cn/chengdu/zuuu%'"
    cur = conn.execute(
        f"""
        SELECT settlement_actual
        FROM event_daily_temps
        WHERE event_date = ?
          AND market_var = ?
          AND market_unit = ?
          AND (
              lower(city) LIKE ?
              OR upper(station_id) = upper(?)
          )
          {source_filter}
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (event_date, market_var, market_unit, city_like, station_id),
    )
    for row in cur.fetchall():
        value = _to_float(row[0])
        if value is not None:
            return value
    return None


def _insert_settlement_daily(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    actual: float,
    now_ts: str,
    *,
    require_wu_history_existing: bool = False,
) -> Tuple[bool, str]:
    city = str(row.get("city") or "").strip()
    station_id = str(row.get("station_id") or "").strip()
    event_date = str(row.get("event_date") or "").strip()
    if not city or not station_id or not event_date:
        return False, "missing_city_station_or_event_date"

    existing = _existing_settlement_actual_daily(
        conn,
        city=city,
        station_id=station_id,
        event_date=event_date,
        market_var=str(row.get("market_var") or "temperature_2m_max").strip(),
        market_unit=str(row.get("market_unit") or "C").strip(),
        require_wu_history=require_wu_history_existing,
    )
    if existing is not None:
        return True, "daily_settlement_already_settled"

    conn.execute(
        """
        INSERT INTO event_daily_temps (
            run_date, event_date, slug, title, city, station_id, resolution_source,
            market_var, market_unit, settlement_actual, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(row.get("event_date") or str(now_ts))[:10],
            event_date,
            str(row.get("slug") or ""),
            str(row.get("title") or ""),
            city,
            station_id,
            str(row.get("resolution_source") or ""),
            str(row.get("market_var") or "temperature_2m_max"),
            str(row.get("market_unit") or "C"),
            float(actual),
            now_ts,
        ),
    )
    return True, "daily_settlement_actual_inserted"


def _table_columns(conn: sqlite3.Connection) -> List[str]:
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(event_runs)").fetchall() if str(r[1]) != "id"]


def _insert_settlement_row(
    conn: sqlite3.Connection,
    row: Mapping[str, Any],
    actual: float,
    now_ts: str,
    event_date: str,
) -> Tuple[bool, str]:
    slug = str(row.get("slug") or "").strip()
    if not slug or not event_date:
        return False, "missing_slug_or_event_date"

    existing = _existing_settlement_actual(conn, slug=slug, event_date=event_date)
    if existing is not None:
        return True, "already_settled"

    latest = conn.execute(
        """
        SELECT *
        FROM event_runs
        WHERE slug = ?
          AND event_date = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()
    if latest is None:
        return False, "event_run_missing"
    latest_map = dict(latest)

    cols = _table_columns(conn)
    if not cols:
        return False, "event_runs_schema_unreadable"
    if "settlement_actual" not in cols:
        return False, "settlement_actual_column_missing"

    merged = {col: latest_map.get(col) for col in cols}
    if "run_date" in merged:
        merged["run_date"] = event_date
    if "event_date" in merged:
        merged["event_date"] = event_date
    for key in ("title", "event_url", "resolution_source", "station_id", "market_var", "market_unit", "event_end_utc"):
        if key in merged and row.get(key) not in (None, ""):
            merged[key] = row[key]
    if "market_closed" in merged:
        merged["market_closed"] = 1
    merged["settlement_actual"] = float(actual)
    merged["created_at"] = now_ts

    placeholders = ", ".join("?" for _ in cols)
    try:
        conn.execute(
            f"INSERT INTO event_runs ({', '.join(cols)}) VALUES ({placeholders})",
            [merged.get(col) for col in cols],
        )
        return True, "settlement_actual_inserted"
    except sqlite3.OperationalError as exc:
        if swm._sqlite_is_locked_error(exc):
            return False, "db_locked"
        return False, f"db_insert_error:{exc}"


def _snapshot_rows(
    db_path: Path,
    city: str,
    station_id: str,
    market_scope: str,
    start_date: str,
    end_date: str,
    include_open: bool,
    fetch_preview: bool,
    backfill_mode: str,
    scraper_mode: str,
    timeout: int,
    fallback_source: str,
    open_meteo_station_id: str = "",
    preload_open_meteo_values: bool = False,
    allow_open_meteo_fallback: bool = True,
    require_wu_history_existing: bool = False,
    limit: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    if backfill_mode == "event_runs":
        rows = _collect_candidate_rows(
            db_path,
            city=city,
            station_id=station_id,
            market_scope=market_scope,
            start_date=start_date,
            end_date=end_date,
        )
    else:
        rows = _collect_daily_history_candidates(
            db_path,
            city=city,
            station_id=station_id,
            market_scope=market_scope,
            start_date=start_date,
            end_date=end_date,
            wu_base_url=fallback_source,
        )
    if limit and int(limit) > 0:
        rows = list(rows)[: int(limit)]
    now_utc = datetime.now(timezone.utc)
    today_et = _today_et()
    open_meteo_values: Dict[str, float] = {}
    wu_history_api_values: Dict[str, Dict[str, Any]] = {}
    market_unit: Optional[str] = None
    if backfill_mode == "daily_temps" and preload_open_meteo_values and allow_open_meteo_fallback and rows:
        market_unit = str(rows[0].get("market_unit") or "C")
        open_meteo_values = _collect_open_meteo_range_values(
            station_id=open_meteo_station_id or station_id,
            rows=rows,
            market_unit=market_unit,
            timeout=timeout,
        )
    if (
        backfill_mode == "daily_temps"
        and fetch_preview
        and require_wu_history_existing
        and rows
    ):
        market_unit = str(rows[0].get("market_unit") or "C")
        wu_history_api_values = _fetch_wu_history_api_range_values(
            station_id=station_id,
            rows=rows,
            market_unit=market_unit,
            timeout=timeout,
        )
    counter = Counter()
    out: List[Dict[str, Any]] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        for row in rows:
            row_data: Dict[str, Any] = dict(row)
            event_day = _row_event_date(row)
            row_data["event_date"] = event_day
            row_data["is_closed"] = _is_closed(row_data, now_utc=now_utc, today_et=today_et)

            if backfill_mode == "event_runs":
                existing = _to_float(
                    _existing_settlement_actual(
                        conn,
                        slug=str(row_data.get("slug") or ""),
                        event_date=event_day,
                    )
                )
            else:
                existing = _existing_settlement_actual_daily(
                    conn,
                    city=str(row_data.get("city") or city),
                    station_id=str(row_data.get("station_id") or station_id),
                    event_date=event_day,
                    market_var=str(row_data.get("market_var") or "temperature_2m_max").strip(),
                    market_unit=str(row_data.get("market_unit") or "C").strip(),
                    require_wu_history=require_wu_history_existing,
                )

            inline_existing = _to_float(row.get("settlement_actual"))
            if existing is None:
                existing = inline_existing
            if existing is not None:
                row_data["already_settled"] = True
                row_data["action"] = "skip_already_settled"
                row_data["reason"] = "settlement_actual_exists"
                row_data["candidate_settlement_actual"] = existing
                counter["already_settled"] += 1
                out.append(row_data)
                continue
            row_data["already_settled"] = False

            preloaded_wu = wu_history_api_values.get(event_day)
            if preloaded_wu and preloaded_wu.get("value") is not None:
                row_data["action"] = "ready_apply"
                row_data["reason"] = "fetch_succeeded"
                row_data["candidate_settlement_actual"] = float(preloaded_wu["value"])
                row_data["fetch_meta"] = dict(preloaded_wu.get("meta") or {})
                row_data["resolution_source"] = _build_resolution_source(
                    row_data,
                    station_id=station_id,
                    city=city,
                    fallback_base=fallback_source,
                )
                row_data["resolution_source_ok"] = True
                row_data["resolution_source_reason"] = "ok"
                counter["ready_apply"] += 1
                out.append(row_data)
                continue

            if backfill_mode == "daily_temps":
                prefill = open_meteo_values.get(event_day)
                if prefill is not None:
                    row_data["action"] = "ready_apply"
                    row_data["reason"] = "fetch_succeeded"
                    row_data["candidate_settlement_actual"] = float(prefill)
                    row_data["fetch_meta"] = {
                        "ok": True,
                        "source_url": "https://archive-api.open-meteo.com/v1/archive",
                        "method": "open-meteo-archive-curl-range",
                    }
                    row_data["resolution_source"] = "open-meteo-archive"
                    row_data["resolution_source_ok"] = True
                    row_data["resolution_source_reason"] = "open-meteo_range_archive"
                    counter["ready_apply"] += 1
                    out.append(row_data)
                    continue

            if not include_open and not row_data["is_closed"]:
                row_data["action"] = "skip_not_closed"
                row_data["reason"] = "event_not_closed"
                counter["not_closed"] += 1
                out.append(row_data)
                continue

            source = _build_resolution_source(
                row_data,
                station_id=station_id,
                city=city,
                fallback_base=fallback_source,
            )
            row_data["resolution_source"] = source or row_data.get("resolution_source")
            source_ok, source_reason = _resolution_source_ready(source, station_id)
            row_data["resolution_source_ok"] = source_ok
            row_data["resolution_source_reason"] = source_reason

            if not source_ok:
                row_data["action"] = "skip_source_not_usable"
                row_data["reason"] = source_reason
                counter["source_not_ready"] += 1
                out.append(row_data)
                continue

            row_data["action"] = "ready_preview"
            row_data["reason"] = "ready_for_preview"
            counter["ready_for_preview"] += 1
            if not fetch_preview:
                out.append(row_data)
                continue

            actual, meta = _fetch_wu_settlement_actual(
                source=source,
                event_date=event_day,
                station_id=station_id,
                market_unit=str(row_data.get("market_unit") or "") or "C",
                scraper_mode=scraper_mode,
                timeout=timeout,
                allow_open_meteo_fallback=allow_open_meteo_fallback,
            )
            if actual is None:
                row_data["action"] = "skip_fetch_failed"
                row_data["reason"] = str(meta.get("error") or "fetch_failed")
                row_data["fetch_meta"] = meta
                row_data["candidate_settlement_actual"] = None
                counter["fetch_failed"] += 1
                out.append(row_data)
                continue

            row_data["action"] = "ready_apply"
            row_data["reason"] = "fetch_succeeded"
            row_data["candidate_settlement_actual"] = _to_float(actual)
            row_data["fetch_meta"] = meta
            counter["ready_apply"] += 1
            out.append(row_data)
    finally:
        conn.close()

    return out, dict(counter)


def _backup_db(db_path: Path, backup_dir: Path) -> Dict[str, Any]:
    if not db_path.exists():
        return {"requested": True, "created": False, "path": "", "reason": "source_db_missing"}
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"archive.before-chengdu-settlement-backfill.{stamp}.db"
    n = 1
    while backup_path.exists():
        backup_path = backup_dir / f"archive.before-chengdu-settlement-backfill.{stamp}.{n}.db"
        n += 1

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    try:
        dst = sqlite3.connect(str(backup_path), timeout=30.0)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return {"requested": True, "created": True, "path": str(backup_path), "reason": "created_before_apply"}


def _write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_report(
    db_path: Path,
    city: str,
    station_id: str,
    market_scope: str,
    days: int,
    start_date: Optional[str],
    end_date: Optional[str],
    only_closed: bool,
    fetch_preview: bool,
    apply: bool,
    confirm_apply: str,
    pause_ms: int,
    no_backup: bool,
    backup_dir: Path,
    wu_base_url: str,
    timeout: int,
    backfill_mode: str,
    scraper_mode: str,
    all_history: bool,
    strict_wu_history: bool = False,
    limit: int = 0,
) -> Dict[str, Any]:
    include_open = not only_closed
    if all_history and backfill_mode == "event_runs" and (not start_date):
        earliest = _earliest_event_date(
            db_path,
            city=city,
            station_id=station_id,
            market_scope=market_scope,
        )
        if earliest:
            start_date = earliest
    start_dt, end_dt = _coerce_run_date_range(start_date=start_date, end_date=end_date, days=days)

    rows, counts = _snapshot_rows(
        db_path=db_path,
        city=city,
        station_id=station_id,
        market_scope=market_scope,
        start_date=start_dt,
        end_date=end_dt,
        include_open=include_open,
        fetch_preview=fetch_preview,
        backfill_mode=backfill_mode,
        scraper_mode=scraper_mode,
        timeout=timeout,
        fallback_source=wu_base_url,
        open_meteo_station_id=station_id,
        preload_open_meteo_values=(backfill_mode == "daily_temps"),
        allow_open_meteo_fallback=not bool(strict_wu_history),
        require_wu_history_existing=bool(strict_wu_history),
        limit=max(0, int(limit or 0)),
    )

    apply_candidates = [
        row for row in rows if row.get("action") == "ready_apply" and row.get("candidate_settlement_actual") is not None
    ]
    inserted = 0
    already = 0
    failed = 0
    blocked_apply = 0
    applied_rows: List[Dict[str, Any]] = []
    backup_info = {"requested": False, "created": False, "path": "", "reason": "not_requested"}

    confirmation_ok = str(confirm_apply or "").strip() == APPLY_CONFIRM_PHRASE
    can_apply = bool(apply and confirmation_ok)
    if apply and not confirmation_ok:
        for row in apply_candidates:
            row["action"] = "blocked_missing_confirmation"
            row["reason"] = "apply_requested_but_confirmation_missing"
        blocked_apply = len(apply_candidates)

    if can_apply:
        if not no_backup:
            backup_info = _backup_db(db_path, backup_dir)
        if backup_info.get("created") or no_backup:
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                now_ts = _utc_now()
                did_attempt = False
                for row in sorted(apply_candidates, key=lambda item: str(item.get("event_date") or "")):
                    if row.get("already_settled"):
                        already += 1
                        row["action"] = "skip_already_settled"
                        applied_rows.append(row)
                        continue
                    did_attempt = True
                    if backfill_mode == "event_runs":
                        ok, status = _insert_settlement_row(
                            conn,
                            row,
                            actual=float(row["candidate_settlement_actual"]),
                            now_ts=now_ts,
                            event_date=str(row.get("event_date") or ""),
                        )
                    else:
                        ok, status = _insert_settlement_daily(
                            conn,
                            row,
                            actual=float(row["candidate_settlement_actual"]),
                            now_ts=now_ts,
                            require_wu_history_existing=bool(strict_wu_history),
                        )
                    if status == "already_settled":
                        already += 1
                    elif ok:
                        inserted += 1
                    else:
                        failed += 1
                    row["action"] = f"apply_{status}"
                    row["apply_status"] = status
                    row["applied_at"] = now_ts
                    applied_rows.append(row)
                    if pause_ms > 0:
                        time.sleep(max(0.0, pause_ms) / 1000.0)
                if did_attempt:
                    conn.commit()
                    conn.close()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                for row in apply_candidates:
                    row.setdefault("action", "apply_blocked_db_error")
                    row["apply_status"] = f"db_error:{type(exc).__name__}"
                    row["reason"] = str(exc)
                blocked_apply = len(apply_candidates)
                failed = len(apply_candidates)
                inserted = 0
            finally:
                conn.close()
        else:
            blocked_apply = len(apply_candidates)
            failed = len(apply_candidates)
            for row in apply_candidates:
                row["action"] = "blocked_backup_required"
                row["reason"] = "backup_failed"
                row["apply_status"] = "backup_not_ready"

    if not can_apply and apply:
        blocked_apply = len(apply_candidates)
        failed = len(apply_candidates)

    ready_for_apply_count = counts.get("ready_apply", 0)
    return {
        "ok": True,
        "db_path": str(db_path),
        "city": str(city or ""),
        "station_id": str(station_id or ""),
        "market_scope": str(market_scope or ""),
        "backfill_mode": backfill_mode,
        "start_date": start_dt,
        "end_date": end_dt,
        "days": int(days),
        "row_count": len(rows),
        "limit": max(0, int(limit or 0)),
        "ready_for_apply_count": ready_for_apply_count,
        "inserted": inserted,
        "already_settled": already,
        "apply_failed": failed,
        "blocked_apply": blocked_apply,
        "fetch_preview_enabled": bool(fetch_preview),
        "only_closed": bool(only_closed),
        "include_open": bool(include_open),
        "required_confirm_apply": APPLY_CONFIRM_PHRASE,
        "scraper_mode": _normalize_scraper_mode(scraper_mode),
        "strict_wu_history": bool(strict_wu_history),
        "allow_open_meteo_fallback": not bool(strict_wu_history),
        "generated_at": _utc_now(),
        "backup": backup_info,
        "counts": counts,
        "rows": rows,
        "applied_rows": applied_rows,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill Chengdu historical settlements from wunderground history pages. "
            "Default scope is highest-temperature in Chengdu."
        )
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--city", default=DEFAULT_CITY)
    parser.add_argument("--station-id", default=DEFAULT_STATION_ID)
    parser.add_argument("--market-scope", default="highest_temperature")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--all-history", action="store_true", help="Backfill from earliest available event date")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--only-closed", action="store_true", default=True)
    parser.add_argument("--include-open", action="store_true", help="Include markets not closed yet")
    parser.add_argument("--apply", action="store_true", help="Write settlement_actual rows")
    parser.add_argument("--confirm-apply", default="", help=f"Must match: {APPLY_CONFIRM_PHRASE}")
    parser.add_argument("--no-preview", action="store_true", help="Skip settlement fetch preview")
    parser.add_argument("--pause-ms", type=int, default=250)
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--wu-base-url", default=DEFAULT_WU_BASE_URL)
    parser.add_argument(
        "--scraper-mode",
        default=DEFAULT_SCRAPER_MODE,
        help="Scraper mode: auto|requests|beautifulsoup|playwright|selenium|apify",
    )
    parser.add_argument(
        "--backfill-mode",
        default=DEFAULT_BACKFILL_MODE,
        help="Backfill mode: event_runs|daily_temps",
    )
    parser.add_argument(
        "--strict-wu-history",
        action="store_true",
        help="Require WU history evidence; do not use Open-Meteo archive fallback.",
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--limit", type=int, default=0, help="Limit candidate rows for safe preview batches")
    parser.add_argument("--out", default=str(DEFAULT_REPORT))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.only_closed and args.include_open:
        only_closed = False
    else:
        only_closed = bool(args.only_closed)

    report = build_report(
        db_path=Path(args.db),
        city=str(args.city or ""),
        station_id=str(args.station_id or ""),
        market_scope=str(args.market_scope or "highest_temperature"),
        days=int(args.days or DEFAULT_DAYS),
        start_date=(str(args.start_date or "") or None),
        end_date=(str(args.end_date or "") or None),
        only_closed=only_closed,
        fetch_preview=not bool(args.no_preview),
        apply=bool(args.apply),
        confirm_apply=str(args.confirm_apply or ""),
        pause_ms=max(0, int(args.pause_ms or 0)),
        no_backup=bool(args.no_backup),
        backup_dir=Path(args.backup_dir),
        wu_base_url=str(args.wu_base_url or ""),
        timeout=max(1, int(args.timeout or 30)),
        backfill_mode=_normalize_backfill_mode(str(args.backfill_mode or "")),
        scraper_mode=str(args.scraper_mode or "auto"),
        all_history=bool(args.all_history),
        strict_wu_history=bool(args.strict_wu_history),
        limit=max(0, int(args.limit or 0)),
    )

    if args.out:
        _write_report(report, Path(args.out))

    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0 if report.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
